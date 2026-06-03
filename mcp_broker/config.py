from functools import cached_property
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    public_url: str
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    database_url: str = "sqlite+aiosqlite:////data/mcp-broker.sqlite3"
    secrets_encryption_key: str
    session_secret: str
    oidc_issuer: str
    jwks_uri: str | None = None
    ui_oidc_client_id: str
    ui_oidc_client_secret: str
    expected_issuer: str | None = None
    expected_audience: Annotated[list[str], NoDecode]
    litellm_base_url: str
    litellm_admin_key: str
    admin_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)
    rate_limit_requests_per_minute: int = 120

    @field_validator("admin_emails", mode="before")
    @classmethod
    def parse_admin_emails(cls, value: object) -> list[str] | object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [email.strip().lower() for email in value.split(",") if email.strip()]
        return value

    @field_validator("expected_audience", mode="before")
    @classmethod
    def parse_expected_audience(cls, value: object) -> list[str] | object:
        if isinstance(value, str):
            return [audience.strip() for audience in value.split(",") if audience.strip()]
        return value

    @field_validator("public_url", "oidc_issuer", "litellm_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @cached_property
    def issuer(self) -> str:
        return (self.expected_issuer or self.oidc_issuer).rstrip("/")

    @cached_property
    def jwks_endpoint(self) -> str:
        if self.jwks_uri:
            return self.jwks_uri
        return f"{self.oidc_issuer}/.well-known/jwks.json"

    @cached_property
    def cookie_secure(self) -> bool:
        return self.public_url.startswith("https://")
