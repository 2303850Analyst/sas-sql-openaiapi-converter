"""Local connection settings. Only DPAPI ciphertext can be written to disk."""
from dataclasses import replace
import base64
import json
import os
from pathlib import Path
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field

from .contracts import Contract, digest
from .runtime import ModelProfile, Runtime
from .secrets_store import transform


class Connection(Contract):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2048)
    model: str = Field(max_length=200)
    reviewer: str = Field(default="", max_length=200)
    url_kind: Literal["base", "operation"] = "base"
    auth: Literal["bearer", "none"] = "bearer"
    allow_http: bool = False
    ca_file: str | None = None
    proxy: str | None = None
    tariff: Literal["corporate", "paid", "unknown"] = "unknown"
    approve_source: bool = False
    budget_usd: float = Field(default=4.0, gt=0, le=4.0)
    input_rate: float = Field(default=0.0, ge=0, lt=100)
    output_rate: float = Field(default=0.0, ge=0, lt=100)
    timeout: int = Field(default=90, ge=15, le=600)
    max_tokens: int = Field(default=8000, ge=1, le=16000)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    response_format: Literal["json_object", "none"] = "none"
    thinking: Literal["default", "disabled", "enabled"] = "default"
    reviewer_reasoning: Literal["default", "low", "medium", "high"] = "default"

    def endpoint(self):
        url = self.url.strip().rstrip("/")
        try:
            parsed = urlsplit(url)
            parsed.port
        except ValueError:
            raise ValueError("Некорректный адрес API") from None
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() or c == "\\" for c in url)
                or parsed.scheme == "http" and not self.allow_http):
            raise ValueError("Укажите HTTPS URL без реквизитов или явно подтвердите внутренний HTTP")
        suffix = "/chat/completions"
        if self.url_kind == "operation" and not parsed.path.endswith(suffix):
            raise ValueError("Полный адрес должен оканчиваться на /chat/completions")
        if parsed.path.count(suffix) > 1 or "/v1/v1/" in parsed.path + "/":
            raise ValueError("В пути API повторяется операция или версия")
        if self.proxy:
            proxy = urlsplit(self.proxy)
            if (proxy.scheme not in {"http", "https"} or not proxy.hostname or proxy.username
                    or proxy.password or proxy.query or proxy.fragment or proxy.path not in {"", "/"}):
                raise ValueError("Укажите HTTP(S) proxy без учётных данных")
        return url if parsed.path.endswith(suffix) else url + suffix

    def profile(self, key, hashes, *, probe=False):
        endpoint = self.endpoint()
        if not probe and (not self.model.strip() or self.tariff == "unknown"):
            raise ValueError("Укажите модель и подтвердите режим тарификации перед вызовом")
        if self.auth == "bearer" and not key:
            raise ValueError("Сначала сохраните API-ключ")
        if self.tariff == "paid" and not probe and not (self.input_rate and self.output_rate):
            raise ValueError("Для платного профиля задайте обе ставки")
        return ModelProfile(endpoint, self.model.strip(), "", "", self.input_rate, self.output_rate,
                            budget_usd=self.budget_usd, authorized_budget_usd=4, max_output_tokens=self.max_tokens,
                            exposure="corporate_approved" if self.tariff == "corporate" and self.approve_source else "synthetic_only",
                            synthetic_archives=hashes, ca_file=self.ca_file or None, proxy=self.proxy or None,
                            request_timeout_seconds=self.timeout, output_token_parameter=self.token_parameter,
                            response_format_mode=self.response_format,
                            extra={} if self.thinking == "default" else {"thinking": {"type": self.thinking}}, auth=self.auth, allow_http=self.allow_http,
                            tariff=self.tariff, local_key=key, interactive=True)

    def review_profile(self, generator):
        model = self.reviewer.strip() or generator.model
        extra = generator.extra if model == generator.model else {}
        if self.reviewer_reasoning != "default":
            extra = {**extra, "reasoning_effort": self.reviewer_reasoning}
        return replace(generator, model=model, extra=extra)


class ProfileUpdate(Contract):
    settings: Connection
    key: str = Field(default="", max_length=8192)
    key_action: Literal["keep", "session", "save", "remove"] = "keep"


class Profiles:
    def __init__(self, root, fallback):
        self.path = root / "model-profiles.json"
        self.fallback = fallback
        self.lock = RLock()
        self.session = {}
        self.hashes = tuple(sorted(
            digest(p.read_bytes()) for folder in ("expansion/corpus", "fixtures/v2_pilot")
            for p in (Path(__file__).resolve().parents[1] / folder).glob("*.egp")
        ))
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {"active": None, "profiles": {}}

    def key(self, ident, record):
        if ident in self.session:
            return self.session[ident]
        if record.get("protected"):
            try:
                return transform(base64.b64decode(record["protected"], validate=True), False).decode("utf-8")
            except (OSError, ValueError):
                raise ValueError("Ключ недоступен текущему пользователю Windows. Замените его или выберите ключ на сессию.") from None
        return None

    def view(self):
        with self.lock:
            return {"active": self.data["active"], "profiles": [
                {"id": ident, "settings": row["settings"], "has_key": ident in self.session or bool(row.get("protected")),
                 "key_storage": "session" if ident in self.session else "protected" if row.get("protected") else "none",
                 "endpoint": Connection.model_validate(row["settings"]).endpoint()}
                for ident, row in self.data["profiles"].items()]}

    def update(self, ident, request):
        with self.lock:
            existing = self.data["profiles"].get(ident)
            if ident and not existing:
                raise ValueError("Профиль не найден")
            ident = ident or uuid4().hex
            endpoint = request.settings.endpoint()
            previous = Connection.model_validate(existing["settings"]) if existing else None
            if request.key_action in {"save", "session"} and request.settings.auth != "bearer":
                raise ValueError("Сохранение ключа доступно только для Bearer")
            if len(request.key.strip()) >= 8 and request.key.strip() in request.settings.model_dump_json():
                raise ValueError("Вводите ключ только в отдельное поле")
            protected = (existing or {}).get("protected")
            session = dict(self.session)
            if (previous and (previous.endpoint() != endpoint or previous.proxy != request.settings.proxy
                              or previous.ca_file != request.settings.ca_file or previous.auth != request.settings.auth)
                    and request.key_action == "keep" and (protected or ident in session)):
                raise ValueError("Адрес или параметры транспорта изменились. Замените или удалите прежний ключ.")
            if request.key_action != "keep":
                protected = None
                session.pop(ident, None)
            if request.key_action in {"session", "save"}:
                value = request.key.strip()
                if not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
                    raise ValueError("Введите непустой ключ без пробелов и управляющих символов")
                if request.key_action == "session":
                    session[ident] = value
                else:
                    try:
                        protected = base64.b64encode(transform(value.encode(), True)).decode("ascii")
                    except (OSError, ValueError):
                        raise ValueError("DPAPI недоступен. Выберите хранение ключа только на эту сессию.") from None
            row = {"settings": request.settings.model_dump(mode="json"), "protected": protected}
            updated = {"active": ident, "profiles": {**self.data["profiles"], ident: row}}
            self.persist(updated)
            self.session = session
            return self.view()

    def persist(self, updated):
        temp = self.path.with_suffix(".pending")
        try:
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(updated, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)
        self.data = updated

    def activate(self, ident):
        with self.lock:
            if ident not in self.data["profiles"]:
                raise ValueError("Профиль не найден")
            self.persist({**self.data, "active": ident})
            return self.view()

    def resolve(self, ident=None, *, probe=False):
        with self.lock:
            ident = ident or self.data["active"]
            if ident is None:
                return self.fallback
            row = self.data["profiles"].get(ident)
            if row is None:
                raise ValueError("Профиль не найден")
            settings = Connection.model_validate(row["settings"])
            profile = settings.profile(self.key(ident, row) if settings.auth == "bearer" else None, self.hashes, probe=probe)
            reviewer = settings.review_profile(profile)
            return Runtime(replace(profile, profile_slot=ident), self.fallback.database, replace(reviewer, profile_slot=ident))

    def reviewer_identity(self, ident):
        with self.lock:
            record = self.data["profiles"].get(ident)
            if not record:
                return None
            settings = Connection.model_validate(record["settings"])
            profile = settings.profile("identity-only", self.hashes, probe=True)
            return settings.review_profile(profile).identity()


