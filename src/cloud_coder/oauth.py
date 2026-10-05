"""OAuth 2.1 authorization server for the MCP endpoint of the HTTP API.

Clients that cannot send a static bearer token (ChatGPT apps, for one) reach /mcp with
OAuth instead: they register (RFC 7591), send the user to an approval page that asks for
an existing write token, and exchange the code (PKCE S256 only) for tokens.

Nothing is stored. Client IDs, authorization codes, access and refresh tokens are
self-contained: base64url JSON claims plus an HMAC-SHA256 over them, with a key derived
(HKDF) from the write token that approved the grant. So they survive restarts, and
removing a write token from the configuration revokes every grant it approved. Client
IDs are signed with the first write token's key.
"""

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from cloud_coder.config import ConfigError

PUBLIC_URL_ENV = "CLOUD_CODER_PUBLIC_URL"
REDIRECT_URIS_ENV = "CLOUD_CODER_OAUTH_REDIRECT_URIS"
# https://developers.openai.com/plugins/build/auth: the first for authorization servers
# that send `iss` with the code (RFC 9207, which this one does), the second otherwise.
CHATGPT_REDIRECT_URIS = (
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://chatgpt.com/connector/oauth/*",
)
MCP_PATH = "/mcp"
APPROVAL_PATH = "/authorize/approve"
METADATA_PATH = "/.well-known/oauth-authorization-server"
OFFLINE_ACCESS = "offline_access"
TOKEN_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")

ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
CODE_TTL = 300
APPROVAL_TTL = 600
MAX_FAILED_APPROVALS = 10
FAILED_APPROVAL_WINDOW = 600

HKDF_SALT = b"cloud-coder oauth v1"

_URL_AS_IS = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))


@dataclass(frozen=True)
class OAuthSettings:
    """Where the API is reachable from outside, and which redirect URIs clients may use."""

    public_url: str
    redirect_uris: tuple[str, ...] = CHATGPT_REDIRECT_URIS

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "OAuthSettings | None":
        """None (no /mcp) unless CLOUD_CODER_PUBLIC_URL is set."""
        public_url = environ.get(PUBLIC_URL_ENV, "").strip().rstrip("/")
        if not public_url:
            return None
        url = urlparse(public_url)
        local = url.hostname in ("localhost", "127.0.0.1")
        if not (url.scheme == "https" or (url.scheme == "http" and local)) or not url.netloc:
            raise ConfigError(f"{PUBLIC_URL_ENV} must be an https URL, got {public_url!r}")
        if url.path or url.query or url.fragment:
            raise ConfigError(f"{PUBLIC_URL_ENV} must have no path, query or fragment")
        patterns = tuple(
            p.strip() for p in environ.get(REDIRECT_URIS_ENV, "").split(",") if p.strip()
        )
        return cls(public_url, patterns or CHATGPT_REDIRECT_URIS)

    @property
    def resource_url(self) -> str:
        return self.public_url + MCP_PATH

    @property
    def issuer_url(self) -> AnyHttpUrl:
        """The public URL as is: issuers compare as strings, so no trailing slash added."""
        return _URL_AS_IS.validate_python(self.public_url)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(secret: bytes, info: str) -> bytes:
    """HKDF-SHA256 (RFC 5869) with one 32-byte output block."""
    prk = hmac.new(HKDF_SALT, secret, hashlib.sha256).digest()
    return hmac.new(prk, info.encode() + b"\x01", hashlib.sha256).digest()


class SignedTokens:
    """Self-contained tokens: claims and an HMAC keyed by one of the write tokens.

    Every kind of token has its own key, so a token of one kind is never valid as another.
    """

    KINDS = ("client", "client_secret", "approval", "code", "access", "refresh")

    def __init__(self, write_tokens: tuple[bytes, ...]):
        if not write_tokens:
            raise ConfigError("OAuth needs a write token to approve grants with")
        self._keys = [{kind: _hkdf(t, kind) for kind in self.KINDS} for t in write_tokens]

    def _mac(self, signer: int, kind: str, body: str) -> bytes:
        return hmac.new(self._keys[signer][kind], body.encode(), hashlib.sha256).digest()

    def seal(self, kind: str, claims: dict[str, Any], ttl: int | None, signer: int = 0) -> str:
        if ttl is not None:
            claims = {**claims, "exp": int(time.time()) + ttl}
        body = _b64encode(json.dumps(claims, separators=(",", ":")).encode())
        return f"{body}.{_b64encode(self._mac(signer, kind, body))}"

    def open(self, kind: str, token: str) -> tuple[dict[str, Any], int] | None:
        """The claims and the signer of a valid, unexpired token; None otherwise."""
        body, _, mac = token.partition(".")
        try:
            mac_bytes = _b64decode(mac)
        except ValueError:
            return None
        signers = [
            i
            for i in range(len(self._keys))
            if hmac.compare_digest(self._mac(i, kind, body), mac_bytes)
        ]
        if not signers:
            return None
        claims = json.loads(_b64decode(body))
        if "exp" in claims and claims["exp"] < time.time():
            return None
        return claims, signers[0]

    def derive(self, kind: str, value: str, signer: int) -> str:
        """A secret that only the holder of the signer's key can compute from ``value``."""
        return _b64encode(self._mac(signer, kind, value))


class SignedAuthorizationCode(AuthorizationCode):
    signer: int


class SignedRefreshToken(RefreshToken):
    signer: int


class FailedAttempts:
    """Refuses further attempts after too many failures within a sliding window.

    In memory: Cloud Run runs at most one instance, and a restart only resets the count.
    """

    def __init__(self, limit: int = MAX_FAILED_APPROVALS, window: int = FAILED_APPROVAL_WINDOW):
        self.limit = limit
        self.window = window
        self._times: deque[float] = deque()

    def _prune(self) -> None:
        while self._times and self._times[0] < time.monotonic() - self.window:
            self._times.popleft()

    def blocked(self) -> bool:
        self._prune()
        return len(self._times) >= self.limit

    def record(self) -> None:
        self._times.append(time.monotonic())


def _redirect_uri_matcher(patterns: tuple[str, ...]) -> re.Pattern[str]:
    """Exact URIs; ``*`` stands for one non-empty path segment."""
    alternatives = (re.escape(p).replace(r"\*", r"[^/?#]+") for p in patterns)
    return re.compile("|".join(f"(?:{a})" for a in alternatives))


class AuthorizationServer:
    """The OAuth provider (``OAuthAuthorizationServerProvider``) for /mcp. Approving a
    grant takes one of the API's write tokens, and every grant carries ``scopes``, the
    scopes of a write token.
    """

    def __init__(
        self, settings: OAuthSettings, write_tokens: tuple[bytes, ...], scopes: tuple[str, ...]
    ):
        self.settings = settings
        self.scopes = scopes
        self._write_tokens = write_tokens
        self._signed = SignedTokens(write_tokens)
        self._redirect_uris = _redirect_uri_matcher(settings.redirect_uris)
        self._used_codes: dict[str, float] = {}
        self.failed_approvals = FailedAttempts()

    # --- clients (RFC 7591) --------------------------------------------------------

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or not all(self._redirect_uris.fullmatch(u) for u in uris):
            raise RegistrationError(
                "invalid_redirect_uri",
                "redirect_uris must match: " + ", ".join(self.settings.redirect_uris),
            )
        if client_info.token_endpoint_auth_method not in TOKEN_AUTH_METHODS:
            raise RegistrationError(
                "invalid_client_metadata",
                "token_endpoint_auth_method must be one of " + ", ".join(TOKEN_AUTH_METHODS),
            )
        registered = client_info.model_dump(
            mode="json",
            include={
                "redirect_uris",
                "token_endpoint_auth_method",
                "grant_types",
                "response_types",
                "scope",
                "client_name",
            },
            exclude_none=True,
        )
        # The registration handler answers with this object: replace the random client_id
        # (and secret) it minted with ones that need no storage.
        client_info.client_id = self._signed.seal("client", registered, ttl=None)
        if client_info.client_secret is not None:
            client_info.client_secret = self._signed.derive(
                "client_secret", client_info.client_id, signer=0
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        opened = self._signed.open("client", client_id)
        if opened is None:
            return None
        registered, signer = opened
        secret = None
        if registered.get("token_endpoint_auth_method") != "none":
            secret = self._signed.derive("client_secret", client_id, signer)
        return OAuthClientInformationFull(
            **registered, client_id=client_id, client_secret=secret, client_secret_expires_at=0
        )

    # --- authorization ---------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        # RFC 8707: ChatGPT names this server by the URL the user entered (…/mcp), but
        # accept the base URL as well; the token is bound to the /mcp URL either way.
        resource = (params.resource or "").rstrip("/")
        if resource not in ("", self.settings.resource_url, self.settings.public_url):
            raise AuthorizeError("invalid_target", "unknown resource")
        request = {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "state": params.state,
            "scopes": params.scopes,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
        }
        sealed = self._signed.seal("approval", request, ttl=APPROVAL_TTL)
        return f"{self.settings.public_url}{APPROVAL_PATH}?{urlencode({'request': sealed})}"

    def approve(self, request: dict[str, Any], write_token: str) -> str | None:
        """The redirect back to the client, with a code, when ``write_token`` is one of
        the write tokens; None otherwise. ``request`` is an opened approval request."""
        # Compare against every token, without stopping at a match.
        matches = [hmac.compare_digest(write_token.encode(), t) for t in self._write_tokens]
        if not any(matches):
            return None
        code = self._signed.seal(
            "code",
            {
                "client_id": request["client_id"],
                "scopes": sorted({*(request["scopes"] or []), *self.scopes}),
                "code_challenge": request["code_challenge"],
                "redirect_uri": request["redirect_uri"],
                "redirect_uri_provided_explicitly": request["redirect_uri_provided_explicitly"],
                "jti": secrets.token_urlsafe(16),
            },
            ttl=CODE_TTL,
            signer=matches.index(True),
        )
        return construct_redirect_uri(
            request["redirect_uri"],
            code=code,
            state=request["state"],
            iss=self.settings.public_url,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> SignedAuthorizationCode | None:
        opened = self._signed.open("code", authorization_code)
        if opened is None:
            return None
        claims, signer = opened
        # Codes are single use, best effort: a restart forgets the used ones, but a code
        # lives only CODE_TTL seconds and needs the client's PKCE verifier as well.
        now = time.time()
        self._used_codes = {jti: exp for jti, exp in self._used_codes.items() if exp > now}
        if claims["jti"] in self._used_codes:
            return None
        self._used_codes[claims["jti"]] = claims["exp"]
        return SignedAuthorizationCode(
            code=authorization_code,
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            client_id=claims["client_id"],
            code_challenge=claims["code_challenge"],
            redirect_uri=claims["redirect_uri"],
            redirect_uri_provided_explicitly=claims["redirect_uri_provided_explicitly"],
            resource=self.settings.resource_url,
            signer=signer,
        )

    # --- tokens ----------------------------------------------------------------------

    def _issue(self, client: OAuthClientInformationFull, scopes: list[str], signer: int):
        claims = {"client_id": client.client_id, "scopes": scopes}
        access = self._signed.seal(
            "access", {**claims, "aud": self.settings.resource_url}, ACCESS_TOKEN_TTL, signer
        )
        refresh = None
        if "refresh_token" in client.grant_types:
            refresh = self._signed.seal("refresh", claims, REFRESH_TOKEN_TTL, signer)
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: SignedAuthorizationCode
    ) -> OAuthToken:
        return self._issue(client, authorization_code.scopes, authorization_code.signer)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> SignedRefreshToken | None:
        opened = self._signed.open("refresh", refresh_token)
        if opened is None:
            return None
        claims, signer = opened
        return SignedRefreshToken(
            token=refresh_token,
            client_id=claims["client_id"],
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            resource=self.settings.resource_url,
            signer=signer,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: SignedRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return self._issue(client, scopes, refresh_token.signer)

    async def load_access_token(self, token: str) -> AccessToken | None:
        opened = self._signed.open("access", token)
        if opened is None:
            return None
        claims, _ = opened
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            resource=claims["aud"],
        )

    # --- routes ----------------------------------------------------------------------

    def routes(self) -> list[Route]:
        """Metadata (RFC 8414), /authorize, /token, /register and the approval page."""
        issuer = self.settings.issuer_url
        registration = ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[*self.scopes, OFFLINE_ACCESS],
            default_scopes=[*self.scopes, OFFLINE_ACCESS],
        )
        revocation = RevocationOptions(enabled=False)  # nothing to revoke without storage
        metadata = build_metadata(issuer, None, registration, revocation)
        # The SDK advertises only the secret-based methods; ChatGPT registers public clients.
        metadata.token_endpoint_auth_methods_supported = list(TOKEN_AUTH_METHODS)
        metadata.authorization_response_iss_parameter_supported = True
        sdk_routes = [
            r
            for r in create_auth_routes(self, issuer, None, registration, revocation)
            if r.path != METADATA_PATH
        ]
        return [
            Route(
                METADATA_PATH,
                endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            ),
            *sdk_routes,
            Route(APPROVAL_PATH, self._approval_page, methods=["GET", "POST"]),
        ]

    async def _approval_page(self, request: Request) -> Response:
        # No CSRF token: approving takes the write token itself, which the page never has.
        posted = request.method == "POST"
        if posted and self.failed_approvals.blocked():
            return _page(_TOO_MANY, 429)
        params = await request.form() if posted else request.query_params
        sealed = str(params.get("request", ""))
        opened = self._signed.open("approval", sealed)
        if opened is None:
            return _page(_EXPIRED, 400)
        approval_request, _ = opened
        if not posted:
            return _page(_form(sealed, approval_request, error=None))
        redirect = self.approve(approval_request, str(params.get("token", "")))
        if redirect is None:
            self.failed_approvals.record()
            return _page(_form(sealed, approval_request, error="That is not a write token."), 401)
        return RedirectResponse(redirect, status_code=303, headers={"Cache-Control": "no-store"})


_EXPIRED = "<p>This authorization request is invalid or has expired. Start again from the app.</p>"
_TOO_MANY = "<p>Too many wrong tokens. Try again in a few minutes.</p>"


def _form(sealed: str, request: dict[str, Any], error: str | None) -> str:
    client = html.escape(request.get("client_name") or "An application")
    redirect_host = html.escape(urlparse(request["redirect_uri"]).netloc)
    problem = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""
<p><b>{client}</b> ({redirect_host}) asks for full access to this cloud-coder worker:
start and stop the VM, open sessions and send prompts to Claude Code.</p>
<p>Paste a write token of the cloud-coder API to allow it.</p>
{problem}
<form method="post">
  <input type="hidden" name="request" value="{html.escape(sealed)}">
  <input type="password" name="token" autocomplete="off" required autofocus>
  <button type="submit">Allow</button>
</form>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    page = f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cloud-coder authorization</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 2rem auto; padding: 0 1rem; }}
input[type=password] {{ width: 100%; padding: .5rem; margin: .5rem 0; box-sizing: border-box; }}
.error {{ color: #b00020; }}
</style></head>
<body><h1>cloud-coder</h1>{body}</body></html>"""
    return HTMLResponse(
        page,
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
        },
    )
