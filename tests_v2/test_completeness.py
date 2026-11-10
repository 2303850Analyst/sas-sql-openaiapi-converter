"""Synthetic regressions for the completeness redesign; no model transport."""
from decimal import Decimal, localcontext
import json
import pytest
import sqlglot
from app.sas_lexer import lex_statements
from app.macro_processor import MacroProcessor
from migration_v2.execution import canonical, isolate_statement
from migration_v2.context import task_context
from migration_v2.contracts import digest
from migration_v2.workflow import Workbench
from fixtures.v2_pilot.build import archive
from migration_v2.examples import load_example


def test_exact_numbers_independent_of_context():
    a = Decimal('123456789012345678901234567890')
    b = Decimal('123456789012345678901234567891')
    with localcontext() as ctx:
        ctx.prec = 3
        assert canonical(a) != canonical(b)
        assert canonical(Decimal('1.000')) == canonical(1)
        assert canonical(Decimal('-0.000')) == canonical(0)
    assert canonical(a) != canonical(b)


@pytest.mark.parametrize('value', [Decimal('NaN'), float('inf'), float('-inf')])
def test_nonfinite_not_silently_equal(value):
    with pytest.raises(ValueError): canonical(value)


def test_qualified_temp_rewrite():
    tree = sqlglot.parse_one('CREATE TABLE dst.second AS SELECT dst.first.id FROM dst.first', read='postgres')
    rewritten = isolate_statement(tree, {'dst.first':['tmp_first'], 'dst.second':['tmp_second']}).sql(dialect='postgres')
    assert 'dst.first.id' not in rewritten
    assert 'first.id' in rewritten and 'tmp_first' in rewritten
    assert 'dst.first.id' in tree.sql()


def test_macro_quote_does_not_create_fake_definition():
    text = '%let label=%nrstr(Fixture: ; %macro &literal); proc sql; create table x as select id from a.b; quit;'
    statements, cutoff = lex_statements(text)
    assert cutoff is None and len(statements) == 4
    assert statements[0]['text'] == '%let label=%nrstr(Fixture: ; %macro &literal);'
    # Unsupported quoting must become an explicit requirement, never an exception.
    expanded = MacroProcessor().expand_program('synthetic', text)
    assert expanded.requirements


def test_lists_and_append_effects():
    class Vault:
        def text(self, source): return text
    text = 'data one(drop=x) two; set src.a(rename=(x=y)) src.b(keep=id); run; proc append base=one data=src.c; run;'
    snapshot = {'sources':[{'id':'p','status':'available','sha256':digest(text.encode())}]}
    result = task_context(snapshot, {'source_ids':['p']}, Vault())
    assert {'src.a','src.b','src.c'} <= {i['original'] for i in result['inputs']}
    assert [(w['name'],w['version']) for w in result['writes']] == [('one',1),('two',1),('one',2)]


def test_comma_sql_input():
    class Vault:
        def text(self, source): return text
    text='proc sql; create table result as select a.id from src.a a, src.b b where a.id=b.id; quit;'
    result=task_context({'sources':[{'id':'p','status':'available','sha256':digest(text.encode())}]}, {'source_ids':['p']}, Vault())
    assert {'src.a','src.b'} <= {i['original'] for i in result['inputs']}


def test_missing_source_attachment_and_history(tmp_path):
    wb=Workbench(tmp_path)
    p=wb.import_egp('synthetic.egp',archive())
    # Construct a missing-source revision using the synthetic fixture only.
    p['snapshot']['sources'][0]['status']='missing'
    with wb.connect() as db:
        db.execute('UPDATE revisions SET body=? WHERE id=?',(json.dumps(p),p['id']))
    before=wb.get(p['id'])
    assert any(r['code']=='SOURCE_FILE_REQUIRED' for r in before['preparation']['requirements'])
    after=wb.command(p['id'],p['revision'],'attach_source',{'source_id':p['snapshot']['sources'][0]['id'],'filename':'replacement.sas','text':'data x; set src.a; run;'})
    assert after['snapshot']['hash'] != before['snapshot']['hash']
    assert wb.get(p['id'],1)['snapshot']['sources'][0]['status']=='missing'
    assert after['gate1'] is None
    assert not any(r['code']=='SOURCE_FILE_REQUIRED' for r in after['preparation']['requirements'])


def test_offline_generation_is_integrated(tmp_path):
    wb=Workbench(tmp_path)
    p=load_example(wb,wb.import_egp('synthetic.egp',archive()),'ctas')
    p=wb.command(p['id'],p['revision'],'approve_plan',{})
    p=wb.command(p['id'],p['revision'],'compile_offline',{})
    assert p['offline']['api_calls']==0
    assert not p['offline']['requirements'], p['offline']['requirements']
    assert p['candidate'] and p['evidence']['static_passed']
    assert not p['evidence']['release_allowed']
    assert 'PROC SQL' not in p['candidate']['artifacts'][0]['sql'].upper()
    assert 'sky_raw.stations' in p['candidate']['artifacts'][0]['sql']


def test_old_verifier_cannot_release(tmp_path):
    wb=Workbench(tmp_path)
    p=wb.import_egp('synthetic.egp',archive())
    with pytest.raises(ValueError,match='stale'):
        wb.command(p['id'],p['revision'],'approve_release',{})


def test_source_assignment_wins_after_initial_parameter(tmp_path):
    from tests_v2.test_local_migration import egp
    wb=Workbench(tmp_path)
    p=wb.import_egp('synthetic.egp',egp(b'%let year=2024; %macro copy; data result; set src.year_&year.; run; %mend; %copy;'))
    p=wb.command(p['id'],p['revision'],'select_scenario',{'source_ids':['CodeTask-synthetic']})
    p=wb.command(p['id'],p['revision'],'set_parameters',{'parameters':{'year':'2026'}})
    assert [i['name'] for i in p['context']['inputs']]==[['src','year_2024']]
    assert p['context']['expansion_trace']


def test_independent_reference_registry_cannot_drop_output(tmp_path):
    wb=Workbench(tmp_path)
    p=load_example(wb,wb.import_egp('synthetic.egp',archive()),'ctas')
    manifest={'schema_version':1,'run_id':'synthetic-reference-registry','source_snapshot_hash':p['snapshot']['hash'],
        'parameters_hash':digest(p['plan']['parameters']),
        'outputs':[{'output':{'parts':['active_ids']},'columns':[{'name':'station_id','data_type':'INTEGER'}]}]}
    p=wb.command(p['id'],p['revision'],'attach_reference',manifest)
    assert p['reference_registry']['run_id']==manifest['run_id']
    assert p['evidence'] is None and p['gate2'] is None
    manifest['outputs'].append({'output':{'parts':['forgotten']},'columns':[{'name':'amount','data_type':'NUMERIC(18,2)'}]})
    with pytest.raises(ValueError,match='Independent full output registry'):
        wb.command(p['id'],p['revision'],'attach_reference',manifest)


def test_no_empty_column_contract_can_be_approved(tmp_path):
    wb=Workbench(tmp_path)
    p=load_example(wb,wb.import_egp('synthetic.egp',archive()),'ctas')
    plan=p['plan'];plan['bindings'][0]['columns']=[]
    plan['bindings'][0]['unique_keys']=[]
    p=wb.command(p['id'],p['revision'],'save_plan',plan)
    assert any(r['code']=='COLUMN_TYPES_REQUIRED' for r in p['preparation']['requirements'])
    with pytest.raises(ValueError,match='column_types_required'):
        wb.command(p['id'],p['revision'],'approve_plan',{})
