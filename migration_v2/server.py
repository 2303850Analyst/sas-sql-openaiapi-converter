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

    @app.get("/style.css")
    def css():
        return FileResponse(Path(__file__).with_name("web") / "style.css", media_type="text/css")

    @app.get("/api/status")
    def status():
        from .rule_identity import rules_fingerprint
        return {"version": "v2-pilot-0.2", **runtime.public(), "budget": app.state.budget.status(),
                "equivalence": "per_project", "release_allowed": False, "rules_fingerprint": rules_fingerprint()}

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
        data = parse_json(JobRequest, body)
        return app.state.jobs.submit(ident, data.revision, data.kind)

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

    def project_view(state):
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
        return project_view(app.state.workbench.command(ident, data.revision, data.action, data.payload))

    @app.get("/api/projects/{ident}/task")
    def task(ident: str):
        wb = app.state.workbench
        return wb.task_package(wb.get(ident))

    @app.get("/api/projects/{ident}/draft")
    def export(ident: str):
        wb = app.state.workbench
        return Response(wb.export_draft(wb.get(ident)), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="migration-v2-unverified.zip"'})

    return app
