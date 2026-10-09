"""OAuth 2.1 authorization server for the MCP endpoint of the HTTP API.

There is one client, configured in advance (no Dynamic Client Registration): ChatGPT,
with a client secret (``client_secret_post``) and the redirect URI
https://chatgpt.com/connector_platform_oauth_redirect. A grant is approved by a person:
the consent page names the client, its redirect URI and the scopes, and "Continue with
Google" signs the person in with Google (OpenID Connect, code flow with PKCE and nonce).
Only a Google account whose ``sub`` is on the allowlist gets an authorization code, which
the client exchanges (PKCE S256 and its secret) for tokens. Google is used only to find
out who approves; its tokens never leave this server, and /mcp accepts only the access
tokens issued here (``aud`` = <public URL>/mcp).

Nothing is stored. Authorization requests, the Google ``state``, codes, access and refresh
tokens are self-contained: base64url JSON claims plus an HMAC-SHA256 over them, with keys
derived (HKDF) from the OAuth signing keys. The first key signs; every key verifies, so a
key can be rotated without breaking grants. Every grant carries the approver's ``sub`` and
the time of approval: it ends GRANT_LIFETIME after approval, and taking the ``sub`` off
the allowlist ends it at once. See docs/mcp-oauth.md for the design.
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx2
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl, ConfigDict, TypeAdapter
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cloud_coder.config import ConfigError
from cloud_coder.guards import READ, WRITE

log = logging.getLogger(__name__)

PUBLIC_URL_ENV = "CLOUD_CODER_PUBLIC_URL"
CLIENT_SECRET_ENV = "CLOUD_CODER_OAUTH_CLIENT_SECRET"
SIGNING_KEYS_ENV = "CLOUD_CODER_OAUTH_SIGNING_KEYS"
ALLOWED_SUBS_ENV = "CLOUD_CODER_OAUTH_ALLOWED_SUBS"
REDIRECT_URIS_ENV = "CLOUD_CODER_OAUTH_REDIRECT_URIS"
GOOGLE_CLIENT_ID_ENV = "CLOUD_CODER_GOOGLE_CLIENT_ID"
GOOGLE_CLIENT_SECRET_ENV = "CLOUD_CODER_GOOGLE_CLIENT_SECRET"

CLIENT_ID = "cloud-coder"
# https://developers.openai.com/plugins/build/auth: the redirect URI for authorization
# servers that send `iss` with the code (RFC 9207), as this one does.
CHATGPT_REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
TOKEN_AUTH_METHOD = "client_secret_post"
OFFLINE_ACCESS = "offline_access"
SCOPES = (READ, WRITE, OFFLINE_ACCESS)
DEFAULT_SCOPES = SCOPES  # when the client asks for none
MIN_SECRET_LENGTH = 32

MCP_PATH = "/mcp"
AUTHORIZE_PATH = "/authorize"
METADATA_PATH = "/.well-known/oauth-authorization-server"
CONSENT_PATH = "/authorize/consent"
GOOGLE_CALLBACK_PATH = "/authorize/google/callback"

# https://accounts.google.com/.well-known/openid-configuration
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_TIMEOUT = 10

ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 14 * 24 * 3600  # a refresh token unused this long expires
GRANT_LIFETIME = 30 * 24 * 3600  # from approval; then the client must be approved again
CODE_TTL = 300
FLOW_TTL = 600  # from the authorization request to the Google callback

# Set on the consent page (CSRF token of its form), replaced when the person continues to
# Google (binds the Google callback to this browser), deleted at the callback.
FLOW_COOKIE = "__Host-cloud-coder-oauth"

HKDF_SALT = b"cloud-coder oauth v2"

_URL_AS_IS = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))
_GOOGLE_ERROR = re.compile(r"[a-z_]{1,64}")


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def _required(environ: Mapping[str, str], name: str, what: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"no {what}: set {name}")
    return value


def _strong(name: str, value: str) -> str:
    if len(value) < MIN_SECRET_LENGTH:
        raise ConfigError(
            f"{name} must be at least {MIN_SECRET_LENGTH} characters (e.g. openssl rand -base64 32)"
        )
    return value


@dataclass(frozen=True)
class OAuthSettings:
    """The OAuth configuration: where the API is reachable from outside, the client, the
    keys, and who may approve. Secrets are left out of ``repr``."""

    public_url: str
    client_secret: str = field(repr=False)
    signing_keys: tuple[str, ...] = field(repr=False)
    google_client_id: str
    google_client_secret: str = field(repr=False)
    allowed_subs: frozenset[str] = frozenset()
    redirect_uris: tuple[str, ...] = (CHATGPT_REDIRECT_URI,)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "OAuthSettings":
        public_url = environ.get(PUBLIC_URL_ENV, "").strip().rstrip("/")
        if not public_url:
            raise ConfigError(
                f"no public URL: set {PUBLIC_URL_ENV} to the URL clients reach the API at "
                "(e.g. http://localhost:8787 locally)"
            )
        url = urlparse(public_url)
        local = url.hostname in ("localhost", "127.0.0.1")
        if not (url.scheme == "https" or (url.scheme == "http" and local)) or not url.netloc:
            raise ConfigError(f"{PUBLIC_URL_ENV} must be an https URL, got {public_url!r}")
        if url.path or url.query or url.fragment:
            raise ConfigError(f"{PUBLIC_URL_ENV} must have no path, query or fragment")
        signing_keys = _split(environ.get(SIGNING_KEYS_ENV))
        if not signing_keys:
            raise ConfigError(f"no OAuth signing key: set {SIGNING_KEYS_ENV} (comma-separated)")
        return cls(
            public_url=public_url,
            client_secret=_strong(
                CLIENT_SECRET_ENV, _required(environ, CLIENT_SECRET_ENV, "OAuth client secret")
            ),
            signing_keys=tuple(_strong(SIGNING_KEYS_ENV, k) for k in signing_keys),
            google_client_id=_required(environ, GOOGLE_CLIENT_ID_ENV, "Google OAuth client ID"),
            google_client_secret=_required(
                environ, GOOGLE_CLIENT_SECRET_ENV, "Google OAuth client secret"
            ),
            allowed_subs=frozenset(_split(environ.get(ALLOWED_SUBS_ENV))),
            redirect_uris=_split(environ.get(REDIRECT_URIS_ENV)) or (CHATGPT_REDIRECT_URI,),
        )

    @property
    def resource_url(self) -> str:
        return self.public_url + MCP_PATH

    @property
    def issuer_url(self) -> AnyHttpUrl:
        """The public URL as is: issuers compare as strings, so no trailing slash added."""
        return _URL_AS_IS.validate_python(self.public_url)

    @property
    def google_redirect_uri(self) -> str:
        return self.public_url + GOOGLE_CALLBACK_PATH


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(secret: bytes, info: str) -> bytes:
    """HKDF-SHA256 (RFC 5869) with one 32-byte output block."""
    prk = hmac.new(HKDF_SALT, secret, hashlib.sha256).digest()
    return hmac.new(prk, info.encode() + b"\x01", hashlib.sha256).digest()


class SignedTokens:
    """Self-contained tokens: claims and an HMAC keyed by one of the signing keys.

    Every kind of token has its own key, so a token of one kind is never valid as another.
    The first signing key signs; all of them verify.
    """

    KINDS = ("request", "state", "code", "access", "refresh", "csrf", "binding", "pkce", "nonce")

    def __init__(self, signing_keys: tuple[str, ...]):
        self._keys = [{kind: _hkdf(k.encode(), kind) for kind in self.KINDS} for k in signing_keys]

    @staticmethod
    def _mac(keys: dict[str, bytes], kind: str, body: str) -> bytes:
        return hmac.new(keys[kind], body.encode(), hashlib.sha256).digest()

    def seal(self, kind: str, claims: dict[str, Any], exp: int | None) -> str:
        if exp is not None:
            claims = {**claims, "exp": exp}
        body = _b64encode(json.dumps(claims, separators=(",", ":")).encode())
        return f"{body}.{_b64encode(self._mac(self._keys[0], kind, body))}"

    def open(self, kind: str, token: str) -> dict[str, Any] | None:
        """The claims of a valid, unexpired token; None otherwise."""
        body, _, mac = token.partition(".")
        try:
            mac_bytes = _b64decode(mac)
        except ValueError:
            return None
        # Check every key, without stopping at a match.
        matches = [hmac.compare_digest(self._mac(k, kind, body), mac_bytes) for k in self._keys]
        if not any(matches):
            return None
        claims = json.loads(_b64decode(body))
        if "exp" in claims and claims["exp"] < time.time():
            return None
        return claims

    def derive(self, kind: str, value: str) -> str:
        """A value that only the holder of the first signing key can compute from ``value``."""
        return _b64encode(self._mac(self._keys[0], kind, value))


class SingleUse:
    """Remembers used IDs until they expire. In memory: Cloud Run runs at most one
    instance, and what a restart forgets is short-lived and bound by other checks."""

    def __init__(self) -> None:
        self._used: dict[str, float] = {}

    def claim(self, key: str, expires_at: float) -> bool:
        """True the first time ``key`` is claimed."""
        now = time.time()
        self._used = {k: exp for k, exp in self._used.items() if exp > now}
        if key in self._used:
            return False
        self._used[key] = expires_at
        return True


class GoogleLoginError(Exception):
    """The Google sign-in did not give a usable ID token. The message is safe to log."""


@dataclass(frozen=True)
class GoogleLogin:
    """Google as the identity provider of the consent step (OpenID Connect code flow)."""

    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    transport: httpx2.AsyncBaseTransport | None = None

    def authorization_url(self, state: str, nonce: str, code_verifier: str) -> str:
        challenge = _b64encode(hashlib.sha256(code_verifier.encode()).digest())
        query = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(query)}"

    async def identify(self, code: str, code_verifier: str, nonce: str) -> dict[str, Any]:
        """The claims of the ID token that ``code`` gives, once checked."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            async with httpx2.AsyncClient(
                transport=self.transport, timeout=GOOGLE_TIMEOUT, follow_redirects=False
            ) as http:
                response = await http.post(GOOGLE_TOKEN_ENDPOINT, data=form)
        except httpx2.HTTPError as e:
            raise GoogleLoginError(
                f"Google token endpoint unreachable ({type(e).__name__})"
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, dict):
            error = body.get("error") if isinstance(body, dict) else None
            if not (isinstance(error, str) and _GOOGLE_ERROR.fullmatch(error)):
                error = "unexpected response"
            raise GoogleLoginError(f"Google token endpoint: HTTP {response.status_code} {error}")
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            raise GoogleLoginError("Google gave no ID token")
        return self.checked_claims(id_token, nonce)

    def checked_claims(self, id_token: str, nonce: str) -> dict[str, Any]:
        """OpenID Connect Core 3.1.3.7. The token comes straight from Google's token
        endpoint over TLS, in exchange for the client secret, so TLS stands in for the
        signature (step 6); the claims are checked all the same."""
        try:
            claims = json.loads(_b64decode(id_token.split(".")[1]))
        except (IndexError, ValueError):
            raise GoogleLoginError("malformed ID token") from None
        if not isinstance(claims, dict):
            raise GoogleLoginError("malformed ID token")
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise GoogleLoginError("ID token from another issuer")
        aud = claims.get("aud")
        if aud != self.client_id and not (isinstance(aud, list) and self.client_id in aud):
            raise GoogleLoginError("ID token for another client")
        if claims.get("azp", self.client_id) != self.client_id:
            raise GoogleLoginError("ID token for another client")
        exp = claims.get("exp")
        if not isinstance(exp, int | float) or exp < time.time():
            raise GoogleLoginError("expired ID token")
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise GoogleLoginError("ID token with another nonce")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise GoogleLoginError("ID token without sub")
        return claims


# `subject` (from the SDK) is the approver's Google sub; `approved_at` is when the grant was
# approved, which bounds its lifetime.


class GrantCode(AuthorizationCode):
    subject: str
    jti: str
    approved_at: int


class GrantRefreshToken(RefreshToken):
    subject: str
    approved_at: int


class AuthorizationServer:
    """The OAuth provider (``OAuthAuthorizationServerProvider``) for /mcp, and its token
    verifier."""

    def __init__(self, settings: OAuthSettings, google_transport=None):
        self.settings = settings
        self._signed = SignedTokens(settings.signing_keys)
        self._google = GoogleLogin(
            settings.google_client_id,
            settings.google_client_secret,
            settings.google_redirect_uri,
            google_transport,
        )
        self._used_codes = SingleUse()
        self._used_flows = SingleUse()
        self.client = OAuthClientInformationFull(
            client_id=CLIENT_ID,
            client_secret=settings.client_secret,
            client_secret_expires_at=0,
            client_name="ChatGPT",
            redirect_uris=[AnyUrl(u) for u in settings.redirect_uris],
            token_endpoint_auth_method=TOKEN_AUTH_METHOD,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=" ".join(SCOPES),
        )

    # --- the one client ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.client if client_id == CLIENT_ID else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("dynamic client registration is disabled")

    def _allowed(self, sub: str) -> bool:
        return sub in self.settings.allowed_subs

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
            "state": params.state,
            "scopes": _granted(params.scopes),
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
        }
        sealed = self._signed.seal("request", request, exp=int(time.time()) + FLOW_TTL)
        return f"{self.settings.public_url}{CONSENT_PATH}?{urlencode({'request': sealed})}"

    async def _consent(self, request: Request) -> Response:
        """GET: the consent page. POST (its form): on to Google."""
        posted = request.method == "POST"
        params = await request.form() if posted else request.query_params
        sealed = str(params.get("request", ""))
        authorization = self._signed.open("request", sealed)
        if authorization is None:
            return _page(_EXPIRED, 400)
        if not posted:
            csrf = secrets.token_urlsafe(32)
            page = _page(_consent_form(sealed, authorization, self._signed.derive("csrf", csrf)))
            _set_flow_cookie(page, csrf)
            return page
        # CSRF: the form's token must match this browser's cookie, which a cross-site form
        # post does not carry (SameSite=Lax).
        cookie = request.cookies.get(FLOW_COOKIE, "")
        expected = self._signed.derive("csrf", cookie)
        if not cookie or not hmac.compare_digest(expected, str(params.get("csrf", ""))):
            return _page(_RESTART, 403)
        # The browser keeps `binding` in its cookie; Google's round trip carries only a
        # MAC of it, and the PKCE verifier and nonce are derived from it.
        binding = secrets.token_urlsafe(32)
        state = self._signed.seal(
            "state",
            {"request": authorization, "binding": self._signed.derive("binding", binding)},
            exp=int(time.time()) + FLOW_TTL,
        )
        url = self._google.authorization_url(
            state,
            nonce=self._signed.derive("nonce", binding),
            code_verifier=self._signed.derive("pkce", binding),
        )
        response = RedirectResponse(url, status_code=303, headers=_NO_STORE)
        _set_flow_cookie(response, binding)
        return response

    async def _google_callback(self, request: Request) -> Response:
        params = request.query_params
        state = self._signed.open("state", str(params.get("state", "")))
        if state is None:
            log.warning("OAuth: refused Google callback: invalid or expired state")
            return _page(_EXPIRED, 400)
        binding = request.cookies.get(FLOW_COOKIE, "")
        if not binding or not hmac.compare_digest(
            self._signed.derive("binding", binding), state["binding"]
        ):
            log.warning("OAuth: refused Google callback: not from the browser that consented")
            return _page(_RESTART, 400)
        if not self._used_flows.claim(state["binding"], state["exp"]):
            log.warning("OAuth: refused Google callback: used twice")
            return _page(_RESTART, 400)
        authorization = state["request"]
        if "error" in params:
            # The person cancelled at Google: tell the client (RFC 6749 4.1.2.1), with iss.
            log.info("OAuth: Google sign-in cancelled")
            return _done(
                construct_redirect_uri(
                    authorization["redirect_uri"],
                    error="access_denied",
                    state=authorization["state"],
                    iss=self.settings.public_url,
                )
            )
        try:
            identity = await self._google.identify(
                str(params.get("code", "")),
                code_verifier=self._signed.derive("pkce", binding),
                nonce=self._signed.derive("nonce", binding),
            )
        except GoogleLoginError as e:
            log.warning(f"OAuth: Google sign-in failed: {e}")
            return _done(_page(_GOOGLE_FAILED, 502))
        sub = identity["sub"]
        if not self._allowed(sub):
            log.warning("OAuth: refused a Google account that is not on the allowlist")
            return _done(_page(_not_allowed(sub, identity.get("email")), 403))
        now = int(time.time())
        code = self._signed.seal(
            "code",
            {
                **{k: authorization[k] for k in _CODE_CLAIMS},
                "jti": secrets.token_urlsafe(16),
                "sub": sub,
                "approved_at": now,
            },
            exp=now + CODE_TTL,
        )
        log.info(f"OAuth: approved a grant ({' '.join(authorization['scopes'])})")
        return _done(
            construct_redirect_uri(
                authorization["redirect_uri"],
                code=code,
                state=authorization["state"],
                iss=self.settings.public_url,
            )
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> GrantCode | None:
        claims = self._signed.open("code", authorization_code)
        if claims is None:
            return None
        return GrantCode(
            code=authorization_code,
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            client_id=CLIENT_ID,
            code_challenge=claims["code_challenge"],
            redirect_uri=claims["redirect_uri"],
            redirect_uri_provided_explicitly=claims["redirect_uri_provided_explicitly"],
            resource=self.settings.resource_url,
            subject=claims["sub"],
            jti=claims["jti"],
            approved_at=claims["approved_at"],
        )

    # --- tokens ----------------------------------------------------------------------

    def _issue(
        self, *, grant_scopes: list[str], access_scopes: list[str], sub: str, approved_at: int
    ) -> OAuthToken:
        """An access token for ``access_scopes`` (within the grant's), and a refresh token
        for the whole grant (RFC 6749 6) if the grant includes offline_access."""
        access_scopes = _granted(access_scopes)
        now = int(time.time())
        grant_ends = approved_at + GRANT_LIFETIME
        claims = {"sub": sub, "approved_at": approved_at}
        access_exp = min(now + ACCESS_TOKEN_TTL, grant_ends)
        access = self._signed.seal(
            "access",
            {**claims, "scopes": access_scopes, "aud": self.settings.resource_url},
            exp=access_exp,
        )
        refresh = None
        if OFFLINE_ACCESS in grant_scopes:
            refresh = self._signed.seal(
                "refresh",
                {**claims, "scopes": grant_scopes},
                exp=min(now + REFRESH_TOKEN_TTL, grant_ends),
            )
        return OAuthToken(
            access_token=access,
            expires_in=access_exp - now,
            scope=" ".join(access_scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: GrantCode
    ) -> OAuthToken:
        # Called once the client secret and the PKCE verifier are checked. Codes are
        # single use, best effort: a restart forgets the used ones, but a code lives only
        # CODE_TTL seconds and needs the verifier and the client secret as well.
        if not self._used_codes.claim(authorization_code.jti, authorization_code.expires_at):
            raise TokenError("invalid_grant", "authorization code already used")
        if not self._allowed(authorization_code.subject):
            raise TokenError("invalid_grant", "the approver is no longer allowed")
        log.info("OAuth: exchanged an authorization code")
        return self._issue(
            grant_scopes=authorization_code.scopes,
            access_scopes=authorization_code.scopes,
            sub=authorization_code.subject,
            approved_at=authorization_code.approved_at,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> GrantRefreshToken | None:
        claims = self._signed.open("refresh", refresh_token)
        if claims is None or not self._allowed(claims["sub"]):
            return None
        return GrantRefreshToken(
            token=refresh_token,
            client_id=CLIENT_ID,
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            resource=self.settings.resource_url,
            subject=claims["sub"],
            approved_at=claims["approved_at"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: GrantRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # The SDK has checked that `scopes` are within the grant's.
        age = int(time.time()) - refresh_token.approved_at
        log.info(f"OAuth: refreshed a grant approved {age // 3600}h ago")
        return self._issue(
            grant_scopes=refresh_token.scopes,
            access_scopes=scopes,
            sub=refresh_token.subject,
            approved_at=refresh_token.approved_at,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        claims = self._signed.open("access", token)
        if claims is None or not self._allowed(claims["sub"]):
            return None
        return AccessToken(
            token=token,
            client_id=CLIENT_ID,
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            resource=claims["aud"],
            subject=claims["sub"],
        )

    verify_token = load_access_token  # TokenVerifier: /mcp takes these access tokens only

    # --- routes ----------------------------------------------------------------------

    def routes(self) -> list[Route]:
        """Metadata (RFC 8414), /authorize, /token, the consent page and Google's callback."""
        issuer = self.settings.issuer_url
        registration = ClientRegistrationOptions(enabled=False, valid_scopes=list(SCOPES))
        revocation = RevocationOptions(enabled=False)  # nothing to revoke without storage
        metadata = build_metadata(issuer, None, registration, revocation)
        metadata.scopes_supported = list(SCOPES)
        metadata.token_endpoint_auth_methods_supported = [TOKEN_AUTH_METHOD]
        metadata.authorization_response_iss_parameter_supported = True
        sdk_routes = [
            Route(r.path, _WithIss(r.app, self.settings.public_url), methods=r.methods)
            if r.path == AUTHORIZE_PATH
            else r
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
            Route(CONSENT_PATH, self._consent, methods=["GET", "POST"]),
            Route(GOOGLE_CALLBACK_PATH, self._google_callback, methods=["GET"]),
        ]


_CODE_CLAIMS = ("scopes", "code_challenge", "redirect_uri", "redirect_uri_provided_explicitly")
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


class _WithIss:
    """The SDK's /authorize, with ``iss`` added to the error responses it redirects to the
    client: RFC 9207 wants it in every authorization response, errors included. (A class:
    Starlette would take a plain function for a request handler, not an ASGI app.)"""

    def __init__(self, app: ASGIApp, issuer: str):
        self.app = app
        self.issuer = issuer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_iss(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, _add_iss(v.decode(), self.issuer).encode() if k == b"location" else v)
                    for k, v in message.get("headers", [])
                ]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_iss)


def _add_iss(location: str, issuer: str) -> str:
    query = parse_qs(urlparse(location).query)
    if "error" not in query or "iss" in query:
        return location
    return construct_redirect_uri(location, iss=issuer)


def _granted(requested: list[str] | None) -> list[str]:
    """The scopes to grant for a request: all of them when none are asked for. ``write``
    implies ``read``, which /mcp requires of every token. (The SDK has already refused
    scopes the client is not registered for.)"""
    scopes = set(requested or DEFAULT_SCOPES)
    if WRITE in scopes:
        scopes.add(READ)
    return [s for s in SCOPES if s in scopes]


def _set_flow_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        FLOW_COOKIE,
        value,
        max_age=FLOW_TTL,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _done(response: Response | str) -> Response:
    """The end of a flow: redirect to the client (or show a page) and drop the cookie."""
    if isinstance(response, str):
        response = RedirectResponse(response, status_code=303, headers=_NO_STORE)
    response.delete_cookie(FLOW_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


_EXPIRED = "<p>This authorization request is invalid or has expired. Start again from the app.</p>"
_RESTART = (
    "<p>This page was not opened from the consent page of this browser, or was used "
    "already. Start again from the app.</p>"
)
_GOOGLE_FAILED = "<p>Signing in with Google failed. Start again from the app.</p>"

_SCOPE_TEXT = {
    READ: "see the VM, its sessions and their screens",
    WRITE: "start and stop the VM, open sessions and send prompts to Claude Code",
    OFFLINE_ACCESS: (
        f"stay connected without asking again, for up to {GRANT_LIFETIME // 86400} days"
    ),
}


def _consent_form(sealed: str, request: dict[str, Any], csrf: str) -> str:
    redirect_uri = html.escape(request["redirect_uri"])
    scopes = "".join(f"<li>{html.escape(_SCOPE_TEXT[s])}</li>" for s in request["scopes"])
    return f"""
<p><b>ChatGPT</b> asks for access to this cloud-coder worker. It may:</p>
<ul>{scopes}</ul>
<p>It will receive the grant at <code>{redirect_uri}</code>.</p>
<p><b>Continue only if you started this connection yourself</b> (from ChatGPT) just now.
Then sign in with a Google account that is allowed to approve.</p>
<form method="post">
  <input type="hidden" name="request" value="{html.escape(sealed)}">
  <input type="hidden" name="csrf" value="{html.escape(csrf)}">
  <button type="submit">Continue with Google</button>
</form>"""


def _not_allowed(sub: str, email: Any) -> str:
    who = f" ({html.escape(email)})" if isinstance(email, str) else ""
    return f"""
<p>This Google account{who} may not approve access to this cloud-coder worker.</p>
<p>To allow it, add its subject ID to {ALLOWED_SUBS_ENV} and restart the API:</p>
<p><code>{html.escape(sub)}</code></p>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    page = f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cloud-coder authorization</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 2rem auto; padding: 0 1rem; }}
button {{ padding: .5rem 1rem; }}
</style></head>
<body><h1>cloud-coder</h1>{body}</body></html>"""
    return HTMLResponse(
        page,
        status_code=status,
        headers={
            **_NO_STORE,
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "frame-ancestors 'none'",
        },
    )
