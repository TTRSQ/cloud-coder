"""/mcp of the HTTP API: the MCP server over Streamable HTTP, behind OAuth endpoints that
relay to Google, and Google access tokens checked with Google's tokeninfo."""

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from starlette.testclient import TestClient

from cloud_coder import connect, gce, http_api, oauth
from cloud_coder.config import Config, ConfigError

CFG = Config(project="p")
BASE = "https://cc.example"
CHATGPT = oauth.CHATGPT_REDIRECT_URI
CALLBACK = BASE + oauth.CALLBACK_PATH
SIGNING_KEY = "signing-key-" + "k" * 32
GOOGLE_CLIENT_ID = "google-client.apps.googleusercontent.com"
GOOGLE_SECRET = "google-secret-" + "g" * 20
ALLOWED_SUB = "1000001"
SETTINGS = oauth.OAuthSettings(
    public_url=BASE,
    signing_keys=(SIGNING_KEY,),
    google_client_id=GOOGLE_CLIENT_ID,
    allowed_subs=frozenset({ALLOWED_SUB}),
)
ENV = {
    oauth.PUBLIC_URL_ENV: BASE,
    oauth.SIGNING_KEYS_ENV: SIGNING_KEY,
    oauth.GOOGLE_CLIENT_ID_ENV: GOOGLE_CLIENT_ID,
    oauth.ALLOWED_SUBS_ENV: ALLOWED_SUB,
}
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=")
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
GOOGLE_SCOPE = f"openid {oauth.GOOGLE_EMAIL_SCOPE}"


class FakeGoogle:
    """Google's token and tokeninfo endpoints, for the Google OAuth client above."""

    def __init__(self):
        self.codes: dict[str, tuple[str, str]] = {}  # code -> (code_challenge, sub)
        self.refresh_tokens: dict[str, str] = {}  # refresh token -> sub
        self.access_tokens: dict[str, dict] = {}  # access token -> tokeninfo
        self.token_requests: list[dict] = []
        self.tokeninfo_requests = 0
        self.reply: httpx2.Response | None = None
        self.unreachable = False

    def prepare(self, google_url: str, sub=ALLOWED_SUB) -> tuple[str, str]:
        """Sign ``sub`` in at the Google URL /authorize sent the browser to: a code and the
        state to send to the callback."""
        query = {k: v[0] for k, v in parse_qs(urlparse(google_url).query).items()}
        code = "g-code-" + secrets.token_urlsafe(8)
        self.codes[code] = (query["code_challenge"], sub)
        return code, query["state"]

    def access_token(self, sub=ALLOWED_SUB, **overrides) -> str:
        token = "ya29." + secrets.token_urlsafe(16)
        self.access_tokens[token] = {
            "azp": GOOGLE_CLIENT_ID,
            "aud": GOOGLE_CLIENT_ID,
            "sub": sub,
            "scope": GOOGLE_SCOPE,
            "exp": str(int(time.time()) + 3600),
            "expires_in": "3599",
            "email": "me@example.com",
            "email_verified": "true",
            "access_type": "offline",
            **overrides,
        }
        return token

    def _tokens(self, sub: str, refresh_token: str | None = None) -> httpx2.Response:
        refresh_token = refresh_token or "1//" + secrets.token_urlsafe(16)
        self.refresh_tokens[refresh_token] = sub
        body = {
            "access_token": self.access_token(sub),
            "expires_in": 3599,
            "refresh_token": refresh_token,
            "scope": GOOGLE_SCOPE,
            "token_type": "Bearer",
            "id_token": "eyJ.google-id-token.sig",
        }
        return httpx2.Response(200, json=body)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.unreachable:
            raise httpx2.ConnectError("unreachable")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if str(request.url) == oauth.GOOGLE_TOKENINFO_ENDPOINT:
            self.tokeninfo_requests += 1
            if self.reply is not None:
                return self.reply
            info = self.access_tokens.get(form.get("access_token"))
            if info is None:
                return httpx2.Response(400, json={"error": "invalid_token"})
            return httpx2.Response(200, json=info)
        assert str(request.url) == oauth.GOOGLE_TOKEN_ENDPOINT
        self.token_requests.append(form)
        if self.reply is not None:
            return self.reply
        if form.get("client_id") != GOOGLE_CLIENT_ID or form.get("client_secret") != GOOGLE_SECRET:
            return httpx2.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "refresh_token":
            sub = self.refresh_tokens.get(form["refresh_token"])
            if sub is None:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return self._tokens(sub, form["refresh_token"])
        entry = self.codes.pop(form.get("code"), None)
        if entry is None or form.get("redirect_uri") != CALLBACK:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        challenge, sub = entry
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        return self._tokens(sub)


@pytest.fixture
def google():
    return FakeGoogle()


def build(google, settings=SETTINGS):
    return http_api.build_app(CFG, settings, google_transport=httpx2.MockTransport(google.handle))


@pytest.fixture
def vm_calls(monkeypatch):
    """The VM operations the tools reach, recorded instead of run."""
    calls = []
    monkeypatch.setattr(connect, "status", lambda cfg: calls.append("status") or {"vm": "x"})
    monkeypatch.setattr(gce, "stop", lambda cfg, wait: calls.append("stop") or "STOPPING")
    for module, name in [(connect, "up"), (connect, "launch"), (connect, "read_session")]:
        monkeypatch.setattr(
            module, name, lambda *a, _n=name, **kw: pytest.fail(f"unexpected call {_n}")
        )
    return calls


@pytest.fixture
def client(vm_calls, google):
    with TestClient(build(google), base_url=BASE) as c:
        yield c


def rpc(client, token, method, params=None):
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=headers)


def call_tool(client, token, name, arguments=None):
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def authorize(client, **params):
    """ChatGPT's authorization request, as it sent it on 2026-10-09 (with this server's
    scopes)."""
    query = {
        "response_type": "code",
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": CHATGPT,
        "scope": "openid email",
        "code_challenge": CHALLENGE.decode(),
        "code_challenge_method": "S256",
        "resource": f"{BASE}/mcp",
        "state": "chatgpt-state",
        "ui_locales": "ja-JP",
        **params,
    }
    query = {k: v for k, v in query.items() if v is not None}
    return client.get(oauth.AUTHORIZE_PATH, params=query, follow_redirects=False)


def to_google(client, **params) -> str:
    response = authorize(client, **params)
    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith(oauth.GOOGLE_AUTHORIZATION_ENDPOINT + "?")
    return response.headers["location"]


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def callback(client, code, state, **params):
    query = {"code": code, "state": state, "iss": oauth.GOOGLE_ISSUER, **params}
    query = {k: v for k, v in query.items() if v is not None}
    return client.get(oauth.CALLBACK_PATH, params=query, follow_redirects=False)


def code_for(client, google, sub=ALLOWED_SUB, **params) -> str:
    code, state = google.prepare(to_google(client, **params), sub)
    response = callback(client, code, state)
    assert response.status_code == 302, response.text
    return query_of(response.headers["location"])["code"]


def exchange(client, code=None, verifier=VERIFIER, secret=GOOGLE_SECRET, **extra):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CHATGPT,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": secret,
        "code_verifier": verifier,
        "resource": f"{BASE}/mcp",
        **extra,
    }
    return client.post(oauth.TOKEN_PATH, data={k: v for k, v in form.items() if v is not None})


def refresh(client, refresh_token, secret=GOOGLE_SECRET, **extra):
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": secret,
        **extra,
    }
    return client.post(oauth.TOKEN_PATH, data=form)


def grant(client, google, sub=ALLOWED_SUB) -> dict:
    response = exchange(client, code_for(client, google, sub))
    assert response.status_code == 200, response.text
    return response.json()


_REAL_TIME = time.time


def later(monkeypatch, seconds):
    """Move the clock to ``seconds`` from now (the real now, also when moved already)."""
    now = _REAL_TIME() + seconds
    monkeypatch.setattr(oauth.time, "time", lambda: now)


# --- settings ----------------------------------------------------------------------


def test_settings_from_env():
    settings = oauth.OAuthSettings.from_env(
        {**ENV, oauth.PUBLIC_URL_ENV: BASE + "/", oauth.ALLOWED_SUBS_ENV: " 1, 2 ,"}
    )
    assert settings == replace(SETTINGS, allowed_subs=frozenset({"1", "2"}))
    assert settings.callback_url == CALLBACK


def test_settings_do_not_show_the_signing_keys():
    shown = repr(SETTINGS)
    assert GOOGLE_CLIENT_ID in shown and SIGNING_KEY not in shown


@pytest.mark.parametrize(
    "name", [oauth.PUBLIC_URL_ENV, oauth.SIGNING_KEYS_ENV, oauth.GOOGLE_CLIENT_ID_ENV]
)
def test_refuses_to_start_without(name):
    with pytest.raises(ConfigError, match=name):
        oauth.OAuthSettings.from_env({**ENV, name: "  "})


def test_refuses_short_signing_keys():
    with pytest.raises(ConfigError, match="at least 32"):
        oauth.OAuthSettings.from_env({**ENV, oauth.SIGNING_KEYS_ENV: f"{SIGNING_KEY},short"})


def test_an_empty_allowlist_starts_and_lets_nobody_in():
    assert oauth.OAuthSettings.from_env({**ENV, oauth.ALLOWED_SUBS_ENV: ""}).allowed_subs == set()


@pytest.mark.parametrize(
    "url", ["http://cc.example", "https://cc.example/api", "https://cc.example?a=1", "cc.example"]
)
def test_public_url_must_be_a_bare_https_origin(url):
    with pytest.raises(ConfigError):
        oauth.OAuthSettings.from_env({**ENV, oauth.PUBLIC_URL_ENV: url})


def test_healthz_needs_no_token_and_never_touches_the_vm(client, vm_calls):
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert vm_calls == []


# --- metadata ----------------------------------------------------------------------


def test_authorization_server_metadata(client):
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata == {
        "issuer": BASE,  # exactly, no trailing slash: it is compared as a string
        "authorization_endpoint": f"{BASE}/mcp/oauth/authorize",
        "token_endpoint": f"{BASE}/mcp/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "scopes_supported": ["openid", "email"],
        "authorization_response_iss_parameter_supported": True,
    }


def test_protected_resource_metadata_names_the_scopes_to_ask_for(client):
    """ChatGPT asks for the scopes listed here (on 2026-10-09 it asked for `read` alone,
    the one listed then, and got no refresh token)."""
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == f"{BASE}/mcp"
    assert metadata["authorization_servers"] == [BASE]
    assert metadata["scopes_supported"] == ["openid", "email"]


def test_mcp_without_a_token_points_to_the_metadata(client):
    response = rpc(client, None, "tools/list")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in challenge


@pytest.mark.parametrize(
    "path", ["/register", "/revoke", "/authorize", "/token", "/authorize/consent"]
)
def test_no_other_oauth_endpoints(client, path):
    assert client.post(path, data={}).status_code in (404, 405)


# --- /authorize ----------------------------------------------------------------------


def test_authorize_redirects_to_google_with_fixed_parameters(client):
    response = authorize(client)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    query = query_of(response.headers["location"])
    state = query.pop("state")
    assert query == {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "scope": "openid email",
        "code_challenge": CHALLENGE.decode(),
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    assert "chatgpt-state" not in state  # signed and carried, not passed as is
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(oauth.FLOW_COOKIE_PREFIX)
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", f"Path={oauth.CALLBACK_PATH}"):
        assert attribute in cookie
    assert f"Max-Age={oauth.FLOW_TTL}" in cookie


def test_no_scope_requested_asks_google_for_openid_email(client):
    assert query_of(to_google(client, scope=None))["scope"] == "openid email"


@pytest.mark.parametrize("resource", [None, BASE, BASE + "/", f"{BASE}/mcp"])
def test_resource_may_name_this_server_or_be_left_out(client, resource):
    assert "resource" not in query_of(to_google(client, resource=resource))


@pytest.mark.parametrize(
    "params",
    [
        {"client_id": "cloud-coder"},
        {"client_id": None},
        {"redirect_uri": "https://chatgpt.com/connector/oauth/abc123"},
        {"redirect_uri": "https://evil.example/cb"},
        {"redirect_uri": CHATGPT + "?x=1"},
        {"redirect_uri": None},
        {"response_type": "token"},
        {"code_challenge": None},
        {"code_challenge": "short"},
        {"code_challenge_method": "plain"},
        {"scope": "read write offline_access"},
        {"scope": "openid https://www.googleapis.com/auth/drive"},
        {"resource": "https://other.example/mcp"},
    ],
)
def test_bad_authorization_requests_are_400_and_redirect_nowhere(client, google, params):
    response = authorize(client, **params)
    assert response.status_code == 400
    assert "location" not in response.headers and "set-cookie" not in response.headers


# --- the Google callback -------------------------------------------------------------


def test_callback_hands_googles_code_to_chatgpt_with_iss(client, google):
    code, state = google.prepare(to_google(client))
    response = callback(client, code, state)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(CHATGPT + "?")
    assert query_of(location) == {"code": code, "state": "chatgpt-state", "iss": BASE}
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "Max-Age=0" in response.headers["set-cookie"]  # the flow's cookie is dropped


def test_callback_without_googles_iss_is_accepted(client, google):
    code, state = google.prepare(to_google(client))
    response = callback(client, code, state, iss=None)
    assert response.status_code == 302 and query_of(response.headers["location"])["iss"] == BASE


def test_chatgpt_without_state_gets_none_back(client, google):
    code, state = google.prepare(to_google(client, state=None))
    assert "state" not in query_of(callback(client, code, state).headers["location"])


def test_googles_error_goes_back_to_chatgpt_with_iss(client, google):
    _, state = google.prepare(to_google(client))
    response = client.get(
        oauth.CALLBACK_PATH,
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert query_of(response.headers["location"]) == {
        "error": "access_denied",
        "state": "chatgpt-state",
        "iss": BASE,
    }


def assert_refused(response):
    assert response.status_code == 400
    assert "location" not in response.headers
    assert response.json() == {"error": "invalid_request"}


def test_callback_without_the_cookie_is_refused(client, google):
    code, state = google.prepare(to_google(client))
    client.cookies.clear()
    assert_refused(callback(client, code, state))


def test_callback_with_the_cookie_of_another_flow_is_refused(client, google):
    code, state = google.prepare(to_google(client))
    cookie = dict(client.cookies)
    client.cookies.clear()
    to_google(client)  # another authorization in another browser
    other = dict(client.cookies)
    client.cookies.clear()
    (value,) = cookie.values()
    (other_name,) = other
    client.cookies.set(other_name, value, path=oauth.CALLBACK_PATH)
    assert_refused(callback(client, code, state))


def test_cookie_with_another_value_is_refused(client, google):
    code, state = google.prepare(to_google(client))
    (name,) = dict(client.cookies)
    client.cookies.clear()
    client.cookies.set(name, "forged", path=oauth.CALLBACK_PATH)
    assert_refused(callback(client, code, state))


def test_state_with_non_ascii_is_refused(client, google):
    code, _ = google.prepare(to_google(client))
    assert_refused(callback(client, code, "e30.\u00e9"))


def test_tampered_state_is_refused(client, google):
    code, state = google.prepare(to_google(client))
    body, mac = state.split(".")
    claims = base64.urlsafe_b64decode(body + "==").replace(b"chatgpt-state", b"attacker-st")
    tampered = base64.urlsafe_b64encode(claims).rstrip(b"=").decode() + "." + mac
    assert_refused(callback(client, code, tampered))


def test_state_signed_with_another_key_is_refused(google, vm_calls):
    other = replace(SETTINGS, signing_keys=("other-key-" + "o" * 32,))
    with TestClient(build(google, other), base_url=BASE) as c:
        _, state = google.prepare(to_google(c))
    with TestClient(build(google), base_url=BASE) as c:
        code, _ = google.prepare(to_google(c))
        assert_refused(callback(c, code, state))


def test_state_signed_with_an_older_key_still_works(google, vm_calls):
    """Rotation: add a new key first; flows started under the old one still finish."""
    old = "old-key-" + "o" * 32
    with TestClient(build(google, replace(SETTINGS, signing_keys=(old,))), base_url=BASE) as c:
        code, state = google.prepare(to_google(c))
        cookies = dict(c.cookies)
    rotated = replace(SETTINGS, signing_keys=(SIGNING_KEY, old))
    with TestClient(build(google, rotated), base_url=BASE) as c:
        for name, value in cookies.items():
            c.cookies.set(name, value, path=oauth.CALLBACK_PATH)
        assert callback(c, code, state).status_code == 302


def test_expired_state_is_refused(client, google, monkeypatch):
    code, state = google.prepare(to_google(client))
    later(monkeypatch, oauth.FLOW_TTL + 1)
    assert_refused(callback(client, code, state))


@pytest.mark.parametrize(
    "params",
    [
        {"iss": "https://evil.example"},
        {"code": None},
        {"code": ""},
    ],
)
def test_unacceptable_callbacks_are_refused(client, google, params):
    code, state = google.prepare(to_google(client))
    query = {"code": code, "state": state, "iss": oauth.GOOGLE_ISSUER, **params}
    query = {k: v for k, v in query.items() if v is not None}
    assert_refused(client.get(oauth.CALLBACK_PATH, params=query, follow_redirects=False))


# --- /token --------------------------------------------------------------------------


def test_token_forwards_fixed_parameters_and_returns_googles_answer(client, google):
    code = code_for(client, google)
    response = exchange(client, code)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["refresh_token"] and body["access_token"].startswith("ya29.")
    assert google.token_requests == [
        {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": CALLBACK,  # not ChatGPT's: Google issued the code to the callback
        }
    ]


@pytest.mark.parametrize(
    "extra",
    [{"client_id": "cloud-coder"}, {"client_secret": None}, {"client_secret": ""}],
)
def test_token_needs_the_google_client(client, google, extra):
    response = (
        refresh(client, "r", **extra) if "client_id" in extra else exchange(client, "c", **extra)
    )
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_client"}
    assert google.token_requests == []


def test_wrong_client_secret_is_googles_to_refuse(client, google):
    response = exchange(client, code_for(client, google), secret="wrong")
    assert response.status_code == 401 and response.json() == {"error": "invalid_client"}


@pytest.mark.parametrize(
    "grant_type", ["password", "client_credentials", "implicit", "urn:ietf:params:x", None]
)
def test_other_grant_types_are_refused_without_asking_google(client, google, grant_type):
    form = {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_SECRET}
    if grant_type:
        form["grant_type"] = grant_type
    response = client.post(oauth.TOKEN_PATH, data=form)
    assert response.status_code == 400
    assert response.json() == {"error": "unsupported_grant_type"}
    assert google.token_requests == []


@pytest.mark.parametrize("extra", [{"code_verifier": None}, {"code": None}])
def test_incomplete_code_exchanges_are_refused(client, google, extra):
    assert exchange(client, **{"code": "c", **extra}).status_code == 400
    assert google.token_requests == []


def test_wrong_pkce_verifier_is_refused_by_google(client, google):
    response = exchange(client, code_for(client, google), verifier="w" * 64)
    assert response.status_code == 400 and response.json() == {"error": "invalid_grant"}


def test_code_is_single_use_at_google(client, google):
    code = code_for(client, google)
    assert exchange(client, code).status_code == 200
    assert exchange(client, code).status_code == 400


def test_google_unreachable_is_503(client, google):
    google.unreachable = True
    response = refresh(client, "r")
    assert response.status_code == 503 and response.json() == {"error": "temporarily_unavailable"}


# --- /mcp with Google's tokens ---------------------------------------------------------


def test_full_flow_gives_a_token_for_every_tool(client, google, vm_calls):
    tokens = grant(client, google)
    access = tokens["access_token"]
    names = {t["name"] for t in rpc(client, access, "tools/list").json()["result"]["tools"]}
    assert {"status", "up", "start_session", "send_prompt", "read_session", "stop"} <= names
    assert not call_tool(client, access, "status").get("isError")
    assert not call_tool(client, access, "stop").get("isError")  # no read-only grants
    assert vm_calls == ["status", "stop"]


def test_refresh_gives_a_new_working_access_token(client, google, vm_calls):
    tokens = grant(client, google)
    response = refresh(client, tokens["refresh_token"])
    assert response.status_code == 200
    renewed = response.json()["access_token"]
    assert renewed != tokens["access_token"]
    assert google.token_requests[-1] == {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }
    assert not call_tool(client, renewed, "status").get("isError")


def test_refresh_refused_by_google_passes_through(client, google):
    response = refresh(client, "1//revoked")
    assert response.status_code == 400 and response.json() == {"error": "invalid_grant"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "another-client.apps.googleusercontent.com"},
        {"exp": str(int(time.time()) - 1)},
        {"sub": None},
        {"sub": "2000002"},
    ],
)
def test_unacceptable_google_tokens_are_401(client, google, overrides):
    token = google.access_token(**{k: v for k, v in overrides.items() if k != "sub"})
    if "sub" in overrides:
        if overrides["sub"] is None:
            del google.access_tokens[token]["sub"]
        else:
            google.access_tokens[token]["sub"] = overrides["sub"]
    assert rpc(client, token, "tools/list").status_code == 401


def test_unknown_token_is_401(client, google):
    assert rpc(client, "ya29.unknown", "tools/list").status_code == 401


def test_account_off_the_allowlist_is_logged_by_its_sub(client, google, caplog):
    token = google.access_token(sub="2000002")
    with caplog.at_level(logging.INFO):
        assert rpc(client, token, "tools/list").status_code == 401
    assert "not on the allowlist: 2000002" in caplog.text


def test_taking_the_sub_off_the_allowlist_ends_its_access(google, vm_calls):
    token = google.access_token()
    with TestClient(build(google), base_url=BASE) as c:
        assert rpc(c, token, "tools/list").status_code == 200
    with TestClient(build(google, replace(SETTINGS, allowed_subs=frozenset())), base_url=BASE) as c:
        assert rpc(c, token, "tools/list").status_code == 401


def test_tokeninfo_answers_are_cached_for_a_minute(client, google, monkeypatch):
    token = google.access_token()
    assert rpc(client, token, "tools/list").status_code == 200
    assert rpc(client, token, "tools/list").status_code == 200
    assert google.tokeninfo_requests == 1
    later(monkeypatch, oauth.TOKENINFO_CACHE_TTL + 1)
    assert rpc(client, token, "tools/list").status_code == 200
    assert google.tokeninfo_requests == 2


def test_cache_does_not_outlive_the_token(client, google, monkeypatch):
    token = google.access_token(exp=str(int(time.time()) + 10))
    assert rpc(client, token, "tools/list").status_code == 200
    later(monkeypatch, 11)
    assert rpc(client, token, "tools/list").status_code == 401


def test_tokeninfo_unavailable_is_not_a_401(google, vm_calls):
    """A 401 would send ChatGPT to sign in again; Google's outage is not the token's fault."""
    token = google.access_token()
    google.reply = httpx2.Response(503)
    with TestClient(build(google), base_url=BASE, raise_server_exceptions=False) as c:
        assert rpc(c, token, "tools/list").status_code == 500


def test_tokeninfo_gets_the_token_in_the_body_not_the_url(client, google):
    seen = []
    handle = google.handle
    google.handle = lambda request: seen.append(str(request.url)) or handle(request)
    with TestClient(build(google), base_url=BASE) as c:
        token = google.access_token()
        assert rpc(c, token, "tools/list").status_code == 200
    assert seen == [oauth.GOOGLE_TOKENINFO_ENDPOINT]


def test_token_in_the_query_string_is_not_accepted(client, google):
    token = google.access_token()
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response = client.post(f"/mcp?access_token={token}", json=body, headers=MCP_HEADERS)
    assert response.status_code == 401


# --- logs ----------------------------------------------------------------------------


def test_logs_show_no_secrets(client, google, caplog):
    with caplog.at_level(logging.DEBUG):
        code, state = google.prepare(to_google(client))
        callback(client, code, state)
        tokens = exchange(client, code).json()
        refreshed = refresh(client, tokens["refresh_token"]).json()
        rpc(client, refreshed["access_token"], "tools/list")
        exchange(client, code, secret="wrong")
        rpc(client, "ya29.unknown", "tools/list")
    # What the server logs; the test client's own request log shows the URLs it opens.
    logged = "\n".join(r.getMessage() for r in caplog.records if BASE not in r.getMessage())
    assert "OAuth: Google granted tokens (authorization_code)" in logged
    assert "OAuth: Google granted tokens (refresh_token)" in logged
    for secret in (
        GOOGLE_SECRET,
        SIGNING_KEY,
        VERIFIER,
        code,
        state,
        tokens["access_token"],
        tokens["refresh_token"],
        refreshed["access_token"],
        "ya29.unknown",
    ):
        assert secret not in logged
