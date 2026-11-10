"""Reuse AI-translated SQL only for a bounded common TEMP CTAS subset.

Every adapted target still receives its own static and independent AI review.
This module does not translate SAS or approve a candidate.
"""
import sqlglot
from sqlglot import exp
from .contracts import digest
from .linker import DIALECTS


def adapt(candidate,source_target,target,task_hash,obligations):
    artifacts=[]
    allowed={exp.Select,exp.From,exp.Table,exp.Identifier,exp.TableAlias,exp.Column,exp.Alias,
        exp.Where,exp.And,exp.Or,exp.Not,exp.Is,exp.Null,exp.Literal,exp.GT,exp.GTE,exp.LT,exp.LTE,exp.EQ,exp.NEQ,exp.Paren}
    for artifact in candidate['artifacts']:
        trees=sqlglot.parse(artifact['sql'],read=DIALECTS[source_target])
        output=[]
        for tree in trees:
            if not isinstance(tree,exp.Create) or tree.args.get('kind')!='TABLE' or not isinstance(tree.this,exp.Table) or not isinstance(tree.expression,exp.Select):return None
            props=tree.args.get('properties')
            if not props or not any(isinstance(p,exp.TemporaryProperty) for p in props.expressions):return None
            if any(type(n) not in allowed for n in tree.expression.walk()):return None
            if any(isinstance(n,exp.Literal) and n.is_string for n in tree.expression.walk()):return None
            if any(not isinstance(e,(exp.Column,exp.Alias)) or isinstance(e,exp.Alias) and not isinstance(e.this,exp.Column) for e in tree.expression.expressions):return None
            tree=tree.copy()
            properties=[exp.TemporaryProperty()]
            if target=='clickhouse':properties.append(exp.EngineProperty(this=exp.Var(this='Memory')))
            tree.set('properties',exp.Properties(expressions=properties))
            output.append(tree.sql(dialect=DIALECTS[target],unsupported_level=sqlglot.ErrorLevel.RAISE)+';')
        artifacts.append({'name':artifact['name'],'sql':'\n'.join(output)})
    return {'schema_version':1,'task_hash':task_hash,'artifacts':artifacts,'source_claims':list(obligations),'assumptions':[]}
