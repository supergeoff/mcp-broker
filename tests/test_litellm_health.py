import httpx
import pytest

from mcp_broker.litellm_health import LiteLLMHealthClient

pytestmark = pytest.mark.anyio


async def test_litellm_health_client_checks_health_with_admin_authorization(settings) -> None:
    captured: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        captured["x_litellm_api_key"] = request.headers.get("x-litellm-api-key")
        return httpx.Response(
            200,
            json={
                "healthy_endpoints": [
                    {"model": "gpt-4o-mini", "api_base": "https://api.openai.com/v1"}
                ],
                "unhealthy_endpoints": [
                    {
                        "model": "claude-sonnet",
                        "api_base": "https://api.anthropic.com",
                        "error": "401 upstream auth failed",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await LiteLLMHealthClient(settings, client).check_upstream_health()

    assert captured == {
        "path": "/health",
        "authorization": "Bearer admin-read-key",
        "x_litellm_api_key": None,
    }
    assert [(endpoint.model, endpoint.status) for endpoint in report.endpoints] == [
        ("gpt-4o-mini", "healthy"),
        ("claude-sonnet", "unhealthy"),
    ]
    assert report.endpoints[0].api_base == "https://api.openai.com/v1"
    assert report.endpoints[1].error == "401 upstream auth failed"


async def test_litellm_health_client_reports_http_auth_failures(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await LiteLLMHealthClient(settings, client).check_upstream_health()

    assert [(endpoint.model, endpoint.status) for endpoint in report.endpoints] == [
        ("LiteLLM health", "unhealthy")
    ]
    assert "401" in (report.endpoints[0].error or "")
    assert "invalid token" in (report.endpoints[0].error or "")
