"""Single-request OpenAI Responses API adapter for deep grading."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

import requests


class OpenAIGradingProvider:
    def __init__(self, model: str = "gpt-5.4-mini") -> None:
        self._model = model

    @property
    def identity(self) -> str:
        return f"openai-responses:{self._model}"

    def complete(self, request: Mapping[str, Any]) -> str:
        return self._respond(request, schema=_DEEP_GRADE_SCHEMA)

    def resolve_and_grade(
        self,
        lead: Mapping[str, Any],
        profile: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        request = {
            "contract": {
                "mode": "resolve_official_vacancy_and_deep_grade",
                "instructions": [
                    "You must use web search before answering.",
                    "Find the exact current vacancy matching the alert company, title, and location.",
                    "Use only the employer's official careers domain or its employer-specific ATS page.",
                    "Set resolved_vacancy.company to the actual hiring employer, never the VC, portfolio job-board operator, recruiter, or ATS vendor.",
                    "Never treat LinkedIn, Glassdoor, Indeed, or another aggregator as the official vacancy.",
                    "If one exact current official vacancy cannot be established, return unavailable with null vacancy and grade.",
                    "When verified, copy the full official description and requirements faithfully, then grade against the supplied professional profile.",
                    "All grade sources must be public URLs used during web search.",
                    "For matrix evidence_ids, use only exact id values present in professional_grading_profile.professional_evidence.",
                    "Never invent or transform an evidence id; use an empty list and unknown status when no canonical evidence applies.",
                    "Include exactly one matrix row for every resolved_vacancy requirement and copy each requirement text verbatim.",
                ],
            },
            "alert_lead": dict(lead),
            "professional_grading_profile": dict(profile),
        }
        output = self._respond(
            request,
            schema=_WEB_RESOLVED_GRADE_SCHEMA,
            web_search=True,
        )
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            raise RuntimeError(
                "OpenAI web-resolved grading returned invalid structured output"
            ) from None
        if not isinstance(parsed, Mapping):
            raise RuntimeError("OpenAI web-resolved grading failed safely")
        return parsed

    def _respond(
        self,
        request: Mapping[str, Any],
        *,
        schema: Mapping[str, Any],
        web_search: bool = False,
    ) -> str:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for verified deep grading")
        payload = {
            "model": self._model,
            "input": json.dumps(request, sort_keys=True),
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": (
                        "job_agent_web_resolved_grade"
                        if web_search
                        else "job_agent_deep_grade"
                    ),
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        if web_search:
            payload["tools"] = [{"type": "web_search"}]
            payload["tool_choice"] = "required"
            payload["max_output_tokens"] = 16000
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120 if web_search else 90,
            )
            response.raise_for_status()
            response_payload = response.json()
            return _response_text(response_payload)
        except requests.HTTPError as exc:
            detail = _safe_http_error_detail(exc.response)
            raise RuntimeError(
                f"OpenAI deep grading failed safely ({detail})"
            ) from None
        except Exception as exc:
            # Provider bodies may contain request context. Keep the workflow's
            # public error path free of remote response text and credentials.
            raise RuntimeError(
                "OpenAI deep grading failed safely "
                f"(transport={type(exc).__name__})"
            ) from None


def _safe_http_error_detail(response: Any) -> str:
    """Expose only low-cardinality API metadata, never response messages."""
    status = getattr(response, "status_code", "unknown")
    code = "unknown"
    param = "unknown"
    error_type = "unknown"
    try:
        body = response.json()
        error = body.get("error", {}) if isinstance(body, Mapping) else {}
        if isinstance(error, Mapping):
            code = str(error.get("code") or "unknown")
            param = str(error.get("param") or "unknown")
            error_type = str(error.get("type") or "unknown")
    except Exception:
        pass
    return (
        f"HTTP {status}, type={error_type}, code={code}, param={param}"
    )


def _response_text(payload: Mapping[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for output in payload.get("output", ()):
        if not isinstance(output, Mapping):
            continue
        for content in output.get("content", ()):
            if isinstance(content, Mapping) and isinstance(content.get("text"), str):
                return str(content["text"])
    raise ValueError("OpenAI response contained no text output")


__all__ = ["OpenAIGradingProvider"]


_EXPLAINED_SCORE = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "explanation": {"type": "string"},
    },
    "required": ["score", "explanation"],
    "additionalProperties": False,
}
_FACT = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "source": {"type": "string"},
        "date": {"type": "string"},
        "currency": {"type": "string"},
        "confidence": {"enum": ["low", "medium", "high", "unknown"]},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "value",
        "source",
        "date",
        "currency",
        "confidence",
        "assumptions",
    ],
    "additionalProperties": False,
}
_COMPENSATION_PART = {
    "type": "object",
    "properties": {
        "status": {"enum": ["published", "unknown", "not_applicable"]},
        "facts": {"type": "array", "items": _FACT},
    },
    "required": ["status", "facts"],
    "additionalProperties": False,
}
_DEEP_GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"enum": ["job-agent.deep-grade.v1"]},
        "overall_score": {"type": "number", "minimum": 0, "maximum": 100},
        "top_tier": {
            "type": "object",
            "properties": {
                "value": {"type": "boolean"},
                "explanation": {"type": "string"},
            },
            "required": ["value", "explanation"],
            "additionalProperties": False,
        },
        "rank_explanation": {"type": "string"},
        "components": {
            "type": "object",
            "properties": {
                name: _EXPLAINED_SCORE
                for name in (
                    "fit",
                    "research_preference",
                    "geography",
                    "compensation_confidence",
                    "wealth_potential",
                    "language",
                    "immigration",
                    "ownership",
                    "freshness",
                    "deadline",
                    "risk",
                )
            },
            "required": [
                "fit",
                "research_preference",
                "geography",
                "compensation_confidence",
                "wealth_potential",
                "language",
                "immigration",
                "ownership",
                "freshness",
                "deadline",
                "risk",
            ],
            "additionalProperties": False,
        },
        "compensation": {
            "type": "object",
            "properties": {
                "base_cash": _COMPENSATION_PART,
                "bonus": _COMPENSATION_PART,
                "equity": _COMPENSATION_PART,
                "benchmarks": {"type": "array", "items": _FACT},
                "wealth_potential": {
                    "type": "object",
                    "properties": {
                        "confidence": {"enum": ["low", "medium", "high", "unknown"]},
                        "explanation": {"type": "string"},
                        "assumptions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["confidence", "explanation", "assumptions"],
                    "additionalProperties": False,
                },
            },
            "required": ["base_cash", "bonus", "equity", "benchmarks", "wealth_potential"],
            "additionalProperties": False,
        },
        "sponsorship": {
            "type": "object",
            "properties": {
                "status": {"enum": ["yes", "no", "not_stated"]},
                "source": {"type": "string"},
                "verified_at": {"type": "string"},
                "visa_obstacle": {"type": "boolean"},
            },
            "required": ["status", "source", "verified_at", "visa_obstacle"],
            "additionalProperties": False,
        },
        "ownership": {
            "type": "object",
            "properties": {
                "classification": {"type": "string"},
                "source": {"type": "string"},
                "verified_at": {"type": "string"},
            },
            "required": ["classification", "source", "verified_at"],
            "additionalProperties": False,
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "requirements_evidence_matrix": {
            "type": "object",
            "properties": {
                "version": {"enum": ["job-agent.requirements-evidence.v1"]},
                "official_vacancy_version": {"type": ["string", "null"]},
                "rows": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "requirement": {"type": "string"},
                            "importance": {"enum": ["required", "preferred"]},
                            "status": {"enum": ["matched", "partial", "gap", "unknown"]},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "explanation": {"type": "string"},
                        },
                        "required": ["id", "requirement", "importance", "status", "evidence_ids", "explanation"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["version", "official_vacancy_version", "rows"],
            "additionalProperties": False,
        },
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "schema_version",
        "overall_score",
        "top_tier",
        "rank_explanation",
        "components",
        "compensation",
        "sponsorship",
        "ownership",
        "risks",
        "gaps",
        "requirements_evidence_matrix",
        "sources",
    ],
    "additionalProperties": False,
}

_RESOLVED_VACANCY_SCHEMA = {
    "type": "object",
    "properties": {
        "official_url": {"type": "string"},
        "official_job_id": {"type": "string"},
        "title": {"type": "string"},
        "company": {"type": "string"},
        "team": {"type": "string"},
        "location": {"type": "string"},
        "modality": {"type": "string"},
        "seniority": {"type": "string"},
        "official_description": {"type": "string"},
        "requirements": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
        },
        "published_at": {"type": ["string", "null"]},
        "process_language": {
            "enum": ["english", "italian", "french", "german", "unknown"]
        },
    },
    "required": [
        "official_url",
        "official_job_id",
        "title",
        "company",
        "team",
        "location",
        "modality",
        "seniority",
        "official_description",
        "requirements",
        "published_at",
        "process_language",
    ],
    "additionalProperties": False,
}
_WEB_RESOLVED_GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "resolution_status": {"enum": ["verified", "unavailable"]},
        "resolved_vacancy": {
            "anyOf": [_RESOLVED_VACANCY_SCHEMA, {"type": "null"}]
        },
        "grade": {"anyOf": [_DEEP_GRADE_SCHEMA, {"type": "null"}]},
    },
    "required": ["resolution_status", "resolved_vacancy", "grade"],
    "additionalProperties": False,
}
