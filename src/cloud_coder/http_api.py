"""HTTP API: the worker VM and its Claude Code sessions as a small REST API.

Like the MCP server, it is built for one target VM, fixed by the configuration it is
created with, and returns quickly: starting or stopping the VM is only requested, and
the caller polls. Every route under /v1 needs a bearer token; GET routes accept a read
or write token, the others only a write token. With a public URL configured it also serves
the MCP server at /mcp (Streamable HTTP) for OAuth clients such as ChatGPT; see oauth.py.
`cloud-coder api` serves it with uvicorn.
"""

import functools
import hmac
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

import uvicorn
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from cloud_coder import connect, gce, guards, mcp_server
from cloud_coder.config import Config, ConfigError
from cloud_coder.guards import READ, WRITE
from cloud_coder.oauth import AuthorizationServer, OAuthSettings

READ_TOKENS_ENV = "CLOUD_CODER_API_READ_TOKENS"
WRITE_TOKENS_ENV = "CLOUD_CODER_API_WRITE_TOKENS"
RETRY_AFTER_SECONDS = 10
MAX_LINES = 2000


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
        if not tokens.read and not tokens.write:
            raise ConfigError(
                f"no API token: set {READ_TOKENS_ENV} and/or {WRITE_TOKENS_ENV} (comma-separated)"
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


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: Mapping[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.headers = dict(headers or {})


def _bearer_challenge(error: str | None = None, scope: str | None = None) -> dict[str, str]:
    params = ['realm="cloud-coder"']
    if error:
        params.append(f'error="{error}"')
    if scope:
        params.append(f'scope="{scope}"')
    return {"WWW-Authenticate": "Bearer " + ", ".join(params)}


def authorize(request: Request, tokens: ApiTokens, scope: str) -> None:
    """ApiError 401 without a valid bearer token, 403 when it lacks ``scope``."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "a bearer token is required", _bearer_challenge())
    granted = tokens.scopes(token.encode())
    if not granted:
        raise ApiError(401, "invalid token", _bearer_challenge("invalid_token"))
    if scope not in granted:
        raise ApiError(
            403,
            f"this token lacks the {scope} scope",
            _bearer_challenge("insufficient_scope", scope),
        )


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    repo: str | None = None
    new: bool = False
    session: str | None = None
    # An empty prompt is the client's mistake; without this the VM agent would report it.
    prompt: str | None = Field(default=None, pattern=r"\S")


class PromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(pattern=r"\S")


async def _body[M: BaseModel](request: Request, model: type[M]) -> M:
    try:
        data = await request.json()
    except ValueError as e:
        raise ApiError(422, "the request body must be JSON") from e
    return model.model_validate(data)


def _lines(request: Request) -> int:
    raw = request.query_params.get("lines", "200")
    try:
        lines = int(raw)
    except ValueError:
        lines = 0
    if not 1 <= lines <= MAX_LINES:
        raise ApiError(422, f"lines must be an integer from 1 to {MAX_LINES}")
    return lines


def _error(status: int, message: str, headers: Mapping[str, str] | None = None, **extra: Any):
    return JSONResponse({"error": message, **extra}, status_code=status, headers=headers)


def _agent_error_status(message: str) -> int:
    """The VM agent reports failures as text; tell the client's mistakes from ours."""
    if message.startswith("unknown session"):
        return 404
    if message.endswith("prompt not sent"):  # Claude Code is BUSY or its state unknown
        return 409
    if "needs a repository" in message:  # a prompt (or --new) with neither repo nor session
        return 422
    return 502


async def _on_error(request: Request, exc: Exception) -> JSONResponse:
    match exc:
        case ApiError():
            return _error(exc.status, str(exc), exc.headers)
        case ValidationError():
            details = json.loads(exc.json(include_url=False, include_input=False))
            return _error(422, "invalid request", details=details)
        case guards.PromptRejected():
            return _error(422, str(exc))
        case guards.VmNotReady():
            return _error(
                503,
                f"{exc}; retry, or poll POST /v1/vm/start until ready is true",
                {"Retry-After": str(RETRY_AFTER_SECONDS)},
                vm_action=exc.vm_action,
            )
        case guards.VmNotRunning():
            return _error(409, f"{exc}; this request does not start it", vm=exc.vm_status)
        case connect.AgentError():
            return _error(_agent_error_status(str(exc)), str(exc))
        case gce.GcloudError() | RuntimeError() | TimeoutError() | OSError():
            return _error(502, str(exc))
        case HTTPException():
            return _error(exc.status_code, exc.detail, exc.headers)
    return _error(500, "internal error")


HANDLED_ERRORS = (
    ApiError,
    ValidationError,
    guards.PromptRejected,
    guards.VmNotReady,
    guards.VmNotRunning,
    connect.AgentError,
    gce.GcloudError,
    RuntimeError,
    TimeoutError,
    OSError,
    HTTPException,
)

Endpoint = Callable[[Request], Awaitable[JSONResponse]]


def build_app(cfg: Config, tokens: ApiTokens, oauth: OAuthSettings | None = None) -> Starlette:
    def requires(scope: str) -> Callable[[Endpoint], Endpoint]:
        def decorate(endpoint: Endpoint) -> Endpoint:
            @functools.wraps(endpoint)
            async def checked(request: Request) -> JSONResponse:
                authorize(request, tokens, scope)
                return await endpoint(request)

            return checked

        return decorate

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @requires(READ)
    async def status(request: Request) -> JSONResponse:
        return JSONResponse(await run_in_threadpool(connect.status, cfg))

    @requires(WRITE)
    async def start_vm(request: Request) -> JSONResponse:
        result = await run_in_threadpool(guards.up_now, cfg)
        return JSONResponse(asdict(result), status_code=202)

    @requires(WRITE)
    async def stop_vm(request: Request) -> JSONResponse:
        vm = await run_in_threadpool(gce.stop, cfg, wait=False)
        return JSONResponse({"vm": vm}, status_code=202)

    @requires(READ)
    async def list_sessions(request: Request) -> JSONResponse:
        st = await run_in_threadpool(connect.status, cfg)
        if st["vm"] != gce.RUNNING:
            raise guards.VmNotRunning(st["vm"])
        if "sessions" not in st:
            raise connect.AgentError(st.get("agent", "the VM agent reported no sessions"))
        return JSONResponse({"sessions": st["sessions"]})

    def launch(repo: str | None, **kw: Any) -> dict:
        guards.require_ready(cfg)
        return connect.launch(cfg, repo, forward_agent=False, **kw)

    @requires(WRITE)
    async def start_session(request: Request) -> JSONResponse:
        body = await _body(request, SessionRequest)
        if body.prompt is not None:
            guards.checked_prompt(body.prompt)
        result = await run_in_threadpool(
            launch, body.repo, new=body.new, session=body.session, prompt=body.prompt
        )
        return JSONResponse(result)

    @requires(WRITE)
    async def send_prompt(request: Request) -> JSONResponse:
        body = await _body(request, PromptRequest)
        guards.checked_prompt(body.text)
        result = await run_in_threadpool(
            launch, None, session=request.path_params["name"], prompt=body.text
        )
        return JSONResponse(result)

    @requires(READ)
    async def read_session(request: Request) -> JSONResponse:
        lines = _lines(request)

        def read() -> dict:
            guards.require_running(cfg)
            return connect.read_session(cfg, request.path_params["name"], lines)

        return JSONResponse(await run_in_threadpool(read))

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/v1/status", status, methods=["GET"]),
        Route("/v1/vm/start", start_vm, methods=["POST"]),
        Route("/v1/vm/stop", stop_vm, methods=["POST"]),
        Route("/v1/sessions", list_sessions, methods=["GET"]),
        Route("/v1/sessions", start_session, methods=["POST"]),
        Route("/v1/sessions/{name}", read_session, methods=["GET"]),
        Route("/v1/sessions/{name}/prompts", send_prompt, methods=["POST"]),
    ]
    handlers = {error: _on_error for error in HANDLED_ERRORS}
    handlers[Exception] = _on_error
    if oauth is None:
        return Starlette(routes=routes, exception_handlers=handlers)

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
        routes=[*routes, *server.routes(), Mount("/", app=mcp_app)],
        exception_handlers=handlers,
        lifespan=lambda app: mcp.session_manager.run(),
    )


def serve(cfg: Config, host: str, port: int) -> None:
    """Serve the API until interrupted. ConfigError when no token is configured, or when
    a public URL is configured without a write token."""
    environ = os.environ
    app = build_app(cfg, ApiTokens.from_env(environ), OAuthSettings.from_env(environ))
    uvicorn.run(app, host=host, port=port)
