from dataclasses import dataclass
from typing import Any

import httpx

from mcp_broker.config import Settings
from mcp_broker.security import litellm_auth_value


@dataclass(frozen=True)
class LiteLLMHealthEndpoint:
    model: str
    status: str
    api_base: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class LiteLLMHealthReport:
    endpoints: tuple[LiteLLMHealthEndpoint, ...]


class LiteLLMHealthClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http_client = http_client

    async def check_upstream_health(self) -> LiteLLMHealthReport:
        try:
            response = await self._http_client.get(
                f"{self._settings.litellm_base_url}/health",
                headers={"Authorization": litellm_auth_value(self._settings.litellm_admin_key)},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _single_failure(_http_status_error(exc.response))
        except httpx.HTTPError as exc:
            return _single_failure(str(exc))

        try:
            payload = response.json()
        except ValueError:
            return _single_failure("LiteLLM health returned a non-JSON response")
        return normalize_litellm_health_response(payload)


def normalize_litellm_health_response(payload: Any) -> LiteLLMHealthReport:
    if not isinstance(payload, dict):
        return _single_failure("LiteLLM health returned an unexpected response")

    endpoints: list[LiteLLMHealthEndpoint] = []
    endpoints.extend(_endpoint_from_item(item, "healthy") for item in _items(payload, "healthy_endpoints"))
    endpoints.extend(_endpoint_from_item(item, "unhealthy") for item in _items(payload, "unhealthy_endpoints"))
    if endpoints:
        return LiteLLMHealthReport(tuple(endpoints))

    status = str(payload.get("status", "")).strip().lower()
    if status in {"connected", "healthy", "ok"}:
        return LiteLLMHealthReport(
            (LiteLLMHealthEndpoint(model="LiteLLM health", status="healthy"),)
        )
    return _single_failure("LiteLLM health returned no model endpoint data")


def _items(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    return value if isinstance(value, list) else []


def _endpoint_from_item(item: Any, status: str) -> LiteLLMHealthEndpoint:
    if isinstance(item, dict):
        return LiteLLMHealthEndpoint(
            model=_first_text(item, ("model", "model_name", "litellm_model_name", "name")) or "Unknown model",
            status=status,
            api_base=_first_text(item, ("api_base", "api_base_url", "base_url")),
            error=_error_text(item) if status == "unhealthy" else None,
        )
    if isinstance(item, str):
        return LiteLLMHealthEndpoint(model=item, status=status)
    return LiteLLMHealthEndpoint(model="Unknown model", status=status)


def _first_text(item: dict[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = item.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _error_text(item: dict[str, Any]) -> str | None:
    value = _first_text(item, ("error", "error_message", "exception", "message"))
    if value is None:
        return None
    return value[:500]


def _single_failure(error: str) -> LiteLLMHealthReport:
    return LiteLLMHealthReport(
        (LiteLLMHealthEndpoint(model="LiteLLM health", status="unhealthy", error=error[:500]),)
    )


def _http_status_error(response: httpx.Response) -> str:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return f"{response.status_code} {detail}"
