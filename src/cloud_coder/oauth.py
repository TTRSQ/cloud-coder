"""OAuth for the MCP endpoint of the HTTP API: a relay to Google.

Google signs people in and issues the tokens. This server shows ChatGPT an authorization
server of its own (issuer = the public URL) whose three endpoints relay to Google, as
Trillion OS does (AOI-Inc/trillion-os#360):

- ``/mcp/oauth/authorize`` checks ChatGPT's request and redirects to Google with the
  redirect URI replaced by this server's callback, the state replaced by a signed one
  that carries ChatGPT's state, and ``access_type=offline&prompt=consent`` added, so that
  Google issues a refresh token (ChatGPT does not ask for one).
- ``/mcp/oauth/callback`` checks the state (signature, expiry, a cookie in the browser
  that started) and redirects to ChatGPT with Google's code, ChatGPT's state and ``iss`` =
  this issuer (RFC 9207). Then ChatGPT uses its fixed redirect URI, and the Google OAuth
  client needs exactly one redirect URI: this callback.
- ``/mcp/oauth/token`` forwards ``authorization_code`` and ``refresh_token`` grants to
  Google with fixed parameters and returns Google's answer as is. The client is the Google
  OAuth client itself: ChatGPT holds its ID and secret, and Google checks the secret.

/mcp takes Google access tokens issued to that client (``aud``, asked of Google's
tokeninfo) for an account whose ``sub`` is on the allowlist. Nothing is stored. See
docs/mcp-oauth.md.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse

import httpx2
from mcp.server.auth.provider import AccessToken
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from cloud_coder.config import ConfigError

log = logging.getLogger(__name__)

PUBLIC_URL_ENV = "CLOUD_CODER_PUBLIC_URL"
SIGNING_KEYS_ENV = "CLOUD_CODER_OAUTH_SIGNING_KEYS"
ALLOWED_SUBS_ENV = "CLOUD_CODER_OAUTH_ALLOWED_SUBS"
GOOGLE_CLIENT_ID_ENV = "CLOUD_CODER_GOOGLE_CLIENT_ID"

# https://developers.openai.com/apps-sdk/build/auth: the redirect URI ChatGPT uses with an
# authorization server that sends `iss` with the code (RFC 9207), as this one does.
CHATGPT_REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
# Who the person is, nothing more: Google's other APIs stay out of reach.
SCOPES = ("openid", "email")
MIN_KEY_LENGTH = 32

MCP_PATH = "/mcp"
METADATA_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/mcp/oauth/authorize"
CALLBACK_PATH = "/mcp/oauth/callback"
TOKEN_PATH = "/mcp/oauth/token"

GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"  # tokeninfo's `email`
GOOGLE_TIMEOUT = 10

FLOW_TTL = 600  # from the authorization request to the callback
TOKENINFO_CACHE_TTL = 60  # a token Google revokes still passes this long
FLOW_COOKIE_PREFIX = "__Secure-cloud-coder-oauth-"
CODE_CHALLENGE = re.compile(r"[A-Za-z0-9._~-]{43,128}")
OAUTH_ERROR = re.compile(r"[a-z_]{1,64}")

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_URL_AS_IS = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))


@dataclass(frozen=True)
class OAuthSettings:
    """Where the API is reachable from outside, the Google OAuth client, the keys that
    sign the state, and who may use /mcp. Keys stay out of ``repr``."""

    public_url: str
    signing_keys: tuple[str, ...] = field(repr=False)
    google_client_id: str
    allowed_subs: frozenset[str] = frozenset()

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
        if any(len(k) < MIN_KEY_LENGTH for k in signing_keys):
            raise ConfigError(
                f"{SIGNING_KEYS_ENV} must be at least {MIN_KEY_LENGTH} characters each "
                "(e.g. openssl rand -base64 32)"
            )
        google_client_id = environ.get(GOOGLE_CLIENT_ID_ENV, "").strip()
        if not google_client_id:
            raise ConfigError(f"no Google OAuth client ID: set {GOOGLE_CLIENT_ID_ENV}")
        return cls(
            public_url=public_url,
            signing_keys=signing_keys,
            google_client_id=google_client_id,
            allowed_subs=frozenset(_split(environ.get(ALLOWED_SUBS_ENV))),
        )

    @property
    def resource_url(self) -> str:
        return self.public_url + MCP_PATH

    @property
    def issuer_url(self) -> AnyHttpUrl:
        """The public URL as is: issuers compare as strings, so no trailing slash added."""
        return _URL_AS_IS.validate_python(self.public_url)

    @property
    def callback_url(self) -> str:
        return self.public_url + CALLBACK_PATH


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mac(key: str, purpose: str, value: str) -> str:
    """HMAC-SHA256 of ``value`` under a key derived for ``purpose``, so that a value made
    for one purpose is never valid for another."""
    derived = hmac.new(key.encode(), f"cloud-coder oauth {purpose}".encode(), hashlib.sha256)
    return _b64(hmac.new(derived.digest(), value.encode(), hashlib.sha256).digest())


class GoogleUnavailable(Exception):
    """Google could not be asked about a token. The message is safe to log."""


class AuthorizationRelay:
    """The OAuth endpoints that relay to Google, and the token verifier of /mcp."""

    def __init__(self, settings: OAuthSettings, google_transport=None):
        self.settings = settings
        self._google_transport = google_transport  # tests replace the network to Google
        self._verified: dict[str, tuple[AccessToken, float]] = {}  # token hash -> until

    def _google(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=self._google_transport, timeout=GOOGLE_TIMEOUT)

    # --- the state Google carries ---------------------------------------------------

    def _seal_state(self, client_state: str | None) -> tuple[str, str, str]:
        """The state for Google, and the name and value of the cookie that binds it to
        this browser. The first signing key signs."""
        key = self.settings.signing_keys[0]
        flow = secrets.token_urlsafe(16)
        claims = {"flow": flow, "state": client_state, "exp": int(time.time()) + FLOW_TTL}
        body = _b64(json.dumps(claims, separators=(",", ":")).encode())
        state = f"{body}.{_mac(key, 'state', body)}"
        return state, FLOW_COOKIE_PREFIX + flow, _mac(key, "cookie", flow)

    def _open_state(self, state: str, cookies: Mapping[str, str]) -> tuple[dict, str] | None:
        """ChatGPT's state and the cookie's name, if the state is signed by one of the
        keys, unexpired, and comes with its cookie; None otherwise."""
        body, _, mac = state.partition(".")
        for key in self.settings.signing_keys:
            if hmac.compare_digest(_mac(key, "state", body).encode(), mac.encode()):
                claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
                cookie = FLOW_COOKIE_PREFIX + claims["flow"]
                expected = _mac(key, "cookie", claims["flow"])
                presented = cookies.get(cookie, "").encode()
                if claims["exp"] > time.time() and hmac.compare_digest(
                    presented, expected.encode()
                ):
                    return claims, cookie
                return None
        return None

    # --- endpoints ------------------------------------------------------------------

    async def _metadata(self, request: Request) -> Response:
        """RFC 8414."""
        issuer = self.settings.public_url
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": issuer + AUTHORIZE_PATH,
                "token_endpoint": issuer + TOKEN_PATH,
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
                "scopes_supported": list(SCOPES),
                "authorization_response_iss_parameter_supported": True,
            }
        )

    async def _authorize(self, request: Request) -> Response:
        """Check ChatGPT's request and redirect to Google. Any problem is a 400 that
        redirects nowhere."""
        params = request.query_params
        scopes = params.get("scope", " ".join(SCOPES)).split()
        resource = params.get("resource", self.settings.resource_url).rstrip("/")
        if params.get("client_id") != self.settings.google_client_id:
            return _refused("authorize", "client_id")
        if params.get("redirect_uri") != CHATGPT_REDIRECT_URI:
            return _refused("authorize", "redirect_uri")
        if params.get("response_type") != "code":
            return _refused("authorize", "response_type")
        if not CODE_CHALLENGE.fullmatch(params.get("code_challenge", "")) or (
            params.get("code_challenge_method") != "S256"
        ):
            return _refused("authorize", "code_challenge")
        if not scopes or not set(scopes) <= set(SCOPES):
            return _refused("authorize", "scope")
        # RFC 8707: ChatGPT names this server by the URL the user entered (…/mcp).
        if resource not in (self.settings.resource_url, self.settings.public_url):
            return _refused("authorize", "resource")

        state, cookie, binding = self._seal_state(params.get("state"))
        query = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.callback_url,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": params["code_challenge"],
            "code_challenge_method": "S256",
            "access_type": "offline",  # a refresh token
            "prompt": "consent",  # also on the second and later authorizations
        }
        url = f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(query)}"
        response = RedirectResponse(url, status_code=302, headers=_NO_STORE)
        response.set_cookie(
            cookie, binding, max_age=FLOW_TTL, path=CALLBACK_PATH, secure=True, httponly=True
        )
        return response

    async def _callback(self, request: Request) -> Response:
        """Check the state and send Google's code (or error) to ChatGPT, with ``iss``.
        Anything that cannot be checked is a 400 that redirects nowhere."""
        params = request.query_params
        opened = self._open_state(params.get("state", ""), request.cookies)
        if opened is None:
            return _refused("callback", "state invalid, expired or not from this browser")
        claims, cookie = opened
        if params.get("iss", GOOGLE_ISSUER) != GOOGLE_ISSUER:
            return _done(_refused("callback", "iss"), cookie)
        if "error" in params:
            error = params["error"] if OAUTH_ERROR.fullmatch(params["error"]) else "access_denied"
            to_client = {"error": error}
            log.info(f"OAuth: Google answered {error}")
        elif params.get("code"):
            to_client = {"code": params["code"]}
            log.info("OAuth: Google signed someone in; handing the code to ChatGPT")
        else:
            return _done(_refused("callback", "no code"), cookie)
        if claims["state"] is not None:
            to_client["state"] = claims["state"]
        to_client["iss"] = self.settings.public_url
        response = RedirectResponse(
            f"{CHATGPT_REDIRECT_URI}?{urlencode(to_client)}",
            status_code=302,
            headers={**_NO_STORE, "Referrer-Policy": "no-referrer"},
        )
        return _done(response, cookie)

    async def _token(self, request: Request) -> Response:
        """Forward an authorization_code or refresh_token grant to Google with fixed
        parameters, and return Google's answer as is."""
        form = await request.form()
        grant_type = form.get("grant_type")
        if form.get("client_id") != self.settings.google_client_id or not form.get("client_secret"):
            return _refused("token", "client", status=401, error="invalid_client")
        forwarded = {
            "client_id": self.settings.google_client_id,
            "client_secret": str(form["client_secret"]),
            "grant_type": str(grant_type),
        }
        if grant_type == "authorization_code" and form.get("code") and form.get("code_verifier"):
            forwarded |= {
                "code": str(form["code"]),
                "code_verifier": str(form["code_verifier"]),
                # Google issued the code to the callback and wants the same URI here.
                "redirect_uri": self.settings.callback_url,
            }
        elif grant_type == "refresh_token" and form.get("refresh_token"):
            forwarded["refresh_token"] = str(form["refresh_token"])
        elif grant_type in ("authorization_code", "refresh_token"):
            return _refused("token", f"incomplete {grant_type}")
        else:
            return _refused("token", "grant_type", error="unsupported_grant_type")

        try:
            async with self._google() as http:
                answer = await http.post(GOOGLE_TOKEN_ENDPOINT, data=forwarded)
        except httpx2.HTTPError as e:
            log.warning(f"OAuth: Google token endpoint unreachable ({type(e).__name__})")
            return JSONResponse(
                {"error": "temporarily_unavailable"}, status_code=503, headers=_NO_STORE
            )
        if answer.status_code == 200:
            log.info(f"OAuth: Google granted tokens ({grant_type})")
        else:
            log.warning(
                f"OAuth: Google refused a {grant_type} grant: "
                f"HTTP {answer.status_code} {_google_error(answer)}"
            )
        return Response(
            answer.content,
            status_code=answer.status_code,
            media_type=answer.headers.get("content-type", "application/json"),
            headers=_NO_STORE,
        )

    # --- /mcp ----------------------------------------------------------------------

    async def verify_token(self, token: str) -> AccessToken | None:
        """The access token, if Google says it was issued to the Google OAuth client, is
        unexpired, and belongs to an account on the allowlist. GoogleUnavailable when
        Google cannot be asked: a 500, not a 401 that would send ChatGPT to sign in."""
        now = time.time()
        key = hashlib.sha256(token.encode()).hexdigest()
        self._verified = {k: v for k, v in self._verified.items() if v[1] > now}
        if key in self._verified:
            return self._verified[key][0]
        try:
            async with self._google() as http:
                # POST: the token stays out of URLs and their logs.
                answer = await http.post(GOOGLE_TOKENINFO_ENDPOINT, data={"access_token": token})
        except httpx2.HTTPError as e:
            raise GoogleUnavailable(f"tokeninfo unreachable ({type(e).__name__})") from None
        if answer.status_code == 429 or answer.status_code >= 500:
            raise GoogleUnavailable(f"tokeninfo: HTTP {answer.status_code}")
        if answer.status_code != 200:
            log.info(f"OAuth: refused a token: tokeninfo HTTP {answer.status_code}")
            return None
        info = answer.json()
        sub, exp = info.get("sub"), int(info.get("exp", 0))
        if info.get("aud") != self.settings.google_client_id:
            log.warning("OAuth: refused a token issued to another client")
            return None
        if exp <= now:
            log.info("OAuth: refused an expired token")
            return None
        if sub not in self.settings.allowed_subs:
            # The subject ID names an account and grants nothing: it is what the allowlist
            # takes.
            log.warning(f"OAuth: refused a token of a Google account not on the allowlist: {sub}")
            return None
        access = AccessToken(
            token=token,
            client_id=info["aud"],
            scopes=[
                "email" if s == GOOGLE_EMAIL_SCOPE else s for s in info.get("scope", "").split()
            ],
            expires_at=exp,
            subject=sub,
        )
        self._verified[key] = (access, min(now + TOKENINFO_CACHE_TTL, exp))
        return access

    def routes(self) -> list[Route]:
        return [
            Route(METADATA_PATH, self._metadata, methods=["GET"]),
            Route(AUTHORIZE_PATH, self._authorize, methods=["GET"]),
            Route(CALLBACK_PATH, self._callback, methods=["GET"]),
            Route(TOKEN_PATH, self._token, methods=["POST"]),
        ]


def _google_error(answer: httpx2.Response) -> str:
    """Only the `error` code of Google's answer: the rest stays out of the logs."""
    try:
        error = answer.json().get("error")
    except (ValueError, AttributeError):
        return "unexpected answer"
    return error if isinstance(error, str) and OAUTH_ERROR.fullmatch(error) else "unknown"


def _refused(
    endpoint: str, reason: str, status: int = 400, error: str = "invalid_request"
) -> Response:
    log.warning(f"OAuth: refused {endpoint}: {reason}")
    return JSONResponse({"error": error}, status_code=status, headers=_NO_STORE)


def _done(response: Response, cookie: str) -> Response:
    """The end of an authorization: its cookie is dropped, so the callback works once."""
    response.delete_cookie(cookie, path=CALLBACK_PATH, secure=True, httponly=True)
    return response
