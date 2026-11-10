"""Dialect-aware whole-chain code checks. No execution or equivalence claims."""
import re
import hashlib
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from .contracts import digest
from .verification import allowed_node, table_key
from .clickhouse_nulls import projection_nullability, preserve_clickhouse_nullable

VERSION="linked-code-review-3"
DIALECTS={"greenplum":"postgres","postgresql":"postgres","clickhouse":"clickhouse"}
SAFE_EXTRA={"ToDate","ToTime","ToChar","StrToDate","TimeToStr","StrPosition","Repeat","Replace","RegexpReplace",
    "RegexpLike","RegexpExtract","Floor","Ceil","Power","Sqrt","Sign","Least","Greatest","If","Array","ArraySize",
    "ArrayContains","Split","Map","DateAdd","DateDiff","DateSub","LastDay","Cast","TryCast","NullSafeEQ",
    "DateFromParts","TimeFromParts","TimestampFromParts","UnixToTime","TimeToUnix","Day","Month","Year","ConcatWs"}
SAFE_ANON={"todate","todatetime","todatetime64","toyear","tomonth","tostartofmonth","addmonths","adddays",
    "ifnull","isnull","isnotnull","assumenotnull","concat","substring","replaceall","trim","trimleft","trimright",
    "tostring","toint32","toint64","tofloat64","todecimal64","rownumberinallblocks","tuple"}
SAFE_ANON.update({'multiif','touint8','touint16','touint32','touint64','toint8','toint16',
    'tofloat32','sumornull','avgornull','minornull','maxornull','tofloat64ornull','toint64ornull',
    'todecimal64ornull'})


def finding(code, message, *, obligation="", location="", severity="blocking"):
    return {"id":"finding-"+digest([code,obligation,location])[:24],"code":code,"message":message,
        "obligation":obligation,"sas_location":obligation,"sql_location":location,"severity":severity,
        "counterexample":"","required_information":""}


def safe_expression(node):
    if allowed_node(node) or type(node).__name__ in SAFE_EXTRA:return True
    if isinstance(node,exp.CombinedAggFunc):return str(node.this).casefold() in {'sumornull','avgornull','minornull','maxornull'}
    if isinstance(node,exp.Anonymous):
        return node.name.casefold() in SAFE_ANON or valid_lag_in_frame(node) or valid_decimal_function(node)
    return False


def valid_decimal_function(node):
    arity={"todecimal128":2,"dividedecimal":3}.get(node.name.casefold())
    args=node.expressions
    if not arity or len(args)!=arity:
        return False
    scale=args[-1]
    return isinstance(scale,exp.Literal) and scale.is_int and 0<=int(scale.this)<=38


def lag_in_frame(node):
    return isinstance(node,exp.Anonymous) and node.name.casefold()=="laginframe"


def valid_lag_in_frame(node):
    if not lag_in_frame(node) or not isinstance(node.parent,exp.Window):
        return False
    args=node.expressions
    if len(args)!=3 or not isinstance(args[2],exp.Null):
        return False
    offset=args[1]
    if not isinstance(offset,exp.Literal) or offset.is_string or not re.fullmatch(r"[1-9][0-9]{0,3}",offset.this):
        return False
    spec=node.parent.args.get("spec")
    return bool(spec and spec.args["kind"]=="ROWS" and spec.args["start"]=="UNBOUNDED"
                and spec.args.get("start_side")=="PRECEDING" and not spec.args.get("exclude")
                and (spec.args["end"]=="CURRENT ROW" or
                     spec.args["end"]=="UNBOUNDED" and spec.args.get("end_side")=="FOLLOWING"))


def output_schema(output):
    declared = {column["name"]: column["data_type"] for column in output.get("column_metadata", [])}
    return {name: declared.get(name, "UNKNOWN") for name in output["columns"]}


def assemble_sql(artifacts, target):
    parts = []
    for artifact in artifacts:
        fragment = artifact["sql"]
        try:
            tokens = sqlglot.tokenize(fragment, read=DIALECTS[target])
        except sqlglot.errors.TokenError:
            tokens = []
        if tokens and tokens[-1].token_type != sqlglot.tokens.TokenType.SEMICOLON:
            fragment += "\n;"
        parts.append(fragment)
    sql = "\n".join(parts)
    return {"sql":sql, "sha256":hashlib.sha256(sql.encode("utf-8")).hexdigest()}


def clickhouse_storage(output, props, proven_nonnull, add, loc):
    engines = [p.this.sql() for p in props if isinstance(p, exp.EngineProperty)]
    order = next((p for p in props if isinstance(p, exp.Order)), None)
    configured = output.get("clickhouse_engine")
    if not engines:
        if output["storage"] == "persistent" or configured:
            add("explicit_engine_required", "Для согласованного хранения ClickHouse нужен явный ENGINE", loc)
        return
    engine = engines[0].partition("(")[0].casefold()
    if configured and engine != configured.casefold():
        add("engine_contract_mismatch", "Движок не совпадает с согласованным контрактом", loc)
    if output["storage"] == "persistent" and engine == "memory" and configured != "Memory":
        add("durable_engine_required", "Для постоянного выхода Memory требует явного согласования", loc)
    if engine == "replacingmergetree" and configured != "ReplacingMergeTree":
        add("replacing_engine_not_approved", "ReplacingMergeTree требует явного согласования", loc)
    if output["storage"] == "temporary" and engine != "memory":
        add("temporary_engine_mismatch", "Временная таблица использует профиль Memory", loc)
    settings = [s for p in props if isinstance(p, exp.SettingsProperty) for s in p.expressions]
    allowed_setting = len(settings) == 1 and settings[0].sql(dialect="clickhouse").casefold() == "allow_nullable_key = 1"
    if settings and not (allowed_setting and output.get("clickhouse_nullable_key") and "mergetree" in engine):
        add("unapproved_table_setting", "Настройка CREATE не согласована в контракте ClickHouse", loc)
    if "mergetree" not in engine:
        return
    if not order:
        add("mergetree_order_required", "Для MergeTree нужен явный ORDER BY", loc)
        return
    nullable_allowed = allowed_setting and output.get("clickhouse_nullable_key")
    declared_nonnull = {c["name"] for c in output.get("column_metadata", [])
                        if c.get("nullable") is False and not c["data_type"].upper().startswith("NULLABLE(")}
    key_terms = [x.this for x in order.expressions]
    for primary in (p for p in props if isinstance(p, exp.PrimaryKey)):
        key_terms.extend(primary.expressions or ([primary.this] if primary.this else []))
    for key in key_terms:
        terms = key.expressions if isinstance(key, exp.Tuple) else [key]
        if empty_tuple(key):
            continue
        if any(not isinstance(term, exp.Column) or term.name not in output["columns"] for term in terms):
            add("unsupported_mergetree_key", "Ключ должен состоять из согласованных полей результата или tuple()", loc)
            break
        if not nullable_allowed and any(term.name not in declared_nonnull & proven_nonnull for term in terms):
            add("mergetree_key_nullability_unconfirmed",
                "Укажите подтверждённый NOT NULL ключ, ORDER BY tuple() или согласуйте allow_nullable_key", loc)
            break


def empty_tuple(node):
    return isinstance(node, exp.Struct) and not node.expressions


def projection_nonnull(query, known):
    """Prove direct non-null projections without guessing through joins or expressions."""
    if not isinstance(query, exp.Select) or query.args.get("joins") or query.args.get("with_"):
        return set()
    relation = query.args.get("from_")
    source = known.get(table_key(relation.this), set()) if relation and isinstance(relation.this, exp.Table) else set()
    result = set()
    for expression in query.selects:
        value = expression.unalias()
        if isinstance(value, exp.Literal) or isinstance(value, exp.Column) and value.name in source:
            result.add(expression.alias_or_name)
    return result


def check_lag_order(tree, contract):
    windows = [w for w in tree.find_all(exp.Window) if isinstance(w.this, (exp.Lag, exp.RowNumber)) or valid_lag_in_frame(w.this)]
    if not windows:
        return False
    required = [(k["column"], k["descending"]) for k in contract["keys"]]

    def terms(window):
        order = window.args.get("order")
        return [(t.this.name.casefold(), bool(t.args.get("desc"))) if isinstance(t.this, exp.Column) else None
                for t in order.expressions] if order else []

    ordinals = {w.parent.alias.casefold() for w in windows if isinstance(w.this, exp.RowNumber)
                and isinstance(w.parent, exp.Alias) and not w.args.get("partition_by") and terms(w) == required}
    lags = [w for w in windows if isinstance(w.this, exp.Lag) or valid_lag_in_frame(w.this)]
    return bool(lags) and all(not w.args.get("partition_by") and (
        terms(w) == required or any(terms(w) == [(name, False)] for name in ordinals)) for w in lags)


def date_interval_returns_timestamp(value):
    if isinstance(value, exp.Cast) and value.to.is_type("DATE"):
        return False
    if isinstance(value, exp.Case):
        branches = [b.args["true"] for b in value.args.get("ifs", [])]
        if value.args.get("default") is not None:
            branches.append(value.args["default"])
        return any(date_interval_returns_timestamp(b) for b in branches)
    if isinstance(value, exp.Paren):
        return date_interval_returns_timestamp(value.this)
    return isinstance(value, (exp.Add, exp.Sub)) and any(isinstance(v, exp.Interval) for v in value.iter_expressions())


def link(plan, artifacts, target, obligations=(), source="", *, predecessors=(), predecessor_nullable=None, predecessor_nonnull=None, require_all=True,expected_writes=None,lag_order=None):
    from app.sas_lexer import lex_statements
    dialect=DIALECTS[target]
    statements,_ = lex_statements(source)
    masked = " ".join(s["masked"] for s in statements).casefold()
    catx_outputs = set(re.findall(r"\b(\w+)\s*=\s*catx\s*\(", masked))
    upcase_inputs = {output:operand for output,operand in
                     re.findall(r"\b(\w+)\s*=\s*upcase\s*\(\s*(\w+)\s*\)\s*;",masked)
                     if not re.search(r"\b"+re.escape(operand)+r"\s*=",masked)}
    inputs={".".join(b["target"]["parts"]):{c["name"]:c["data_type"] for c in b["columns"]} for b in plan["bindings"] if not b.get('members')}
    for binding in plan['bindings']:
        for member in binding.get('members',[]):
            key='.'.join(member['target']['parts']);schema={c['name']:c['data_type'] for c in member['columns']}
            if key in inputs and inputs[key]!=schema:return {'version':VERSION,'target':target,'static_passed':False,'findings':[finding('conflicting_member_schema','Несовместимые схемы одного источника')],'statements':[],'execution':'not_run','equivalence':'not_run','obligations':list(obligations),'hash':digest([plan,artifacts,target,'schema_conflict'])}
            inputs[key]=schema
    outputs={".".join(o["name"]["parts"]):o for o in plan["outputs"]}
    expected=set(outputs) if expected_writes is None else set(expected_writes)
    available=dict(inputs)
    available.update({n:output_schema(outputs[n]) for n in predecessors if n in outputs})
    known_nonnull = {".".join(b["target"]["parts"]):{c["name"] for c in b["columns"] if c.get("nullable") is False}
                     for b in plan["bindings"] if not b.get("members")}
    known_nonnull.update({name:{c["name"] for c in outputs[name].get("column_metadata", []) if c.get("nullable") is False}
                          for name in predecessors if name in outputs and name not in known_nonnull})
    known_nullable = {".".join(b["target"]["parts"]): {c["name"] for c in b["columns"] if c.get("nullable") is True}
                      for b in plan["bindings"] if not b.get("members")}
    for name in predecessors:
        if name in outputs:
            known_nullable.setdefault(name, set()).update(
                c["name"] for c in outputs[name].get("column_metadata", []) if c.get("nullable") is True)
    for name, columns in (predecessor_nullable or {}).items():
        if name in predecessors:
            known_nullable.setdefault(name, set()).update(columns)
    for name, columns in (predecessor_nonnull or {}).items():
        if name in predecessors:
            known_nonnull.setdefault(name, set()).update(columns)
            known_nullable.setdefault(name, set()).difference_update(columns)
    written=set();issues=[];inventory=[]
    lag_order_found = False
    def add(code,msg,loc):issues.append(finding(code,msg,location=loc))
    for artifact in artifacts:
        try:trees=sqlglot.parse(artifact["sql"],read=dialect,error_level="RAISE",error_message_context=0)
        except Exception:
            add("sql_parse_error","SQL не разбирается для выбранного диалекта",artifact["name"]);continue
        for index,tree in enumerate(trees):
            loc=artifact["name"]+":"+str(index+1)
            read_tables=set()
            if tree is None:
                add("empty_statement","Пустой SQL-оператор",loc);continue
            action={exp.Create:"create",exp.Insert:"insert",exp.Update:"update",exp.Delete:"delete",exp.Drop:"drop"}.get(type(tree))
            destination=tree.this.this if isinstance(tree.this,exp.Schema) else tree.this
            if not action or not isinstance(destination,exp.Table):
                add("unsupported_statement","Оператор не входит в разрешённый план исполнения",loc);continue
            try:name=table_key(destination)
            except ValueError:
                add("invalid_destination","Нужно явное имя таблицы в согласованной схеме",loc);continue
            policy=plan.get("allowed_effects",{}).get(name,["create"])
            if name not in outputs or name not in expected or action not in policy:
                add("undeclared_effect","Не согласована операция "+action+" для "+name,loc);continue
            if dialect=="postgres" and tree.find(exp.IgnoreNulls):
                add("ignore_nulls_not_supported","IGNORE NULLS недоступен в проверяемом профиле",loc)
            if target!="clickhouse" and any(lag_in_frame(n) for n in tree.find_all(exp.Anonymous)):
                add("clickhouse_window_not_supported","lagInFrame доступен только в ClickHouse",loc)
            rejected = {n.name if isinstance(n, exp.Anonymous) else n.sql_name()
                        for n in tree.walk() if isinstance(n, exp.Func) and not safe_expression(n) and not
                        (target == "clickhouse" and (isinstance(n.parent, exp.EngineProperty)
                         or empty_tuple(n) and isinstance(n.parent, exp.Ordered)
                         and isinstance(n.parent.parent, exp.Order) and isinstance(n.parent.parent.parent, exp.Properties)))}
            if rejected:
                version = plan.get("target_version", "не задана")
                add("unapproved_function", f"{target} {version}: проверка не подтверждает функцию "
                    + ", ".join(sorted(rejected)), loc)
            if action=="create":
                if tree.args.get("kind")!="TABLE" or tree.args.get("replace") or tree.args.get("exists"):
                    add("create_option_not_supported","Нужен явный CREATE TABLE без скрытой замены/пропуска",loc)
                if name in available:add("duplicate_write","Таблица уже существует в этой цепочке: "+name,loc)
                props=tree.args.get("properties")
                temporary=bool(props and any(isinstance(p,exp.TemporaryProperty) for p in props.expressions))
                if temporary!=(outputs[name]["storage"]=="temporary"):add("storage_mismatch","Нарушен контракт временной/постоянной таблицы",loc)
                for prop in props.expressions if props else []:
                    if isinstance(prop,exp.TemporaryProperty):continue
                    if target=="clickhouse" and type(prop).__name__ in {"EngineProperty","Order","PartitionedByProperty","PrimaryKey","SettingsProperty"}:
                        if type(prop).__name__=="EngineProperty" and not re.fullmatch(r"(?:Memory|MergeTree|ReplacingMergeTree)(?:\([^;]*\))?",prop.this.sql(),re.I):
                            add("unsupported_engine","Движок ClickHouse не разрешён",loc)
                    else:add("unsupported_property","Свойство CREATE требует отдельной проверки: "+type(prop).__name__,loc)
            elif name not in available:
                add("missing_producer","Не создана таблица для "+action+": "+name,loc)
            queries=list(tree.find_all(exp.Select))
            if action in {"create","insert"} and not queries:
                add("query_required","Требуется проверяемый табличный SELECT",loc)
            if action in {"update","delete"}:
                for col in tree.find_all(exp.Column):
                    if col.name not in available.get(name,{}):add("unknown_mutation_column","Неизвестное поле изменения: "+col.name,loc)
            # Resolve SELECT scopes against earlier producers only. Missing aliases are
            # rejected explicitly, even if an optimizer could invent a qualifier.
            query=tree.expression if isinstance(tree.expression,(exp.Select,exp.Union,exp.Intersect,exp.Except)) else None
            nonnull = set()
            nullable = set()
            nullable_ddl_fields = []
            if query is not None:
                if lag_order:
                    lag_order_found |= check_lag_order(query, lag_order)
                unsupported={type(n).__name__ for n in query.walk() if not safe_expression(n)}
                if unsupported:add("unsupported_expression","Нужна проверка выражений: "+",".join(sorted(unsupported)),loc)
                try:
                    for scope in traverse_scope(query):
                        for _,(_,table) in scope.selected_sources.items():
                            if isinstance(table,exp.Table):
                                key=table_key(table)
                                read_tables.add(key)
                                if key not in available:add("missing_producer","Не объявлен источник/предшественник: "+key,loc)
                        for col in scope.columns:
                            if col.table and col.table not in scope.sources and not scope.is_correlated_subquery:
                                add("alias_unresolved","Псевдоним не объявлен: "+col.table,loc)
                    schema={}
                    for key,cols in available.items():
                        parts=key.split(".")
                        schema.setdefault(parts[0] if len(parts)==2 else "",{})[parts[-1]]=cols
                    resolution_query=query.copy()
                    for select in resolution_query.find_all(exp.Select):
                        settings=select.args.get('settings') or []
                        for setting in settings:
                            valid=(target=='clickhouse' and isinstance(setting,exp.EQ) and isinstance(setting.this,exp.Column) and not setting.this.table
                                and setting.this.name.casefold() in {'join_use_nulls','aggregate_functions_null_for_empty'}
                                and isinstance(setting.expression,exp.Literal) and not setting.expression.is_string and setting.expression.this in {'0','1'})
                            if not valid:add('unapproved_query_setting','Не разрешена настройка SELECT; допустимы только явные политики пропусков 0/1',loc)
                        # These are policy identifiers, not input columns. Keep the
                        # original SQL intact and exclude only the validated settings
                        # from SQLGlot's column resolver.
                        select.set('settings',None)
                    resolved=qualify(resolution_query,dialect=dialect,schema=schema,validate_qualify_columns=True,identify=False)
                    if target == "clickhouse" and action == "create":
                        nonnull = projection_nonnull(resolved, known_nonnull)
                        join_nulls = any(isinstance(setting, exp.EQ)
                            and isinstance(setting.this, exp.Column) and setting.this.name.casefold() == "join_use_nulls"
                            and isinstance(setting.expression, exp.Literal) and setting.expression.this == "1"
                            for setting in query.args.get("settings") or [])
                        nullable, proven = projection_nullability(resolved, known_nullable, known_nonnull, join_nulls)
                        nonnull.update(proven)
                    if action=="create" and resolved.named_selects!=outputs[name]["columns"]:
                        add("output_columns_mismatch","Нарушен полный перечень/порядок полей: "+name,loc)
                    if action == "create":
                        for expression in resolved.selects:
                            dtype = output_schema(outputs[name]).get(expression.alias_or_name, "").upper()
                            if (target=="clickhouse" and dtype.startswith(("DECIMAL","NUMERIC"))
                                    and plan.get("source_semantics",{}).get("numeric_arithmetic")=="exact_decimal_transport"
                                    and any(isinstance(n,exp.Avg) or (isinstance(n,(exp.Anonymous,exp.CombinedAggFunc))
                                        and str(n.this).casefold() in {"avg","avgornull"}) for n in expression.walk())):
                                add("decimal_average_via_float","При точном десятичном контракте ClickHouse AVG теряет точность до записи",loc)
                            if dtype == "DATE" and dialect == "postgres":
                                value = expression.unalias()
                                if date_interval_returns_timestamp(value):
                                    add("output_date_requires_final_cast", "DATE требует приведения после арифметики с INTERVAL", loc)
                        for expression in resolved.selects:
                            alias=expression.alias_or_name.casefold()
                            if alias in catx_outputs:
                                trims = list(expression.find_all(exp.Trim))
                                if trims and all(re.search(r"\b(?:TRAILING|RTRIM)\b", t.sql(dialect=dialect), re.I) for t in trims):
                                    add("catx_leading_blanks", "CATX удаляет начальные и конечные ASCII-пробелы", loc)
                            operand=upcase_inputs.get(alias)
                            if operand and any(t.args.get("position")!="TRAILING"
                                    and isinstance(t.this,exp.Column) and t.this.name.casefold()==operand
                                    for upper in expression.find_all(exp.Upper) for t in upper.find_all(exp.Trim)):
                                add("upcase_significant_leading_blanks","UPCASE сохраняет значимые начальные пробелы",loc)
                    if action == "insert":
                        check_append_alignment(plan, source, tree, resolved, name, available, add, loc)
                except Exception:add("column_resolution_failed","Не разрешены поля, типы или SELECT *",loc)
            # Non-query table references in DML still cannot escape the approved catalogue.
            for table in tree.find_all(exp.Table):
                if table is destination:continue
                if not queries and table_key(table) not in available:add("unbound_read","Необъявленный источник",loc)
            if target == "clickhouse" and action == "create":
                clickhouse_storage(outputs[name], props.expressions if props else [], nonnull, add, loc)
                declared = {c["name"]: c.get("nullable") for c in outputs[name].get("column_metadata", [])}
                nullable.update(field for field, required in declared.items() if required is True)
                for field in sorted(nullable):
                    if declared.get(field) is False:
                        add("nullable_source_conflicts_with_contract", "В результате может появиться NULL: " + field, loc)
                if isinstance(tree.this, exp.Schema):
                    for col in tree.this.expressions:
                        if (isinstance(col, exp.ColumnDef) and isinstance(col.kind, exp.DataType)
                                and col.name in nullable and col.kind.args.get("nullable") is not True):
                            if declared.get(col.name) is not False:
                                nullable_ddl_fields.append(col.name)
                            add("physical_nullable_output_required", "DDL должен сохранять NULL: " + col.name, loc)
                known_nullable[name] = nullable - nonnull
                known_nonnull[name] = nonnull
            if action=="drop":
                available.pop(name,None)
                known_nonnull.pop(name,None)
                known_nullable.pop(name,None)
            else:
                if action in {"insert","update"}:
                    known_nonnull.pop(name,None)
                available[name]=output_schema(outputs[name]);written.add(name)
            inventory.append({"artifact":artifact["name"],"statement":index+1,"action":action,"table":name,
                "reads":sorted(read_tables),"columns":outputs[name]['columns'],"storage":outputs[name]['storage'],
                **({"nullable_columns":sorted(nullable - nonnull)} if nullable - nonnull else {}),
                **({"nonnull_columns":sorted(nonnull)} if nonnull else {}),
                **({"nullable_ddl_fields":nullable_ddl_fields} if nullable_ddl_fields else {})})
    for name,output in outputs.items():
        if require_all and name in expected and (name not in written or (output["role"]=="final" and name not in available)):
            issues.append(finding("missing_output","Не получен обязательный результат "+name))
    if lag_order and not lag_order_found:
        issues.append(finding("lag_order_mismatch", "LAG должен воспроизводить согласованный порядок PROC SORT"))
    issues.extend(semantic_checks(plan,source,artifacts,dialect))
    return {"version":VERSION,"target":target,"dialect":dialect,"static_passed":not issues,
        "findings":issues,"statements":inventory,"execution":"not_run","equivalence":"not_run",
        "obligations":list(obligations),"hash":digest([plan,artifacts,target,issues])}


def check_append_alignment(plan, source, tree, resolved, name, available, add, loc):
    """Detect positional renaming for a single statically bound PROC APPEND FORCE."""
    from app.sas_lexer import lex_statements
    statements, _ = lex_statements(source)
    append = [s["masked"] for s in statements if re.match(r"\s*proc\s+append\b", s["masked"], re.I)]
    if len(append) != 1 or not re.search(r"\bforce\b", append[0], re.I):
        return
    source_name = re.search(r"\bdata\s*=\s*([\w.]+)", append[0], re.I)
    base_name = re.search(r"\bbase\s*=\s*([\w.]+)", append[0], re.I)
    if not source_name or not base_name:
        return
    original_base = plan.get("output_sources", {}).get(name, name)
    if base_name[1].casefold().removeprefix("work.") != original_base.casefold().removeprefix("work."):
        return
    inputs = {original.casefold(): ".".join(b["target"]["parts"]) for b in plan["bindings"]
              if (original := plan.get("source_patterns", {}).get(b["source_id"]))}
    physical = inputs.get(source_name[1].casefold())
    source_schema = available.get(physical)
    if physical and physical!=name and isinstance(resolved,exp.SetOperation) and any(
            table_key(table)==name for table in resolved.find_all(exp.Table)):
        add("append_force_base_reinsert","APPEND FORCE добавляет строки DATA и не вставляет строки BASE повторно",loc)
        return
    if not source_schema or not isinstance(resolved, exp.Select):
        return
    from_clause = resolved.args.get("from_")
    if not from_clause or not isinstance(from_clause.this, exp.Table) or table_key(from_clause.this) != physical or resolved.args.get("joins"):
        return
    target_columns = [c.name for c in tree.this.expressions] if isinstance(tree.this, exp.Schema) else list(available.get(name, {}))
    for target, expression in zip(target_columns, resolved.selects):
        value = expression.unalias()
        if isinstance(value, exp.Column) and target.casefold() != value.name.casefold():
            add("append_force_column_alignment", "APPEND FORCE сопоставляет поля по имени: " + target, loc)


def semantic_checks(plan,source,artifacts,dialect):
    from app.sas_lexer import lex_statements
    statements,_=lex_statements(source)
    source=' '.join(s['masked'] for s in statements)
    issues=[];sql="\n".join(a["sql"] for a in artifacts)
    columns={}
    for b in plan["bindings"]:
        for c in b["columns"]:columns.setdefault(c["name"].casefold(),set()).add(c["data_type"].upper())
        pattern=plan.get('source_patterns',{}).get(b['source_id'],'')
        if b.get('members') and pattern and re.search(r'\bset\s+'+re.escape(pattern),source,re.I) and not re.search(r'%(?:if|do)\b',source,re.I):
            try:actual={table_key(t) for tree in sqlglot.parse(sql,read=dialect,error_message_context=0) if tree for t in tree.find_all(exp.Table)}
            except Exception:actual=set()
            missing={'.'.join(m['target']['parts']) for m in b['members']}-actual
            if missing:issues.append(finding('source_family_member_omitted','Не прочитаны согласованные части семейства: '+','.join(sorted(missing))))
    values={k.casefold():v for k,v in plan.get('parameters',{}).items()}
    by_modes={}
    for statement in statements:
        if not re.match(r'\s*by\s+',statement['masked'],re.I):continue
        descending=False
        for word in re.findall(r'\w+',re.sub(r'^\s*by\s+','',statement['masked'],flags=re.I)):
            if word.casefold()=='descending':descending=True;continue
            by_modes.setdefault(word.casefold(),set()).add(descending);descending=False
    try:
        for tree in sqlglot.parse(sql,read=dialect,error_message_context=0):
            if not tree:continue
            for ordered in tree.find_all(exp.Ordered):
                if not isinstance(ordered.this,exp.Column):continue
                name=ordered.this.name.casefold();types=columns.get(name,set());modes=by_modes.get(name,set())
                if len(modes)!=1 or not types or not all(re.match(r'(?:INT|INTEGER|BIGINT|SMALLINT|NUMERIC|DECIMAL|DOUBLE|FLOAT)\b',t) for t in types):continue
                descending=next(iter(modes))
                if bool(ordered.args.get('desc'))==descending and bool(ordered.args.get('nulls_first'))==descending:
                    issues.append(finding('numeric_missing_sort_mismatch','Для SAS BY '+name+' требуется '+('DESC NULLS LAST' if descending else 'ASC NULLS FIRST'),obligation=name))
    except Exception:pass
    values.update({k.casefold():v.strip() for k,v in re.findall(r'%let\s+(\w+)\s*=\s*([^%&;]+);',source,re.I)})
    for name,operand in re.findall(r'\bif\s+(\w+)\s*>\s*(&\w+\.?|[+-]?\d+(?:\.\d+)?)\s*;',source,re.I):
        if not columns.get(name.casefold()) or not all(re.match(r'(?:INT|INTEGER|BIGINT|SMALLINT|NUMERIC|DECIMAL|DOUBLE|FLOAT)\b',t) for t in columns[name.casefold()]):continue
        if operand.startswith('&'):operand=values.get(operand[1:].rstrip('.').casefold(),'unknown')
        try:
            from decimal import Decimal
            if Decimal(operand)<0:continue
        except Exception:continue
        try:
            trees=sqlglot.parse(sql,read=dialect,error_message_context=0)
            for tree in trees:
                if not tree:continue
                for where in tree.find_all(exp.Where):
                    predicate=where.this.unnest()
                    if not isinstance(predicate,exp.Or):continue
                    for comparison,null_test in ((predicate.this.unnest(),predicate.expression.unnest()),(predicate.expression.unnest(),predicate.this.unnest())):
                        if isinstance(comparison,exp.GT) and isinstance(comparison.this,exp.Column) and comparison.this.name.casefold()==name.casefold() and isinstance(null_test,exp.Is) and isinstance(null_test.this,exp.Column) and null_test.this.name.casefold()==name.casefold() and isinstance(null_test.expression,exp.Null):
                            issues.append(finding('numeric_missing_filter_mismatch','OR '+name+' IS NULL добавляет числовые пропуски, отбрасываемые исходным IF',obligation=name))
        except Exception:pass # Parse failures are independently reported above.
    for name in re.findall(r"\bmissing\s*\(\s*([A-Za-z_]\w*)\s*\)",source,re.I):
        types=columns.get(name.casefold(),set())
        if types and all(re.match(r"(?:CHAR|VARCHAR|TEXT|STRING)\b",t) for t in types):
            if re.search(r"\b"+re.escape(name)+r"\b[^;]*\bIS\s+(?:NOT\s+)?NULL",sql,re.I) and not re.search(r"(?:trim|nullif|=\s*'\s*')",sql,re.I):
                issues.append(finding("character_missing_mismatch","MISSING для строки должен учитывать пустую строку и пробелы",obligation=name))
        if types and all(re.match(r"(?:INT|INTEGER|BIGINT|SMALLINT|NUMERIC|DECIMAL|DOUBLE|FLOAT)\b",t) for t in types):
            if re.search(r"\btrim\s*\(\s*(?:\w+\.)?"+re.escape(name)+r"\s*\)",sql,re.I):
                issues.append(finding("numeric_missing_type_mismatch","Числовой MISSING нельзя проверять строковым TRIM",obligation=name))
    if re.search(r"\bmerge\b",source,re.I) and re.search(r"\bjoin\b",sql,re.I):
        by=re.search(r"\bby\s+([\w\s]+);",source,re.I)
        keys=set(by[1].casefold().split()) if by else set()
        if not keys or not all(any(set(k)<=keys for k in b.get("unique_keys",[])) for b in plan["bindings"]):
            issues.append(finding("merge_cardinality_unconfirmed","Обычный JOIN для MERGE требует подтверждения BY-ключей и кратности"))
    if re.search(r"(?<![\w.])\.[A-Z_](?![\w.])",source) and plan.get("source_semantics",{}).get("numeric_missing") in {"ordinary_as_null","special_tags_lost"}:
        issues.append(finding("special_missing_tags_required","Нельзя восстановить различимые специальные missing из одного NULL"))
    return issues
