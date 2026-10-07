"""/mcp of the HTTP API: the MCP server over Streamable HTTP, behind the API's bearer
tokens and its OAuth authorization server."""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from cloud_coder import connect, gce, http_api, oauth
from cloud_coder.config import Config, ConfigError

CFG = Config(project="p")
BASE = "https://cc.example"
SETTINGS = oauth.OAuthSettings(BASE)
ENV = {http_api.READ_TOKENS_ENV: "read-1", http_api.WRITE_TOKENS_ENV: "write-1,write-2"}
CHATGPT = "https://chatgpt.com/connector_platform_oauth_redirect"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=")
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def build(env=ENV, settings=SETTINGS):
    return http_api.build_app(CFG, http_api.ApiTokens.from_env(env), settings)


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
def client(vm_calls):
    with TestClient(build(), base_url=BASE) as c:
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


def register(client, **metadata):
    body = {"redirect_uris": [CHATGPT], "token_endpoint_auth_method": "none", **metadata}
    return client.post("/register", json=body)


def authorize(client, client_id, **params):
    query = {
        "client_id": client_id,
        "response_type": "code",
        "code_challenge": CHALLENGE.decode(),
        "code_challenge_method": "S256",
        "redirect_uri": CHATGPT,
        "state": "st",
        "resource": f"{BASE}/mcp",
        **params,
    }
    return client.get("/authorize", params=query, follow_redirects=False)


def approval_request(client, client_id) -> str:
    response = authorize(client, client_id)
    assert response.status_code == 302, response.text
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == BASE + oauth.APPROVAL_PATH
    return parse_qs(location.query)["request"][0]


def approve(client, sealed, token="write-2"):
    return client.post(
        oauth.APPROVAL_PATH, data={"request": sealed, "token": token}, follow_redirects=False
    )


def code_for(client, client_id) -> str:
    response = approve(client, approval_request(client, client_id))
    assert response.status_code == 303, response.text
    return parse_qs(urlparse(response.headers["location"]).query)["code"][0]


def exchange(client, client_id, code, verifier=VERIFIER, **extra):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CHATGPT,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": f"{BASE}/mcp",
        **extra,
    }
    return client.post("/token", data=form)


def grant(client) -> tuple[str, dict]:
    client_id = register(client).json()["client_id"]
    response = exchange(client, client_id, code_for(client, client_id))
    assert response.status_code == 200, response.text
    return client_id, response.json()


# --- settings ----------------------------------------------------------------------


def test_refuses_to_start_without_tokens():
    with pytest.raises(ConfigError, match="no API token"):
        http_api.ApiTokens.from_env({http_api.READ_TOKENS_ENV: " , "})


def test_public_url_is_required():
    with pytest.raises(ConfigError, match="no public URL"):
        oauth.OAuthSettings.from_env({})


@pytest.mark.parametrize(
    "url", ["http://cc.example", "https://cc.example/api", "https://cc.example?a=1", "cc.example"]
)
def test_public_url_must_be_a_bare_https_origin(url):
    with pytest.raises(ConfigError):
        oauth.OAuthSettings.from_env({oauth.PUBLIC_URL_ENV: url})


def test_settings_from_env():
    settings = oauth.OAuthSettings.from_env(
        {oauth.PUBLIC_URL_ENV: BASE + "/", oauth.REDIRECT_URIS_ENV: " http://localhost/cb, "}
    )
    assert settings == oauth.OAuthSettings(BASE, ("http://localhost/cb",))
    assert oauth.OAuthSettings.from_env({oauth.PUBLIC_URL_ENV: BASE}).redirect_uris == (
        oauth.CHATGPT_REDIRECT_URIS
    )


def test_oauth_needs_a_write_token():
    with pytest.raises(ConfigError, match="write token"):
        build(env={http_api.READ_TOKENS_ENV: "read-1"})


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
    assert metadata["registration_endpoint"] == f"{BASE}/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert "revocation_endpoint" not in metadata


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


# --- the API's own bearer tokens ---------------------------------------------------


def test_read_token_lists_tools_and_calls_read_only_ones(client, vm_calls):
    tools = rpc(client, "read-1", "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} >= {"status", "stop", "send_prompt"}
    result = call_tool(client, "read-1", "status")
    assert not result["isError"] and json.loads(result["content"][0]["text"]) == {"vm": "x"}
    assert vm_calls == ["status"]


def test_read_token_cannot_call_any_tool_that_is_not_read_only(client, vm_calls):
    tools = rpc(client, "read-1", "tools/list").json()["result"]["tools"]
    writing = [t for t in tools if not t["annotations"].get("readOnlyHint")]
    assert {t["name"] for t in writing} == {"up", "start_session", "send_prompt", "stop"}
    for tool in writing:
        arguments = {"session": "s", "text": "go"} if tool["name"] == "send_prompt" else {}
        result = call_tool(client, "read-1", tool["name"], arguments)
        assert result["isError"] and "read-only" in result["content"][0]["text"]
    assert vm_calls == []


def test_write_token_calls_write_tools(client, vm_calls):
    result = call_tool(client, "write-1", "stop")
    assert not result["isError"] and vm_calls == ["stop"]


def test_unknown_token_is_401(client):
    assert rpc(client, "nope", "tools/list").status_code == 401


# --- client registration -----------------------------------------------------------


def test_registration_needs_no_storage(client, vm_calls):
    response = register(client, client_name="ChatGPT")
    assert response.status_code == 201
    client_id = response.json()["client_id"]
    # Another instance (a restart) with the same tokens knows the client.
    with TestClient(build(), base_url=BASE) as restarted:
        assert approval_request(restarted, client_id)


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/cb",
        "https://chatgpt.com/connector/oauth/",
        "https://chatgpt.com/connector/oauth/a/b",
        "https://chatgpt.com/connector_platform_oauth_redirect/x",
    ],
)
def test_registration_refuses_other_redirect_uris(client, uri):
    response = register(client, redirect_uris=[CHATGPT, uri])
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_registration_accepts_the_chatgpt_callback_pattern(client):
    response = register(client, redirect_uris=["https://chatgpt.com/connector/oauth/abc123"])
    assert response.status_code == 201


def test_forged_client_id_is_unknown(client):
    client_id = register(client).json()["client_id"]
    body, mac = client_id.split(".")
    forged = oauth._b64encode(json.dumps({"redirect_uris": ["https://evil.example"]}).encode())
    response = authorize(client, f"{forged}.{mac}", redirect_uri="https://evil.example")
    assert response.status_code == 400 and "not found" in response.json()["error_description"]


# --- authorization -----------------------------------------------------------------


def test_full_flow_gives_tokens_that_work_on_mcp(client, vm_calls):
    client_id = register(client).json()["client_id"]
    sealed = approval_request(client, client_id)

    page = client.get(oauth.APPROVAL_PATH, params={"request": sealed})
    assert page.status_code == 200 and 'type="password"' in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"

    response = approve(client, sealed)
    assert response.status_code == 303
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == CHATGPT
    assert query["state"] == ["st"] and query["iss"] == [BASE]

    tokens = exchange(client, client_id, query["code"][0]).json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == oauth.ACCESS_TOKEN_TTL
    assert set(tokens["scope"].split()) == {"read", "write"}

    assert rpc(client, tokens["access_token"], "tools/list").status_code == 200
    assert not call_tool(client, tokens["access_token"], "stop")["isError"]
    assert vm_calls == ["stop"]


def test_wrong_write_token_is_refused_and_rate_limited(client):
    sealed = approval_request(client, register(client).json()["client_id"])
    for _ in range(oauth.MAX_FAILED_APPROVALS):
        response = approve(client, sealed, token="read-1")
        assert response.status_code == 401 and "not a write token" in response.text
    assert approve(client, sealed, token="read-1").status_code == 429
    assert approve(client, sealed).status_code == 429  # even the right token, for a while


@pytest.mark.parametrize("sealed", ["", "abc.def", "x" * 40])
def test_invalid_approval_request_is_400(client, sealed):
    assert client.get(oauth.APPROVAL_PATH, params={"request": sealed}).status_code == 400
    assert approve(client, sealed).status_code == 400


def test_unregistered_redirect_uri_is_refused_without_redirecting(client):
    client_id = register(client).json()["client_id"]
    response = authorize(client, client_id, redirect_uri="https://chatgpt.com/connector/oauth/x")
    assert response.status_code == 400


def test_other_resource_is_refused(client):
    client_id = register(client).json()["client_id"]
    response = authorize(client, client_id, resource="https://other.example/mcp")
    assert response.status_code == 302
    assert parse_qs(urlparse(response.headers["location"]).query)["error"] == ["invalid_target"]


def test_base_url_is_accepted_as_the_resource(client):
    client_id = register(client).json()["client_id"]
    response = authorize(client, client_id, resource=BASE)
    assert response.headers["location"].startswith(BASE + oauth.APPROVAL_PATH)


def test_code_is_single_use(client):
    client_id = register(client).json()["client_id"]
    code = code_for(client, client_id)
    assert exchange(client, client_id, code).status_code == 200
    assert exchange(client, client_id, code).json()["error"] == "invalid_grant"


def test_wrong_pkce_verifier_is_refused(client):
    client_id = register(client).json()["client_id"]
    response = exchange(client, client_id, code_for(client, client_id), verifier="w" * 64)
    assert response.json()["error"] == "invalid_grant"


def test_code_of_another_client_is_refused(client):
    first = register(client).json()["client_id"]
    second = register(client, client_name="other").json()["client_id"]
    assert exchange(client, second, code_for(client, first)).json()["error"] == "invalid_grant"


def test_confidential_client_needs_its_secret(client):
    registered = register(client, token_endpoint_auth_method="client_secret_post").json()
    client_id, secret = registered["client_id"], registered["client_secret"]
    code = code_for(client, client_id)
    assert exchange(client, client_id, code, client_secret="nope").status_code == 401
    code = code_for(client, client_id)
    assert exchange(client, client_id, code, client_secret=secret).status_code == 200


# --- tokens ------------------------------------------------------------------------


def test_refresh_gives_a_working_access_token(client):
    client_id, tokens = grant(client)
    response = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
    )
    assert response.status_code == 200, response.text
    assert rpc(client, response.json()["access_token"], "tools/list").status_code == 200


def test_access_token_is_not_a_refresh_token(client):
    client_id, tokens = grant(client)
    response = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["access_token"],
            "client_id": client_id,
        },
    )
    assert response.json()["error"] == "invalid_grant"


def test_expired_access_token_is_401(client, monkeypatch):
    _, tokens = grant(client)
    later = time.time() + oauth.ACCESS_TOKEN_TTL + 1
    monkeypatch.setattr(oauth.time, "time", lambda: later)
    response = rpc(client, tokens["access_token"], "tools/list")
    assert (
        response.status_code == 401 and "resource_metadata=" in response.headers["www-authenticate"]
    )


def test_forged_access_token_is_401(client):
    _, tokens = grant(client)
    body, mac = tokens["access_token"].split(".")
    claims = json.loads(oauth._b64decode(body))
    forged = oauth._b64encode(json.dumps({**claims, "exp": claims["exp"] + 10**6}).encode())
    assert rpc(client, f"{forged}.{mac}", "tools/list").status_code == 401


def test_token_for_another_resource_is_401(client):
    signed = oauth.SignedTokens((b"write-1",))
    claims = {"client_id": "c", "scopes": ["read", "write"], "aud": "https://other.example/mcp"}
    token = signed.seal("access", claims, ttl=60)
    assert rpc(client, token, "tools/list").status_code == 401
    token = signed.seal("access", {**claims, "aud": f"{BASE}/mcp"}, ttl=60)
    assert rpc(client, token, "tools/list").status_code == 200


def test_removing_the_approving_write_token_revokes_its_grants(vm_calls):
    with TestClient(build(), base_url=BASE) as c:
        _, tokens = grant(c)  # approved with write-2
    kept = {**ENV, http_api.WRITE_TOKENS_ENV: "write-2,write-3"}
    with TestClient(build(env=kept), base_url=BASE) as c:
        assert rpc(c, tokens["access_token"], "tools/list").status_code == 200
    removed = {**ENV, http_api.WRITE_TOKENS_ENV: "write-1"}
    with TestClient(build(env=removed), base_url=BASE) as c:
        assert rpc(c, tokens["access_token"], "tools/list").status_code == 401


def test_narrowing_the_redirect_allowlist_drops_registered_clients(vm_calls):
    with TestClient(build(), base_url=BASE) as c:
        client_id = register(c).json()["client_id"]
    narrowed = oauth.OAuthSettings(BASE, ("https://chatgpt.com/connector/oauth/*",))
    with TestClient(build(settings=narrowed), base_url=BASE) as c:
        assert authorize(c, client_id).status_code == 400


def test_code_presented_without_the_verifier_stays_usable(client):
    client_id = register(client).json()["client_id"]
    code = code_for(client, client_id)
    assert exchange(client, client_id, code, verifier="w" * 64).status_code == 400
    assert exchange(client, client_id, code).status_code == 200
