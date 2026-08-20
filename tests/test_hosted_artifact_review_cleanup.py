from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import hosted_artifact_review_cleanup as cleanup  # noqa: E402
from notify_telegram import TelegramReceipt  # noqa: E402


def test_cleanup_obligation_survives_restart_until_messages_are_deleted(
    tmp_path, monkeypatch
):
    root = tmp_path / "telegram-review-cleanup-obligations"
    store = cleanup.TelegramReviewCleanupStore(root)
    receipts = (
        TelegramReceipt(message_id=701, chat_id="42"),
        TelegramReceipt(message_id=702, chat_id="42"),
        TelegramReceipt(message_id=703, chat_id="42"),
    )
    store.save(
        review_id="review-token-1",
        expires_at="2026-08-21T10:00:00Z",
        receipts=receipts,
    )
    deleted = []
    monkeypatch.setattr(
        cleanup,
        "delete_telegram_messages",
        lambda values: deleted.extend(values),
    )

    assert cleanup.reconcile_cleanup_obligations(
        cleanup.TelegramReviewCleanupStore(root)
    ) == 1

    assert [item.message_id for item in deleted] == [701, 702, 703]
    assert cleanup.TelegramReviewCleanupStore(root).all() == ()
