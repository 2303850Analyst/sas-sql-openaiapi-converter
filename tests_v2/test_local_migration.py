"""Synthetic regressions for data loss, binding, revisions and false acceptance."""
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from migration_v2.contracts import Candidate, Plan, parse_json
from migration_v2.context import task_context
from migration_v2.server import create_app
from migration_v2.sources import SourceVault, ingest
from migration_v2.verification import verify
from migration_v2.workflow import Workbench, Conflict


def egp(source, name="Synthetic", extra=None):
    xml = ('<ProjectCollection EGVersion="synthetic"><Elements>'
           '<Element Type="SAS.EG.ProjectElements.CodeTask"><Element>'
           '<ID>CodeTask-synthetic</ID><Label>' + name + '</Label></Element></Element>'
           '</Elements></ProjectCollection>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("project.xml", xml)
        z.writestr("CodeTask-synthetic/code.sas", source)
        for path, data in (extra or {}).items():
            z.writestr(path, data)
    return out.getvalue()


SAS = b'proc sql; create table result as select id from old.source; quit;'


def setup_plan(wb):
    p = wb.import_egp("synthetic.egp", egp(SAS))
    p = wb.command(p["id"], p["revision"], "select_scenario", {"source_ids": ["CodeTask-synthetic"]})
    inputs = p["context"]["inputs"]
    assert len(inputs) == 1
    plan = {"scenario_id": p["scenario"]["id"], "target": "greenplum", "target_version": "6.test-not-a-real-stand",
            "mode": "fixed_run", "parameters": {},
            "bindings": [{"source_id": inputs[0]["id"], "decision": "replace",
                          "target": {"parts": ["dwh", "source"]},
                          "columns": [{"name": "id", "data_type": "INTEGER"}], "metadata_status": "user_supplied"}],
            "outputs": [{"name": {"parts": ["result"]}, "storage": "temporary", "columns": ["id"], "role": "final"}],
            "validation_scope": "Synthetic id values including duplicates and NULL, not a SAS oracle",
            "resolutions": [{"issue_id": i["id"], "rationale": "Reviewed full synthetic source and all effects"}
                            for i in p["context"]["issues"] if not i["hard"]]}
    p = wb.command(p["id"], p["revision"], "save_plan", plan)
    assert not p["blockers"]
    return wb.command(p["id"], p["revision"], "approve_plan", {})


def candidate(p, sql='CREATE TEMP TABLE result AS SELECT id FROM dwh.source;'):
    return {"schema_version": 1, "task_hash": p["task"]["hash"], "artifacts": [{"name": "001.sql", "sql": sql}],
            "source_claims": [r["id"] for r in p["context"]["regions"] if r["kind"] != "layout_or_comment"],
            "assumptions": []}


def test_original_bytes_and_unknown_suffix_survive_restart(tmp_path):
    raw = b'\xef\xbb\xbfdata x; input code $; cards;\n001\n;run;\nproc sql; select "unfinished'
    wb = Workbench(tmp_path)
    p = wb.import_egp("synthetic.egp", egp(raw))
    source = p["snapshot"]["sources"][0]
    assert wb.vault.read(source["sha256"]) == raw
    wb = Workbench(tmp_path)
    p = wb.get(p["id"])
    assert wb.source_text(p, source["id"]).endswith('select "unfinished')
    p = wb.command(p["id"], 1, "select_scenario", {"source_ids": [source["id"]]})
    text = wb.source_text(p, source["id"])
    assert ''.join(text[r["start"]:r["end"]] for r in p["context"]["regions"]) == text
    assert any(i["code"] == "lexical_cutoff" and i["hard"] for i in p["context"]["issues"])


@pytest.mark.parametrize('path', ['../x.sas', 'a/../../x.sas', 'C:/x.sas', '/x.sas', 'PROJECT.XML'])
def test_unsafe_archives_rejected(tmp_path, path):
    with pytest.raises(ValueError):
        ingest('synthetic.egp', egp(SAS, extra={path:b'x'}), SourceVault(tmp_path))


def test_versions_preserve_read_then_overwrite(tmp_path):
    wb = Workbench(tmp_path)
    source = b'proc sql; create table old.source as select id from old.source; create table work.r as select id from old.source; quit;'
    p = wb.import_egp('synthetic.egp', egp(source))
    p = wb.command(p['id'],1,'select_scenario',{'source_ids':['CodeTask-synthetic']})
    assert [r['version'] for r in p['context']['references']] == [0,1]
    assert len(p['context']['inputs']) == 1
    assert [w['storage'] for w in p['context']['writes']] == ['persistent','temporary']


def test_macro_body_is_preserved_not_run_and_dynamic_read_blocks(tmp_path):
    wb=Workbench(tmp_path)
    source=b'%macro m; data z; set src.inside; run; %mend; data x; set aaa.meets_&god.:; run; %m;'
    p=wb.import_egp('synthetic.egp',egp(source))
    p=wb.command(p['id'],1,'select_scenario',{'source_ids':['CodeTask-synthetic']})
    assert p['context']['definitions'][0]['name']=='m'
    assert not any(w['name']=='z' for w in p['context']['writes'])
    assert any(i['code']=='dynamic_source' and i['hard'] for i in p['context']['issues'])
    assert 'src.inside' in wb.source_text(p,'CodeTask-synthetic')


def test_good_static_candidate_never_claims_equivalence(tmp_path):
    wb=Workbench(tmp_path); p=setup_plan(wb)
    p=wb.command(p['id'],p['revision'],'submit_candidate',{'candidate_json':json.dumps(candidate(p))})
    assert p['evidence']['static_passed'],p['evidence']
    assert p['evidence']['checks']['execution']=='not_run'
    assert p['evidence']['checks']['equivalence']=='unavailable'
    assert not p['evidence']['release_allowed']
    with pytest.raises(ValueError,match='Release blocked'):
        wb.command(p['id'],p['revision'],'approve_release',{})
    package=zipfile.ZipFile(io.BytesIO(wb.export_draft(p)))
    manifest=json.loads(package.read('manifest.json'))
    assert manifest['status']=='unverified_draft'
    assert not any(n.endswith('.sas') for n in package.namelist())


@pytest.mark.parametrize('sql,expected',[
    ('CREATE TEMP TABLE result AS SELECT id FROM imaginary.source;', 'unbound_read'),
    ('CREATE TABLE result AS SELECT id FROM dwh.source;', 'wrong_storage'),
    ('CREATE TEMP TABLE other AS SELECT id FROM dwh.source;', 'missing_output'),
    ('CREATE TEMP TABLE result AS SELECT bad FROM dwh.source;', 'column_resolution_failed'),
    ('CREATE TEMP TABLE result AS SELECT pg_sleep(1) AS id FROM dwh.source;', 'unsupported_expression'),
    ('CREATE TEMP TABLE result AS SELECT id FROM dwh.source; DROP TABLE dwh.source;', 'unsupported_statement'),
    ('CREATE TEMP TABLE result AS SELECT id FROM dwh.source; SELECT 1;', 'unsupported_statement'),
])
def test_content_mutations_rejected(tmp_path,sql,expected):
    p=setup_plan(Workbench(tmp_path))
    c=parse_json(Candidate,json.dumps(candidate(p,sql)))
    report=verify(p['task']['hash'],parse_json(Plan,json.dumps(p['plan'])),c,c.source_claims)
    assert not report['static_passed']
    assert any(e.startswith(expected) for e in report['errors']),report


def test_cte_binding_and_multiple_outputs(tmp_path):
    p=setup_plan(Workbench(tmp_path))
    c=parse_json(Candidate,json.dumps(candidate(p,'CREATE TEMP TABLE result AS WITH t AS (SELECT id FROM dwh.source) SELECT id FROM t;')))
    report=verify(p['task']['hash'],parse_json(Plan,json.dumps(p['plan'])),c,c.source_claims)
    assert report['static_passed'],report


def test_revision_invalidation_and_stale_reply(tmp_path):
    wb=Workbench(tmp_path);p=setup_plan(wb);old=p
    p=wb.command(p['id'],p['revision'],'save_plan',p['plan'])
    assert p['task'] is None and p['gate1'] is None
    assert wb.get(p['id'],old['revision'])['task']['hash']==old['task']['hash']
    with pytest.raises(Conflict):
        wb.command(p['id'],old['revision'],'approve_plan',{})
    with pytest.raises(ValueError,match='Approve current plan'):
        wb.command(p['id'],p['revision'],'submit_candidate',{'candidate_json':json.dumps(candidate(old))})
    p=wb.command(p['id'],p['revision'],'approve_plan',{})
    assert p['task']['hash']!=old['task']['hash']
    with pytest.raises(Conflict,match='different task'):
        wb.command(p['id'],p['revision'],'submit_candidate',{'candidate_json':json.dumps(candidate(old))})


def test_envelope_rejects_truncation_duplicate_and_forged_approval(tmp_path):
    p=setup_plan(Workbench(tmp_path));c=candidate(p)
    for raw in [json.dumps(c)[:-2],json.dumps(c)[:-1]+',"task_hash":"'+'f'*64+'"}',json.dumps(dict(c,release_allowed=True))]:
        with pytest.raises(ValueError):parse_json(Candidate,raw)


def test_http_requires_local_token_and_rejects_foreign_origin(tmp_path):
    client=TestClient(create_app(tmp_path,'synthetic-test-token'))
    assert client.get('/api/status').status_code==403
    h={'X-Local-Session':'synthetic-test-token'}
    assert client.get('/api/status',headers=h).json()['model_enabled'] is False
    assert client.get('/api/status',headers={**h,'Origin':'https://outside.invalid'}).status_code==403
    p=client.post('/api/projects?filename=synthetic-egp-not-real.egp',content=egp(SAS),headers=h).json()
    assert p['name']=='synthetic-egp-not-real.egp'
    assert p['snapshot']['sources'][0]['status']=='available'
    assert client.get('/api/projects/'+p['id'],headers=h).status_code==200
    assert client.post('/api/projects/'+p['id']+'/commands',headers=h,json={'revision':1,'action':'approve_release','payload':{}}).status_code==400


def test_parameter_resolution_and_manual_input_are_revisioned(tmp_path):
    wb=Workbench(tmp_path)
    p=wb.import_egp('synthetic.egp',egp(b'data result; set src.year_&god.; run;'))
    p=wb.command(p['id'],1,'select_scenario',{'source_ids':['CodeTask-synthetic']})
    oldid=p['context']['inputs'][0]['id']
    assert p['context']['inputs'][0]['dynamic']
    p=wb.command(p['id'],p['revision'],'set_parameters',{'parameters':{'god':'2026'}})
    entry=p['context']['inputs'][0]
    assert entry['id']==oldid and not entry['dynamic'] and entry['name']==['src','year_2026']
    assert not any(i['code']=='dynamic_source' for i in p['context']['issues'])
    p=wb.command(p['id'],p['revision'],'declare_input',{
        'origin_source_id':'CodeTask-synthetic','name':{'parts':['ref','manual']},
        'rationale':'Explicit synthetic macro resource, inspected by reviewer'})
    assert p['context']['inputs'][-1]['provenance']=='human_declared'
    assert len(p['context']['inputs'])==2
    assert not p['gate1']
    with pytest.raises(ValueError,match='Input already present'):
        wb.command(p['id'],p['revision'],'declare_input',{
            'origin_source_id':'CodeTask-synthetic','name':{'parts':['ref','manual']},
            'rationale':'Duplicate resource must not be added again'})


def test_concurrent_edit_is_compare_and_swap(tmp_path):
    wb=Workbench(tmp_path);p=setup_plan(wb)
    def change():
        try:
            wb.command(p['id'],p['revision'],'save_plan',p['plan'])
            return 'saved'
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:change(),range(2)))
    assert sorted(outcomes)==['conflict','saved']


def test_malicious_identifier_and_duplicate_parameters_are_rejected(tmp_path):
    wb=Workbench(tmp_path);p=setup_plan(wb)
    plan=json.loads(json.dumps(p['plan']))
    plan['bindings'][0]['target']['parts']=['dwh','x;drop table y']
    with pytest.raises(ValueError):wb.command(p['id'],p['revision'],'save_plan',plan)
    with pytest.raises(ValueError):
        wb.command(p['id'],p['revision'],'set_parameters',{'parameters':{'GOD':'2026','god':'2025'}})


def test_static_checks_do_not_pretend_to_detect_changed_values(tmp_path):
    p=setup_plan(Workbench(tmp_path))
    c=parse_json(Candidate,json.dumps(candidate(p,'CREATE TEMP TABLE result AS SELECT 999 AS id FROM dwh.source;')))
    report=verify(p['task']['hash'],parse_json(Plan,json.dumps(p['plan'])),c,c.source_claims)
    assert report['static_passed']  # Known limitation: values require independent execution/oracle.
    assert not report['release_allowed'] and report['checks']['equivalence']=='unavailable'


def test_unicode_coordinates_and_included_code_are_not_silently_lost(tmp_path):
    wb=Workbench(tmp_path)
    code='/* синтетический 😀 */ proc sql; select "Москва"; quit; %include "missing.sas";'
    p=wb.import_egp('synthetic.egp',egp(code.encode('utf-8')))
    p=wb.command(p['id'],1,'select_scenario',{'source_ids':['CodeTask-synthetic']})
    assert ''.join(code[r['start']:r['end']] for r in p['context']['regions'])==code
    assert all(r['coordinate_system']=='unicode_codepoints' for r in p['context']['regions'])
    assert any(i['code']=='external_code_contract_required' and i['hard'] for i in p['context']['issues'])


def test_unsupported_task_kept_and_blocks_selected_scenario(tmp_path):
    data=egp(SAS)
    src=zipfile.ZipFile(io.BytesIO(data));out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        xml=src.read('project.xml').replace(b'</Elements>',b'<Element Type="SAS.EG.ProjectElements.ImportTask"><Element><ID>Import-synthetic</ID><Label>Import</Label></Element></Element><Element Type="SAS.EG.ProjectElements.OrderedList"><Element><ID>Order-synthetic</ID><Label>Run</Label></Element><TASKID>Import-synthetic</TASKID><TASKID>CodeTask-synthetic</TASKID></Element></Elements>')
        z.writestr('project.xml',xml)
        z.writestr('CodeTask-synthetic/code.sas',SAS)
    wb=Workbench(tmp_path);p=wb.import_egp('synthetic.egp',out.getvalue())
    assert p['snapshot']['unsupported_nodes'][0]['kind']=='ImportTask'
    p=wb.command(p['id'],1,'select_scenario',{'scenario_id':'Order-synthetic'})
    assert any(i['source_id']=='Import-synthetic' and i['hard'] for i in p['context']['issues'])
