"""HTTP API: the MCP server at /mcp (Streamable HTTP), behind OAuth.

/mcp takes only the access tokens of the API's own OAuth authorization server (see
oauth.py), with read-only tools for the read scope and every tool for the write scope. The
server is built for one target VM, fixed by the configuration it is created with.
`cloud-coder api` serves it with uvicorn.
"""

import os

import httpx2
import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from cloud_coder import mcp_server
from cloud_coder.config import Config
from cloud_coder.guards import READ
from cloud_coder.oauth import AuthorizationServer, OAuthSettings


def build_app(
    cfg: Config, oauth: OAuthSettings, google_transport: httpx2.AsyncBaseTransport | None = None
) -> Starlette:
    """``google_transport`` replaces the network to Google (tests)."""

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    server = AuthorizationServer(oauth, google_transport)
    auth = AuthSettings(
        issuer_url=oauth.issuer_url,
        resource_server_url=AnyHttpUrl(oauth.resource_url),
        required_scopes=[READ],
        validate_token_resource=True,
    )
    mcp = mcp_server.build_server(cfg, auth=auth, token_verifier=server)
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
    """Serve the API until interrupted. ConfigError when the OAuth configuration is missing."""
    uvicorn.run(build_app(cfg, OAuthSettings.from_env(os.environ)), host=host, port=port)
