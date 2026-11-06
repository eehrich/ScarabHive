"""A real MCP server behind real OAuth, for the client's authorization tests: ``python oauth_probe_server.py PORT``.

The SDK's own authorization server routes (discovery, dynamic registration, authorize, token, refresh)
with an in-memory provider that approves every request at once -- the browser step is the test's: it
fetches the authorization URL and reads the code off the redirect, the way a person's browser would
carry it to the callback. Tokens live ACCESS_TTL seconds, so a test can watch a refresh.
"""
import secrets
import sys
import time

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken, construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthToken

ACCESS_TTL = 2


class Provider:
    def __init__(self):
        self.clients, self.codes, self.access, self.refresh = {}, {}, {}, {}
        self.refreshed = 0

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, code):
        return self.codes.get(code)

    def _issue(self, client_id, scopes, resource):
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                          expires_at=int(time.time()) + ACCESS_TTL, resource=resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, refresh_token=refresh,
                          scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, code):
        self.codes.pop(code.code, None)
        return self._issue(client.client_id, code.scopes, code.resource)

    async def load_refresh_token(self, client, refresh_token):
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.refresh.pop(refresh_token.token, None)
        self.refreshed += 1
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token):
        found = self.access.get(token)
        if found is None or (found.expires_at and found.expires_at < time.time()):
            return None
        return found

    async def revoke_token(self, token):
        self.access.pop(getattr(token, "token", ""), None)


PROVIDER = Provider()


def main(port: int) -> None:
    base = f"http://127.0.0.1:{port}"
    server = MCPServer("oauth-probe", auth_server_provider=PROVIDER, auth=AuthSettings(
        issuer_url=base, resource_server_url=f"{base}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True)))

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool()
    def refreshes() -> int:
        """How many refresh grants the server answered."""
        return PROVIDER.refreshed

    server.run(transport="streamable-http", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main(int(sys.argv[1]))
