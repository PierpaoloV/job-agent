"""Send ranked job digest to Telegram."""
import html
import os, requests


def _send(token: str, chat_id: str, text: str, parse_mode: str = "HTML"):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }, timeout=15)
    resp.raise_for_status()


def send_error(message: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    _send(token, chat_id, f"❌ <b>Job Agent Error</b>\n\n{message}")


def send_digest(jobs: list[dict]):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    if not jobs:
        _send(token, chat_id, "🔍 <b>Job Agent</b>\n\nNo new matching jobs today.")
        return

    header = f"🔍 <b>Job Digest</b>\n{len(jobs)} new matches ranked by fit."
    _send(token, chat_id, header)

    for i, job in enumerate(jobs, 1):
        score = job.get("score", 0.0)
        score_bar = "🟢" if score >= 0.75 else "🟡" if score >= 0.55 else "🔴"
        title = job.get("title") or "N/A"
        company = job.get("company") or "N/A"
        location = job.get("location") or "N/A"
        url = job.get("url", "")
        rationale = job.get("rationale", "")
        role_match = job.get("role_match", "")
        priority = job.get("priority") or ("high" if score >= 0.75 else "medium" if score >= 0.55 else "low")
        verdict = job.get("verdict") or rationale
        reasons = _as_list(job.get("reasons"))[:3]
        concerns = _as_list(job.get("concerns"))[:3]
        application_angle = job.get("application_angle", "")

        reason_text = "; ".join(reasons) if reasons else "No specific fit signals returned."
        concern_text = "; ".join(concerns) if concerns else "No major concern flagged."
        priority_label = priority.upper() if priority in {"high", "medium", "low"} else priority

        msg = (
            f"{score_bar} <b>{i}. {html.escape(title)}</b>\n"
            f"<b>{html.escape(company)}</b> · {html.escape(location)}\n\n"
            f"⭐ <b>{score:.2f}</b> · {html.escape(priority_label)} · match: {html.escape(role_match)}\n"
            f"✅ <b>Why:</b> {html.escape(reason_text)}\n"
            f"⚠️ <b>Check:</b> {html.escape(concern_text)}\n"
            f"🎯 <b>Verdict:</b> {html.escape(verdict)}\n"
            f"✍️ <b>Angle:</b> {html.escape(application_angle or 'Tailor application to the strongest fit signals.')}\n\n"
            f'🔗 <a href="{html.escape(url, quote=True)}">View job</a>'
        )
        _send(token, chat_id, msg)

    footer = "\n✅ To log an application: <code>python scripts/mark_applied.py &lt;url&gt;</code>"
    _send(token, chat_id, footer)
    print(f"Sent {len(jobs)} jobs to Telegram")


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []
