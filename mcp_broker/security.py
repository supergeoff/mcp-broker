import json
from typing import Any

import jwt
from cryptography.fernet import Fernet
from jwt import InvalidTokenError, PyJWKClient
from jwt.algorithms import RSAAlgorithm


class JwtValidationError(Exception):
    """Raised when an incoming OAuth access token is not trusted."""


class FernetCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")


class JwtValidator:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str | None = None,
        jwks: dict[str, Any] | None = None,
    ) -> None:
        if not jwks_uri and not jwks:
            raise ValueError("jwks_uri or jwks is required")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self._jwks = jwks
        self._jwk_client = PyJWKClient(jwks_uri) if jwks_uri else None

    def verify(self, token: str) -> dict[str, Any]:
        try:
            key = self._signing_key(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (InvalidTokenError, ValueError) as exc:
            raise JwtValidationError(str(exc)) from exc

        token_type = claims.get("type")
        if token_type is not None and token_type != "oauth-access-token":
            raise JwtValidationError("unexpected token type")
        return claims

    def _signing_key(self, token: str) -> object:
        if self._jwk_client is not None:
            return self._jwk_client.get_signing_key_from_jwt(token).key
        return self._static_signing_key(token)

    def _static_signing_key(self, token: str) -> object:
        if not self._jwks:
            raise ValueError("JWKS is not configured")
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        keys = self._jwks.get("keys", [])
        for key_data in keys:
            if kid is None or key_data.get("kid") == kid:
                return RSAAlgorithm.from_jwk(json.dumps(key_data))
        raise ValueError("no matching JWK")


def litellm_auth_value(key: str) -> str:
    if key.lower().startswith("bearer "):
        return key
    return f"Bearer {key}"
