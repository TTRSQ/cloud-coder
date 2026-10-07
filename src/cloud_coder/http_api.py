"""HTTP API: the MCP server at /mcp (Streamable HTTP), behind bearer tokens.

/mcp takes the API's own tokens (read or write; read-only tools only with a read token)
and the access tokens of its OAuth authorization server, for clients such as ChatGPT that
cannot send a static header; see oauth.py. The server is built for one target VM, fixed by
the configuration it is created with. `cloud-coder api` serves it with uvicorn.
"""

import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass

import uvicorn
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from cloud_coder import mcp_server
from cloud_coder.config import Config, ConfigError
from cloud_coder.guards import READ, WRITE
from cloud_coder.oauth import AuthorizationServer, OAuthSettings

READ_TOKENS_ENV = "CLOUD_CODER_API_READ_TOKENS"
WRITE_TOKENS_ENV = "CLOUD_CODER_API_WRITE_TOKENS"


def _split_tokens(value: str | None) -> tuple[bytes, ...]:
    return tuple(t.strip().encode() for t in (value or "").split(",") if t.strip())


@dataclass(frozen=True)
class ApiTokens:
    """Bearer tokens per scope. Several per scope allow rotation; write implies read."""

    read: tuple[bytes, ...]
    write: tuple[bytes, ...]

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "ApiTokens":
        tokens = cls(
            read=_split_tokens(environ.get(READ_TOKENS_ENV)),
            write=_split_tokens(environ.get(WRITE_TOKENS_ENV)),
        )
        if not tokens.write:  # OAuth grants are approved with a write token
            raise ConfigError(
                f"no write token: set {WRITE_TOKENS_ENV} (comma-separated); "
                f"{READ_TOKENS_ENV} is optional"
            )
        return tokens

    def scopes(self, token: bytes) -> set[str]:
        # Compare against every token, without stopping at a match.
        is_read = [hmac.compare_digest(token, t) for t in self.read]
        is_write = [hmac.compare_digest(token, t) for t in self.write]
        if any(is_write):
            return {READ, WRITE}
        return {READ} if any(is_read) else set()


@dataclass(frozen=True)
class McpTokenVerifier:
    """The bearer tokens /mcp accepts: the API's own tokens, for clients that can send a
    header, and the access tokens of the OAuth authorization server, for those that cannot.
    """

    tokens: ApiTokens
    oauth: AuthorizationServer

    async def verify_token(self, token: str) -> AccessToken | None:
        granted = self.tokens.scopes(token.encode())
        if not granted:
            return await self.oauth.load_access_token(token)
        return AccessToken(
            token=token,
            client_id="api-token",
            scopes=sorted(granted),
            resource=self.oauth.settings.resource_url,
        )


def build_app(cfg: Config, tokens: ApiTokens, oauth: OAuthSettings) -> Starlette:
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    server = AuthorizationServer(oauth, tokens.write, scopes=(READ, WRITE))
    auth = AuthSettings(
        issuer_url=oauth.issuer_url,
        resource_server_url=AnyHttpUrl(oauth.resource_url),
        required_scopes=[READ],
        validate_token_resource=True,
    )
    mcp = mcp_server.build_server(cfg, auth=auth, token_verifier=McpTokenVerifier(tokens, server))
    # Stateless: Cloud Run may stop the instance between requests. No DNS rebinding
    # protection: it guards servers on localhost, and this one is public behind OAuth.
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            *server.routes(),
            Mount("/", app=mcp_app),
        ],
        lifespan=lambda app: mcp.session_manager.run(),
    )


def serve(cfg: Config, host: str, port: int) -> None:
    """Serve the API until interrupted. ConfigError without a write token or a public URL."""
    environ = os.environ
    app = build_app(cfg, ApiTokens.from_env(environ), OAuthSettings.from_env(environ))
    uvicorn.run(app, host=host, port=port)
