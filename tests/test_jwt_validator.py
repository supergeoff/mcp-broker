import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from mcp_broker.security import JwtValidationError, JwtValidator


def _keypair_and_jwks() -> tuple[rsa.RSAPrivateKey, dict[str, list[dict[str, str]]]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "test-key"
    return private_key, {"keys": [jwk]}


def _token(private_key: rsa.RSAPrivateKey, **claims) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "https://id.example.com",
        "aud": "claude-connector-client-id",
        "sub": "pocket-sub",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "type": "oauth-access-token",
    } | claims
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-key"})


def test_jwt_validator_accepts_pocket_id_access_token() -> None:
    private_key, jwks = _keypair_and_jwks()
    validator = JwtValidator(
        issuer="https://id.example.com",
        audience="claude-connector-client-id",
        jwks=jwks,
    )

    claims = validator.verify(_token(private_key))

    assert claims["sub"] == "pocket-sub"
    assert claims["type"] == "oauth-access-token"


def test_jwt_validator_rejects_wrong_audience() -> None:
    private_key, jwks = _keypair_and_jwks()
    validator = JwtValidator(
        issuer="https://id.example.com",
        audience="claude-connector-client-id",
        jwks=jwks,
    )

    with pytest.raises(JwtValidationError):
        validator.verify(_token(private_key, aud="other-client"))
