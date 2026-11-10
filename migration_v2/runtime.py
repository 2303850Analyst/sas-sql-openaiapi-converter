"""Explicit local runtime profiles. Secrets never belong to project revisions."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelProfile:
    endpoint: str
    model: str
    key_file: str
    key_name: str
    input_usd_per_million: float
    output_usd_per_million: float
    budget_usd: float = 0.09
    max_output_tokens: int = 8000
    max_request_bytes: int = 55000
    exposure: str = "synthetic_only"
    synthetic_archives: tuple[str, ...] = ()
    ca_file: str | None = None
    proxy: str | None = None
    extra: dict = field(default_factory=dict)
    output_token_parameter: str = 'max_tokens'
    response_format_mode: str = 'json_object'
    authorized_budget_usd: float = 1.00
    request_timeout_seconds: int = 150

    auth: str = "bearer"
    allow_http: bool = False
    tariff: str = "paid"
    local_key: str | None = field(default=None, repr=False, compare=False)
    interactive: bool = False
    profile_slot: str | None = None

    def __post_init__(self):
        p = urlsplit(self.endpoint)
        if (p.scheme != "https" and not (p.scheme == "http" and self.allow_http)) or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError("Model endpoint must be an explicit HTTPS URL without credentials")
        if self.exposure not in {"synthetic_only", "corporate_approved"}:
            raise ValueError("Invalid model exposure policy")
        if self.exposure == "synthetic_only" and not self.synthetic_archives and not self.interactive:
            raise ValueError("Public experiment requires exact synthetic archive hashes")
        if any(not re.fullmatch(r"[0-9a-f]{64}", x) for x in self.synthetic_archives):
            raise ValueError("Invalid synthetic archive hash")
        if not 0 < self.budget_usd <= self.authorized_budget_usd <= 5.00 or not 1 <= self.max_output_tokens <= 16000:
            raise ValueError("Budget exceeds the explicitly authorized experiment cap (default USD 1, maximum USD 5)")
        if self.auth not in {"bearer", "none"} or self.tariff not in {"paid", "corporate", "unknown"}:
            raise ValueError("Invalid authorization or tariff mode")
        if self.tariff == "paid" and not self.interactive and not (0 < self.input_usd_per_million < 100 and 0 < self.output_usd_per_million < 100):
            raise ValueError("Explicit positive model rates required")
        if set(self.extra) - {"thinking", "reasoning_effort", "temperature", "top_p"}:
            raise ValueError("Unapproved model parameter")
        if self.output_token_parameter not in {'max_tokens','max_completion_tokens'} or self.response_format_mode not in {'json_object','none'}:
            raise ValueError('Unsupported OpenAI-compatible request settings')
        if type(self.request_timeout_seconds) is not int or not 15<=self.request_timeout_seconds<=600:
            raise ValueError('Model request timeout must be between 15 and 600 seconds')

    def secret(self):
        if self.auth == "none":
            return None
        if self.interactive:
            if not self.local_key:
                raise ValueError("API key is missing")
            return self.local_key
        from .secrets_store import read_secret
        return read_secret(self.key_file, self.key_name)

    def identity(self):
        from .contracts import digest
        return digest([self.endpoint, self.model, self.auth, self.ca_file, self.proxy, self.extra,
                       self.output_token_parameter, self.response_format_mode, self.exposure])


@dataclass(frozen=True)
class DatabaseProfile:
    host: str
    port: int
    dbname: str
    user: str
    password_env: str = "SAS_MIGRATION_DB_PASSWORD"
    sslmode: str = "verify-full"
    sslrootcert: str | None = None
    target: str = "postgresql"
    version_prefix: str = "17."
    reference_schemas: tuple[str, ...] = ()
    statement_timeout_ms: int = 15000
    max_rows: int = 10000

    def __post_init__(self):
        if self.target != "postgresql":
            raise ValueError("Execution adapter currently requires a PostgreSQL test database")
        if self.sslmode not in {"verify-full", "disable"} or (self.sslmode == "disable" and self.host not in {"127.0.0.1", "::1", "localhost"}):
            raise ValueError("Remote database requires verify-full TLS")
        if not 1 <= self.port <= 65535 or not 100 <= self.statement_timeout_ms <= 60000 or not 1 <= self.max_rows <= 100000:
            raise ValueError("Invalid execution limits")


@dataclass(frozen=True)
class Runtime:
    model: ModelProfile | None = None
    database: DatabaseProfile | None = None
    reviewer: ModelProfile | None = None

    @classmethod
    def load(cls, path: Path | None):
        if path is None:
            return cls()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if set(data) - {"model", "database", "reviewer"}:
            raise ValueError("Unknown runtime setting")
        if any({"local_key", "interactive", "profile_slot"} & set(data.get(role) or {}) for role in ("model", "reviewer")):
            raise ValueError("Session credentials are available only through local connection settings")
        return cls(ModelProfile(**data["model"]) if data.get("model") else None,
                   DatabaseProfile(**data["database"]) if data.get("database") else None,
                   ModelProfile(**data["reviewer"]) if data.get("reviewer") else None)

    def public(self):
        return {"model_enabled": self.model is not None, "db_enabled": self.database is not None,
                "reviewer_model": self.reviewer.model if self.reviewer else (self.model.model if self.model else None),
                "model": {"id": self.model.model, "endpoint": self.model.endpoint,
                          "exposure": self.model.exposure, "budget_usd": self.model.budget_usd, "tariff": self.model.tariff}
                         if self.model else None,
                "database": {"target": self.database.target, "version": self.database.version_prefix,
                             "rollback_only": True} if self.database else None}
