"""Versioned laboratory comparison. This module never changes input rows or SQL."""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .compare import family

_policy = Path(__file__).with_name("comparison_policy_v2.json").read_bytes()
POLICY = json.loads(_policy)
POLICY_HASH = hashlib.sha256(_policy).hexdigest()
QUANTUM = Decimal(POLICY["numeric"]["quantum"])
LIMIT = POLICY["numeric"]["max_decimal_digits"]
RULES = ["decimal_half_up_0.001", "date_midnight", "legacy_sas_character_transport"]


def _family(type_name):
    value = type_name.strip()
    if value.lower().startswith("nullable(") and value.endswith(")"):
        value = value[9:-1]
    return family(value)


def _numeric(value, transport):
    if isinstance(value, str):
        if not transport:
            raise ValueError("Numeric string requires declared transport")
        if transport == "sas_csv" and value in {"", "."}:
            return ("null",)
        if re.fullmatch(r"\.[A-Za-z_]", value):
            return ("sas_missing", value.upper())
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Expected a numeric value")
    # repr(float) is its stable round-trip decimal, not a preliminary rounding.
    number = Decimal(repr(value) if isinstance(value,float) else value)
    if number.is_nan():
        return ("nan",)
    if number.is_infinite():
        return ("infinity", "-" if number.is_signed() else "+")
    if len(number.as_tuple().digits) > LIMIT or abs(number.adjusted()) > LIMIT:
        raise ValueError("Numeric value exceeds comparison precision bound")
    return ("number", number)


def _temporal(value, kind, transport):
    if isinstance(value, datetime):
        return ("timestamp", value.isoformat())
    if isinstance(value,date):
        return ("date",value.isoformat())
    if not isinstance(value,str) or not transport:
        raise ValueError("Temporal string requires declared transport")
    if transport=="sas_csv":
        if value in {"","."}:
            return ("null",)
        if re.fullmatch(r"[+-]?\d+(?:\.\d+)?",value):
            number=Decimal(value)
            digits=len(number.as_tuple().digits)
            if digits > LIMIT or abs(number.adjusted()) > LIMIT:
                raise ValueError("SAS date/time exceeds comparison precision bound")
            with localcontext() as ctx:
                ctx.prec=max(32,digits+12)
                delta=number*(86400 if kind=="date" else 1)*1000000
            if delta != delta.to_integral_value():
                raise ValueError("SAS date/time transport exceeds microsecond precision")
            result=datetime(1960,1,1)+timedelta(microseconds=int(delta))
            if kind=="date" and result.time()==datetime.min.time():
                return ("date",result.date().isoformat())
            return ("timestamp",result.isoformat())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
        return ("date",date.fromisoformat(value).isoformat())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?",value):
        return ("timestamp",datetime.fromisoformat(value).isoformat())
    raise ValueError("Unrecognized declared date/time transport")


def _raw(value, kind, transport):
    if value is None:
        return ("null",)
    if kind=="numeric":
        return _numeric(value,transport)
    if kind in {"date","timestamp"}:
        return _temporal(value,kind,transport)
    if kind=="text" and isinstance(value,str):
        return ("text",value)
    if kind=="boolean" and type(value) is bool:
        return ("boolean",value)
    raise ValueError("Value does not match the declared field type")


def _normalize(cell, kind, zone):
    tag=cell[0]
    if tag=="number":
        n=cell[1]
        with localcontext() as ctx:
            ctx.prec=max(32,len(n.as_tuple().digits)+5,n.adjusted()+5)
            return ("number",n.quantize(QUANTUM,rounding=ROUND_HALF_UP))
    if kind=="text":
        return ("text","" if tag=="null" else cell[1].rstrip(" "))
    if tag!="timestamp":
        return cell
    dt=datetime.fromisoformat(cell[1])
    if dt.tzinfo is not None:
        if zone is None:
            raise ValueError("Explicit comparison timezone required")
        dt=dt.astimezone(zone)
    midnight=not any((dt.hour,dt.minute,dt.second,dt.microsecond))
    if midnight and (dt.tzinfo is None or dt.replace(fold=0).utcoffset()==dt.replace(fold=1).utcoffset()):
        return ("date",dt.date().isoformat())
    return ("timestamp",dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else dt.isoformat())


def _diff(left,right):
    a,b=Counter(left),Counter(right)
    missing,extra=a-b,b-a
    def entries(items):
        return [{"row":[list(cell) for cell in row],"count":count} for row,count in items.items()]
    return {"missing_rows":sum(missing.values()),"extra_rows":sum(extra.values()),
            "missing":entries(missing),"extra":entries(extra)}


def _collisions(raw, normalized):
    groups=defaultdict(set)
    for original,final in zip(raw,normalized):
        groups[final].add(original)
    return sum(len(originals)>1 for originals in groups.values())


def compare_v2(columns, expected, actual, *, actual_columns=None, keys=(), ordered=False,
               expected_transport=None, actual_transport=None, comparison_zone=None):
    """Compare complete typed row bags. Preserve exact key identity when specified."""
    names=[c["name"] for c in columns]
    kinds=[_family(c["data_type"]) for c in columns]
    result={"passed":False,"raw_values_equal":None,"normalized_values_equal":None,
            "schema_status":"not_recorded","structure_status":"passed","status":"not_checked",
            "policy_hash":POLICY_HASH,"rules":RULES,"expected_rows":len(expected),"actual_rows":len(actual),
            "declared_keys":list(keys),"ordered":ordered,"comparison_zone":comparison_zone}
    if actual_columns is not None:
        result["type_differences"]=[
            {"field":l["name"],"expected":l["data_type"],"actual":r["data_type"]}
            for l,r in zip(columns,actual_columns) if l["data_type"].upper()!=r["data_type"].upper()]
        if [c["name"] for c in actual_columns]!=names:
            result.update(schema_status="column_mismatch",structure_status="column_mismatch",status="mismatch")
            return result
        physical_matches=all(l["data_type"].upper()==r["data_type"].upper() or not l.get("physical_type_required")
                             for l,r in zip(columns,actual_columns))
        family_matches=all(k==a or {k,a}=={"date","timestamp"}
                           for k,a in zip(kinds,(_family(c["data_type"]) for c in actual_columns)))
        result["schema_status"]="passed" if physical_matches and family_matches else "type_mismatch"
    if any(len(row)!=len(names) for rows in (expected,actual) for row in rows):
        result.update(structure_status="row_width_mismatch",status="mismatch")
        return result
    if not set(keys)<=set(names) or len(keys)!=len(set(keys)):
        raise ValueError("Invalid comparison keys")
    key_indices={names.index(name) for name in keys}
    def typed(rows, transport):
        return [tuple(_raw(v,k,transport) for v,k in zip(row,kinds)) for row in rows]
    try:
        left,right=typed(expected,expected_transport),typed(actual,actual_transport)
        result["raw_diff"]=_diff(left,right)
        result["raw_values_equal"]=left==right if ordered else left==right or Counter(left)==Counter(right)
        zone=ZoneInfo(comparison_zone) if comparison_zone and comparison_zone!="UTC" else (timezone.utc if comparison_zone else None)
        def normalized(rows):
            return [tuple(cell if i in key_indices and cell[0]!="timestamp" else _normalize(cell,k,zone)
                          for i,(cell,k) in enumerate(zip(row,kinds))) for row in rows]
        normalized_left,normalized_right=normalized(left),normalized(right)
        # A key retains its raw timestamp even when a zone was required for validation.
        for rows, raw_rows in ((normalized_left,left),(normalized_right,right)):
            if key_indices:
                rows[:]=[tuple(orig[i] if i in key_indices else cell for i,cell in enumerate(norm))
                         for norm,orig in zip(rows,raw_rows)]
        result["normalized_diff"]=_diff(normalized_left,normalized_right)
        if ordered:
            result["position_mismatches"]={
                "raw":sum(a!=b for a,b in zip(left,right))+abs(len(left)-len(right)),
                "normalized":sum(a!=b for a,b in zip(normalized_left,normalized_right))
                             +abs(len(normalized_left)-len(normalized_right))}
        result["normalization_collisions"]={"expected":_collisions(left,normalized_left),"actual":_collisions(right,normalized_right)}
        equal=normalized_left==normalized_right if ordered else Counter(normalized_left)==Counter(normalized_right)
        result["normalized_values_equal"]=equal
        result["passed"]=equal and result["schema_status"]=="passed"
        result["status"]=("mismatch" if not equal or result["schema_status"]=="type_mismatch"
                          else "not_checked" if not result["passed"]
                          else "exact_match" if result["raw_values_equal"] else "normalized_match")
    except (ValueError,TypeError,DecimalException,OverflowError,ZoneInfoNotFoundError) as error:
        result.update(status="indeterminate",reason=str(error))
    return result


def compare_outputs(expected_outputs, actual_outputs):
    """Each output carries columns, rows, and optional comparison arguments."""
    missing=sorted(set(expected_outputs)-set(actual_outputs))
    extra=sorted(set(actual_outputs)-set(expected_outputs))
    outputs={}
    for name in sorted(set(expected_outputs)&set(actual_outputs)):
        wanted,found=expected_outputs[name],actual_outputs[name]
        outputs[name]=compare_v2(wanted["columns"],wanted["rows"],found["rows"],
                                 actual_columns=found.get("columns"),**wanted.get("options",{}))
    return {"passed":not missing and not extra and all(r["passed"] for r in outputs.values()),
            "missing_outputs":missing,"extra_outputs":extra,"policy_hash":POLICY_HASH,"outputs":outputs}
