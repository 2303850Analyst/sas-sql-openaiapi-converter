"""Observed synthetic ClickHouse failures: engine defaults and local functions."""
from migration_v2.linker import link
from tests_v2.test_destinations import base


def test_clickhouse_requires_explicit_persistent_engine_and_mergetree_order(tmp_path):
    p=base(tmp_path);p['outputs']=[{'name':{'parts':['qa','report_test']},'storage':'persistent','columns':['id','value'],'role':'final'}]
    p['allowed_effects']={}
    def check(prop):return link(p,[{'name':'001.sql','sql':'CREATE TABLE qa.report_test '+prop+' AS SELECT id,value FROM lab.events;'}],'clickhouse')
    assert any(f['code']=='explicit_engine_required' for f in check('')['findings'])
    assert any(f['code']=='mergetree_order_required' for f in check('ENGINE=MergeTree()')['findings'])
    assert any(f['code']=='mergetree_key_nullability_unconfirmed' for f in check('ENGINE=MergeTree() ORDER BY id')['findings'])
    assert check('ENGINE=MergeTree() ORDER BY tuple()')['static_passed']
    assert any(f['code']=='durable_engine_required' for f in check('ENGINE=Memory')['findings'])
    p['outputs'][0]['clickhouse_nullable_key']=True
    assert check('ENGINE=MergeTree ORDER BY id SETTINGS allow_nullable_key=1')['static_passed']


def test_local_clickhouse_functions_do_not_allow_network_functions(tmp_path):
    p=base(tmp_path);p['outputs']=[{'name':{'parts':['result']},'storage':'temporary','columns':['id','value'],'role':'final'}]
    good=[{'name':'a.sql','sql':'CREATE TEMPORARY TABLE result ENGINE=Memory AS SELECT id,toUInt8(multiIf(value IS NULL,1,0)) AS value FROM lab.events;'}]
    assert link(p,good,'clickhouse')['static_passed']
    bad=[{'name':'a.sql','sql':"CREATE TEMPORARY TABLE result ENGINE=Memory AS SELECT id,url('https://example.invalid') AS value FROM lab.events;"}]
    assert any(f['code']=='unapproved_function' for f in link(p,bad,'clickhouse')['findings'])


def test_clickhouse_null_policy_settings_are_not_resolved_as_columns(tmp_path):
    p=base(tmp_path);p['outputs']=[{'name':{'parts':['result']},'storage':'temporary','columns':['id','value'],'role':'final'}]
    def checked(setting):return link(p,[{'name':'a.sql','sql':'CREATE TEMPORARY TABLE result ENGINE=Memory AS SELECT id,value FROM lab.events SETTINGS '+setting+';'}],'clickhouse')
    assert checked('join_use_nulls=1')['static_passed']
    assert checked('aggregate_functions_null_for_empty=1')['static_passed']
    assert any(f['code']=='unapproved_query_setting' for f in checked('max_threads=1000')['findings'])
    assert any(f['code']=='unapproved_query_setting' for f in checked('join_use_nulls=id')['findings'])


def test_only_named_null_aggregate_combinators_are_allowed(tmp_path):
    p=base(tmp_path);p['outputs']=[{'name':{'parts':['result']},'storage':'temporary','columns':['id','value'],'role':'final'}]
    artifacts=[{'name':'a.sql','sql':'CREATE TEMPORARY TABLE result ENGINE=Memory AS SELECT id,sumOrNull(value) AS value FROM lab.events GROUP BY id;'}]
    assert link(p,artifacts,'clickhouse')['static_passed']
    artifacts[0]['sql']=artifacts[0]['sql'].replace('sumOrNull(value)',"urlOrNull(value)")
    assert any(f['code']=='unapproved_function' for f in link(p,artifacts,'clickhouse')['findings'])
