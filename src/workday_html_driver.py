"""Executable Workday-like journey over deterministic local HTML."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ats_answer_service import QuestionMeaning
from application_interventions import BrowserInterventionRequired, InterventionKind
from workday_ats import (
    AtsControlKind,
    AtsDocumentSlot,
    AtsField,
    AtsFieldKind,
    BrowserPage,
    BrowserReviewSnapshot,
    DEDICATED_BROWSER_PROFILE,
    WorkdayInspection,
)


class OfflineWorkdayHtmlDriver:
    """Supported representative ATS driver with no submit capability.

    It exercises Workday-like selectors and page transitions without opening a
    real employer or browser session.  A future live driver can implement the
    same ``WorkdayBrowser`` contract for manual acceptance.
    """

    _ROOT_SELECTOR = "[data-automation-id='workday-application']"
    _FIELD_SELECTOR = "[data-automation-id='formField']"
    _UPLOAD_SELECTOR = "[data-automation-id='file-upload']"
    _ACCOUNT_SELECTOR = "[data-automation-id='account-creation']"
    _INTERVENTION_SELECTORS = (
        ("[data-automation-id='captcha']", InterventionKind.CAPTCHA),
        ("[data-automation-id='nonEmailMfa']", InterventionKind.NON_EMAIL_MFA),
        ("[data-automation-id='unusualConsent']", InterventionKind.UNUSUAL_CONSENT),
        ("[data-automation-id='siteRestriction']", InterventionKind.SITE_RESTRICTION),
    )

    def __init__(self, html_path: Path) -> None:
        self._html_path = Path(html_path)
        self._soup: BeautifulSoup | None = None
        self._fields: tuple[AtsField, ...] = ()
        self._slots: tuple[AtsDocumentSlot, ...] = ()
        self._page = BrowserPage.APPLICATION
        self._answers: dict[str, str] = {}
        self._attachments: dict[str, Path] = {}
        self._opened_application: tuple[str, str] | None = None
        self._account_email: str | None = None

    @property
    def opened_application(self) -> tuple[str, str] | None:
        return self._opened_application

    @property
    def account_email(self) -> str | None:
        return self._account_email

    def inspect_application(
        self, application_id: str, profile: str
    ) -> WorkdayInspection:
        if profile != DEDICATED_BROWSER_PROFILE:
            raise ValueError("Workday must use the dedicated application profile")
        if not self._html_path.is_file():
            raise ValueError("Local Workday fixture is unavailable")
        self._soup = BeautifulSoup(
            self._html_path.read_text(encoding="utf-8"), "html.parser"
        )
        intervention = self.detect_intervention("fill")
        if intervention is not None:
            raise BrowserInterventionRequired(
                kind=intervention,
                explanation=(
                    f"Resolve the {intervention.value} guard in the dedicated browser"
                ),
                browser_ready=True,
            )
        root = self._root()
        if str(root.get("data-provider", "")).casefold() != "workday":
            raise ValueError("Local page is not a supported Workday journey")
        self._fields = tuple(
            self._parse_field(item) for item in root.select(self._FIELD_SELECTOR)
        )
        self._slots = tuple(
            self._parse_slot(item) for item in root.select(self._UPLOAD_SELECTOR)
        )
        self._opened_application = (application_id, profile)
        return WorkdayInspection(
            fields=self._fields,
            document_slots=self._slots,
            account_required=root.select_one(self._ACCOUNT_SELECTOR) is not None,
            page=self._page,
        )

    def ensure_account(self, email: str, password: str) -> None:
        if self._root().select_one(self._ACCOUNT_SELECTOR) is None:
            return
        if not email.strip() or not password:
            raise ValueError("Dedicated Workday account credential is unavailable")
        self._account_email = email

    def fill_field(self, field_id: str, value: str) -> None:
        field = next((item for item in self._fields if item.field_id == field_id), None)
        if field is None or field.control_kind == AtsControlKind.UNSUPPORTED:
            raise ValueError("Workday field is unsupported")
        if not value:
            raise ValueError("Workday field value is unavailable")
        self._answers[field_id] = value

    def upload_document(self, field_id: str, path: Path) -> None:
        if not any(item.field_id == field_id for item in self._slots):
            raise ValueError("Workday document slot is unavailable")
        document = Path(path)
        if not document.is_file():
            raise ValueError("Workday document is unavailable")
        self._attachments[field_id] = document

    def advance_to_review(self) -> None:
        missing_fields = {
            item.field_id
            for item in self._fields
            if item.mandatory
            and item.control_kind != AtsControlKind.UNSUPPORTED
            and item.field_id not in self._answers
        }
        missing_documents = {
            item.field_id
            for item in self._slots
            if item.required and item.field_id not in self._attachments
        }
        if missing_fields or missing_documents:
            raise RuntimeError("Workday mandatory form data is incomplete")
        self._page = BrowserPage.REVIEW

    def capture_review(self) -> BrowserReviewSnapshot:
        return BrowserReviewSnapshot(
            page=self._page,
            answers=dict(self._answers),
            attachment_hashes={
                field_id: _file_hash(path)
                for field_id, path in self._attachments.items()
            },
            attachment_names={
                field_id: path.name for field_id, path in self._attachments.items()
            },
        )

    def current_url(self) -> str:
        return self._html_path.resolve().as_uri()

    def detect_intervention(self, guarded_action: str) -> InterventionKind | None:
        if guarded_action not in {"fill", "submit"}:
            raise ValueError("Unknown Workday guarded action")
        if self._soup is None:
            raise RuntimeError("Local Workday journey is not open")
        return next(
            (
                kind
                for selector, kind in self._INTERVENTION_SELECTORS
                if self._soup.select_one(selector) is not None
            ),
            None,
        )

    def intervention_is_resolved(self, application_id: str, kind: str) -> bool:
        del application_id
        detected = self.detect_intervention("fill")
        return detected is None or detected != InterventionKind(kind)

    def _root(self):
        if self._soup is None:
            raise RuntimeError("Local Workday journey is not open")
        root = self._soup.select_one(self._ROOT_SELECTOR)
        if root is None:
            raise ValueError("Local page is not a supported Workday journey")
        return root

    @staticmethod
    def _parse_field(item) -> AtsField:
        field_id = str(item.get("data-field-id", "")).strip()
        prompt = str(item.get("data-prompt", "")).strip()
        if not field_id or not prompt:
            raise ValueError("Workday field metadata is incomplete")
        meaning_value = str(item.get("data-meaning", QuestionMeaning.CUSTOM.value))
        try:
            meaning = QuestionMeaning(meaning_value)
        except ValueError:
            meaning = QuestionMeaning.CUSTOM
        return AtsField(
            field_id=field_id,
            prompt=prompt,
            mandatory=_html_boolean(item.get("data-required")),
            meaning=meaning,
            standardized_voluntary=_html_boolean(
                item.get("data-standardized-voluntary")
            ),
            control_kind=_parse_control_kind(item.get("data-control-kind")),
        )

    @staticmethod
    def _parse_slot(item) -> AtsDocumentSlot:
        field_id = str(item.get("data-field-id", "")).strip()
        if not field_id:
            raise ValueError("Workday document metadata is incomplete")
        return AtsDocumentSlot(
            field_id=field_id,
            kind=AtsFieldKind(str(item.get("data-document-kind", ""))),
            required=_html_boolean(item.get("data-required")),
        )


def _file_hash(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _html_boolean(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _parse_control_kind(value: Any) -> AtsControlKind:
    try:
        return AtsControlKind(str(value or AtsControlKind.TEXT.value))
    except ValueError:
        return AtsControlKind.UNSUPPORTED


__all__ = ["OfflineWorkdayHtmlDriver"]
