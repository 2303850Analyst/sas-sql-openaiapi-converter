"""Synthetic QA controls for the laboratory comparator, not the production executor."""
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from math import nextafter

import pytest

from fixtures.testifier_lab.compare_v2 import compare_v2, compare_outputs, _normalize, POLICY_HASH


def scalar(left, right, dtype="NUMERIC", **options):
    columns=[{"name":"v","data_type":dtype}]
    return compare_v2(columns,[[left]],[[right]],actual_columns=columns,**options)


@pytest.mark.parametrize("left,right,matched,raw", [
    ("2.789473684210526315", "2.7894736842105263", True, False),
    ("2.789473684210526315", "2.79", False, False),
    ("1.2344","1.23449",True,False),
    ("1.23449","1.23451",False,False),
    ("1.2345","1.235",True,False),
    ("-1.2345","-1.235",True,False),
    ("-1.23449","-1.235",False,False),
    ("999.9995","1000",True,False),
    ("-999.9995","-1000",True,False),
    ("123456789012345678901234567890.1235","123456789012345678901234567890.124",True,False),
    ("1","1.0000000001",True,False),
    ("1.234","1.234",True,True),
    ("-0","0",True,True),
    ("NaN","NaN",True,True),
    ("NaN","Infinity",False,False),
    ("Infinity","Infinity",True,True),
    ("Infinity","-Infinity",False,False),
    (None,"0",False,False),
])
def test_numeric_boundary_controls(left,right,matched,raw):
    result=scalar(Decimal(left) if left is not None else None,Decimal(right))
    assert (result["passed"],result["raw_values_equal"])==(matched,raw)
    assert result["policy_hash"]==POLICY_HASH


@pytest.mark.parametrize("left,right,status", [
    (1.2345,1.235,"normalized_match"),
    (nextafter(1.2345,0),1.235,"mismatch"),
    (nextafter(1.2345,2),1.235,"normalized_match"),
    ("1.2345",Decimal("1.235"),"indeterminate"),
    (True,1,"indeterminate"),
    (Decimal("1e1001"),0,"indeterminate"),
])
def test_numeric_transport(left,right,status):
    assert scalar(left,right)["status"]==status


@pytest.mark.parametrize("left,right,passed", [
    ("a","a ",True),(" a","a",False),("a\t","a",False),("a\u00a0","a",False),
    ("001","1",False),("A","a",False),("",None,True),(" ","",True),(" ","\t",False),
])
def test_existing_sas_character_transport(left,right,passed):
    assert scalar(left,right,"VARCHAR(20)")["passed"]==passed


@pytest.mark.parametrize("actual,zone,status", [
    (datetime(2026,11,9),None,"normalized_match"),
    (datetime(2026,11,9,0,0,0,1),None,"mismatch"),
    (datetime(2026,11,9,12),None,"mismatch"),
    (datetime(2026,11,9,tzinfo=timezone.utc),None,"indeterminate"),
    (datetime(2026,11,9,tzinfo=timezone.utc),"UTC","normalized_match"),
    (datetime(2026,11,9,tzinfo=timezone(timedelta(hours=3))),"UTC","mismatch"),
    ("2026-11-09",None,"indeterminate"),
])
def test_date_controls(actual,zone,status):
    assert scalar(date(2026,11,9),actual,"DATE",comparison_zone=zone)["status"]==status


@pytest.mark.parametrize("actual,status", [
    ("0.000001000000000000000000000000000001","indeterminate"),
    ("0.000001000000000000000000000000000000","exact_match"),
])
def test_sas_datetime_fraction_is_checked_before_transport_conversion(actual,status):
    result=scalar(datetime(1960,1,1,0,0,0,1),actual,"TIMESTAMP",actual_transport="sas_csv")
    assert result["status"]==status


def test_physical_timestamp_requirement_is_independent():
    columns=[{"name":"d","data_type":"DATE","physical_type_required":True}]
    res=compare_v2(columns,[[date(2026,11,9)]],[[datetime(2026,11,9)]],
                   actual_columns=[{"name":"d","data_type":"TIMESTAMP"}])
    assert res["normalized_values_equal"] and res["schema_status"]=="type_mismatch" and not res["passed"]


def test_special_missing_tags_distinct():
    assert scalar(".A",".a",expected_transport="sas_csv",actual_transport="sas_csv")["passed"]
    assert not scalar(".A",".B",expected_transport="sas_csv",actual_transport="sas_csv")["passed"]
    assert not scalar(".A",None,expected_transport="sas_csv")["passed"]
    assert scalar(".",None,expected_transport="sas_csv")["passed"]


@pytest.mark.parametrize("mutation",["missing_cell","extra_cell","missing_column","extra_column","reordered_columns","type","lost_duplicate","added_duplicate","missing_output","extra_output"])
def test_structural_mutations(mutation):
    cols=[{"name":"id","data_type":"INTEGER"},{"name":"value","data_type":"NUMERIC"}]
    rows=[[1,Decimal("0.00001")],[1,Decimal("0.00001")]]
    expected={"result":{"columns":cols,"rows":rows}}
    found={"result":{"columns":cols,"rows":rows}}
    r=found["result"]
    if mutation=="missing_cell":r["rows"]=[[1],rows[1]]
    if mutation=="extra_cell":r["rows"]=[rows[0]+[0],rows[1]]
    if mutation=="missing_column":r["columns"]=cols[:1]
    if mutation=="extra_column":r["columns"]=cols+[{"name":"other","data_type":"INTEGER"}]
    if mutation=="reordered_columns":r["columns"]=cols[::-1]
    if mutation=="type":r["columns"]=[cols[0],{"name":"value","data_type":"TEXT"}]
    if mutation=="lost_duplicate":r["rows"]=rows[:1]
    if mutation=="added_duplicate":r["rows"]=rows+rows[:1]
    if mutation=="missing_output":found={}
    if mutation=="extra_output":found["unexpected"]=r
    assert not compare_outputs(expected,found)["passed"]


def test_keys_collisions_order_symmetry_and_raw_immutability():
    cols=[{"name":"id","data_type":"NUMERIC"}]
    left=[[Decimal("1.2344")],[Decimal("1.23449")]]
    right=[[Decimal("1.234")],[Decimal("1.234")]]
    raw=[row[:] for row in left]
    a=compare_v2(cols,left,right,actual_columns=cols)
    b=compare_v2(cols,right,left,actual_columns=cols)
    assert a["passed"] and b["passed"] and not a["raw_values_equal"] and not b["raw_values_equal"]
    assert a["normalization_collisions"]=={"expected":1,"actual":0}
    assert left==raw
    assert not compare_v2(cols,left,right,actual_columns=cols,keys=("id",))["passed"]
    assert compare_v2(cols,left,left[::-1],actual_columns=cols)["raw_values_equal"]
    ordered=compare_v2(cols,left,left[::-1],actual_columns=cols,ordered=True)
    assert ordered["position_mismatches"]["raw"]==2
    v=("number",Decimal("1.2345"))
    n=_normalize(v,"numeric",None)
    assert n==_normalize(n,"numeric",None)
