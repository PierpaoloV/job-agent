"""Single durable Telegram update owner for the local macOS worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Mapping, Protocol, Sequence

from local_worker import (
    CapabilityExecution,
    CapabilityExecutionUnavailable,
    WorkerCommand,
    WorkerPaused,
    WorkerStopped,
)
from local_worker_store import (
    CapabilityClaimStatus,
    ReconciliationDecision,
    ReconciliationOutcome,
    StaleResumeGeneration,
)


_CAPABILITY = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_RECONCILE_CALLBACK_PREFIX = "worker:reconcile:"
_CALLBACK_PREFIX = "worker:cb:"
_CALLBACK_VERSION = "v1"
_MAX_CALLBACK_TTL = timedelta(minutes=30)


class CallbackAuthorizationStatus(str, Enum):
    AUTHORIZED = "authorized"
    REPLAYED = "replayed"
    STALE = "stale"
    MISMATCHED = "mismatched"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CallbackAuthorization:
    token: str
    actor_id: str
    chat_id: str
    route: str
    capability: str
    payload: str
    version: str
    resume_generation: int
    expires_at: str

    @property
    def callback_data(self) -> str:
        return f"{_CALLBACK_PREFIX}{self.version}:{self.token}"


@dataclass(frozen=True)
class CallbackAuthorizationResult:
    status: CallbackAuthorizationStatus
    authorization: CallbackAuthorization | None = None
    recoverable_stale_replay: bool = False


@dataclass(frozen=True)
class CallbackContext:
    actor_id: str
    chat_id: str
    update_id: int
    payload: str
    authorization: CallbackAuthorization


class WorkerControl(Protocol):
    def control(self, command: WorkerCommand) -> Mapping[str, object]: ...

    def status(self) -> Mapping[str, object]: ...

    def execute_gated_action(
        self, capability: str, action: Callable[[CapabilityExecution], Any]
    ) -> Any: ...

    def reconcile_capability(
        self, capability: str, *, actor: str, provenance: str
    ) -> ReconciliationDecision: ...


class TelegramWorkerApi(Protocol):
    def poll_updates(self, *, offset: int | None, timeout: int): ...

    def send_status(self, text: str) -> None: ...

    def acknowledge_callback(self, callback_query_id: str, text: str) -> None: ...


class TelegramWorkerHttpApi:
    """Telegram transport for owner commands and callbacks on the local Mac."""

    def __init__(self, *, token: str, chat_id: str, http: Any | None = None) -> None:
        if not token.strip() or not str(chat_id).strip():
            raise ValueError("Telegram token and chat ID are required")
        if http is None:
            import requests

            http = requests
        self._base = f"https://api.telegram.org/bot{token}"
        self._chat_id = str(chat_id)
        self._http = http

    def poll_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            response = self._http.get(
                f"{self._base}/getUpdates",
                params=params,
                timeout=timeout + 5,
            )
            payload = response.json()
        except Exception:
            raise RuntimeError("Telegram worker polling failed safely") from None
        if (
            not getattr(response, "ok", False)
            or not isinstance(payload, Mapping)
            or payload.get("ok") is not True
            or not isinstance(payload.get("result"), list)
        ):
            raise RuntimeError("Telegram worker polling failed safely")
        return [dict(item) for item in payload["result"] if isinstance(item, Mapping)]

    def send_status(self, text: str) -> None:
        self._post("sendMessage", {"chat_id": self._chat_id, "text": text})

    def send_message(self, message) -> None:
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": str(message.text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if message.reply_markup is not None:
            payload["reply_markup"] = message.reply_markup
        self._post("sendMessage", payload)

    def acknowledge_callback(self, callback_query_id: str, text: str) -> None:
        self._post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    def _post(self, method: str, payload: Mapping[str, object]) -> None:
        try:
            response = self._http.post(
                f"{self._base}/{method}", json=dict(payload), timeout=15
            )
        except Exception:
            raise RuntimeError("Telegram worker delivery failed safely") from None
        if not getattr(response, "ok", False):
            raise RuntimeError("Telegram worker delivery failed safely")


@dataclass(frozen=True)
class CallbackRoute:
    route: str
    prefixes: tuple[str, ...]
    handler: Callable[[CapabilityExecution, CallbackContext], str]
    stale_handler: Callable[[CapabilityExecution, CallbackContext], str] | None = None
    capability: str = "telegram_callback"
    # Opt in only when the stale handler has its own pre-send delivery ledger.
    recover_stale_replay: bool = False

    def __post_init__(self) -> None:
        if not _CAPABILITY.fullmatch(self.route):
            raise ValueError("Callback route name is invalid")
        if not self.prefixes or any(not prefix for prefix in self.prefixes):
            raise ValueError("Callback routes require non-empty prefixes")
        if not _CAPABILITY.fullmatch(self.capability):
            raise ValueError("Callback route capability is invalid")
        if (
            type(self.recover_stale_replay) is not bool
            or self.recover_stale_replay
            and self.stale_handler is None
        ):
            raise ValueError("Stale replay recovery route is invalid")

    def handles(self, value: str) -> bool:
        return value.startswith(self.prefixes)


class TelegramUpdateStore:
    """Persist Bot API offsets and fail-closed callback claims."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        self._now = now or (lambda: datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS updates ("
                "update_id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata ("
                "key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS callback_authorizations ("
                "token TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "actor_id TEXT NOT NULL, chat_id TEXT NOT NULL, "
                "route TEXT NOT NULL, capability TEXT NOT NULL, "
                "payload TEXT NOT NULL, version TEXT NOT NULL, "
                "resume_generation INTEGER NOT NULL, expires_at TEXT NOT NULL)"
            )
        self._path.chmod(0o600)

    def issue_callback_authorization(
        self,
        *,
        actor_id: str,
        chat_id: str,
        route: str,
        capability: str,
        payload: str,
        resume_generation: int,
        expires_at: datetime,
    ) -> CallbackAuthorization:
        if not _CAPABILITY.fullmatch(route) or not _CAPABILITY.fullmatch(capability):
            raise ValueError("Callback authorization scope is invalid")
        if not actor_id.strip() or not chat_id.strip() or not payload:
            raise ValueError("Callback authorization identity and payload are required")
        if resume_generation < 0:
            raise ValueError("Callback resume generation is invalid")
        if expires_at.tzinfo is None:
            raise ValueError("Callback expiry must include a timezone")
        now = self._now()
        if expires_at <= now or expires_at > now + _MAX_CALLBACK_TTL:
            raise ValueError("Callback expiry must be within the short-lived TTL")
        authorization = CallbackAuthorization(
            token=secrets.token_urlsafe(24),
            actor_id=str(actor_id),
            chat_id=str(chat_id),
            route=route,
            capability=capability,
            payload=payload,
            version=_CALLBACK_VERSION,
            resume_generation=resume_generation,
            expires_at=expires_at.isoformat(),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO callback_authorizations("
                "token, status, actor_id, chat_id, route, capability, payload, "
                "version, resume_generation, expires_at) "
                "VALUES (?, 'issued', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    authorization.token,
                    authorization.actor_id,
                    authorization.chat_id,
                    authorization.route,
                    authorization.capability,
                    authorization.payload,
                    authorization.version,
                    authorization.resume_generation,
                    authorization.expires_at,
                ),
            )
        return authorization

    def consume_callback_authorization(
        self,
        *,
        token: str,
        actor_id: str,
        chat_id: str,
        resume_generation: int,
        version: str = _CALLBACK_VERSION,
    ) -> CallbackAuthorizationResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT token, actor_id, chat_id, route, capability, payload, "
                "version, resume_generation, expires_at, status "
                "FROM callback_authorizations WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return CallbackAuthorizationResult(CallbackAuthorizationStatus.UNKNOWN)
            authorization = CallbackAuthorization(*row[:9])
            status = str(row[9])
            expected = (
                authorization.actor_id,
                authorization.chat_id,
                authorization.version,
                authorization.resume_generation,
            )
            actual = (
                str(actor_id),
                str(chat_id),
                version,
                resume_generation,
            )
            if actual != expected:
                return CallbackAuthorizationResult(
                    CallbackAuthorizationStatus.MISMATCHED
                )
            if status != "issued":
                return CallbackAuthorizationResult(
                    CallbackAuthorizationStatus.REPLAYED,
                    authorization,
                    recoverable_stale_replay=status == "stale",
                )
            expires_at = datetime.fromisoformat(authorization.expires_at)
            if expires_at <= self._now():
                connection.execute(
                    "UPDATE callback_authorizations SET status = 'stale' "
                    "WHERE token = ? AND status = 'issued'",
                    (token,),
                )
                return CallbackAuthorizationResult(
                    CallbackAuthorizationStatus.STALE, authorization
                )
            changed = connection.execute(
                "UPDATE callback_authorizations SET status = 'consumed' "
                "WHERE token = ? AND status = 'issued'",
                (token,),
            ).rowcount
            if changed != 1:
                return CallbackAuthorizationResult(
                    CallbackAuthorizationStatus.REPLAYED, authorization
            )
            return CallbackAuthorizationResult(
                CallbackAuthorizationStatus.AUTHORIZED, authorization
            )

    def offset(self) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'offset'"
            ).fetchone()
        return None if row is None else int(row[0])

    def begin(self, update_id: int) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM updates WHERE update_id = ?", (update_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO updates(update_id, status) VALUES (?, 'pending')",
                    (update_id,),
                )
                return "new"
            status = str(row[0])
            if status == "pending":
                connection.execute(
                    "UPDATE updates SET status = 'uncertain' WHERE update_id = ?",
                    (update_id,),
                )
                return "uncertain"
            return status

    def finish(self, update_id: int, status: str) -> None:
        if status not in {"completed", "uncertain"}:
            raise ValueError("Unsupported Telegram update status")
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE updates SET status = ? "
                "WHERE update_id = ? AND status = 'pending'",
                (status, update_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Telegram update claim is no longer pending")

    def advance_if_terminal(self, update_ids: Sequence[int]) -> None:
        ids = tuple(sorted(set(update_ids)))
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT update_id, status FROM updates "
                f"WHERE update_id IN ({placeholders})",
                ids,
            ).fetchall()
            states = {int(update_id): str(status) for update_id, status in rows}
            if any(
                states.get(update_id) not in {"completed", "uncertain"}
                for update_id in ids
            ):
                return
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('offset', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)",
                (max(ids) + 1,),
            )

    def _connect(self):
        return sqlite3.connect(self._path, timeout=30, isolation_level="DEFERRED")


class LocalWorkerTelegramRouter:
    def __init__(
        self,
        *,
        api: TelegramWorkerApi,
        store: TelegramUpdateStore,
        worker: WorkerControl,
        actor_id: str,
        chat_id: str,
        routes: Sequence[CallbackRoute] = (),
    ) -> None:
        self._api = api
        self._store = store
        self._worker = worker
        self._actor_id = str(actor_id)
        self._chat_id = str(chat_id)
        self._routes = tuple(routes)
        route_names = [route.route for route in self._routes]
        if len(route_names) != len(set(route_names)):
            raise ValueError("Callback route names must be unique")

    def consume_once(self, *, timeout: int = 25) -> int:
        updates = tuple(
            item
            for item in self._api.poll_updates(
                offset=self._store.offset(), timeout=timeout
            )
            if isinstance(item, Mapping) and int(item.get("update_id", -1)) >= 0
        )
        pauses = tuple(item for item in updates if self._is_authorized_pause(item))
        ordered = pauses + tuple(item for item in updates if item not in pauses)
        pause_dominant = bool(pauses)
        handled = 0
        for update in ordered:
            self._consume_update(update, pause_dominant=pause_dominant)
            handled += 1
        self._store.advance_if_terminal(
            tuple(int(item["update_id"]) for item in updates)
        )
        return handled

    def _consume_update(
        self, update: Mapping[str, object], *, pause_dominant: bool
    ) -> None:
        update_id = int(update["update_id"])
        state = self._store.begin(update_id)
        callback = update.get("callback_query")
        callback_id = (
            str(callback.get("id", "")) if isinstance(callback, Mapping) else ""
        )
        if state == "completed":
            if callback_id:
                self._api.acknowledge_callback(callback_id, "Azione già elaborata")
            return
        if state == "uncertain":
            if callback_id:
                self._api.acknowledge_callback(
                    callback_id, "Esito incerto: verifica manuale"
                )
            return
        try:
            if isinstance(callback, Mapping):
                self._consume_callback(
                    callback,
                    update_id=update_id,
                    suppress_action=pause_dominant,
                )
            else:
                message = update.get("message")
                if isinstance(message, Mapping):
                    self._consume_message(message, suppress_resume=pause_dominant)
            self._store.finish(update_id, "completed")
        except Exception:
            self._store.finish(update_id, "uncertain")
            raise

    def _consume_message(
        self, message: Mapping[str, object], *, suppress_resume: bool
    ) -> None:
        if not self._authorized(message):
            return
        text = str(message.get("text", "")).strip().casefold()
        if text == "/pausa":
            status = self._worker.control(WorkerCommand.PAUSE)
        elif text == "/riprendi":
            status = (
                self._worker.status()
                if suppress_resume
                else self._worker.control(WorkerCommand.RESUME)
            )
        elif text == "/stato":
            status = self._worker.status()
        elif text.startswith("/riconcilia "):
            capability = text.removeprefix("/riconcilia ").strip()
            self._worker.reconcile_capability(
                capability,
                actor=self._actor_id,
                provenance="telegram:command",
            )
            status = self._worker.status()
        else:
            return
        self._api.send_status(_safe_status_text(status))

    def _consume_callback(
        self,
        callback: Mapping[str, object],
        *,
        update_id: int,
        suppress_action: bool,
    ) -> None:
        callback_id = str(callback.get("id", ""))
        if not self._authorized(callback):
            self._api.acknowledge_callback(callback_id, "Non autorizzato")
            return
        if suppress_action:
            self._api.acknowledge_callback(callback_id, "Worker in pausa")
            return
        data = str(callback.get("data", ""))
        parsed = _parse_callback_data(data)
        if parsed is None:
            self._api.acknowledge_callback(callback_id, "Azione non valida")
            return
        version, token = parsed
        worker_status = self._worker.status()
        if str(worker_status.get("state")) != WorkerCommand.RESUME.value:
            self._api.acknowledge_callback(callback_id, "Worker in pausa")
            return
        try:
            resume_generation = int(worker_status["resume_generation"])
        except (KeyError, TypeError, ValueError):
            self._api.acknowledge_callback(callback_id, "Azione non valida")
            return
        authorization_result = self._store.consume_callback_authorization(
            token=token,
            actor_id=self._actor_id,
            chat_id=self._chat_id,
            resume_generation=resume_generation,
            version=version,
        )
        if authorization_result.status == CallbackAuthorizationStatus.STALE:
            authorization = authorization_result.authorization
            route = (
                None
                if authorization is None
                else next(
                    (
                        item
                        for item in self._routes
                        if item.route == authorization.route
                    ),
                    None,
                )
            )
            if (
                authorization is None
                or route is None
                or route.capability != authorization.capability
                or not route.handles(authorization.payload)
                or route.stale_handler is None
            ):
                self._api.acknowledge_callback(
                    callback_id, "Autorizzazione scaduta o non valida"
                )
                return
            self._dispatch_callback_route(
                callback_id=callback_id,
                update_id=update_id,
                authorization=authorization,
                route=route,
                handler=route.stale_handler,
            )
            return
        if (
            authorization_result.status == CallbackAuthorizationStatus.REPLAYED
            and authorization_result.recoverable_stale_replay
        ):
            authorization = authorization_result.authorization
            route = (
                None
                if authorization is None
                else next(
                    (
                        item
                        for item in self._routes
                        if item.route == authorization.route
                    ),
                    None,
                )
            )
            if route is not None and route.recover_stale_replay:
                assert authorization is not None
                assert route.stale_handler is not None
                if (
                    route.capability != authorization.capability
                    or not route.handles(authorization.payload)
                ):
                    self._api.acknowledge_callback(
                        callback_id, "Autorizzazione scaduta o non valida"
                    )
                    return
                self._dispatch_callback_route(
                    callback_id=callback_id,
                    update_id=update_id,
                    authorization=authorization,
                    route=route,
                    handler=route.stale_handler,
                )
                return
        if authorization_result.status != CallbackAuthorizationStatus.AUTHORIZED:
            message = (
                "Azione già elaborata"
                if authorization_result.status == CallbackAuthorizationStatus.REPLAYED
                else "Autorizzazione scaduta o non valida"
            )
            self._api.acknowledge_callback(callback_id, message)
            return
        authorization = authorization_result.authorization
        assert authorization is not None
        route_name = authorization.route
        capability = authorization.capability
        payload = authorization.payload
        if route_name == "reconcile" and payload.startswith(_RECONCILE_CALLBACK_PREFIX):
            target = payload.removeprefix(_RECONCILE_CALLBACK_PREFIX)
            if target != capability:
                self._api.acknowledge_callback(
                    callback_id, "Autorizzazione scaduta o non valida"
                )
                return
            decision = self._worker.reconcile_capability(
                target,
                actor=self._actor_id,
                provenance="telegram:callback",
            )
            message = (
                "Riconciliazione verificata"
                if decision.outcome == ReconciliationOutcome.RETRY_VERIFIED
                else "Riconciliazione bloccata"
            )
            self._api.acknowledge_callback(callback_id, message)
            return
        route = next((item for item in self._routes if item.route == route_name), None)
        if (
            route is None
            or route.capability != capability
            or not route.handles(payload)
        ):
            self._api.acknowledge_callback(callback_id, "Azione non valida")
            return
        self._dispatch_callback_route(
            callback_id=callback_id,
            update_id=update_id,
            authorization=authorization,
            route=route,
            handler=route.handler,
        )

    def _dispatch_callback_route(
        self,
        *,
        callback_id: str,
        update_id: int,
        authorization: CallbackAuthorization,
        route: CallbackRoute,
        handler: Callable[[CapabilityExecution, CallbackContext], str],
    ) -> None:
        context = CallbackContext(
            actor_id=self._actor_id,
            chat_id=self._chat_id,
            update_id=update_id,
            payload=authorization.payload,
            authorization=authorization,
        )
        try:

            def execute_callback(execution: CapabilityExecution) -> str:
                # Route dispatch itself is the external-action boundary. Real
                # handlers may checkpoint again immediately before later effects.
                execution.checkpoint()
                return handler(execution, context)

            result = self._worker.execute_gated_action(
                route.capability,
                execute_callback,
            )
        except CapabilityExecutionUnavailable as error:
            if error.status == CapabilityClaimStatus.UNCERTAIN:
                message = "Esito incerto: verifica manuale"
            else:
                message = "Azione già in corso"
            self._api.acknowledge_callback(callback_id, message)
            return
        except StaleResumeGeneration:
            self._api.acknowledge_callback(
                callback_id, "Esito incerto: verifica manuale"
            )
            return
        except (WorkerPaused, WorkerStopped):
            self._api.acknowledge_callback(callback_id, "Worker in pausa")
            return
        self._api.acknowledge_callback(callback_id, str(result)[:120])

    def _is_authorized_pause(self, update: Mapping[str, object]) -> bool:
        message = update.get("message")
        return (
            isinstance(message, Mapping)
            and self._authorized(message)
            and str(message.get("text", "")).strip().casefold() == "/pausa"
        )

    def _authorized(self, value: Mapping[str, object]) -> bool:
        sender = value.get("from")
        message = value.get("message")
        chat = (
            message.get("chat") if isinstance(message, Mapping) else value.get("chat")
        )
        return (
            isinstance(sender, Mapping)
            and isinstance(chat, Mapping)
            and str(sender.get("id")) == self._actor_id
            and str(chat.get("id")) == self._chat_id
        )


def _safe_status_text(status: Mapping[str, object]) -> str:
    safe = {
        key: status[key]
        for key in ("state", "health", "resume_generation", "capabilities")
        if key in status
    }
    return json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_callback_data(value: str) -> tuple[str, str] | None:
    if not value.startswith(_CALLBACK_PREFIX):
        return None
    parts = value.removeprefix(_CALLBACK_PREFIX).split(":")
    if len(parts) != 2:
        return None
    version, token = parts
    if version != _CALLBACK_VERSION or not token:
        return None
    return version, token


__all__ = [
    "CallbackAuthorization",
    "CallbackAuthorizationResult",
    "CallbackAuthorizationStatus",
    "CallbackContext",
    "CallbackRoute",
    "LocalWorkerTelegramRouter",
    "TelegramWorkerHttpApi",
    "TelegramUpdateStore",
]
