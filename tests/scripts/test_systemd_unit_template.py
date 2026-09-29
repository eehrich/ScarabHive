"""The systemd unit template runs the server unprivileged and on loopback, and names no network.

It ran as root from /root, bound uvicorn to a LAN address on port 80 -- a
network map in a public file, and the login, the API and the agents' tools
published without TLS. Read the way systemd reads it: Key=Value lines of the
[Service] section, repeated keys kept.
"""
from __future__ import annotations

import ipaddress
import re
import shlex
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "systemd" / "agent-system.service.template"


def _service() -> dict[str, list[str]]:
    section, found = None, {}
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line
        elif section == "[Service]" and "=" in line:
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def test_the_service_runs_as_an_unprivileged_user_outside_root_s_home():
    service = _service()
    assert service.get("User") and service["User"][-1] not in ("root", "0"), service.get("User")
    assert not service["WorkingDirectory"][-1].startswith("/root"), service["WorkingDirectory"]


def test_the_server_listens_on_loopback_only():
    [command] = service_exec = _service()["ExecStart"]
    words = shlex.split(command)
    assert "--host" in words, service_exec
    assert ipaddress.ip_address(words[words.index("--host") + 1]).is_loopback, command


def test_the_template_names_no_private_address_and_keeps_secrets_out():
    text = TEMPLATE.read_text(encoding="utf-8")
    private = [candidate for candidate in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
               if ipaddress.ip_address(candidate).is_private and not ipaddress.ip_address(candidate).is_loopback]
    assert private == []
    assert _service().get("EnvironmentFile"), "secrets need a place outside the unit and the checkout"
