"""`agent-cli reload`: tell the running server to re-read its config.

Nothing of the system starts here -- the command is one ``POST
/admin/reload-config`` to the server (admin key required) and the report of
what it refreshed. Its own module because it shares nothing with the other
commands but the event loop it posts on.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from ..event_loop import run_async


def run_reload_command(args: Any, config: Any) -> None:
    """Post the reload and print the server's report; exit 1 when it failed."""
    try:
        import httpx
        from ...llm.tls import httpx_verify
    except ImportError:
        print(json.dumps({"error": "httpx library not installed",
                          "message": "Install with: pip install httpx"}, indent=2))
        sys.exit(1)

    base_url = (args.reload_url or os.environ.get("AGENT_SERVER_URL")
                or "http://127.0.0.1:8000").rstrip("/")
    api_key = (args.reload_api_key or os.environ.get("AGENT_ADMIN_API_KEY")
               or os.environ.get("AGENT_API_KEY"))
    timeout = args.timeout if args.timeout is not None else (
        config.network.cli_request_timeout if config and config.network else 30.0)
    url = f"{base_url}/admin/reload-config"
    headers = {"X-API-Key": api_key} if api_key else {}

    async def _do_reload():
        async with httpx.AsyncClient(timeout=timeout, verify=httpx_verify()) as client:
            return await client.post(url, headers=headers)

    try:
        resp = run_async(_do_reload())
    except httpx.ConnectError:
        print(json.dumps({"error": "cannot connect to server", "url": url,
                          "hint": "is the server running? set --url / AGENT_SERVER_URL"}, indent=2))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e), "url": url}, indent=2))
        sys.exit(1)

    if resp.status_code in (401, 403):
        print(json.dumps({"error": f"auth failed (HTTP {resp.status_code})",
                          "hint": "pass --api-key or set AGENT_ADMIN_API_KEY to an admin user's API key "
                                  "(a password change revokes it: generate a new one)"}, indent=2))
        sys.exit(1)
    if resp.status_code != 200:
        print(json.dumps({"error": f"server returned HTTP {resp.status_code}",
                          "body": resp.text[:500]}, indent=2))
        sys.exit(1)

    data = resp.json()
    report = data.get("report", {})
    # A server that failed to refresh makes the reload a failure (exit 1)
    # -- in both formats, so a script sees it.
    failed = bool(report.get("errors"))
    if args.out_format == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
        if failed:
            sys.exit(1)
        return

    refreshed = report.get("refreshed", [])
    print("\nConfig reload:")
    if not refreshed:
        print("  No live server changed (already up to date, or the change needs a restart).")
    for item in refreshed:
        changes = item.get("changes", {})
        print(f"  [ok] {item.get('server')}: {', '.join(sorted(changes.keys()))}")
        for field, ch in changes.items():
            print(f"       {field}: {ch.get('old')!r} -> {ch.get('new')!r}")
    unsupported = report.get("unsupported", [])
    if unsupported:
        print(f"  ({len(unsupported)} server(s) without hot-reload support — a new/changed "
              f"definition there needs a restart)")
    for err in report.get("errors", []):
        print(f"  [ERR] {err.get('server')}: {err.get('error')}")
    if failed:
        sys.exit(1)
