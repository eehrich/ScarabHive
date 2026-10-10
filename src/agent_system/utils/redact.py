"""Credentials out of what is shown or logged.

Two kinds, told apart by their place, not by their value: a field whose name
says it holds a credential (``api_key``, ``secret_key``, ``password``,
``Authorization`` ...), and a credential in a URL: a parameter of its query
(``?key=...`` -- Gemini's REST API, ``?api_key=...`` -- an MCP server's URL) or
the password of its userinfo (``redis://:pw@host``, ``postgres://app:pw@db``).
"""
from __future__ import annotations

import re
from typing import Any

MASK = "***"

#: A field name that ends in one of these holds a credential. ``*_env`` names a variable (forge's
#: ``token_env: FORGE_GITLAB_TOKEN``), not its value, and stays; so do counts like ``max_tokens``.
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:secret|secret_key|client_secret|password|passwd|passphrase|api_key|apikey|"
    r"token|access_token|refresh_token|auth_token|private_key|authorization|cookie)$")

#: A credential parameter of a query, up to the next separator. No nested quantifier: linear in the text.
_CREDENTIAL_PARAM = re.compile(
    r"([?&](?:key|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|client[_-]?secret|"
    r"password|passwd|sig|signature)=)[^&#\s\"']+",
    re.IGNORECASE)

#: The password of a URL's userinfo (``scheme://user:password@host``); the user name stays.
_USERINFO_PASSWORD = re.compile(r"(://[^/@\s:\"']*:)[^/@\s\"']+(?=@)")

#: What a text must contain for mask_url_credentials to find a query parameter: a cheap test before the regex.
_PARAM_MARKS = ("key=", "token=", "secret=", "password=", "passwd=", "sig=", "signature=")


def is_secret_name(name: Any) -> bool:
    """Whether a field called ``name`` holds a credential."""
    return isinstance(name, str) and bool(_SECRET_NAME.search(name.lower().replace("-", "_")))


def may_hold_url_credential(text: str) -> bool:
    """Cheap pre-check for mask_url_credentials; ``text`` lower-cased by the caller."""
    return any(mark in text for mark in _PARAM_MARKS) or ("://" in text and "@" in text)


def mask_url_credentials(text: str) -> str:
    """``text`` with the value of every credential parameter in a query masked (``?key=***&alt=sse``),
    and the password in every URL's userinfo (``redis://:***@host``)."""
    text = _USERINFO_PASSWORD.sub(rf"\1{MASK}", text)
    return _CREDENTIAL_PARAM.sub(rf"\1{MASK}", text)


def redact_secrets(value: Any) -> Any:
    """``value`` with every credential masked: a non-empty string under a credential's name, and the
    credentials of every URL in a string. Dicts, lists and tuples are copied; any other
    object -- a cancellation token among tool parameters -- is the very object, unchanged."""
    if isinstance(value, dict):
        return {key: (MASK if is_secret_name(key) and isinstance(item, str) and item else redact_secrets(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, str) and may_hold_url_credential(value.lower()):
        return mask_url_credentials(value)
    return value
