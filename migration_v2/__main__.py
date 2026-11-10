"""python -m migration_v2: local pilot, no model/DB network calls."""
import argparse
import os
from pathlib import Path
import secrets
import threading
import webbrowser

import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    from .runtime import Runtime
    from .network import install_policy
    runtime = Runtime.load(args.config)
    install_policy(runtime)
    from .server import create_app
    root = args.data_dir or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SasSqlMigrationV2"
    token = os.environ.get("SAS_MIGRATION_LOCAL_SESSION") or secrets.token_urlsafe(32)
    app = create_app(root, token, runtime)
    if not args.no_browser:
        # The fragment is not sent in HTTP requests or server access logs.
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/#" + token)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
