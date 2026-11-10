import json
import os
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from migration_v2 import generation, model_profiles
from migration_v2.server import create_app


HEADERS = {"X-Local-Session": "local-test"}
MARKER = "test-only-secret-marker-84"


@contextmanager
def browser(path):
    with TestClient(create_app(path, "local-test"), headers=HEADERS) as client:
        yield client


def settings(**updates):
    return {"name": "Laboratory", "url": "https://inference.example/prefix/v1",
            "model": "alias/internal", "tariff": "corporate", **updates}


def test_profile_restart_guard_and_redaction(tmp_path, monkeypatch):
    if os.name != "nt":
        monkeypatch.setattr(model_profiles, "transform", lambda value, _: bytes(x ^ 173 for x in value))
    seen = []

    def provider(request):
        seen.append((str(request.url), request.headers.get("Authorization")))
        if request.method == "GET":
            return httpx.Response(404, json={"message": MARKER})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}],
                                         "echo": MARKER, "usage": {"prompt_tokens": 9, "completion_tokens": 1}})

    monkeypatch.setattr(generation, "model_client", lambda _: httpx.Client(transport=httpx.MockTransport(provider)))
    with browser(tmp_path) as client:
        assert client.get("/api/model-profiles", headers={"X-Local-Session": ""}).status_code == 403
        assert client.post("/api/model-profiles", headers={"Origin": "https://outside.invalid"}, json={}).status_code == 403
        r = client.post("/api/model-profiles", json={"settings": settings(), "key_action": "save", "key": MARKER})
        assert r.status_code == 200, r.text
        assert MARKER not in r.text
        ident = r.json()["active"]
        assert r.json()["profiles"][0]["endpoint"] == "https://inference.example/prefix/v1/chat/completions"
        assert client.put(f"/api/model-profiles/{ident}", json={"settings": settings(url="https://different.example/v1")}).status_code == 400
        assert client.get("/api/status").json()["model_enabled"]
    with browser(tmp_path) as client:
        assert client.get("/api/model-profiles").json()["profiles"][0]["has_key"]
        catalog = client.post(f"/api/model-profiles/{ident}/models")
        assert catalog.status_code == 400 and "404" in catalog.text and MARKER not in catalog.text
        probe = client.post(f"/api/model-profiles/{ident}/probe")
        assert probe.status_code == 200 and probe.json()["model"] == "alias/internal"
        assert seen == [("https://inference.example/prefix/v1/models", "Bearer " + MARKER),
                        ("https://inference.example/prefix/v1/chat/completions", "Bearer " + MARKER)]
        assert client.get("/api/status").json()["budget"]["charged_or_reserved_usd"] == 0
        assert client.put(f"/api/model-profiles/{ident}", json={"settings": settings(), "key_action": "remove"}).status_code == 200
        assert client.post(f"/api/model-profiles/{ident}/probe").status_code == 400
    assert all(MARKER.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


def test_session_and_explicit_http(tmp_path):
    with browser(tmp_path) as client:
        request = {"settings": settings(url="http://127.0.0.1:8080/x/chat/completions", url_kind="operation"),
                   "key_action": "session", "key": MARKER}
        assert client.post("/api/model-profiles", json=request).status_code == 400
        request["settings"]["allow_http"] = True
        result = client.post("/api/model-profiles", json=request)
        assert result.status_code == 200
        assert result.json()["profiles"][0]["endpoint"] == request["settings"]["url"]
        assert result.json()["profiles"][0]["key_storage"] == "session"
    with browser(tmp_path) as client:
        assert not client.get("/api/model-profiles").json()["profiles"][0]["has_key"]
    assert MARKER not in (tmp_path / "model-profiles.json").read_text(encoding="utf-8")


def test_profile_rejects_duplicate_version_and_unknown_tariff(tmp_path):
    with browser(tmp_path) as client:
        assert client.post("/api/model-profiles", json={"settings": settings(url="https://h.example/v1/v1")}).status_code == 400
        result = client.post("/api/model-profiles", json={"settings": settings(tariff="unknown", auth="none")})
        assert result.status_code == 200
        ident = result.json()["active"]
        assert client.post(f"/api/model-profiles/{ident}/probe").status_code == 400
        assert client.get("/api/status").json()["budget"]["requests"] == 0



def test_registered_synthetic_archive_does_not_authorize_uploaded_sources():
    from migration_v2.ai_pipeline import exposure
    from migration_v2.model_profiles import Connection
    approved = "a" * 64
    profile = Connection.model_validate(settings(tariff="paid", input_rate=2.0, output_rate=5.0)).profile(MARKER, (approved,))
    p = {"snapshot": {"archive_sha256": approved}}
    exposure(p, profile, {"test": "synthetic"})
    for attachment in ("attachments", "include_sources", "manual_inputs"):
        with pytest.raises(ValueError, match="синтетические исходники"):
            exposure({**p, attachment: [{"status": "available"}]}, profile, {"test": "synthetic"})
