"""Idempotent hosted completion notification for prepared application files."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Callable, Sequence

from application_domain import PreparedArtifacts
from hosted_artifact_review import GatewayArtifactReviewPublisher
from hosted_artifact_review_cleanup import TelegramReviewCleanupStore
from notify_telegram import TelegramSendRejected
from telegram_delivery import TelegramDeliveryLedger


_CANONICAL_APPLICATION_ID = re.compile(r"approved-[0-9a-f]{16}")
_CANONICAL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_GITHUB_RUN_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[1-9][0-9]*"
)
HOSTED_APPLICATION_STATE_VERSION = "job-agent.hosted-application-state.v1"
HOSTED_ARTIFACT_REVIEW_DECISION_VERSION = (
    "job-agent.hosted-artifact-review-decision.v1"
)
_CANONICAL_REVIEW_ID = re.compile(r"[A-Za-z0-9_-]{8,48}")


@dataclass(frozen=True)
class HostedApplicationState:
    """Candidate-safe proof of one hosted application lifecycle transition."""

    version: str
    application_id: str
    official_vacancy_version: str
    lifecycle_state: str
    package_hash: str
    run_url: str
    history: tuple[dict[str, str], ...]

    @classmethod
    def from_dict(cls, value: dict) -> "HostedApplicationState":
        if set(value) != {
            "version",
            "application_id",
            "official_vacancy_version",
            "lifecycle_state",
            "package_hash",
            "run_url",
            "history",
        }:
            raise ValueError("Hosted application state schema is not canonical")
        history = value.get("history")
        if not isinstance(history, list) or any(
            not isinstance(event, dict) or set(event) != {"state"}
            for event in history
        ):
            raise ValueError("Hosted application history is not canonical")
        state = cls(
            version=str(value["version"]),
            application_id=str(value["application_id"]),
            official_vacancy_version=str(value["official_vacancy_version"]),
            lifecycle_state=str(value["lifecycle_state"]),
            package_hash=str(value["package_hash"]),
            run_url=str(value["run_url"]),
            history=tuple({"state": str(event["state"])} for event in history),
        )
        state.validate()
        return state

    def validate(self) -> None:
        if self.version != HOSTED_APPLICATION_STATE_VERSION:
            raise ValueError("Unsupported hosted application state version")
        if not _CANONICAL_APPLICATION_ID.fullmatch(self.application_id):
            raise ValueError("Hosted application id must be canonical")
        if not _CANONICAL_SHA256.fullmatch(self.official_vacancy_version):
            raise ValueError("Hosted vacancy version must be canonical")
        if not _CANONICAL_SHA256.fullmatch(self.package_hash):
            raise ValueError("Hosted package hash must be canonical")
        if not _GITHUB_RUN_URL.fullmatch(self.run_url):
            raise ValueError("Hosted run URL must be canonical")
        if self.lifecycle_state != "CV pronto" or self.history != (
            {"state": "approvata"},
            {"state": "CV pronto"},
        ):
            raise ValueError("Hosted application lifecycle is not canonical")

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "application_id": self.application_id,
            "official_vacancy_version": self.official_vacancy_version,
            "lifecycle_state": self.lifecycle_state,
            "package_hash": self.package_hash,
            "run_url": self.run_url,
            "history": [dict(event) for event in self.history],
        }


class HostedApplicationStateStore:
    """Durably persist candidate-safe hosted lifecycle state."""

    def __init__(self, root: Path):
        self._root = Path(root)

    def load(self, application_id: str) -> HostedApplicationState:
        path = self._path(application_id)
        if not path.exists():
            raise KeyError(application_id)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Hosted application state must be an object")
        state = HostedApplicationState.from_dict(value)
        if state.application_id != application_id:
            raise ValueError("Hosted application state identity mismatch")
        return state

    def record_cv_ready(
        self,
        *,
        application_id: str,
        official_vacancy_version: str,
        package_hash: str,
        run_url: str,
    ) -> HostedApplicationState:
        state = HostedApplicationState(
            version=HOSTED_APPLICATION_STATE_VERSION,
            application_id=application_id,
            official_vacancy_version=official_vacancy_version,
            lifecycle_state="CV pronto",
            package_hash=package_hash,
            run_url=run_url,
            history=({"state": "approvata"}, {"state": "CV pronto"}),
        )
        state.validate()
        try:
            existing = self.load(application_id)
        except KeyError:
            pass
        else:
            if existing.official_vacancy_version != official_vacancy_version:
                raise RuntimeError(
                    "Hosted application already targets a different vacancy version"
                )
            if existing == state:
                return existing
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        path = self._path(application_id)
        temporary = path.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return state

    def _path(self, application_id: str) -> Path:
        if not _CANONICAL_APPLICATION_ID.fullmatch(str(application_id)):
            raise ValueError("Hosted application id must be canonical")
        return self._root / f"{application_id}.json"


class HostedPreparationWorkflowCoordinator:
    """Own the hosted application lifecycle transition after artifact publish."""

    def __init__(self, store: HostedApplicationStateStore):
        self._store = store

    def complete(
        self,
        *,
        application_id: str,
        official_vacancy_version: str,
        package_hash: str,
        run_url: str,
    ) -> HostedApplicationState:
        return self._store.record_cv_ready(
            application_id=application_id,
            official_vacancy_version=official_vacancy_version,
            package_hash=package_hash,
            run_url=run_url,
        )

    def require_completed(
        self,
        *,
        application_id: str,
        official_vacancy_version: str,
        package_hash: str,
        run_url: str,
    ) -> HostedApplicationState:
        state = self._store.load(application_id)
        if (
            state.official_vacancy_version != official_vacancy_version
            or state.package_hash != package_hash
            or state.run_url != run_url
        ):
            raise RuntimeError(
                "Hosted preparation state identity does not match dispatch"
            )
        return state


@dataclass(frozen=True)
class HostedArtifactReviewDecision:
    version: str
    review_id: str
    application_id: str
    official_vacancy_version: str
    package_hash: str
    action: str
    status: str
    actor_id: str
    chat_id: str

    def validate(self) -> None:
        if self.version != HOSTED_ARTIFACT_REVIEW_DECISION_VERSION:
            raise ValueError("Unsupported hosted artifact review decision version")
        if not _CANONICAL_REVIEW_ID.fullmatch(self.review_id):
            raise ValueError("Hosted artifact review id must be canonical")
        if not _CANONICAL_APPLICATION_ID.fullmatch(self.application_id):
            raise ValueError("Hosted artifact review application id must be canonical")
        if not _CANONICAL_SHA256.fullmatch(
            self.official_vacancy_version
        ) or not _CANONICAL_SHA256.fullmatch(self.package_hash):
            raise ValueError("Hosted artifact review hashes must be canonical")
        expected_status = {
            "approve_artifacts": "approved",
            "regenerate_artifacts": "regenerate_requested",
        }.get(self.action)
        if self.status != expected_status:
            raise ValueError("Hosted artifact review action is not canonical")
        if not self.actor_id or not self.chat_id:
            raise ValueError("Hosted artifact review actor scope is incomplete")

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "review_id": self.review_id,
            "application_id": self.application_id,
            "official_vacancy_version": self.official_vacancy_version,
            "package_hash": self.package_hash,
            "action": self.action,
            "status": self.status,
            "actor_id": self.actor_id,
            "chat_id": self.chat_id,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "HostedArtifactReviewDecision":
        expected = {
            "version",
            "review_id",
            "application_id",
            "official_vacancy_version",
            "package_hash",
            "action",
            "status",
            "actor_id",
            "chat_id",
        }
        if set(value) != expected:
            raise ValueError("Hosted artifact review decision is not canonical")
        decision = cls(**{key: str(value[key]) for key in expected})
        decision.validate()
        return decision


class HostedArtifactReviewDecisionStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def save(
        self, decision: HostedArtifactReviewDecision
    ) -> HostedArtifactReviewDecision:
        decision.validate()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        path = self._path(decision.application_id, decision.package_hash)
        encoded = json.dumps(decision.to_dict(), indent=2, sort_keys=True) + "\n"
        if path.exists():
            if path.read_text(encoding="utf-8") == encoded:
                return decision
            raise RuntimeError("Hosted artifact review decision already differs")
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        return decision

    def _path(self, application_id: str, package_hash: str) -> Path:
        if not _CANONICAL_APPLICATION_ID.fullmatch(application_id):
            raise ValueError("Hosted artifact review application id must be canonical")
        if not _CANONICAL_SHA256.fullmatch(package_hash):
            raise ValueError("Hosted artifact review package hash must be canonical")
        return self._root / (
            f"{application_id}-{package_hash.removeprefix('sha256:')}.json"
        )


def record_hosted_artifact_review_decision(
    *,
    application_id: str,
    official_vacancy_version: str,
    package_hash: str,
    review_id: str,
    action: str,
    actor_id: str,
    chat_id: str,
    expected_actor_id: str,
    expected_chat_id: str,
    application_states: HostedApplicationStateStore,
    decisions: HostedArtifactReviewDecisionStore,
) -> HostedArtifactReviewDecision:
    if actor_id != expected_actor_id or chat_id != expected_chat_id:
        raise RuntimeError("Hosted artifact review actor scope does not match")
    HostedPreparationWorkflowCoordinator(application_states).require_completed(
        application_id=application_id,
        official_vacancy_version=official_vacancy_version,
        package_hash=package_hash,
        run_url=application_states.load(application_id).run_url,
    )
    status = {
        "approve_artifacts": "approved",
        "regenerate_artifacts": "regenerate_requested",
    }.get(action, "")
    decision = HostedArtifactReviewDecision(
        version=HOSTED_ARTIFACT_REVIEW_DECISION_VERSION,
        review_id=review_id,
        application_id=application_id,
        official_vacancy_version=official_vacancy_version,
        package_hash=package_hash,
        action=action,
        status=status,
        actor_id=actor_id,
        chat_id=chat_id,
    )
    return decisions.save(decision)


def arm_remote_preparation_completion(
    *,
    application_id: str,
    official_vacancy_version: str,
    package_hash: str,
    run_url: str,
    ledger: TelegramDeliveryLedger,
    application_states: HostedApplicationStateStore,
) -> str | None:
    """Persist CV-ready state and the at-most-once Telegram send boundary."""

    delivery_key = _completion_delivery_key(
        application_id,
        official_vacancy_version,
        package_hash,
        run_url,
    )
    ledger.stage_outbound(delivery_key)
    claim_token = ledger.claim_outbound(delivery_key)
    if claim_token is None:
        return None
    try:
        HostedPreparationWorkflowCoordinator(application_states).complete(
            application_id=application_id,
            official_vacancy_version=official_vacancy_version,
            package_hash=package_hash,
            run_url=run_url,
        )
    except Exception:
        ledger.release_outbound(delivery_key, claim_token)
        raise
    if not ledger.mark_outbound_sending(delivery_key, claim_token):
        raise RuntimeError(
            "Hosted preparation notification could not enter sending state"
        )
    return claim_token


def dispatch_remote_preparation_completion(
    *,
    application_id: str,
    official_vacancy_version: str,
    package_hash: str,
    run_url: str,
    claim_token: str,
    ledger: TelegramDeliveryLedger,
    application_states: HostedApplicationStateStore,
    artifacts: PreparedArtifacts,
    review_publisher: Callable[..., object],
) -> bool:
    """Publish a previously staged protected review at most once."""

    delivery_key = _completion_delivery_key(
        application_id,
        official_vacancy_version,
        package_hash,
        run_url,
    )
    if not ledger.outbound_claim_is_sending(delivery_key, claim_token):
        raise RuntimeError(
            "Hosted preparation notification claim is not active"
        )
    HostedPreparationWorkflowCoordinator(application_states).require_completed(
        application_id=application_id,
        official_vacancy_version=official_vacancy_version,
        package_hash=package_hash,
        run_url=run_url,
    )
    try:
        review_publisher(
            application_id=application_id,
            official_vacancy_version=official_vacancy_version,
            package_hash=package_hash,
            run_url=run_url,
            artifacts=artifacts,
        )
    except TelegramSendRejected:
        ledger.requeue_outbound_rejected(delivery_key, claim_token)
        raise
    except Exception:
        ledger.mark_outbound_uncertain(delivery_key, claim_token)
        raise
    if not ledger.mark_outbound_sent(delivery_key, claim_token):
        raise RuntimeError(
            "Hosted preparation notification acknowledgement was not persisted"
        )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage or dispatch a hosted preparation completion"
    )
    parser.add_argument("command", choices=("arm", "dispatch", "decide"))
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--official-vacancy-version", required=True)
    parser.add_argument("--run-url")
    parser.add_argument("--package-hash", required=True)
    parser.add_argument("--claim-token")
    parser.add_argument("--artifact-version")
    parser.add_argument("--cv-path")
    parser.add_argument("--cover-letter-path")
    parser.add_argument("--cv-hash")
    parser.add_argument("--cover-letter-hash")
    parser.add_argument("--review-id")
    parser.add_argument("--action")
    parser.add_argument("--actor-id")
    parser.add_argument("--chat-id")
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("data/telegram-deliveries.sqlite"),
    )
    args = parser.parse_args(argv)
    application_states = HostedApplicationStateStore(
        Path("data/hosted-application-state")
    )
    if args.command == "decide":
        required_values = (
            args.review_id,
            args.action,
            args.actor_id,
            args.chat_id,
        )
        if not all(required_values):
            raise ValueError("Hosted artifact decision arguments are incomplete")
        record_hosted_artifact_review_decision(
            application_id=args.application_id,
            official_vacancy_version=args.official_vacancy_version,
            package_hash=args.package_hash,
            review_id=args.review_id,
            action=args.action,
            actor_id=args.actor_id,
            chat_id=args.chat_id,
            expected_actor_id=os.environ["TELEGRAM_ACTOR_ID"],
            expected_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            application_states=application_states,
            decisions=HostedArtifactReviewDecisionStore(
                Path("data/hosted-artifact-review-decisions")
            ),
        )
        return 0
    if not args.run_url:
        raise ValueError("Hosted completion requires a run URL")
    ledger = TelegramDeliveryLedger(args.ledger)
    kwargs = {
        "application_id": args.application_id,
        "official_vacancy_version": args.official_vacancy_version,
        "package_hash": args.package_hash,
        "run_url": args.run_url,
        "ledger": ledger,
        "application_states": application_states,
    }
    if args.command == "arm":
        claim_token = arm_remote_preparation_completion(**kwargs)
        _github_output("should_send", "true" if claim_token else "false")
        if claim_token is not None:
            _github_output("claim_token", claim_token)
    else:
        if not args.claim_token:
            raise ValueError("Hosted completion dispatch requires a claim token")
        artifact_values = (
            args.artifact_version,
            args.cv_path,
            args.cover_letter_path,
            args.cv_hash,
            args.cover_letter_hash,
        )
        if not all(artifact_values):
            raise ValueError(
                "Hosted completion dispatch requires both prepared artifacts"
            )
        publisher = GatewayArtifactReviewPublisher(
            endpoint=os.environ["JOB_AGENT_CALLBACK_GATEWAY_URL"],
            internal_token=os.environ["JOB_AGENT_CALLBACK_GATEWAY_TOKEN"],
            actor_id=os.environ["TELEGRAM_ACTOR_ID"],
            chat_id=os.environ["TELEGRAM_CHAT_ID"],
            cleanup_store=TelegramReviewCleanupStore(
                Path("data/telegram-review-cleanup-obligations")
            ),
        )
        dispatch_remote_preparation_completion(
            **kwargs,
            claim_token=args.claim_token,
            artifacts=PreparedArtifacts(
                version=args.artifact_version,
                cv_path=args.cv_path,
                cover_letter_path=args.cover_letter_path,
                cv_hash=args.cv_hash,
                cover_letter_hash=args.cover_letter_hash,
            ),
            review_publisher=publisher.publish,
        )
    return 0


def _github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        raise RuntimeError("GITHUB_OUTPUT is required for hosted completion")
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def _completion_delivery_key(
    application_id: str,
    official_vacancy_version: str,
    package_hash: str,
    run_url: str,
) -> str:
    if not _CANONICAL_APPLICATION_ID.fullmatch(str(application_id)):
        raise ValueError("Hosted completion application id must be canonical")
    if not _CANONICAL_SHA256.fullmatch(str(official_vacancy_version)):
        raise ValueError("Hosted completion vacancy version must be canonical")
    if not _CANONICAL_SHA256.fullmatch(str(package_hash)):
        raise ValueError("Hosted completion package hash must be canonical")
    if not _GITHUB_RUN_URL.fullmatch(str(run_url)):
        raise ValueError("Hosted completion run URL must be canonical")
    return (
        "hosted-preparation-complete:"
        f"{application_id}:{official_vacancy_version}:{package_hash}:review:0"
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HOSTED_APPLICATION_STATE_VERSION",
    "HOSTED_ARTIFACT_REVIEW_DECISION_VERSION",
    "HostedArtifactReviewDecision",
    "HostedArtifactReviewDecisionStore",
    "HostedApplicationState",
    "HostedApplicationStateStore",
    "HostedPreparationWorkflowCoordinator",
    "arm_remote_preparation_completion",
    "dispatch_remote_preparation_completion",
    "record_hosted_artifact_review_decision",
]
