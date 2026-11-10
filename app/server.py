from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import LIMIT, export_package, import_project, plan_hash, project_program_preview, reimport_project, transition
from .compiler import VERSION
from .embedded_rules import embedded_rule_pack

ROOT = Path(__file__).resolve().parents[1]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    action: str
    payload: dict = Field(default_factory=dict)


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    block_id: str
    message: str = Field(min_length=1, max_length=4000)
    mode: Literal["describe", "rewrite"] = "describe"


class ProposedChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    name: str = Field(min_length=1, max_length=200)
    purpose: str = Field(max_length=2000)


class ProposedSqlChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    manual_sql: str = Field(min_length=1, max_length=100000)
    purpose: str = Field(max_length=2000)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS revisions (id TEXT, revision INTEGER, body TEXT NOT NULL, PRIMARY KEY(id, revision))")

    @contextmanager
    def connect(self):
        # sqlite3.Connection.__exit__ commits/rolls back but does not close.
        # Close deterministically so shutdown releases the file on Windows.
        with closing(sqlite3.connect(self.path, timeout=15)) as connection:
            with connection:
                yield connection

    def get(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT body FROM revisions WHERE id=? ORDER BY revision DESC LIMIT 1", (ident,)).fetchone()
        if not row:
            raise HTTPException(404, "Проект не найден")
        return json.loads(row[0])

    def get_revision(self, ident, revision):
        with self.connect() as db:
            row = db.execute("SELECT body FROM revisions WHERE id=? AND revision=?", (ident, revision)).fetchone()
        if not row:
            raise HTTPException(404, "Ревизия не найдена")
        return json.loads(row[0])

    def add(self, project):
        with self.connect() as db:
            db.execute("INSERT INTO revisions VALUES (?, ?, ?)", (project["id"], 1, json.dumps(project, ensure_ascii=False)))
        return project

    def recent(self):
        with self.connect() as db:
            rows = db.execute("SELECT r.body FROM revisions r JOIN (SELECT id, MAX(revision) v FROM revisions GROUP BY id) s ON r.id=s.id AND r.revision=s.v ORDER BY r.rowid DESC LIMIT 100").fetchall()
        projects = (json.loads(row[0]) for row in rows)
        return [{key: project[key] for key in ("id", "name", "revision", "target")}
                for project in projects if project.get("name", "").lower().endswith(".egp")][:20]

    def command(self, ident, revision, action, payload):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM revisions WHERE id=? ORDER BY revision DESC LIMIT 1", (ident,)).fetchone()
            if not row:
                raise HTTPException(404, "Проект не найден")
            old = json.loads(row[0])
            if old["revision"] != revision:
                raise HTTPException(409, "Проект изменился. Обновите страницу и повторите действие.")
            try:
                new = transition(old, action, payload)
            except (ValueError, TypeError, KeyError) as error:
                raise HTTPException(400, str(error)) from None
            new["revision"] = revision + 1
            new["events"].append({"action": action, "revision": new["revision"],
                                  "at": datetime.now(timezone.utc).isoformat(), "actor": "local_user"})
            db.execute("INSERT INTO revisions VALUES (?, ?, ?)", (ident, new["revision"], json.dumps(new, ensure_ascii=False)))
        return new

    def reimport(self, ident, revision, name, raw):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM revisions WHERE id=? ORDER BY revision DESC LIMIT 1", (ident,)).fetchone()
            if not row:
                raise HTTPException(404, "Проект не найден")
            old = json.loads(row[0])
            if old["revision"] != revision:
                raise HTTPException(409, "Проект изменился. Повторите импорт из актуальной ревизии.")
            new = reimport_project(old, name, raw)
            new["events"].append({"action": "reimport", "revision": new["revision"],
                                  "at": datetime.now(timezone.utc).isoformat(), "actor": "local_user"})
            db.execute("INSERT INTO revisions VALUES (?, ?, ?)",
                       (ident, new["revision"], json.dumps(new, ensure_ascii=False)))
        return new


def model_config():
    # User requested deterministic-only operation. Do not read endpoint or keys.
    return {"available": False, "model": "ИИ отключён по настройке проекта"}


def validate_proposal(raw, block_id, mode="describe"):
    schema = ProposedSqlChange if mode == "rewrite" else ProposedChange
    change = schema.model_validate(raw)
    if change.id != block_id:
        raise ValueError("Модель предложила изменение другого блока")
    return change.model_dump()


def create_app(db_path=None):
    app = FastAPI(title="SAS → SQL Workbench", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    data_dir = Path(os.environ.get("MIGRATION_DATA_DIR", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SasSqlMigration")))
    store = Store(Path(db_path) if db_path else data_dir / "workbench.sqlite3")
    app.state.store = store

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.headers.get("x-migration-client") != "workbench":
                return Response("Local client header required", status_code=403)
            origin = request.headers.get("origin")
            # The packaged launcher may use another local port. Derive it from
            # the actual server socket, never from an untrusted Origin header.
            server_port = (request.scope.get("server") or (None, 8765))[1]
            allowed_origins = {f"http://127.0.0.1:{server_port}", f"http://localhost:{server_port}"}
            if origin and origin not in allowed_origins:
                return Response("Origin not allowed", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.get("/api/status")
    def status():
        config = model_config()
        active_pack = embedded_rule_pack()
        from .core import INTAKE_REVISION
        from .project_compiler import PROJECT_COMPILER_REVISION
        return {"version": "1.0.2", "rule_version": VERSION, "project_compiler_revision": PROJECT_COMPILER_REVISION,
                "intake_revision": INTAKE_REVISION,
                "model": {k: config[k] for k in ("available", "model")},
                "database_execution": False, "mode": "draft_workbench", "embedded_rule_pack_id": active_pack["rule_pack_id"],
                "rule_pack_statistics": active_pack.get("statistics", {})}

    @app.get("/api/projects")
    def projects():
        return store.recent()

    @app.get("/api/evals")
    def evals():
        path = ROOT / "evidence" / "evals.json"
        if not path.exists():
            raise HTTPException(404, "Evals ещё не запускались")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/api/projects")
    async def upload(file: UploadFile = File(...)):
        if not (file.filename or "").lower().endswith(".egp"):
            raise HTTPException(400, "Пользовательский импорт принимает только проект SAS Enterprise Guide (.egp)")
        raw = await file.read(LIMIT + 1)
        try:
            project = import_project(file.filename or "input.egp", raw, embedded_rule_pack())
        except Exception:
            raise HTTPException(400, "Не удалось прочитать .egp до 32 МБ; проверьте целостность и структуру проекта.") from None
        return store.add(project)

    @app.post("/api/demo")
    def demo():
        source = b"/* SYNTHETIC EXAMPLE. Not a corporate SAS fixture. */\nproc sql;\ncreate table selected_sales as select id, amount from sales where amount > 100;\nquit;\n"
        project = import_project("synthetic_example.sas", source, embedded_rule_pack())
        project["synthetic"] = True
        return store.add(project)

    @app.get("/api/projects/{ident}")
    def project(ident: str):
        return store.get(ident)

    @app.get("/api/projects/{ident}/revisions/{revision}")
    def project_revision(ident: str, revision: int):
        return store.get_revision(ident, revision)

    @app.post("/api/projects/{ident}/reimport")
    async def reimport(ident: str, revision: int, file: UploadFile = File(...)):
        if not (file.filename or "").lower().endswith(".egp"):
            raise HTTPException(400, "Повторный импорт принимает только .egp")
        raw = await file.read(LIMIT + 1)
        try:
            return store.reimport(ident, revision, file.filename or "input.egp", raw)
        except HTTPException:
            raise
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "Не удалось повторно импортировать EGP; проверьте исходный архив.") from None

    @app.post("/api/projects/{ident}/commands")
    def command(ident: str, request: Command):
        if request.action not in {"edit", "gate1", "build", "gate2", "reanalyze"}:
            raise HTTPException(400, "Действие недоступно")
        return store.command(ident, request.revision, request.action, request.payload)

    @app.post("/api/projects/{ident}/assistant")
    async def assistant(ident: str, request: Ask):
        raise HTTPException(403, "ИИ и внешние API отключены. Используйте редактор логики и правила конвертации.")

    @app.get("/api/projects/{ident}/logic/{block_id}")
    def logic(ident: str, block_id: str):
        p = store.get(ident)
        b = next((b for b in p["blocks"] if b["id"] == block_id), None)
        if not b:
            raise HTTPException(404, "Блок не найден")
        try:
            parsed = project_program_preview(p, block_id)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        return {"revision":p["revision"],"rule_version":VERSION,**parsed}

    @app.post("/api/projects/{ident}/logic/{block_id}/preview")
    def preview(ident: str, block_id: str, request: Command):
        p = store.get(ident)
        if p["revision"] != request.revision:
            raise HTTPException(409, "Проект изменился. Откройте редактор заново.")
        if request.action != "preview":
            raise HTTPException(400, "Ожидался предпросмотр")
        b = next((b for b in p["blocks"] if b["id"] == block_id), None)
        if not b:
            raise HTTPException(404, "Блок не найден")
        edits = {**b.get("logic_edits",{}), **request.payload}
        try:
            parsed = project_program_preview(p, block_id, edits)
            return {"revision":p["revision"], **parsed}
        except (ValueError,TypeError,KeyError) as error:
            raise HTTPException(400,str(error)) from None

    @app.get("/api/projects/{ident}/export")
    def export(ident: str):
        try:
            data = export_package(store.get(ident))
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        return Response(data, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="migration-draft.zip"'})

    dist = ROOT / "frontend" / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def index():
        if not (dist / "index.html").exists():
            return Response("Build frontend first: npm run build", status_code=503)
        return FileResponse(dist / "index.html")

    return app


app = create_app()
