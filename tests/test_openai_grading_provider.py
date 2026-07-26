import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import openai_grading_provider
from openai_grading_provider import OpenAIGradingProvider


class FakeResponse:
    output = '{"schema_version":"job-agent.deep-grade.v1"}'

    def raise_for_status(self):
        return None

    def json(self):
        return {"output_text": self.output}


def test_provider_uses_one_non_stored_strict_structured_response(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(openai_grading_provider.requests, "post", post)

    output = OpenAIGradingProvider().complete({"official_vacancy": {"id": "42"}})

    assert output.startswith("{")
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://api.openai.com/v1/responses"
    assert kwargs["json"]["store"] is False
    assert kwargs["json"]["text"]["format"]["strict"] is True
    assert kwargs["json"]["text"]["format"]["type"] == "json_schema"
    assert kwargs["headers"]["Authorization"] == "Bearer test-key"
    fact_schema = kwargs["json"]["text"]["format"]["schema"]["properties"][
        "compensation"
    ]["properties"]["base_cash"]["properties"]["facts"]["items"]
    assert set(fact_schema["required"]) == {
        "value",
        "source",
        "date",
        "currency",
        "confidence",
        "assumptions",
    }


def test_missing_key_fails_before_network(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        openai_grading_provider.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        OpenAIGradingProvider().complete({})


def test_web_resolution_and_grading_remain_one_structured_api_call(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []

    class WebResponse(FakeResponse):
        output = (
            '{"resolution_status":"unavailable",'
            '"resolved_vacancy":null,"grade":null}'
        )

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return WebResponse()

    monkeypatch.setattr(openai_grading_provider.requests, "post", post)

    result = OpenAIGradingProvider().resolve_and_grade(
        {"company": "Example AI", "title": "Research Scientist"},
        {"professional_summary": "AI researcher"},
    )

    assert result["resolution_status"] == "unavailable"
    assert len(calls) == 1
    payload = calls[0][1]["json"]
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["tool_choice"] == "required"
    assert payload["max_output_tokens"] == 16000
    assert payload["store"] is False
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["required"] == [
        "resolution_status",
        "resolved_vacancy",
        "grade",
    ]
    assert "const" not in str(payload["text"]["format"]["schema"])


def test_truncated_web_structured_output_fails_safely(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class TruncatedResponse(FakeResponse):
        output = '{"resolution_status":"verified","resolved_vacancy":{"title":"AI'

    monkeypatch.setattr(
        openai_grading_provider.requests,
        "post",
        lambda *args, **kwargs: TruncatedResponse(),
    )

    with pytest.raises(RuntimeError, match="invalid structured output"):
        OpenAIGradingProvider().resolve_and_grade({}, {})


def test_http_failure_reports_only_safe_api_metadata(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class RejectedResponse:
        status_code = 400
        text = "sensitive prompt and bearer token"

        def raise_for_status(self):
            error = openai_grading_provider.requests.HTTPError("unsafe message")
            error.response = self
            raise error

        def json(self):
            return {
                "error": {
                    "message": "sensitive request body",
                    "type": "invalid_request_error",
                    "code": "unsupported_value",
                    "param": "tools[0].type",
                }
            }

    monkeypatch.setattr(
        openai_grading_provider.requests,
        "post",
        lambda *args, **kwargs: RejectedResponse(),
    )

    with pytest.raises(RuntimeError) as caught:
        OpenAIGradingProvider().resolve_and_grade({}, {})

    message = str(caught.value)
    assert "HTTP 400" in message
    assert "type=invalid_request_error" in message
    assert "code=unsupported_value" in message
    assert "param=tools[0].type" in message
    assert "sensitive" not in message
