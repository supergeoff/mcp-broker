import re

HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
MAX_HEADER_NAME_LENGTH = 128
RESERVED_SECRET_HEADER_NAMES = {
    "authorization",
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
    "x-litellm-api-key",
}


def normalize_secret_header_name(value: str) -> str:
    return value.strip()


def is_valid_secret_header_name(value: str) -> bool:
    header_name = normalize_secret_header_name(value)
    return (
        bool(header_name)
        and len(header_name) <= MAX_HEADER_NAME_LENGTH
        and bool(HEADER_NAME_RE.fullmatch(header_name))
        and header_name.lower() not in RESERVED_SECRET_HEADER_NAMES
    )
