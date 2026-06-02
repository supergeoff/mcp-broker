from mcp_broker.config import Settings


def test_settings_accepts_empty_admin_emails_env(monkeypatch, encryption_key: str) -> None:
    monkeypatch.setenv("PUBLIC_URL", "https://broker.example.com")
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", encryption_key)
    monkeypatch.setenv("SESSION_SECRET", "session-secret")
    monkeypatch.setenv("OIDC_ISSUER", "https://id.example.com")
    monkeypatch.setenv("UI_OIDC_CLIENT_ID", "ui-client")
    monkeypatch.setenv("UI_OIDC_CLIENT_SECRET", "ui-secret")
    monkeypatch.setenv("EXPECTED_AUDIENCE", "claude-client")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.example.com")
    monkeypatch.setenv("LITELLM_ADMIN_KEY", "litellm-admin")
    monkeypatch.setenv("ADMIN_EMAILS", "")

    settings = Settings()

    assert settings.admin_emails == []
