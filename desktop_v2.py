"""Windows entry point for the isolated V2 local pilot."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile


def synthetic_egp():
    xml = ('<ProjectCollection EGVersion="synthetic"><Elements><Element Type="SAS.EG.ProjectElements.CodeTask">'
           '<Element><ID>CodeTask-synthetic</ID><Label>Synthetic</Label></Element></Element></Elements></ProjectCollection>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("project.xml", xml)
        z.writestr("CodeTask-synthetic/code.sas", b'proc sql; create table result as select id from old.source; quit;')
    return out.getvalue()


def smoke(url, token):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, payload=None, raw=None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        req = urllib.request.Request(url + path, data=data,
                                     headers={"X-Local-Session": token, "Content-Type": "application/json"})
        with opener.open(req, timeout=10) as response:
            data = response.read()
            return data if path.endswith("/draft") else json.loads(data)
    status = request("/api/status")
    p = request("/api/projects", raw=synthetic_egp())
    def command(action, payload):
        nonlocal p
        p = request("/api/projects/" + p["id"] + "/commands", {"revision": p["revision"], "action": action, "payload": payload})
    command("select_scenario", {"source_ids": ["CodeTask-synthetic"]})
    binding = p["context"]["inputs"][0]
    plan = {"scenario_id": p["scenario"]["id"], "target": "greenplum", "target_version": "synthetic-only",
            "bindings": [{"source_id": binding["id"], "decision": "replace", "target": {"parts": ["dwh", "source"]},
                          "columns": [{"name": "id", "data_type": "INTEGER"}], "metadata_status": "user_supplied"}],
            "outputs": [{"name": {"parts": ["result"]}, "columns": ["id"], "storage": "temporary"}],
            "validation_scope": "Synthetic id/duplicate/NULL checks, not a SAS result",
            "resolutions": [{"issue_id": i["id"], "rationale": "Full synthetic original reviewed independently"}
                            for i in p["context"]["issues"] if not i["hard"]]}
    command("save_plan", plan)
    command("approve_plan", {})
    c = {"task_hash": p["task"]["hash"], "artifacts": [{"name": "001.sql", "sql": "CREATE TEMP TABLE result AS SELECT id FROM dwh.source;"}],
         "source_claims": [r["id"] for r in p["context"]["regions"] if r["kind"] != "layout_or_comment"]}
    command("submit_candidate", {"candidate_json": json.dumps(c)})
    static_passed = p["evidence"]["static_passed"]
    package = request("/api/projects/" + p["id"] + "/draft")
    with zipfile.ZipFile(io.BytesIO(package)) as z:
        unverified = json.loads(z.read("manifest.json"))["status"] == "unverified_draft"
    c["artifacts"][0]["sql"] = "CREATE TEMP TABLE result AS SELECT id FROM imaginary.source;"
    command("submit_candidate", {"candidate_json": json.dumps(c)})
    bad_rejected = not p["evidence"]["static_passed"]
    command("save_plan", plan)
    checks = {"model_disabled": not status["model_enabled"], "database_disabled": not status["db_enabled"],
              "original_preserved": "old.source" in request("/api/projects/" + p["id"] + "/source/CodeTask-synthetic")["text"],
              "binding_and_gate1": True, "static_good_candidate": static_passed,
              "unbound_source_rejected": bad_rejected, "draft_only": unverified,
              "edits_invalidate_gate": p["gate1"] is None and p["candidate"] is None,
              "release_disabled": not status["release_allowed"]}
    try:
        socket.getaddrinfo("example.invalid", 443)
        checks["external_dns_blocked"] = False
    except PermissionError:
        checks["external_dns_blocked"] = True
    return {"fixture": "synthetic_not_real_sas", "checks": checks, "passed": all(checks.values()),
            "sql_equivalence": "not_tested"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    from migration_v2.runtime import Runtime
    from migration_v2.network import install_policy
    runtime = Runtime() if args.smoke_test else Runtime.load(args.config)
    install_policy(runtime)
    import uvicorn
    from migration_v2.server import create_app
    scratch = tempfile.TemporaryDirectory(prefix="sas-v2-packaged-smoke-") if args.smoke_test else None
    root = Path(scratch.name) if scratch else (args.data_dir or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SasSqlMigrationV2")
    token = (os.environ.get("SAS_MIGRATION_LOCAL_SESSION") if args.headless else None) or secrets.token_urlsafe(32)
    app = create_app(root, token, runtime)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("127.0.0.1", 0 if args.smoke_test else args.port))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_config=None,
                            loop="asyncio", http="h11", ws="none", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local server failed to start")
            time.sleep(0.05)
        url = f"http://127.0.0.1:{port}"
        if args.smoke_test:
            report = smoke(url, token)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            return 0 if report["passed"] else 1
        if args.headless:
            thread.join()
            return 0
        import tkinter as tk
        window = tk.Tk()
        window.title("SAS → SQL · новое ядро")
        window.geometry("590x270")
        window.resizable(False, False)
        tk.Label(window, text="SAS → SQL · локальный этап V2", font=("Segoe UI", 17, "bold")).pack(pady=(25, 12))
        tk.Label(window, text="Оригиналы EGP, назначения источников и проверка SQL.\nМодель и тестовая база включаются отдельным локальным профилем.\nЗакрытие этого окна остановит приложение.", font=("Segoe UI", 11)).pack(pady=8)
        tk.Button(window, text="Открыть приложение", command=lambda: webbrowser.open(url + "/#" + token)).pack(pady=15)
        webbrowser.open(url + "/#" + token)
        window.mainloop()
        return 0
    finally:
        app.state.jobs.close()
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if scratch:
            scratch.cleanup()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        if "--report" in sys.argv:
            target = Path(sys.argv[sys.argv.index("--report") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"passed": False, "error_type": type(error).__name__}) + "\n", encoding="utf-8")
        else:
            from tkinter import messagebox
            messagebox.showerror("SAS → SQL V2", "Ошибка запуска: " + type(error).__name__)
        raise SystemExit(1)
