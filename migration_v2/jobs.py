"""Persistent single worker. A late result never replaces a newer revision."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import uuid

from pydantic import ValidationError

from . import generation, execution
from .contracts import digest
from .workflow import Conflict, now


class Jobs:
    def __init__(self, wb, runtime):
        self.wb, self.runtime = wb, runtime
        from .instance_lock import InstanceLock
        self.instance_lock = InstanceLock(wb.root)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sas-v2-job")
        self.lock = threading.RLock()
        self.connections = {}
        with closing(wb.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, task_hash TEXT NOT NULL, created TEXT NOT NULL, result TEXT, error TEXT)")
            db.execute("UPDATE jobs SET state='interrupted',error='Application stopped; no automatic retry' WHERE state IN ('queued','running','cancelling')")

    def get(self, ident):
        with closing(self.wb.connect()) as db:
            db.row_factory = __import__('sqlite3').Row
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise KeyError("Job not found")
        item = dict(row)
        if item["result"]:
            item["result"] = json.loads(item["result"])
        return item

    def recent(self, project_id):
        with closing(self.wb.connect()) as db:
            ids = db.execute("SELECT id FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 20", (project_id,)).fetchall()
        return [self.get(i[0]) for i in ids]

    def submit(self, ident, revision, kind, runtime=None):
        runtime = runtime or self.runtime
        if kind not in {"generate", "execute", "plan"}:
            raise ValueError("Unknown job kind")
        p = self.wb.get(ident)
        if p["revision"] != revision:
            raise Conflict("Project changed; reload before starting a job")
        if kind=='plan':
            from .plan_advisor import project_for_intake
            from .ai_pipeline import exposure
            draft=project_for_intake(p)
            exposure(draft,runtime.model,{'intake':p['intake']})
            p['task']={'hash':digest([p['snapshot']['hash'],p['intake'],p['scope_orders']])}
        elif not p["task"] or not p["gate1"]:
            raise ValueError("Approve current contract first")
        if kind == "generate":
            if not runtime.model:
                raise ValueError("Model profile is not configured")
            if p["plan"].get("acceptance")=="code_review":
                from .ai_pipeline import exposure
                exposure(p,runtime.model,self.wb.task_package(p))
                if runtime.reviewer:exposure(p,runtime.reviewer,self.wb.task_package(p))
            else:generation.model_payload(self.wb, p, runtime.model)
        elif kind=='execute' and (not p["candidate"] or not runtime.database):
            raise ValueError("A candidate and a test database are required")
        native_gp = kind == "execute" and p["plan"].get("acceptance") == "code_review"
        if native_gp and (runtime.database.target != "greenplum" or not self.wb.code_review_current(p)
                          or not (p.get("pipeline") or {}).get("profiles", {}).get("greenplum", {}).get("passed")):
            raise ValueError("Configure Greenplum and obtain current Greenplum code review before native validation")
        if kind == "execute" and not native_gp and runtime.database.target != "postgresql":
            raise ValueError("The legacy runner requires PostgreSQL")
        job_id = uuid.uuid4().hex
        with self.lock, closing(self.wb.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running','cancelling')", (ident,)).fetchone():
                raise Conflict("A job is already active for this project")
            db.execute("INSERT INTO jobs(id,project_id,revision,kind,state,task_hash,created) VALUES(?,?,?,?,?,?,?)",
                       (job_id, ident, revision, kind, "queued", p["task"]["hash"], now()))
        if kind == "execute":
            # A failed recheck must not leave an earlier successful run current.
            evidence = json.loads(json.dumps(p["evidence"]))
            evidence["checks"].update(execution="not_run", comparison="not_run", equivalence="unavailable")
            evidence["release_allowed"] = False
            for field in ('comparison', 'steps', 'run_id', 'input_snapshot_hash', 'input_fingerprints'):
                evidence.pop(field, None)
            evidence.pop('hash', None); evidence['hash'] = digest(evidence)
            try:
                p = (self.wb.record_greenplum(ident, revision, {"status":"not_run","passed":False})
                     if native_gp else self.wb.record_execution(ident, revision, evidence))
                with closing(self.wb.connect()) as db, db:
                    db.execute("UPDATE jobs SET revision=? WHERE id=?", (p["revision"], job_id))
            except Conflict:
                self.set(job_id, 'stale', error='Project changed before execution')
                return self.get(job_id)
        self.pool.submit(self.run, job_id, p, runtime)
        return self.get(job_id)

    def set(self, ident, state, result=None, error=None):
        with closing(self.wb.connect()) as db, db:
            db.execute("UPDATE jobs SET state=?,result=?,error=? WHERE id=?", (state, json.dumps(result) if result is not None else None, error, ident))

    def cancel(self, ident):
        with self.lock:
            job = self.get(ident)
            if job["state"] not in {"queued", "running", "cancelling"}:
                return job
            self.set(ident, "cancelling")
            connection = self.connections.get(ident, {}).get("connection")
            if connection is not None:
                try:
                    connection.cancel()
                except Exception:
                    pass
            return self.get(ident)

    def run(self, ident, project, runtime):
        cancelled = lambda: self.get(ident)["state"] == "cancelling"
        result = None
        try:
            with self.lock:
                if cancelled():
                    self.set(ident, "cancelled")
                    return
                self.set(ident, "running")
            job = self.get(ident)
            if job['kind']=='plan':
                from .plan_advisor import propose
                result={'proposal':propose(self.wb,project,runtime,cancelled)}
            elif job["kind"] == "generate":
                if project["plan"].get("acceptance")=="code_review":
                    from .ai_pipeline import run
                    result={"pipeline":run(self.wb,project,runtime,cancelled)}
                else:result = generation.propose(self.wb, project, runtime.model, cancelled)
            else:
                holder = self.connections.setdefault(ident, {})
                if project["plan"].get("acceptance") == "code_review":
                    from .greenplum_execution import execute
                    native = execute(self.wb,project,runtime.database,cancelled,holder)
                    result = {"greenplum_validation": {**native,"status":"passed"}}
                else:
                    result = {"evidence": execution.execute(self.wb, project, runtime.database, cancelled, holder)}
            with self.lock:
                if cancelled():
                    if result and result.get('pipeline'):
                        try:
                            saved=self.wb.record_pipeline(project['id'],project['revision'],result['pipeline'])
                            result['applied_revision']=saved['revision']
                        except Conflict:pass
                    self.set(ident, "cancelled", result)
                else:
                    try:
                        if job['kind']=='plan':
                            p=self.wb.record_plan_proposal(project['id'],project['revision'],result['proposal'])
                        elif job["kind"] == "generate":
                            if "pipeline" in result:
                                p=self.wb.record_pipeline(project["id"],project["revision"],result["pipeline"])
                            else:p = self.wb.command(project["id"], project["revision"], "submit_candidate",
                                                {"candidate_json": json.dumps(result["candidate"])})
                        else:
                            p = (self.wb.record_greenplum(project["id"], project["revision"], result["greenplum_validation"])
                                 if "greenplum_validation" in result else
                                 self.wb.record_execution(project["id"], project["revision"], result["evidence"]))
                        result["applied_revision"] = p["revision"]
                        self.set(ident, "succeeded", result)
                    except Conflict:
                        self.set(ident, "stale", result, "Project changed; result retained but not applied")
        except Exception as error:
            with self.lock:
                state = "cancelled" if cancelled() else "failed"
                safe = "Invalid structured model response" if isinstance(error, ValidationError) else (
                    str(error) if isinstance(error, (ValueError, generation.PipelineStop)) else "Job failed: " + type(error).__name__)
                self.set(ident, state, result, safe)
                if self.get(ident)['kind'] == 'execute':
                    failure = json.loads(json.dumps(project['evidence']))
                    failure['checks'].update(execution='failed' if state == 'failed' else 'not_run', comparison='not_run', equivalence='unavailable')
                    failure.update(release_allowed=False, execution_error=safe)
                    failure.pop('hash', None); failure['hash'] = digest(failure)
                    try:
                        if project["plan"].get("acceptance") == "code_review":
                            self.wb.record_greenplum(project["id"],project["revision"],
                                {"status":"failed" if state=="failed" else "not_run","passed":False,"error":safe})
                        else:
                            self.wb.record_execution(project['id'], project['revision'], failure)
                    except Conflict:
                        pass
        finally:
            self.connections.pop(ident, None)

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.instance_lock.close()
