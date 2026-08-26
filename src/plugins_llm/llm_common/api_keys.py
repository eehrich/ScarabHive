"""The API key follows the ENDPOINT, not the provider name.

Every OpenAI-compatible provider here can be pointed at a different host via
``base_url``. Falling back to ``OPENAI_API_KEY`` regardless of that host means
a missing gateway key sends the OpenAI secret to a third party in an
``Authorization`` header — the client comes up healthy and only the first turn
fails, after the key has already left the machine. That is not hypothetical:
``${DEEPSEEK_API_KEY}`` expands to an empty string when the variable is unset
(settings.py), the empty string is falsy, and the fallback takes over.

So the env fallback is an ALLOWLIST: a host gets a key only if this table
names one for it, or if it is local (where an OpenAI-compatible server
typically accepts any key). Anything else must carry ``api_key:`` in its model
entry — a loud config error beats a secret sent to the wrong host.

The rule was built for ``openai_responses`` (commit 676021ac) and lived there
as a copy; this is the one implementation all OpenAI-compatible factories
share.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Optional
from urllib.parse import urlparse

#: hostname (or parent domain) -> environment variable that serves it.
#: Matched against the parsed hostname, never as a substring of the URL:
#: "openrouter.ai" appears in https://myproxy/openrouter.ai/v1 too, and this
#: function's whole job is deciding which secret goes to which host.
_HOST_KEYS = {
    "openrouter.ai": "OPENROUTER_API_KEY",
    "api.openai.com": "OPENAI_API_KEY",
}
#: Local endpoints (LM Studio, vLLM, llama.cpp, an Ollama box on the LAN)
#: speak the OpenAI wire format and usually ignore the key entirely. Keeping
#: the fallback here costs nothing: the secret does not leave the network.
_LOCAL_KEY_VAR = "OPENAI_API_KEY"


def _is_local(hostname: str) -> bool:
    # An IP decides on its own range — and it must be asked FIRST: an IPv6
    # address carries no dot, so the "no TLD" rule below would wave through
    # every public v6 endpoint. 100.64.0.0/10 (CGNAT/Tailscale) is not private
    # here on purpose — that space belongs to a carrier, not to us.
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return address.is_loopback or address.is_private
    # A name without a dot has no TLD, so it cannot be a public endpoint:
    # "localhost", a docker-compose service ("ollama", "vllm"), a k8s service,
    # a LAN hostname.
    return "." not in hostname or hostname == "host.docker.internal" \
        or hostname.endswith(".local")


def key_var_for(base_url: str) -> Optional[str]:
    """Environment variable that serves this endpoint, or None if none does."""
    hostname = (urlparse(base_url or "").hostname or "").lower()
    if not hostname:
        return None
    for host, var in _HOST_KEYS.items():
        if hostname == host or hostname.endswith("." + host):
            return var
    return _LOCAL_KEY_VAR if _is_local(hostname) else None


def resolve_api_key(
    api_key: Optional[str], base_url: Optional[str], *,
    default_base_url: str, provider: str,
) -> tuple[str, str]:
    """Return ``(api_key, effective_base_url)`` for an OpenAI-compatible client.

    An explicit ``api_key`` from the model entry always wins — only the env
    fallback follows the endpoint. Raises ValueError naming BOTH the variable
    that was looked for and the host it was meant for; "OPENAI_API_KEY is
    required" while the request goes to openrouter.ai is the confusing half.
    """
    effective_url = base_url or default_base_url
    if api_key:
        return api_key, effective_url
    wanted = key_var_for(effective_url)
    if wanted is None:
        raise ValueError(
            f"provider={provider} targets {effective_url}, and no environment "
            f"variable is configured for that host — set `api_key:` on the "
            f"model entry (e.g. api_key: ${{YOUR_PROVIDER_API_KEY}}). Falling "
            f"back to {_LOCAL_KEY_VAR} would send that secret there.")
    key = os.getenv(wanted)
    if not key:
        raise ValueError(
            f"{wanted} is required when provider={provider} targets "
            f"{effective_url}")
    return key, effective_url
