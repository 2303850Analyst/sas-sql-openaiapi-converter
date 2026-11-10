"""Reviewed synthetic example contracts. Filling them does not approve gate 1."""
import json
from pathlib import Path


def load_example(wb, project, example):
    catalog = json.loads((Path(__file__).with_name("example_contracts.json")).read_text(encoding="utf-8"))
    manifest = catalog.get(project["snapshot"]["archive_sha256"], {}).get(example)
    if not manifest:
        raise ValueError("No built-in contract for this archive; configure your sources and outputs")
    p = wb.command(project["id"], project["revision"], "select_scenario", {"source_ids": manifest["source_ids"]})
    for name in manifest["inputs"]:
        if name not in {i["original"] for i in p["context"]["inputs"]}:
            p = wb.command(p["id"], p["revision"], "declare_input",
                           {"origin_source_id": manifest["source_ids"][0], "name": {"parts": name.split('.')},
                            "rationale": "Reviewed synthetic macro source in complete program"})
    plan = dict(manifest["plan"], scenario_id=p["scenario"]["id"])
    # These facts apply only to the archive hashes registered above, never to arbitrary EGP.
    plan["source_semantics"] = {"numeric_missing": "ordinary_as_null",
        "character_missing": "null_empty_ascii_spaces", "character_storage": "unpadded_varchar",
        "input_observation_order": "explicit_sort_only"}
    plan["bindings"] = [{"source_id": i["id"], "decision": "replace", "metadata_status": "user_supplied",
                         **manifest["inputs"][i["original"]]} for i in p["context"]["inputs"]]
    plan["resolutions"] = [{"issue_id": i["id"], "rationale": "Reviewed complete synthetic scenario and explicit source/output contracts"}
                           for i in p["context"]["issues"] if not i["hard"]]
    return wb.command(p["id"], p["revision"], "save_plan", plan)
