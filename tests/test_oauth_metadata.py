import httpx
import pytest

from mcp_broker.app import create_app

pytestmark = pytest.mark.anyio


async def test_named_protected_resource_metadata_points_claude_to_pocket_id(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-protected-resource/dokploy")

    assert response.status_code == 200
    assert response.json() == {
        "resource": "https://broker.example.com/dokploy",
        "authorization_servers": ["https://id.example.com"],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [],
        "resource_documentation": "https://broker.example.com/",
    }



async def test_named_mcp_without_bearer_token_returns_oauth_challenge(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/dokploy", content=b"{}")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://broker.example.com/.well-known/oauth-protected-resource/dokploy"'
    )
