from datetime import datetime, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import main  # noqa: E402
from watchlist_adapters import (  # noqa: E402
    CallableBrowserJobAlertDriver,
    DeliveryDefinitiveError,
)
from watchlist_domain import JobAlertCandidate  # noqa: E402


class FixedClock:
    def now(self):
        return datetime(2026, 7, 16, 12, tzinfo=timezone.utc)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    seed = root / "watchlist" / "targeted-companies.md"
    seed.parent.mkdir(parents=True)
    seed.write_text("- Seed AI — https://seed.example/\n", encoding="utf-8")
    return root


def test_shipped_watchlist_runtime_imports_seed_and_fails_closed_unconfigured(
    tmp_path,
):
    runtime = main.build_watchlist_runtime(
        repository_root=_repository(tmp_path),
        clock=FixedClock(),
    )

    assert runtime.imported_seed.company_names == ("Seed AI",)
    assert runtime.callback_registration.prefixes == ("wc:", "wa:")
    assert runtime.store.active_company_names() == ()

    proposal = runtime.service.propose_job_alert(JobAlertCandidate(
        source="LinkedIn",
        source_url="https://linkedin.com/jobs",
        expected_coverage="English AI roles",
        query="AI Research Scientist",
        location="Zurich",
    ))
    callback = runtime.handler.subscription_callback(
        proposal,
        intended_actor="synthetic-owner",
        intended_chat_id="42",
    )
    report = runtime.callback_registration.handle(
        callback,
        actor="synthetic-owner",
        chat_id="42",
    )

    assert report.status == "failed"
    assert report.error_type == "SubscriptionDefinitiveError"

    try:
        runtime.notifier.send_alert_proposal(
            proposal,
            intended_actor="synthetic-owner",
            intended_chat_id="42",
        )
    except DeliveryDefinitiveError:
        pass
    else:
        raise AssertionError("unconfigured Telegram delivery must fail closed")


def test_shipped_watchlist_runtime_uses_explicit_browser_adapter_boundary(tmp_path):
    events = []
    state = {"url": "", "alert_clicked": False}

    def navigate(url):
        state["url"] = url
        events.append(("open", url))

    def activate(locator):
        events.append(("click", locator))
        if "alert" in locator.casefold():
            state["alert_clicked"] = True

    driver = CallableBrowserJobAlertDriver(
        navigate=navigate,
        set_value=lambda locator, value: events.append(("fill", locator, value)),
        activate=activate,
        has_element=lambda locator: False,
        read_text=lambda locator: (
            "Alert created: fixture-42" if state["alert_clicked"] else None
        ),
        read_current_url=lambda: state["url"],
    )
    runtime = main.build_watchlist_runtime(
        repository_root=_repository(tmp_path),
        clock=FixedClock(),
        browser_driver=driver,
        telegram_sender=lambda message: None,
    )
    proposal = runtime.service.propose_job_alert(JobAlertCandidate(
        source="LinkedIn",
        source_url="https://linkedin.com/jobs",
        expected_coverage="English AI roles",
        query="AI Research Scientist",
        location="Zurich",
    ))
    callback = runtime.handler.subscription_callback(
        proposal,
        intended_actor="synthetic-owner",
        intended_chat_id="42",
    )

    report = runtime.callback_registration.handle(
        callback,
        actor="synthetic-owner",
        chat_id="42",
    )

    assert report.status == "subscribed"
    assert events[0] == ("open", "https://linkedin.com/jobs")
