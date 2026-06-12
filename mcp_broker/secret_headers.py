import re

HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
MAX_HEADER_NAME_LENGTH = 128
UNIVERSAL_RESERVED_SECRET_HEADER_NAMES = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
LITELLM_MANAGED_SECRET_HEADER_NAMES = {"x-litellm-api-key"}
RESERVED_SECRET_HEADER_NAMES = UNIVERSAL_RESERVED_SECRET_HEADER_NAMES | LITELLM_MANAGED_SECRET_HEADER_NAMES


def normalize_secret_header_name(value: str) -> str:
    return value.strip()


def is_valid_secret_header_name(
    value: str,
    *,
    reserved_names: set[str] = UNIVERSAL_RESERVED_SECRET_HEADER_NAMES,
) -> bool:
    header_name = normalize_secret_header_name(value)
    return (
        bool(header_name)
        and len(header_name) <= MAX_HEADER_NAME_LENGTH
        and bool(HEADER_NAME_RE.fullmatch(header_name))
        and header_name.lower() not in reserved_names
    )


def is_valid_litellm_secret_header_name(value: str) -> bool:
    return is_valid_secret_header_name(value, reserved_names=RESERVED_SECRET_HEADER_NAMES)
