"""Disposable synthetic BI only. No corporate connections or secrets."""
import os
os.makedirs('/tmp/superset_home',exist_ok=True)
SECRET_KEY='synthetic-testifier-dashboard-only-20261108'
SQLALCHEMY_DATABASE_URI='sqlite:////tmp/superset_home/metadata.db'
WTF_CSRF_ENABLED=True
TALISMAN_ENABLED=False
ENABLE_PROXY_FIX=False
FEATURE_FLAGS={'DASHBOARD_RBAC':True}
PREVENT_UNSAFE_DB_CONNECTIONS=False
SQLLAB_TIMEOUT=30
SUPERSET_WEBSERVER_TIMEOUT=60
LOG_LEVEL='WARNING'
