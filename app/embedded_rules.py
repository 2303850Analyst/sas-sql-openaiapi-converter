"""Load and verify the single machine-readable RulePack bundled with the app."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files

from .handler_registry import REGISTRY


def _hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=1)
def embedded_rule_pack() -> dict:
    path = files("app.rules").joinpath("rule_pack.json")
    pack = json.loads(path.read_text(encoding="utf-8"))
    required = {"schema_version", "compiler_revision", "workbook_sha256", "rules", "rule_pack_id", "statistics"}
    if not required <= set(pack):
        raise RuntimeError("Встроенный RulePack повреждён: отсутствуют обязательные поля")
    rule_ids = [rule.get("id") for rule in pack["rules"]]
    if len(rule_ids) != len(set(rule_ids)) or any(not isinstance(value, str) for value in rule_ids):
        raise RuntimeError("Встроенный RulePack повреждён: конфликт ID")
    fingerprint_payload = {key: pack[key] for key in ("schema_version", "compiler_revision", "workbook_sha256", "rules")}
    expected = "sha256:" + _hash(fingerprint_payload)
    if pack["rule_pack_id"] != expected:
        raise RuntimeError("Встроенный RulePack повреждён: fingerprint не совпадает")
    bound = 0
    for rule in pack["rules"]:
        spec = REGISTRY.for_rule(rule["id"])
        if rule.get("binding_status") == "bound":
            if spec is None or rule.get("handler_key") != spec.key or rule.get("handler_revision") != spec.revision:
                raise RuntimeError(f"Встроенный RulePack несовместим с обработчиком {rule['id']}")
            bound += 1
        elif rule.get("handler_key") is not None:
            raise RuntimeError(f"Справочное правило {rule['id']} не должно иметь обработчик")
    expected_stats = {"rules": len(pack["rules"]), "bound": bound, "reference_only": len(pack["rules"]) - bound}
    if pack["statistics"] != expected_stats:
        raise RuntimeError("Встроенный RulePack повреждён: статистика не совпадает")
    return pack


def embedded_rule_pack_summary() -> dict:
    pack = embedded_rule_pack()
    return {key: pack[key] for key in ("rule_pack_id", "compiler_revision", "statistics", "kind", "display_name") if key in pack}

