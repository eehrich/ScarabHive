"""OAuth for a remote MCP server (``auth: {type: oauth}``): the SDK's authorization-code flow with PKCE.

The SDK (``mcp.client.auth.OAuthClientProvider``) does discovery, dynamic client registration, the token
exchange and the refresh. What it leaves to the client, and what lives here:

* where the tokens are kept -- a file per server in the data directory, readable by this user only;
* how a person reaches the authorization page and how its answer comes back. This process runs as a
  server, so nothing opens a browser: the authorization URL is handed to whoever asked to authorize (the
  plugin's ``<instance>_authorize`` tool), and the authorization server sends the browser back to this app's own
  callback route (``/plugins/<instance>/oauth/callback``), which hands the code to the waiting flow.

A connect that is not such an authorization -- at startup, on demand, after a crash -- never waits for a
person: without tokens that work (or refresh) it fails at once and says to authorize.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from agent_system.paths import data_path

logger = logging.getLogger(__name__)

#: How long an authorization waits for the person to finish it in the browser.
AUTHORIZE_WAIT_S = 600.0
#: How often a waiting authorization looks for the browser's answer.
POLL_S = 1.0


class AuthorizationRequired(RuntimeError):
    """The server needs a person to authorize first; raised by a connect that may not wait for one."""


def _tokens_dir() -> Path:
    return data_path("mcp_client", "oauth")


class FileTokenStorage:
    """The SDK's TokenStorage, one JSON file per server: the tokens and the registered client.

    A ``client_id`` set in the config wins over a registered one: a server without dynamic registration
    (GitHub) is used with an OAuth app made by hand.
    """

    def __init__(self, server: str, config: Any, redirect_uri: str, directory: Optional[Path] = None) -> None:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in server)
        self.path = (directory or _tokens_dir()) / f"{safe}.json"
        self.config = config
        self.redirect_uri = redirect_uri

    def _read(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning("MCP OAuth: %s is unreadable (%s); authorize again", self.path, e)
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A temporary name of its own (two processes may refresh at once), owner only from the
        # first byte on POSIX (mkstemp): the file holds a refresh token.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.stem, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        data["tokens_saved_at"] = time.time()
        self._write(data)

    def expiry(self) -> Optional[float]:
        """When the stored access token runs out (epoch seconds); None if unknown."""
        data = self._read()
        expires_in = (data.get("tokens") or {}).get("expires_in")
        saved_at = data.get("tokens_saved_at")
        return float(saved_at) + float(expires_in) if expires_in is not None and saved_at else None

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        client_id = getattr(self.config, "client_id", None)
        if client_id:
            secret = getattr(self.config, "client_secret", None)
            return OAuthClientInformationFull(
                client_id=client_id, client_secret=secret or None,
                redirect_uris=[self.redirect_uri],
                token_endpoint_auth_method="client_secret_post" if secret else "none")
        raw = self._read().get("client_info")
        # Kept even when registered for another callback address: the refresh grant needs the
        # client, not the address. Only an authorization needs the address (see the redirect handler).
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    def registered_for_another_address(self, info: Any) -> bool:
        """A client registered (by us) for a callback address that is not this one any more."""
        if info is None or getattr(self.config, "client_id", None):
            return False
        return self.redirect_uri not in [str(uri) for uri in getattr(info, "redirect_uris", None) or []]

    def drop_client_info(self) -> None:
        data = self._read()
        if data.pop("client_info", None) is not None:
            self._write(data)

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


def _read_answer(path: Path) -> Optional[Dict[str, Any]]:
    """The browser's answer once it is there and whole; None before (the callback writes it after
    creating the file -- a half-written one is read again on the next poll)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _provider_class():
    """The SDK's provider, told when stored tokens run out.

    It loads stored tokens without their expiry and takes them for valid: the expired one went out, came
    back 401, and the SDK started a whole new authorization -- a person in a browser -- instead of the
    refresh grant it would have used within one process. Measured with the OAuth probe (a 2 s token, a
    restart): without this, every restart after the token ran out asked for authorization again.
    """
    from mcp.client.auth import OAuthClientProvider

    class Provider(OAuthClientProvider):
        async def _initialize(self) -> None:
            # ponytail: overrides a private SDK hook (mcp 2.2); the probe test fails if it is renamed
            await super()._initialize()
            if self.context.current_tokens is not None and self.context.token_expiry_time is None:
                self.context.token_expiry_time = self.context.storage.expiry()

    return Provider


class OAuthBroker:
    """The authorizations waiting for a person, by the ``state`` their URL carries.

    A waiting authorization and the browser's answer meet in files, not in memory: the authorization may
    run in another process than the app whose callback route the browser reaches (agent-cli has its own
    connections). ``<state>.wait`` says an authorization waits; the callback writes ``<state>.answer``
    beside it, which the waiting flow picks up.
    """

    def __init__(self, directory: Optional[Path] = None, authorize_tool: str = "mcp_client_authorize") -> None:
        #: Where the tokens and the waiting authorizations are kept; None: the data directory's.
        self._directory = directory
        #: The tool a person calls to authorize (named after the plugin instance).
        self.authorize_tool = authorize_tool
        #: server -> the URL its running authorization waits on (for the tool to hand out).
        self.urls: Dict[str, str] = {}
        self._url_events: Dict[str, asyncio.Event] = {}

    def _file(self, state: str, suffix: str) -> Path:
        name = hashlib.sha256(state.encode()).hexdigest()[:32]
        return (self._directory or _tokens_dir()) / "pending" / f"{name}.{suffix}"

    def url_event(self, server: str) -> asyncio.Event:
        return self._url_events.setdefault(server, asyncio.Event())

    def provider(self, server: str, config: Any, redirect_uri: str, *, interactive: bool,
                 storage: Optional[FileTokenStorage] = None):
        """An ``httpx2.Auth`` for *server*. *interactive*: its handshake may wait for a person; the
        connection sets ``provider.interactive`` False once it stands -- a refresh that fails later
        must not hold every call of the connection for a browser nobody was sent to."""
        from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

        url = getattr(config, "url", "")
        auth = getattr(config, "auth", None)
        storage = storage or FileTokenStorage(server, auth, redirect_uri, self._directory)
        metadata = OAuthClientMetadata(
            client_name="ScarabHive", redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="client_secret_post" if getattr(auth, "client_secret", None) else "none")
        expected: Dict[str, str] = {}

        async def redirect(authorization_url: str) -> None:
            if not provider.interactive:
                raise AuthorizationRequired(
                    f"MCP server '{server}' needs authorization: call {self.authorize_tool} with server "
                    f"'{server}' and open the link it gives in a browser")
            if storage.registered_for_another_address(provider.context.client_info):
                # redirect_uri or the port changed since the registration: the authorization server
                # would refuse this address. Forgotten here, the next authorization registers anew.
                storage.drop_client_info()
                raise AuthorizationRequired(
                    f"MCP server '{server}': this app was registered there for another callback address; "
                    f"the registration is dropped -- call {self.authorize_tool} again")
            state = (parse_qs(urlsplit(authorization_url).query).get("state") or [""])[0]
            if not state:
                raise AuthorizationRequired(f"MCP server '{server}': the authorization URL carries no state")
            expected["state"] = state
            wait = self._file(state, "wait")
            wait.parent.mkdir(parents=True, exist_ok=True)
            wait.write_text(server, encoding="utf-8")
            self.urls[server] = authorization_url
            self.url_event(server).set()
            logger.info("MCP server '%s' waits for authorization in a browser", server)

        async def callback() -> AuthorizationCodeResult:
            state = expected.get("state", "")
            wait, answer = self._file(state, "wait"), self._file(state, "answer")
            deadline = time.monotonic() + AUTHORIZE_WAIT_S
            try:
                data = _read_answer(answer)
                while data is None:
                    if time.monotonic() > deadline:
                        raise AuthorizationRequired(
                            f"MCP server '{server}': the authorization was not finished within "
                            f"{AUTHORIZE_WAIT_S:.0f}s; call {self.authorize_tool} again")
                    await asyncio.sleep(POLL_S)
                    data = _read_answer(answer)
            finally:
                for path in (wait, answer):
                    path.unlink(missing_ok=True)
                self.urls.pop(server, None)
            if data.get("error"):
                raise AuthorizationRequired(f"MCP server '{server}': the authorization was refused: {data['error']}")
            return AuthorizationCodeResult(code=data["code"], state=state, iss=data.get("iss"))

        provider = _provider_class()(server_url=url, client_metadata=metadata, storage=storage,
                                     redirect_handler=redirect, callback_handler=callback)
        provider.interactive = interactive
        return provider

    def _answer(self, state: str, data: Dict[str, Any]) -> bool:
        if not state or not self._file(state, "wait").exists():
            return False
        answer = self._file(state, "answer")
        try:
            fd = os.open(answer, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False                    # answered already: the first answer counts
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return True

    def complete(self, state: str, code: str, iss: Optional[str] = None) -> bool:
        """The browser came back with *code* for *state*: False if no authorization waits for it."""
        return self._answer(state, {"code": code, "iss": iss})

    def fail(self, state: str, error: str) -> bool:
        """The authorization server refused (``error=access_denied``...)."""
        return self._answer(state, {"error": error})
