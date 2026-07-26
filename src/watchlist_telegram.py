"""Transport-neutral callback envelopes for watchlist human approvals."""

from __future__ import annotations

from datetime import timedelta

from watchlist_domain import CompanyProposal, JobAlertProposal
from watchlist_service import DEFAULT_CALLBACK_TTL, WatchlistService


class WatchlistTelegramHandler:
    _COMPANY = "wc"
    _SUBSCRIPTION = "wa"

    def __init__(self, service: WatchlistService):
        self._service = service

    def company_callback(
        self,
        proposal: CompanyProposal,
        *,
        intended_actor: str,
        intended_chat_id: str,
        ttl: timedelta = DEFAULT_CALLBACK_TTL,
    ) -> str:
        token = self._service.issue_company_authorization(
            proposal,
            intended_actor=intended_actor,
            intended_chat_id=intended_chat_id,
            ttl=ttl,
        )
        return f"{self._COMPANY}:{token}"

    def subscription_callback(
        self,
        proposal: JobAlertProposal,
        *,
        intended_actor: str,
        intended_chat_id: str,
        ttl: timedelta = DEFAULT_CALLBACK_TTL,
    ) -> str:
        token = self._service.issue_subscription_authorization(
            proposal,
            intended_actor=intended_actor,
            intended_chat_id=intended_chat_id,
            ttl=ttl,
        )
        return f"{self._SUBSCRIPTION}:{token}"

    def handle_callback(self, callback: str, *, actor: str, chat_id: str):
        parts = callback.split(":")
        if len(parts) != 2:
            return self._mismatch()
        kind, token = parts
        if kind == self._COMPANY:
            return self._service.approve_company_authorization(
                token, actor=actor, chat_id=chat_id
            )
        if kind == self._SUBSCRIPTION:
            return self._service.confirm_subscription_authorization(
                token, actor=actor, chat_id=chat_id
            )
        return self._mismatch()

    @staticmethod
    def _mismatch():
        from watchlist_domain import DecisionResult

        return DecisionResult("mismatched")
