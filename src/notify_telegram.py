"""Send ranked job digest to Telegram."""
from dataclasses import dataclass
import html
import os, requests
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class TelegramMessage:
    text: str
    reply_markup: dict | None = None


@dataclass(frozen=True)
class TelegramReceipt:
    message_id: int
    chat_id: str


class TelegramSendRejected(RuntimeError):
    """Telegram definitively rejected the request before delivery."""


class TelegramSendUncertain(RuntimeError):
    """The request may have crossed the delivery boundary."""


RoleButtonFactory = Callable[[Mapping[str, Any]], Sequence[Mapping[str, str]]]


def _send(
    token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None,
) -> TelegramReceipt:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        raise TelegramSendUncertain(
            "Telegram send outcome is uncertain "
            f"(transport={type(exc).__name__})"
        ) from None
    try:
        body: Any = resp.json()
    except Exception:
        raise TelegramSendUncertain(
            "Telegram returned an invalid delivery acknowledgement"
        ) from None
    error_code = (
        body.get("error_code", "unknown")
        if isinstance(body, Mapping)
        else "unknown"
    )
    if not resp.ok or not isinstance(body, Mapping) or body.get("ok") is not True:
        raise TelegramSendRejected(
            "Telegram send rejected "
            f"(HTTP {resp.status_code}, error_code={error_code})"
        )
    result = body.get("result")
    if not isinstance(result, Mapping):
        raise TelegramSendUncertain(
            "Telegram acknowledgement omitted the message result"
        )
    chat = result.get("chat")
    message_id = result.get("message_id")
    if (
        not isinstance(chat, Mapping)
        or not isinstance(message_id, int)
        or chat.get("id") is None
    ):
        raise TelegramSendUncertain(
            "Telegram acknowledgement omitted delivery identity"
        )
    acknowledged_chat_id = str(chat["id"])
    if chat_id.lstrip("-").isdigit() and acknowledged_chat_id != chat_id:
        raise TelegramSendUncertain(
            "Telegram acknowledged a different destination chat"
        )
    return TelegramReceipt(
        message_id=message_id,
        chat_id=acknowledged_chat_id,
    )


def send_error(message: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    _send(token, chat_id, f"❌ <b>Job Agent Error</b>\n\n{message}")


def send_message(message: TelegramMessage) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    _send(token, chat_id, message.text, reply_markup=message.reply_markup)


def send_digest(
    jobs: list[dict],
    *,
    overflow_count: int = 0,
    overflow_callback_data: str | None = None,
):
    for message in digest_messages(
        jobs,
        overflow_count=overflow_count,
        overflow_callback_data=overflow_callback_data,
    ):
        send_message(message)
    print(f"Sent {len(jobs)} jobs to Telegram")


def digest_messages(
    jobs: list[dict],
    *,
    overflow_count: int = 0,
    overflow_callback_data: str | None = None,
    role_button_factory: RoleButtonFactory | None = None,
) -> tuple[TelegramMessage, ...]:
    messages: list[TelegramMessage] = []

    if not jobs:
        return (TelegramMessage("🔍 <b>Job Agent</b>\n\nNo new matching jobs today."),)

    header = f"🔍 <b>Job Digest</b>\n{len(jobs)} new matches ranked by fit."
    messages.append(TelegramMessage(header))

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
        evaluation = (
            job.get("portfolio_evaluation")
            if isinstance(job.get("portfolio_evaluation"), Mapping)
            else {}
        )
        gaps = _as_list(evaluation.get("gaps") or job.get("gaps"))[:3]
        risks = _as_list(evaluation.get("risks") or job.get("risks"))[:3]
        freshness = (
            evaluation.get("vacancy_retrieved_at")
            or job.get("retrieved_at")
            or "unknown"
        )
        compensation = evaluation.get("compensation", job.get("compensation"))
        sponsorship = evaluation.get("sponsorship", job.get("sponsorship"))
        ownership = evaluation.get("ownership", job.get("ownership"))
        rank_explanation = (
            evaluation.get("rank_explanation")
            or job.get("rationale")
            or "Not provided"
        )

        reason_text = "; ".join(reasons) if reasons else "No specific fit signals returned."
        concern_text = "; ".join(concerns) if concerns else "No major concern flagged."
        priority_label = priority.upper() if priority in {"high", "medium", "low"} else priority

        msg = (
            f"{score_bar} <b>{i}. {html.escape(title)}</b>\n"
            f"<b>{html.escape(company)}</b> · {html.escape(location)}\n\n"
            f"⭐ <b>{score:.2f}</b> · {html.escape(priority_label)} · match: {html.escape(role_match)}\n"
            f"🏢 <b>Modality:</b> {html.escape(str(job.get('modality') or 'unknown'))}\n"
            f"📨 <b>Source:</b> {html.escape(str(job.get('source') or 'unknown'))}\n"
            f"🕒 <b>Freshness:</b> {html.escape(str(freshness))}\n"
            f"✅ <b>Why:</b> {html.escape(reason_text)}\n"
            f"🧩 <b>Gaps:</b> {html.escape('; '.join(gaps) or 'None reported')}\n"
            f"💰 <b>Compensation:</b> {html.escape(_status(compensation))}\n"
            f"🛂 <b>Immigration:</b> {html.escape(_status(sponsorship))}\n"
            f"🏷 <b>Ownership:</b> {html.escape(_status(ownership))}\n"
            f"⚠️ <b>Check:</b> {html.escape(concern_text)}\n"
            f"⚠️ <b>Risks:</b> {html.escape('; '.join(risks) or 'None reported')}\n"
            f"📊 <b>Rank:</b> {html.escape(_short(rank_explanation))}\n"
            f"🎯 <b>Verdict:</b> {html.escape(verdict)}\n"
            f"✍️ <b>Angle:</b> {html.escape(application_angle or 'Tailor application to the strongest fit signals.')}\n\n"
            f'🔗 <a href="{html.escape(url, quote=True)}">View job</a>'
        )
        reply_markup = None
        if role_button_factory is not None:
            buttons = [
                {
                    "text": str(button["text"]),
                    "callback_data": str(button["callback_data"]),
                }
                for button in role_button_factory(job)
            ]
            if buttons:
                reply_markup = {"inline_keyboard": [buttons]}
        messages.append(TelegramMessage(msg, reply_markup=reply_markup))

    footer = "\n✅ To log an application: <code>python scripts/mark_applied.py &lt;url&gt;</code>"
    reply_markup = None
    if overflow_count and overflow_callback_data:
        reply_markup = {
            "inline_keyboard": [[{
                "text": f"Mostra altre ({overflow_count})",
                "callback_data": overflow_callback_data,
            }]]
        }
    messages.append(TelegramMessage(footer, reply_markup=reply_markup))
    return tuple(messages)


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _status(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("status", "classification", "confidence", "value"):
            item = value.get(key)
            if item is not None and not isinstance(item, (Mapping, list, tuple)):
                return _short(item)
        base_cash = value.get("base_cash")
        if isinstance(base_cash, Mapping) and base_cash.get("status") is not None:
            return _short(base_cash["status"])
        return "available in details"
    if value is None or not str(value).strip():
        return "unknown"
    return _short(value)


def _short(value: Any, limit: int = 360) -> str:
    text = str(value).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
