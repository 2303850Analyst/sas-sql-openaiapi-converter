"""Loopback-only HTTP interface for the isolated local pilot."""
from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .sources import MAX_ARCHIVE
from .workflow import Workbench, Conflict


def create_app(data_dir: Path, token: str | None = None, runtime=None):
    from .runtime import Runtime
    from .jobs import Jobs
    from .generation import Budget
    from .model_profiles import Profiles
    runtime = runtime or Runtime()
    @asynccontextmanager
    async def lifespan(app):
        yield
        app.state.jobs.close()
    app = FastAPI(title="SAS Migration V2", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.state.workbench = Workbench(data_dir)
    app.state.token = token or secrets.token_urlsafe(32)
    app.state.runtime = runtime
    app.state.jobs = Jobs(app.state.workbench, runtime)
    profiles = Profiles(app.state.workbench.root, runtime)
    app.state.budget = Budget(app.state.workbench)

    @app.middleware("http")
    async def local_session(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get("X-Local-Session", ""), app.state.token):
                return Response(status_code=403)
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "testserver"} or parsed.netloc != request.url.netloc:
                    return Response(status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        # Validation errors never include source code/inline data in HTTP logs.
        from fastapi.responses import JSONResponse
        if isinstance(error, Conflict):
            return JSONResponse({"error": str(error)}, status_code=409)
        from pydantic import ValidationError
        message = "Invalid structured contract" if isinstance(error, ValidationError) else str(error)
        return JSONResponse({"error": message}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Project or source not found"}, status_code=404)

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).with_name("web") / "index.html")

    @app.get("/app.js")
    def javascript():
        return FileResponse(Path(__file__).with_name("web") / "app.js", media_type="text/javascript")

    @app.get("/profiles.js")
    def profile_javascript():
        return FileResponse(Path(__file__).with_name("web") / "profiles.js", media_type="text/javascript")

    @app.get("/style.css")
    def css():
        return FileResponse(Path(__file__).with_name("web") / "style.css", media_type="text/css")

    @app.get("/api/status")
    def status():
        from .rule_identity import rules_fingerprint
        try:
            current = profiles.resolve()
        except ValueError:
            current = Runtime(database=runtime.database)
        return {"version": "v2-trial-0.3", **current.public(), "budget": app.state.budget.status(),
                "equivalence": "per_project", "release_allowed": False, "rules_fingerprint": rules_fingerprint()}

    @app.get("/api/model-profiles")
    def profile_list():
        return profiles.view()

    @app.post("/api/model-profiles")
    @app.put("/api/model-profiles/{ident}")
    async def profile_save(request: Request, ident: str | None = None):
        from starlette.concurrency import run_in_threadpool
        from .model_profiles import ProfileUpdate
        from .contracts import parse_json
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 32768:
                raise HTTPException(413, "Profile request too large")
        return await run_in_threadpool(profiles.update, ident, parse_json(ProfileUpdate, bytes(raw)))

    @app.post("/api/model-profiles/{ident}/activate")
    def profile_activate(ident: str):
        return profiles.activate(ident)

    @app.post("/api/model-profiles/{ident}/models")
    def profile_models(ident: str):
        from .generation import discover_models
        return {"models": discover_models(profiles.resolve(ident, probe=True).model)}

    @app.post("/api/model-profiles/{ident}/probe")
    def profile_probe(ident: str):
        from dataclasses import replace
        from .generation import completion_payload, request_json
        current = profiles.resolve(ident).model
        probe = replace(current, max_output_tokens=min(current.max_output_tokens, 64), response_format_mode="none")
        payload = completion_payload(probe, [{"role": "user", "content": "Reply with the single word OK."}])
        content, provenance = request_json(app.state.workbench, probe, payload)
        if content.strip().strip(".").upper() != "OK":
            raise ValueError("Модель ответила, но не прошла синтетическую проверку формата")
        return {"passed": True, "model": current.model, "usage": provenance["usage"]}

    @app.post("/api/projects/{ident}/jobs")
    async def start_job(ident: str, request: Request):
        body = await request.body()
        if len(body) > 2048:
            raise HTTPException(413, "Job request too large")
        from pydantic import Field
        from .contracts import Contract, parse_json
        from typing import Literal
        class JobRequest(Contract):
            revision: int = Field(ge=1)
            kind: Literal["generate", "execute", "plan"]
            profile_id: str | None = Field(default=None, max_length=80)
        data = parse_json(JobRequest, body)
        selected = profiles.resolve(data.profile_id) if data.kind in {"plan", "generate"} else runtime
        return app.state.jobs.submit(ident, data.revision, data.kind, selected)

    @app.get("/api/projects/{ident}/jobs")
    def recent_jobs(ident: str):
        return app.state.jobs.recent(ident)

    @app.get("/api/jobs/{ident}")
    def job(ident: str):
        return app.state.jobs.get(ident)

    @app.post("/api/jobs/{ident}/cancel")
    def cancel_job(ident: str):
        return app.state.jobs.cancel(ident)

    @app.get("/api/projects")
    def recent():
        return app.state.workbench.recent()

    @app.post("/api/projects")
    async def upload(request: Request):
        # EGP bytes, not multipart; enforce limit while streaming, before full allocation.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_ARCHIVE:
                raise HTTPException(413, "EGP exceeds 32 MiB")
        try:
            filename = request.query_params.get("filename", "project.egp")
            if len(filename) > 255 or "\x00" in filename:
                raise ValueError("Invalid project filename")
            return app.state.workbench.import_egp(filename, bytes(raw))
        except Exception as error:
            # Archive decoder errors must not echo XML or source fragments.
            if isinstance(error, ValueError):
                raise
            raise ValueError("Unreadable EGP archive") from None

    @app.get("/api/projects/{ident}")
    def project(ident: str):
        return project_view(app.state.workbench.get(ident))

    def stale_profile(state):
        pipeline = state.get("pipeline") or {}
        slot = pipeline.get("review_profile_slot")
        return bool(slot and profiles.reviewer_identity(slot) != pipeline.get("review_profile"))

    def project_view(state):
        if stale_profile(state):
            state = {**state, "stale_profile_review": True}
            if state.get("gate2"):
                state.update(stale_gate2=state["gate2"], gate2=None, state="NEEDS_HITL")
        from .contracts import digest
        retired = state.get("retired_test_evidence", {})
        records = state.get("quality_evidence", []) + state.get("unit_quality_failures", [])
        return {**state,"test_evidence_registry":[
            {"id":digest(record),"label":record.get("summary") or record.get("message"),
             "retired":retired.get(digest(record))}
            for record in records if not record.get("matched")]}

    @app.post("/api/projects/{ident}/example/{example}")
    async def example_plan(ident: str, example: str, request: Request):
        from .examples import load_example
        from .contracts import Contract, parse_json
        class ExampleRequest(Contract):
            revision: int
        data = parse_json(ExampleRequest, await request.body())
        p = app.state.workbench.get(ident)
        if p["revision"] != data.revision:
            raise Conflict("Project changed")
        return load_example(app.state.workbench, p, example)

    @app.get("/api/projects/{ident}/source/{source_id}")
    def source(ident: str, source_id: str):
        wb = app.state.workbench
        return {"text": wb.source_text(wb.get(ident), source_id)}

    @app.post("/api/projects/{ident}/commands")
    async def command(ident: str, request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 3_000_000:
                raise HTTPException(413, "Command too large")
        from pydantic import Field
        from .contracts import Contract, parse_json

        class Command(Contract):
            revision: int = Field(ge=1)
            action: str
            payload: dict

        data = parse_json(Command, bytes(raw))
        if data.action == "approve_release" and stale_profile(app.state.workbench.get(ident)):
            raise Conflict("Параметры профиля ревью изменились. Перепроверьте сохранённый SQL.")
        return project_view(app.state.workbench.command(ident, data.revision, data.action, data.payload))

    @app.get("/api/projects/{ident}/task")
    def task(ident: str):
        wb = app.state.workbench
        return wb.task_package(wb.get(ident))

    @app.get("/api/projects/{ident}/draft")
    def export(ident: str, target: str | None = None):
        wb = app.state.workbench
        state = wb.get(ident)
        if stale_profile(state):
            state = {**state, "gate2": None, "stale_profile_review": True}
        return Response(wb.export_draft(state, target), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="migration-v2-unverified.zip"'})

    return app
