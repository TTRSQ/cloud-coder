"""/mcp of the HTTP API: the MCP server over Streamable HTTP, behind the API's own OAuth
authorization server, which has one pre-registered client and asks Google who approves."""

import base64
import hashlib
import json
import logging
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
CLIENT_SECRET = "client-secret-" + "c" * 32
SIGNING_KEY = "signing-key-" + "k" * 32
GOOGLE_CLIENT_ID = "google-client.apps.googleusercontent.com"
GOOGLE_SECRET = "google-secret-" + "g" * 20
ALLOWED_SUB = "1000001"
SETTINGS = oauth.OAuthSettings(
    public_url=BASE,
    client_secret=CLIENT_SECRET,
    signing_keys=(SIGNING_KEY,),
    google_client_id=GOOGLE_CLIENT_ID,
    google_client_secret=GOOGLE_SECRET,
    allowed_subs=frozenset({ALLOWED_SUB}),
)
ENV = {
    oauth.PUBLIC_URL_ENV: BASE,
    oauth.CLIENT_SECRET_ENV: CLIENT_SECRET,
    oauth.SIGNING_KEYS_ENV: SIGNING_KEY,
    oauth.GOOGLE_CLIENT_ID_ENV: GOOGLE_CLIENT_ID,
    oauth.GOOGLE_CLIENT_SECRET_ENV: GOOGLE_SECRET,
    oauth.ALLOWED_SUBS_ENV: ALLOWED_SUB,
}
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=")
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


class FakeGoogle:
    """Google's token endpoint: hands out the ID token prepared for a code, once."""

    def __init__(self):
        self.pending: dict[str, tuple[str, dict]] = {}  # code -> (code_challenge, claims)
        self.requests: list[dict] = []
        self.reply: httpx2.Response | None = None

    def prepare(self, google_url: str, sub=ALLOWED_SUB, code="g-code", **overrides):
        query = {k: v[0] for k, v in parse_qs(urlparse(google_url).query).items()}
        claims = {
            "iss": "https://accounts.google.com",
            "aud": GOOGLE_CLIENT_ID,
            "azp": GOOGLE_CLIENT_ID,
            "sub": sub,
            "email": "me@example.com",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "nonce": query["nonce"],
            **overrides,
        }
        self.pending[code] = (query["code_challenge"], claims)
        return code, query["state"]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == oauth.GOOGLE_TOKEN_ENDPOINT
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append(form)
        if self.reply is not None:
            return self.reply
        entry = self.pending.pop(form.get("code"), None)
        if entry is None or form.get("client_secret") != GOOGLE_SECRET:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        challenge, claims = entry
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        id_token = f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"
        return httpx2.Response(200, json={"id_token": id_token, "access_token": "ya29.google"})


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
    query = {
        "client_id": oauth.CLIENT_ID,
        "response_type": "code",
        "code_challenge": CHALLENGE.decode(),
        "code_challenge_method": "S256",
        "redirect_uri": CHATGPT,
        "state": "st",
        "scope": "read write offline_access",
        "resource": f"{BASE}/mcp",
        **params,
    }
    query = {k: v for k, v in query.items() if v is not None}
    return client.get("/authorize", params=query, follow_redirects=False)


def consent_page(client, **params):
    response = authorize(client, **params)
    assert response.status_code == 302, response.text
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == BASE + oauth.CONSENT_PATH
    sealed = parse_qs(location.query)["request"][0]
    page = client.get(oauth.CONSENT_PATH, params={"request": sealed})
    assert page.status_code == 200, page.text
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    return sealed, csrf, page


def to_google(client, **params) -> str:
    sealed, csrf, _ = consent_page(client, **params)
    response = client.post(
        oauth.CONSENT_PATH, data={"request": sealed, "csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith(oauth.GOOGLE_AUTHORIZATION_ENDPOINT + "?")
    return response.headers["location"]


def callback(client, code, state):
    return client.get(
        oauth.GOOGLE_CALLBACK_PATH, params={"code": code, "state": state}, follow_redirects=False
    )


def code_for(client, google, **params) -> str:
    code, state = google.prepare(to_google(client, **params))
    response = callback(client, code, state)
    assert response.status_code == 303, response.text
    return parse_qs(urlparse(response.headers["location"]).query)["code"][0]


def exchange(client, code, verifier=VERIFIER, secret=CLIENT_SECRET, **extra):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CHATGPT,
        "client_id": oauth.CLIENT_ID,
        "client_secret": secret,
        "code_verifier": verifier,
        "resource": f"{BASE}/mcp",
        **extra,
    }
    return client.post("/token", data={k: v for k, v in form.items() if v is not None})


def refresh(client, refresh_token, secret=CLIENT_SECRET, **extra):
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": oauth.CLIENT_ID,
        "client_secret": secret,
        **extra,
    }
    return client.post("/token", data=form)


def grant(client, google, **params) -> dict:
    response = exchange(client, code_for(client, google, **params))
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
    assert settings.redirect_uris == (CHATGPT,)
    assert settings.google_redirect_uri == BASE + oauth.GOOGLE_CALLBACK_PATH


def test_settings_do_not_show_secrets():
    shown = repr(SETTINGS)
    assert GOOGLE_CLIENT_ID in shown
    for secret in (CLIENT_SECRET, SIGNING_KEY, GOOGLE_SECRET):
        assert secret not in shown


@pytest.mark.parametrize(
    "name",
    [
        oauth.PUBLIC_URL_ENV,
        oauth.CLIENT_SECRET_ENV,
        oauth.SIGNING_KEYS_ENV,
        oauth.GOOGLE_CLIENT_ID_ENV,
        oauth.GOOGLE_CLIENT_SECRET_ENV,
    ],
)
def test_refuses_to_start_without(name):
    with pytest.raises(ConfigError, match=name):
        oauth.OAuthSettings.from_env({**ENV, name: "  "})


@pytest.mark.parametrize("name", [oauth.CLIENT_SECRET_ENV, oauth.SIGNING_KEYS_ENV])
def test_refuses_short_secrets(name):
    with pytest.raises(ConfigError, match="at least 32"):
        oauth.OAuthSettings.from_env({**ENV, name: "short"})


def test_an_empty_allowlist_starts_and_lets_nobody_approve():
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
    assert metadata["issuer"] == BASE  # exactly, no trailing slash: it is compared as a string
    assert metadata["authorization_endpoint"] == f"{BASE}/authorize"
    assert metadata["token_endpoint"] == f"{BASE}/token"
    assert "registration_endpoint" not in metadata  # the client is configured in advance
    assert "revocation_endpoint" not in metadata
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["client_secret_post"]
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert metadata["authorization_response_iss_parameter_supported"] is True


def test_protected_resource_metadata(client):
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == f"{BASE}/mcp"
    assert metadata["authorization_servers"] == [BASE]


def test_mcp_without_a_token_points_to_the_metadata(client):
    response = rpc(client, None, "tools/list")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in challenge


def test_dynamic_client_registration_is_off(client):
    body = {"redirect_uris": [CHATGPT], "token_endpoint_auth_method": "none"}
    assert client.post("/register", json=body).status_code == 404


@pytest.mark.parametrize("token", [CLIENT_SECRET, SIGNING_KEY, GOOGLE_SECRET, "ya29.google"])
def test_mcp_takes_no_other_bearer_token(client, token):
    """No static API tokens, and no Google tokens: only access tokens issued here."""
    assert rpc(client, token, "tools/list").status_code == 401


# --- the whole flow ----------------------------------------------------------------


def test_full_flow_gives_tokens_that_work_on_mcp(client, google, vm_calls):
    sealed, csrf, page = consent_page(client)
    assert "Continue with Google" in page.text and CHATGPT in page.text
    assert "start and stop the VM" in page.text  # the write scope is spelt out
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    cookie = page.headers["set-cookie"]
    assert cookie.startswith(oauth.FLOW_COOKIE + "=")
    for attribute in ("HttpOnly", "Path=/", "SameSite=lax", "Secure", "Max-Age=600"):
        assert attribute in cookie
    assert "Domain" not in cookie

    response = client.post(
        oauth.CONSENT_PATH, data={"request": sealed, "csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["cache-control"] == "no-store"
    google_url = urlparse(response.headers["location"])
    query = {k: v[0] for k, v in parse_qs(google_url.query).items()}
    assert set(query) == {
        "client_id",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
        "nonce",
        "code_challenge",
        "code_challenge_method",
    }
    assert query["client_id"] == GOOGLE_CLIENT_ID
    assert query["redirect_uri"] == BASE + oauth.GOOGLE_CALLBACK_PATH
    assert query["scope"] == "openid email" and query["code_challenge_method"] == "S256"

    code, state = google.prepare(response.headers["location"])
    response = callback(client, code, state)
    assert response.status_code == 303 and response.headers["cache-control"] == "no-store"
    assert f"{oauth.FLOW_COOKIE}=" in response.headers["set-cookie"]  # deleted
    assert (
        "Max-Age=0" in response.headers["set-cookie"]
        or "expires=" in response.headers["set-cookie"]
    )
    location = urlparse(response.headers["location"])
    reply = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == CHATGPT
    assert reply["state"] == ["st"] and reply["iss"] == [BASE]
    sent = google.requests[-1]
    assert set(sent) == {
        "grant_type",
        "code",
        "code_verifier",
        "redirect_uri",
        "client_id",
        "client_secret",
    }
    assert sent["redirect_uri"] == BASE + oauth.GOOGLE_CALLBACK_PATH

    tokens = exchange(client, reply["code"][0])
    assert tokens.status_code == 200 and tokens.headers["cache-control"] == "no-store"
    tokens = tokens.json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == oauth.ACCESS_TOKEN_TTL
    assert set(tokens["scope"].split()) == {"read", "write", "offline_access"}
    assert tokens["refresh_token"]

    assert rpc(client, tokens["access_token"], "tools/list").status_code == 200
    assert not call_tool(client, tokens["access_token"], "stop")["isError"]
    assert vm_calls == ["stop"]


def test_no_scope_requested_gives_every_scope(client, google):
    tokens = grant(client, google, scope=None)
    assert tokens["scope"] == "read write offline_access" and tokens["refresh_token"]


def test_no_refresh_token_without_offline_access(client, google):
    tokens = grant(client, google, scope="read write")
    assert tokens["scope"] == "read write" and "refresh_token" not in tokens


def test_narrowed_refresh_keeps_the_grant(client, google):
    """RFC 6749 6: a new refresh token has the scope of the one presented."""
    tokens = grant(client, google)
    narrowed = refresh(client, tokens["refresh_token"], scope="read").json()
    assert narrowed["scope"] == "read"
    widened = refresh(client, narrowed["refresh_token"], scope="read write").json()
    assert widened["scope"] == "read write"


def test_read_scope_alone_cannot_call_write_tools(client, google, vm_calls):
    tokens = grant(client, google, scope="read")
    assert tokens["scope"] == "read"
    assert not call_tool(client, tokens["access_token"], "status")["isError"]
    tools = rpc(client, tokens["access_token"], "tools/list").json()["result"]["tools"]
    writing = [t for t in tools if not t["annotations"].get("readOnlyHint")]
    assert {t["name"] for t in writing} == {"up", "start_session", "send_prompt", "stop"}
    for tool in writing:
        arguments = {"session": "s", "text": "go"} if tool["name"] == "send_prompt" else {}
        result = call_tool(client, tokens["access_token"], tool["name"], arguments)
        assert result["isError"] and "read-only" in result["content"][0]["text"]
    assert vm_calls == ["status"]


def test_unknown_scope_is_refused(client):
    response = authorize(client, scope="read admin")
    reply = parse_qs(urlparse(response.headers["location"]).query)
    assert reply["error"] == ["invalid_scope"] and reply["iss"] == [BASE]


def test_write_implies_read(client, google, vm_calls):
    tokens = grant(client, google, scope="write write offline_access")
    assert tokens["scope"] == "read write offline_access"
    narrowed = refresh(client, tokens["refresh_token"], scope="write").json()
    assert narrowed["scope"] == "read write"
    assert not call_tool(client, narrowed["access_token"], "stop")["isError"]


# --- the authorization request -----------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/cb",
        "https://chatgpt.com/connector/oauth/abc123",
        "https://chatgpt.com/connector_platform_oauth_redirect/x",
        "https://chatgpt.com/connector_platform_oauth_redirect?x=1",
        "http://chatgpt.com/connector_platform_oauth_redirect",
    ],
)
def test_other_redirect_uris_are_refused_without_redirecting(client, uri):
    response = authorize(client, redirect_uri=uri)
    assert response.status_code == 400 and "location" not in response.headers


def test_unknown_client_is_refused_without_redirecting(client):
    response = authorize(client, client_id="someone-else")
    assert response.status_code == 400 and "location" not in response.headers


def test_plain_pkce_is_refused(client):
    response = authorize(client, code_challenge_method="plain")
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == CHATGPT
    reply = parse_qs(location.query)
    assert reply["error"] == ["invalid_request"] and reply["state"] == ["st"]
    assert reply["iss"] == [BASE]  # RFC 9207: in error responses too


def test_other_resource_is_refused(client):
    response = authorize(client, resource="https://other.example/mcp")
    assert response.status_code == 302
    reply = parse_qs(urlparse(response.headers["location"]).query)
    assert reply["error"] == ["invalid_target"] and reply["iss"] == [BASE]


def test_base_url_is_accepted_as_the_resource(client):
    response = authorize(client, resource=BASE)
    assert response.headers["location"].startswith(BASE + oauth.CONSENT_PATH)


# --- the consent page --------------------------------------------------------------


@pytest.mark.parametrize("sealed", ["", "abc.def", "x" * 40])
def test_invalid_authorization_request_is_400(client, sealed):
    assert client.get(oauth.CONSENT_PATH, params={"request": sealed}).status_code == 400
    response = client.post(oauth.CONSENT_PATH, data={"request": sealed, "csrf": "x"})
    assert response.status_code == 400


def test_tampered_authorization_request_is_400(client):
    sealed, csrf, _ = consent_page(client)
    body, mac = sealed.split(".")
    claims = json.loads(oauth._b64decode(body))
    forged = b64({**claims, "redirect_uri": "https://evil.example/cb"})
    response = client.post(oauth.CONSENT_PATH, data={"request": f"{forged}.{mac}", "csrf": csrf})
    assert response.status_code == 400


def test_expired_authorization_request_is_400(client, monkeypatch):
    sealed, csrf, _ = consent_page(client)
    later(monkeypatch, oauth.FLOW_TTL + 1)
    response = client.post(oauth.CONSENT_PATH, data={"request": sealed, "csrf": csrf})
    assert response.status_code == 400


def test_consent_without_the_cookie_is_refused(client):
    """A cross-site form post carries no SameSite=Lax cookie."""
    sealed, csrf, _ = consent_page(client)
    client.cookies.clear()
    response = client.post(
        oauth.CONSENT_PATH, data={"request": sealed, "csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 403 and "location" not in response.headers


def test_consent_with_another_csrf_token_is_refused(client):
    sealed, _, _ = consent_page(client)
    response = client.post(
        oauth.CONSENT_PATH, data={"request": sealed, "csrf": "guess"}, follow_redirects=False
    )
    assert response.status_code == 403


# --- the Google callback -----------------------------------------------------------


def test_callback_without_the_cookie_is_refused(client, google):
    code, state = google.prepare(to_google(client))
    client.cookies.clear()
    assert callback(client, code, state).status_code == 400
    assert google.requests == []  # Google is not asked


def test_callback_with_the_cookie_of_another_flow_is_refused(client, google):
    code, state = google.prepare(to_google(client))
    to_google(client)  # a second flow replaces the cookie
    response = callback(client, code, state)
    assert response.status_code == 400 and google.requests == []


def test_tampered_state_is_refused(client, google):
    code, state = google.prepare(to_google(client))
    body, mac = state.split(".")
    claims = json.loads(oauth._b64decode(body))
    claims["request"]["redirect_uri"] = "https://evil.example/cb"
    response = callback(client, code, f"{b64(claims)}.{mac}")
    assert response.status_code == 400 and "location" not in response.headers


def test_state_signed_with_another_key_is_refused(client, google):
    code, state = google.prepare(to_google(client))
    claims = json.loads(oauth._b64decode(state.split(".")[0]))
    other = oauth.SignedTokens(("other-key-" + "o" * 32,)).seal("state", claims, claims["exp"])
    assert callback(client, code, other).status_code == 400


def test_expired_state_is_refused(client, google, monkeypatch):
    code, state = google.prepare(to_google(client))
    later(monkeypatch, oauth.FLOW_TTL + 1)
    assert callback(client, code, state).status_code == 400


def test_callback_is_single_use(client, google):
    google_url = to_google(client)
    code, state = google.prepare(google_url)
    cookies = dict(client.cookies)
    assert callback(client, code, state).status_code == 303
    google.prepare(google_url)  # even if Google took the code again
    client.cookies.update(cookies)  # and the browser kept the cookie
    assert callback(client, code, state).status_code == 400


def test_cancelled_google_sign_in_goes_back_to_the_client(client, google):
    _, state = google.prepare(to_google(client))
    response = client.get(
        oauth.GOOGLE_CALLBACK_PATH,
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 303
    reply = parse_qs(urlparse(response.headers["location"]).query)
    assert reply == {"error": ["access_denied"], "state": ["st"], "iss": [BASE]}
    assert google.requests == []


def test_account_not_on_the_allowlist_gets_no_code(client, google):
    code, state = google.prepare(to_google(client), sub="2000002")
    response = callback(client, code, state)
    assert response.status_code == 403 and "location" not in response.headers
    assert "2000002" in response.text and oauth.ALLOWED_SUBS_ENV in response.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "another-client.apps.googleusercontent.com"},
        {"azp": "another-client.apps.googleusercontent.com"},
        {"iss": "https://evil.example"},
        {"nonce": "another-nonce"},
        {"exp": int(time.time()) - 10},
        {"sub": ""},
    ],
)
def test_unacceptable_id_token_gets_no_code(client, google, overrides):
    code, state = google.prepare(to_google(client), **overrides)
    response = callback(client, code, state)
    assert response.status_code == 502 and "location" not in response.headers


def test_google_refusing_the_code_gets_no_code(client, google):
    _, state = google.prepare(to_google(client))
    response = callback(client, "not-the-code", state)
    assert response.status_code == 502 and "location" not in response.headers


def test_google_unreachable_gets_no_code(client, google):
    _, state = google.prepare(to_google(client))

    def unreachable(request):
        raise httpx2.ConnectError("down")

    google.handle = unreachable  # type: ignore[method-assign]
    with TestClient(build(google), base_url=BASE) as other:
        other.cookies.update(client.cookies)
        assert callback(other, "g-code", state).status_code == 502


# --- the token endpoint ------------------------------------------------------------


def test_code_is_single_use(client, google):
    code = code_for(client, google)
    assert exchange(client, code).status_code == 200
    assert exchange(client, code).json()["error"] == "invalid_grant"


def test_wrong_pkce_verifier_is_refused(client, google):
    response = exchange(client, code_for(client, google), verifier="w" * 64)
    assert response.json()["error"] == "invalid_grant"


def test_code_presented_with_a_wrong_verifier_stays_usable(client, google):
    code = code_for(client, google)
    assert exchange(client, code, verifier="w" * 64).status_code == 400
    assert exchange(client, code).status_code == 200


@pytest.mark.parametrize("secret", [None, "wrong-" + "x" * 32])
def test_token_endpoint_needs_the_client_secret(client, google, secret):
    code = code_for(client, google)
    assert exchange(client, code, secret=secret).status_code == 401
    assert exchange(client, code).status_code == 200


def test_forged_code_is_refused(client, google):
    code = code_for(client, google)
    body, mac = code.split(".")
    claims = json.loads(oauth._b64decode(body))
    forged = b64({**claims, "scopes": ["read", "write", "admin"]})
    assert exchange(client, f"{forged}.{mac}").json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "grant_type",
    [
        "client_credentials",
        "password",
        "implicit",
        "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "urn:ietf:params:oauth:grant-type:device_code",
    ],
)
def test_other_grant_types_are_refused(client, grant_type):
    response = client.post(
        "/token",
        data={
            "grant_type": grant_type,
            "client_id": oauth.CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "assertion": "x",
            "username": "u",
            "password": "p",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] in ("unsupported_grant_type", "invalid_request")


def test_refresh_gives_a_working_access_token(client, google):
    tokens = grant(client, google)
    response = refresh(client, tokens["refresh_token"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert rpc(client, response.json()["access_token"], "tools/list").status_code == 200


def test_refresh_needs_the_client_secret(client, google):
    tokens = grant(client, google)
    assert refresh(client, tokens["refresh_token"], secret="wrong-" + "x" * 32).status_code == 401


def test_refresh_cannot_widen_the_scope(client, google):
    tokens = grant(client, google, scope="read offline_access")
    response = refresh(client, tokens["refresh_token"], scope="read write")
    assert response.json()["error"] == "invalid_scope"


def test_access_token_is_not_a_refresh_token(client, google):
    tokens = grant(client, google)
    assert refresh(client, tokens["access_token"]).json()["error"] == "invalid_grant"


def test_unused_refresh_token_expires(client, google, monkeypatch):
    tokens = grant(client, google)
    later(monkeypatch, oauth.REFRESH_TOKEN_TTL + 1)
    assert refresh(client, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_grant_ends_its_lifetime_after_approval(client, google, monkeypatch):
    tokens = grant(client, google)
    # Refreshing within the lifetime keeps the time of approval ...
    for days in (10, 20, 29):
        later(monkeypatch, days * 86400)
        response = refresh(client, tokens["refresh_token"])
        assert response.status_code == 200, response.text
        tokens = response.json()
        assert tokens["expires_in"] <= oauth.ACCESS_TOKEN_TTL
    # ... so the grant ends GRANT_LIFETIME after approval, however often it was refreshed.
    later(monkeypatch, oauth.GRANT_LIFETIME + 1)
    assert refresh(client, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert rpc(client, tokens["access_token"], "tools/list").status_code == 401


def test_access_tokens_end_with_the_grant(client, google, monkeypatch):
    tokens = grant(client, google)
    for seconds in (13 * 86400, 26 * 86400, oauth.GRANT_LIFETIME - 600):
        later(monkeypatch, seconds)
        tokens = refresh(client, tokens["refresh_token"]).json()
    assert 0 < tokens["expires_in"] <= 600


def test_expired_access_token_is_401(client, google, monkeypatch):
    tokens = grant(client, google)
    later(monkeypatch, oauth.ACCESS_TOKEN_TTL + 1)
    response = rpc(client, tokens["access_token"], "tools/list")
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_forged_access_token_is_401(client, google):
    tokens = grant(client, google)
    body, mac = tokens["access_token"].split(".")
    claims = json.loads(oauth._b64decode(body))
    forged = b64({**claims, "exp": claims["exp"] + 10**6})
    assert rpc(client, f"{forged}.{mac}", "tools/list").status_code == 401


def test_token_for_another_resource_is_401(client):
    signed = oauth.SignedTokens((SIGNING_KEY,))
    claims = {"scopes": ["read"], "sub": ALLOWED_SUB, "approved_at": int(time.time())}
    exp = int(time.time()) + 60
    token = signed.seal("access", {**claims, "aud": "https://other.example/mcp"}, exp)
    assert rpc(client, token, "tools/list").status_code == 401
    token = signed.seal("access", {**claims, "aud": f"{BASE}/mcp"}, exp)
    assert rpc(client, token, "tools/list").status_code == 200


def test_token_in_the_query_string_is_not_accepted(client, google):
    tokens = grant(client, google)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response = client.post(
        f"/mcp?access_token={tokens['access_token']}", json=body, headers=MCP_HEADERS
    )
    assert response.status_code == 401


# --- revocation and key rotation ---------------------------------------------------


def test_taking_the_sub_off_the_allowlist_ends_its_grants(vm_calls, google):
    with TestClient(build(google), base_url=BASE) as c:
        tokens = grant(c, google)
        code = code_for(c, google)
    removed = replace(SETTINGS, allowed_subs=frozenset())
    with TestClient(build(google, removed), base_url=BASE) as c:
        assert rpc(c, tokens["access_token"], "tools/list").status_code == 401
        assert refresh(c, tokens["refresh_token"]).json()["error"] == "invalid_grant"
        assert exchange(c, code).json()["error"] == "invalid_grant"


def test_signing_keys_rotate_without_breaking_grants(vm_calls, google):
    with TestClient(build(google), base_url=BASE) as c:
        tokens = grant(c, google)
    new_key = "new-signing-key-" + "n" * 32
    both = replace(SETTINGS, signing_keys=(new_key, SIGNING_KEY))
    with TestClient(build(google, both), base_url=BASE) as c:
        assert rpc(c, tokens["access_token"], "tools/list").status_code == 200
        tokens = refresh(c, tokens["refresh_token"]).json()  # now signed with the new key
    new_only = replace(SETTINGS, signing_keys=(new_key,))
    with TestClient(build(google, new_only), base_url=BASE) as c:
        assert rpc(c, tokens["access_token"], "tools/list").status_code == 200
        assert refresh(c, tokens["refresh_token"]).status_code == 200
    old_only = replace(SETTINGS, signing_keys=("other-key-" + "o" * 32,))
    with TestClient(build(google, old_only), base_url=BASE) as c:
        assert rpc(c, tokens["access_token"], "tools/list").status_code == 401


# --- logs --------------------------------------------------------------------------


def test_logs_show_no_secrets(client, google, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    google_url = to_google(client)
    secrets_seen = [CLIENT_SECRET, SIGNING_KEY, GOOGLE_SECRET, "ya29.google", "g-code", VERIFIER]
    code, state = google.prepare(google_url)
    secrets_seen += [state, client.cookies[oauth.FLOW_COOKIE]]
    secrets_seen += [parse_qs(urlparse(google_url).query)["nonce"][0]]
    response = callback(client, code, state)
    our_code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    tokens = exchange(client, our_code).json()
    refreshed = refresh(client, tokens["refresh_token"]).json()
    secrets_seen += [our_code, tokens["access_token"], tokens["refresh_token"]]
    secrets_seen += [refreshed["access_token"], refreshed["refresh_token"]]
    # and the failures
    exchange(client, our_code)
    refresh(client, tokens["refresh_token"], secret="wrong-" + "x" * 32)
    callback(client, code, state)
    google.reply = httpx2.Response(
        400, json={"error": "invalid_grant", "error_description": GOOGLE_SECRET}
    )
    code2, state2 = google.prepare(to_google(client), code="g-code-2")
    secrets_seen += [state2, "g-code-2"]
    callback(client, code2, state2)

    # The test client's own request log (httpx2, to cc.example) is not the server's.
    logged = "\n".join(
        r.getMessage()
        for r in caplog.records
        if not (r.name.startswith("httpx") and BASE in r.getMessage())
    )
    assert "refreshed a grant" in logged and "approved a grant" in logged
    assert "Google sign-in failed: Google token endpoint: HTTP 400 invalid_grant" in logged
    for secret in secrets_seen:
        assert secret not in logged
