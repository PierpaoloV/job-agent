from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telegram_delivery import TelegramDeliveryLedger, TelegramScheduledNotifier


def test_delivery_claim_is_atomic_across_concurrent_workers(tmp_path):
    path = tmp_path / "deliveries.sqlite"

    def claim_once(_):
        return TelegramDeliveryLedger(path).claim("digest:one:message:0")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(claim_once, range(16)))

    assert results.count(True) == 1


def test_production_telegram_transport_consumes_each_message_key_once(tmp_path):
    sent = []
    notifier = TelegramScheduledNotifier(
        ledger=TelegramDeliveryLedger(tmp_path / "deliveries.json"),
        message_sender=sent.append,
    )

    for _ in range(2):
        notifier.send_digest(
            [{"stable_id": "one"}],
            overflow_count=2,
            batch_id="digest-1-abc",
            idempotency_key="digest:digest-1-abc",
        )

    assert len(sent) == 3
    assert sent[-1].reply_markup["inline_keyboard"][0][0]["callback_data"] == (
        "discovery-overflow:digest-1-abc"
    )


def test_successful_alert_is_one_self_contained_message(tmp_path):
    ledger = TelegramDeliveryLedger(tmp_path / "deliveries.sqlite")
    notifier = TelegramScheduledNotifier(
        ledger=ledger,
        message_sender=lambda message: None,
    )

    notifier.send_alert(
        {
            "stable_id": "example:42",
            "title": "AI Scientist",
            "company": "Example AI",
            "location": "Zurich",
            "score": 0.91,
            "modality": "hybrid",
            "source": "Indeed",
            "portfolio_evaluation": {
                "vacancy_retrieved_at": "2026-07-26T08:00:00+00:00",
                "gaps": ["German not demonstrated"],
                "risks": ["Salary not published"],
                "compensation": {"base_cash": {"status": "unknown"}},
                "sponsorship": {"status": "not_stated"},
                "ownership": {"classification": "verified"},
                "rank_explanation": "Strong research and engineering fit.",
            },
        },
        reason="top_tier",
        idempotency_key="alert:example:42",
    )

    assert ledger.outbound_status("alert:example:42:message:0") == "sent"
    assert ledger.outbound_status("alert:example:42:message:1") is None


def test_interactive_notifier_attaches_decisions_to_each_role_card(tmp_path):
    sent = []
    notifier = TelegramScheduledNotifier(
        ledger=TelegramDeliveryLedger(tmp_path / "deliveries.sqlite"),
        message_sender=sent.append,
        role_button_factory=lambda job: (
            {"text": "👍", "callback_data": f"prepare:{job['stable_id']}"},
            {"text": "👎", "callback_data": f"discard:{job['stable_id']}"},
            {"text": "Dimmi di più", "callback_data": f"details:{job['stable_id']}"},
        ),
    )

    notifier.send_alert(
        {
            "stable_id": "example:42",
            "title": "AI Scientist",
            "company": "Example AI",
            "location": "Zurich",
            "score": 0.91,
            "modality": "hybrid",
            "source": "Indeed",
            "portfolio_evaluation": {
                "vacancy_retrieved_at": "2026-07-26T08:00:00+00:00",
                "gaps": ["German not demonstrated"],
                "risks": ["Salary not published"],
                "compensation": {"base_cash": {"status": "unknown"}},
                "sponsorship": {"status": "not_stated"},
                "ownership": {"classification": "verified"},
                "rank_explanation": "Strong research and engineering fit.",
            },
        },
        reason="top_tier",
        idempotency_key="interactive:example:42",
    )

    assert len(sent) == 1
    buttons = sent[0].reply_markup["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == [
        "👍",
        "👎",
        "Dimmi di più",
    ]
    card = sent[0].text
    for expected in (
        "Modality:</b> hybrid",
        "Source:</b> Indeed",
        "Freshness:</b> 2026-07-26",
        "Gaps:</b> German not demonstrated",
        "Compensation:</b> unknown",
        "Immigration:</b> not_stated",
        "Ownership:</b> verified",
        "Risks:</b> Salary not published",
        "Rank:</b> Strong research and engineering fit.",
    ):
        assert expected in card


def test_mid_digest_retry_resumes_unclaimed_messages_without_replaying_prior_ones(
    tmp_path,
):
    attempts: list[str] = []
    fail_once = {"value": True}

    def sender(message):
        attempts.append(message.text)
        if len(attempts) == 2 and fail_once["value"]:
            fail_once["value"] = False
            raise RuntimeError("response lost after possible send")

    notifier = TelegramScheduledNotifier(
        ledger=TelegramDeliveryLedger(tmp_path / "deliveries.json"),
        message_sender=sender,
    )

    try:
        notifier.send_digest(
            [{"stable_id": "one"}, {"stable_id": "two"}],
            overflow_count=0,
            batch_id="digest-1-abc",
            idempotency_key="digest:digest-1-abc",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("first ambiguous failure was swallowed")

    notifier.send_digest(
        [{"stable_id": "one"}, {"stable_id": "two"}],
        overflow_count=0,
        batch_id="digest-1-abc",
        idempotency_key="digest:digest-1-abc",
    )

    assert len(attempts) == 4
    assert sum("Job Digest" in text for text in attempts) == 1
    assert sum("1. N/A" in text for text in attempts) == 1
    assert sum("2. N/A" in text for text in attempts) == 1
