"""One truthful report projection for intervention and uncertain outcomes."""

from __future__ import annotations

from application_domain import ApplicationSnapshot


def render_safety_state(application: ApplicationSnapshot) -> tuple[str, ...]:
    if application.intervention is not None:
        intervention = application.intervention
        return (
            f"- Intervention: {intervention.kind.value}",
            f"- Guarded action: {intervention.action}",
            f"- Detected: {intervention.detected_at}",
            f"- Browser ready: {intervention.browser_ready}",
            f"- Explanation: {intervention.explanation}",
        )
    if application.uncertain_submission is not None:
        inspection = application.uncertain_submission.inspection
        checked = ", ".join(item.value for item in inspection.sources_checked)
        unavailable = ", ".join(
            item.value for item in inspection.sources_unavailable
        )
        return (
            f"- Inspection: {inspection.status.value}",
            f"- Inspected: {inspection.checked_at}",
            f"- Sources checked: {checked or 'none'}",
            f"- Sources unavailable: {unavailable or 'none'}",
            "- Automatic retry: forbidden",
        )
    return ("- No active intervention or uncertain outcome",)


__all__ = ["render_safety_state"]
