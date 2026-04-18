"""Send ranked job digest to Telegram."""
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


def send_digest(jobs: list[dict]):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    if not jobs:
        _send(token, chat_id, "🔍 <b>Job Agent</b>\n\nNo new matching jobs today.")
        return

    header = f"🔍 <b>Job Digest — {len(jobs)} new matches</b>\n\n"
    _send(token, chat_id, header)

    for i, job in enumerate(jobs, 1):
        score_bar = "🟢" if job["score"] >= 0.7 else "🟡" if job["score"] >= 0.5 else "🔴"
        title = job.get("title") or "N/A"
        company = job.get("company") or "N/A"
        location = job.get("location") or "N/A"
        url = job.get("url", "")
        score = job.get("score", 0.0)
        rationale = job.get("rationale", "")
        role_match = job.get("role_match", "")

        msg = (
            f"{score_bar} <b>{i}. {title}</b>\n"
            f"🏢 {company}  |  📍 {location}\n"
            f"⭐ Score: {score:.2f}  |  Match: {role_match}\n"
            f"💬 {rationale}\n"
            f'🔗 <a href="{url}">View job</a>'
        )
        _send(token, chat_id, msg)

    footer = "\n✅ To log an application: <code>python scripts/mark_applied.py &lt;url&gt;</code>"
    _send(token, chat_id, footer)
    print(f"Sent {len(jobs)} jobs to Telegram")
