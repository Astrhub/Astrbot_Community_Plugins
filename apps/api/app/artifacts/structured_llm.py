from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

STRUCTURED_LLM_ADAPTER_VERSION = "structured-llm-v1"
MAX_STRUCTURED_LLM_RESPONSE_BYTES = 128 * 1024

_PUBLIC_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2060\ufeff]")
_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----.*?"
        r"-----END(?: [A-Z]+)? PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"https://(?:canary\.)?discord(?:app)?\.com/api/webhooks/[^\s]+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE),
    re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE),
    re.compile(
        r"([?&](?:access_token|api[_-]?key|key|password|secret|signature|token)=)"
        r"[^&#\s]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(accountkey|api[_-]?key|access[_-]?token|client[_-]?secret|password|"
        r"private[_-]?token|secret|token)"
        r"(\s*[:=]\s*)[^\s,;\"'}]+",
        re.IGNORECASE,
    ),
)


class LlmError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        raw_response: Any = None,
        attempts: int = 0,
        usage: Mapping[str, int | bool] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.raw_response = redact_private_payload(raw_response)
        self.attempts = max(0, int(attempts))
        self.usage = {
            key: value
            for key, value in dict(usage or {}).items()
            if key
            in {
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cost_microusd",
                "estimated",
            }
            and isinstance(value, (bool, int))
        }

class LlmProviderTimeout(LlmError):
    def __init__(self, *, attempts: int = 0) -> None:
        super().__init__(
            "llm_provider_timeout",
            "LLM provider timed out",
            retryable=True,
            attempts=attempts,
        )


class LlmProviderRateLimited(LlmError):
    def __init__(self, *, attempts: int = 0) -> None:
        super().__init__(
            "llm_provider_rate_limited",
            "LLM provider rate limited the request",
            retryable=True,
            attempts=attempts,
        )


class LlmProviderUnavailable(LlmError):
    def __init__(
        self,
        message: str = "LLM provider is unavailable",
        *,
        retryable: bool = True,
        attempts: int = 0,
    ) -> None:
        super().__init__(
            "llm_provider_unavailable",
            message,
            retryable=retryable,
            attempts=attempts,
        )


class LlmOutputInvalid(LlmError):
    def __init__(
        self,
        message: str = "LLM provider returned invalid structured JSON",
        *,
        raw_response: Any = None,
        attempts: int = 0,
        usage: Mapping[str, int | bool] | None = None,
    ) -> None:
        super().__init__(
            "llm_output_invalid",
            message,
            raw_response=raw_response,
            attempts=attempts,
            usage=usage,
        )


class LlmBudgetExceeded(LlmError):
    def __init__(
        self,
        message: str = "LLM token budget is insufficient",
        *,
        attempts: int = 0,
        usage: Mapping[str, int | bool] | None = None,
    ) -> None:
        super().__init__(
            "llm_budget_exceeded",
            message,
            attempts=attempts,
            usage=usage,
        )


@dataclass(frozen=True, slots=True)
class StructuredLlmRequest:
    model: str
    prompt_version: str
    schema_name: str
    response_schema: Mapping[str, Any]
    system_prompt: str
    input_json: str
    max_output_tokens: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        for value in (self.model, self.prompt_version, self.schema_name):
            if not _PUBLIC_IDENTIFIER.fullmatch(value):
                raise ValueError("LLM request identifiers must be public versioned names")
        if not self.system_prompt.strip() or len(self.system_prompt) > 8000:
            raise ValueError("LLM system prompt is invalid")
        if not self.input_json or len(self.input_json.encode("utf-8")) > 8 * 1024 * 1024:
            raise ValueError("LLM structured input is invalid")
        if self.max_output_tokens < 64 or self.max_output_tokens > 8192:
            raise ValueError("LLM output token limit is invalid")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 600:
            raise ValueError("LLM request timeout is invalid")


@dataclass(frozen=True, slots=True)
class StructuredLlmResponse:
    content: str
    raw_response: Any
    usage: Mapping[str, int]


@runtime_checkable
class StructuredLlmProvider(Protocol):
    name: str
    version: str

    async def complete(self, request: StructuredLlmRequest) -> StructuredLlmResponse: ...


class DeterministicStructuredLlmProvider:
    name = "deterministic-structured-llm"
    version = "deterministic-structured-llm-v1"

    def __init__(
        self,
        result: Mapping[str, Any] | str,
        *,
        errors: Sequence[Exception] = (),
        usage: Mapping[str, int] | None = None,
    ) -> None:
        self.result = result
        self.errors = list(errors)
        self.usage = dict(usage or {})
        self.requests: list[StructuredLlmRequest] = []

    async def complete(self, request: StructuredLlmRequest) -> StructuredLlmResponse:
        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)
        content = (
            self.result
            if isinstance(self.result, str)
            else json.dumps(self.result, ensure_ascii=True, separators=(",", ":"))
        )
        return StructuredLlmResponse(
            content=content,
            raw_response={"content": content},
            usage=self.usage,
        )


class UnavailableStructuredLlmProvider:
    name = "unavailable-structured-llm"
    version = "unavailable-structured-llm-v1"

    async def complete(self, request: StructuredLlmRequest) -> StructuredLlmResponse:
        del request
        raise LlmProviderUnavailable(retryable=False)


class OpenAICompatibleStructuredLlmProvider:
    name = "openai-compatible-structured-llm"
    version = STRUCTURED_LLM_ADAPTER_VERSION

    def __init__(
        self,
        *,
        endpoint_url: str,
        api_key: str,
        configured_model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint_url = _validated_endpoint(endpoint_url)
        self._api_key = api_key.strip()
        self.configured_model = configured_model.strip()
        if not self._api_key or not _PUBLIC_IDENTIFIER.fullmatch(self.configured_model):
            raise ValueError("structured LLM provider configuration is incomplete")
        self._client = client or httpx.AsyncClient(timeout=600.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(self, request: StructuredLlmRequest) -> StructuredLlmResponse:
        if request.model != self.configured_model:
            raise LlmProviderUnavailable(
                "Configured LLM model does not match the fixed review policy",
                retryable=False,
            )
        payload = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.input_json},
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": dict(request.response_schema),
                },
            },
        }
        try:
            response = await self._client.post(
                self.endpoint_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=request.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise LlmProviderTimeout() from exc
        except httpx.RequestError as exc:
            raise LlmProviderUnavailable() from exc
        if response.status_code == 429:
            raise LlmProviderRateLimited()
        if response.status_code >= 500:
            raise LlmProviderUnavailable()
        if response.is_error:
            raise LlmProviderUnavailable(
                "LLM provider rejected the structured request",
                retryable=False,
            )
        if len(response.content) > MAX_STRUCTURED_LLM_RESPONSE_BYTES:
            raise LlmOutputInvalid("LLM provider response exceeds the private audit limit")
        raw: Any = {"body": response.text[:MAX_STRUCTURED_LLM_RESPONSE_BYTES]}
        try:
            raw = response.json()
            content = _extract_structured_content(raw)
            usage = _normalize_usage(raw.get("usage") if isinstance(raw, Mapping) else None)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise LlmOutputInvalid(raw_response=raw) from exc
        return StructuredLlmResponse(
            content=content,
            raw_response=redact_private_payload(raw),
            usage=usage,
        )


class StructuredLlmCaller:
    def __init__(
        self,
        provider: StructuredLlmProvider,
        *,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        if retry_delay_seconds < 0 or retry_delay_seconds > 5:
            raise ValueError("LLM retry delay must be between 0 and 5 seconds")
        self.provider = provider
        self.retry_delay_seconds = retry_delay_seconds

    async def complete(
        self,
        request: StructuredLlmRequest,
        *,
        max_retries: int,
    ) -> tuple[StructuredLlmResponse, int]:
        attempts = 0
        while True:
            attempts += 1
            try:
                response = await asyncio.wait_for(
                    self.provider.complete(request),
                    timeout=request.timeout_seconds,
                )
                return response, attempts
            except TimeoutError:
                error: LlmError = LlmProviderTimeout(attempts=attempts)
            except LlmError as exc:
                error = exc
                error.attempts = attempts
            if not error.retryable or attempts > max_retries:
                raise error
            if self.retry_delay_seconds:
                await asyncio.sleep(
                    min(self.retry_delay_seconds * (2 ** (attempts - 1)), 5.0)
                )


def estimate_tokens(value: str) -> int:
    """Return a conservative tokenizer-independent estimate for budget gating."""

    size = len(value.encode("utf-8"))
    return max(1, (size + 2) // 3)


def estimate_structured_prompt_tokens(
    *,
    system_prompt: str,
    input_json: str,
    response_schema: Mapping[str, Any],
) -> int:
    schema_json = json.dumps(
        response_schema,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return estimate_tokens(system_prompt) + estimate_tokens(input_json) + estimate_tokens(schema_json) + 32


def redact_llm_text(value: str, *, maximum: int) -> str:
    normalized = _CONTROL.sub(" ", str(value).replace("\x00", " "))
    normalized = _redact_secrets(normalized)
    normalized = " ".join(normalized.split())
    return normalized[:maximum]


def redact_llm_source(value: str, *, maximum: int) -> str:
    normalized = str(value).replace("\x00", " ")
    normalized = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", normalized)
    return _redact_secrets(normalized)[:maximum]


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def output_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def resolved_usage(
    raw_usage: Mapping[str, int],
    *,
    prompt_token_floor: int,
    response_content: str,
) -> Mapping[str, int | bool]:
    completion_floor = estimate_tokens(response_content)
    reported_prompt = int(raw_usage.get("prompt_tokens") or 0)
    reported_completion = int(raw_usage.get("completion_tokens") or 0)
    reported_total = int(raw_usage.get("total_tokens") or 0)
    prompt = max(prompt_token_floor, reported_prompt)
    completion = max(completion_floor, reported_completion)
    total = max(prompt + completion, reported_total)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated": (
            prompt != reported_prompt
            or completion != reported_completion
            or total != reported_total
        ),
    }


def estimate_cost_microusd(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    input_rate: int,
    output_rate: int,
) -> int:
    numerator = max(0, prompt_tokens) * input_rate + max(0, completion_tokens) * output_rate
    return (numerator + 999_999) // 1_000_000


def reject_control_fields(value: Any, *, forbidden: frozenset[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in forbidden:
                raise ValueError("LLM output contains a forbidden command or decision field")
            reject_control_fields(item, forbidden=forbidden)
    elif isinstance(value, list):
        for item in value:
            reject_control_fields(item, forbidden=forbidden)


def redact_private_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: redact_private_payload(item)
            for key, item in list(value.items())[:300]
        }
    if isinstance(value, (list, tuple)):
        return [redact_private_payload(item) for item in value[:300]]
    if isinstance(value, str):
        return redact_llm_text(value, maximum=MAX_STRUCTURED_LLM_RESPONSE_BYTES)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_llm_text(str(value), maximum=1000)


def _redact_secrets(value: str) -> str:
    normalized = value
    for pattern in _SECRET_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            prefix = "".join(group or "" for group in match.groups())
            replacement = f"{prefix}[REDACTED]"
            missing_newlines = match.group(0).count("\n") - prefix.count("\n")
            return replacement + ("\n" * max(0, missing_newlines))

        normalized = pattern.sub(replace, normalized)
    return normalized


def _validated_endpoint(value: str) -> str:
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
        parsed.port
    except ValueError as exc:
        raise ValueError("structured LLM provider endpoint is invalid") from exc
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in ({"https", "http"} if loopback else {"https"})
        or not parsed.hostname
        or not parsed.path
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("structured LLM provider endpoint is invalid")
    return normalized


def _extract_structured_content(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        raise TypeError("provider response must be an object")
    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        message = choices[0].get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    str(part["text"])
                    for part in content
                    if isinstance(part, Mapping)
                    and part.get("type") in {"text", "output_text"}
                    and isinstance(part.get("text"), str)
                ]
                if parts:
                    return "".join(parts)
    if isinstance(raw.get("output_text"), str):
        return str(raw["output_text"])
    if "schema_version" in raw:
        return json.dumps(raw, ensure_ascii=True, separators=(",", ":"))
    raise KeyError("provider response does not contain structured content")


def _normalize_usage(value: Any) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > 10_000_000:
            continue
        result[key] = raw
    if "total_tokens" not in result and {
        "prompt_tokens",
        "completion_tokens",
    } <= result.keys():
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return result
