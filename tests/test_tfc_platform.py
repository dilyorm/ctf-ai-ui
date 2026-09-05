"""Offline tests for The Few Chosen platform client and instance broker.

Uses httpx.MockTransport and loopback sockets, so no network is touched.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json

import httpx
import pytest

from backend.platforms import SUPPORTED_PLATFORMS, make_platform_client
from backend.platforms.tfc import TFCClient, derive_endpoints
from backend.platforms.tfc_instances import Deployment, InstanceBroker

GOOD = "TFCCTF{real_flag}"
CAT = "cat-uuid"
DIFF = "diff-uuid"
CHALL = "chall-uuid"
FLAG = "flag-uuid"


def _jwt(ttl_s: int = 600, username: str = "Xylo") -> str:
    """A structurally valid unsigned JWT — the client only reads ``exp``."""
    exp = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl_s)).timestamp())
    payload = base64.urlsafe_b64encode(
        json.dumps({"username": username, "exp": exp}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.sig"


def _challenge_body(solved: bool = False) -> dict:
    return {
        "categories": [{"id": CAT, "name": "PWN"}],
        "difficulties": [{"id": DIFF, "name": "BABY"}],
        "challenges": [
            {
                "challenge_id": CHALL,
                "challenge_name": "V8 motor",
                "challenge_author": "minipif",
                "category_id": CAT,
                "difficulty_id": DIFF,
                "description": "<b>the sun is a deadly laser</b>",
                "flags": [{"flag_id": FLAG, "flag_points": 278, "is_solved": solved}],
                "files": [{"file_name": "m.zip", "file_url": "https://api.tfc.test/challenge-files/m.zip"}],
                "is_dynamic": True,
                "amount_solves": 44,
                "connection_type": "netcat",
                "image_name": "motor-v8",
                "http_only": False,
            },
            {
                "challenge_id": "static-uuid",
                "challenge_name": "rules",
                "category_id": CAT,
                "difficulty_id": DIFF,
                "description": "read them",
                "flags": [{"flag_id": "f2", "flag_points": 1, "is_solved": True}],
                "files": [],
                "is_dynamic": False,
                "amount_solves": 100,
                "connection_type": None,
                "image_name": None,
                "http_only": None,
            },
        ],
    }


class Recorder:
    def __init__(self) -> None:
        self.logins = 0
        self.refreshes = 0
        self.submits: list[dict] = []
        self.solved = False


def make_client(rec: Recorder, login_ttl: int = 600) -> TFCClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":
            body = json.loads(request.content)
            if body.get("username_or_email") != "Xylo" or body.get("password") != "pw":
                return httpx.Response(401, json={"error": "bad credentials"})
            rec.logins += 1
            return httpx.Response(
                200, json={"access_token": _jwt(login_ttl), "refresh_token": "rt-1"}
            )
        if path == "/auth/refresh":
            rec.refreshes += 1
            return httpx.Response(
                200, json={"access_token": _jwt(login_ttl), "refresh_token": "rt-2"}
            )
        if request.headers.get("Authorization", "") == "":
            return httpx.Response(401, json={"error": "unauthorized"})
        if path == "/challenge":
            return httpx.Response(200, json=_challenge_body(solved=rec.solved))
        if path == "/challenge/submit":
            body = json.loads(request.content)
            rec.submits.append(body)
            if body.get("flag") == GOOD:
                rec.solved = True
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(400, json={"code": "invalid_flag", "error": "invalid flag"})
        if path.startswith("/challenge-files/"):
            return httpx.Response(200, content=b"PK\x03\x04payload")
        return httpx.Response(404, json={"error": "not found"})

    client = TFCClient(base_url="https://tfc.test", username="Xylo", password="pw")
    client._client = httpx.AsyncClient(
        base_url="https://api.tfc.test", transport=httpx.MockTransport(handler)
    )
    return client


# ------------------------------------------------------------------ endpoints
def test_derive_endpoints_from_site_url():
    api, manager, domain = derive_endpoints("https://ctf.thefewchosen.com")
    assert api == "https://api.ctf.thefewchosen.com"
    assert manager == "https://challenge-manager.management.ctf.thefewchosen.com"
    assert domain == "challs.ctf.thefewchosen.com"


def test_derive_endpoints_accepts_api_host_and_trailing_slash():
    assert derive_endpoints("https://api.ctf.thefewchosen.com/")[0] == (
        "https://api.ctf.thefewchosen.com"
    )


def test_registered_as_supported_platform():
    assert "tfc" in SUPPORTED_PLATFORMS
    client = make_platform_client("tfc", "https://ctf.thefewchosen.com", username="u", password="p")
    assert isinstance(client, TFCClient)
    assert client.username == "u"


# ----------------------------------------------------------------- challenges
@pytest.mark.asyncio
async def test_fetch_challenges_normalizes_shape():
    rec = Recorder()
    client = make_client(rec)
    challenges = await client.fetch_all_challenges()

    by_name = {c["name"]: c for c in challenges}
    motor = by_name["V8 motor"]
    assert motor["id"] == CHALL
    assert motor["category"] == "PWN"
    assert motor["tags"] == ["BABY"]
    assert motor["value"] == 278
    assert motor["solves"] == 44
    assert motor["files"] == ["https://api.tfc.test/challenge-files/m.zip"]
    assert motor["is_dynamic"] and motor["image_name"] == "motor-v8"
    assert rec.logins == 1
    await client.close()


@pytest.mark.asyncio
async def test_solved_names_require_every_flag_solved():
    rec = Recorder()
    client = make_client(rec)
    assert await client.fetch_solved_names() == {"rules"}
    await client.close()


@pytest.mark.asyncio
async def test_login_happens_once_then_token_is_reused():
    rec = Recorder()
    client = make_client(rec)
    await client.fetch_challenge_stubs()
    await client.fetch_challenge_stubs()
    assert rec.logins == 1
    assert rec.refreshes == 0
    await client.close()


@pytest.mark.asyncio
async def test_expiring_token_is_refreshed_not_re_logged_in():
    rec = Recorder()
    # A 30s token is inside the renewal margin, so every call renews it.
    client = make_client(rec, login_ttl=30)
    await client.fetch_challenge_stubs()
    await client.fetch_challenge_stubs()
    assert rec.logins == 1
    assert rec.refreshes >= 1
    await client.close()


@pytest.mark.asyncio
async def test_login_failure_surfaces_clearly():
    rec = Recorder()
    client = make_client(rec)
    client.password = "wrong"
    with pytest.raises(RuntimeError, match="TFC login failed"):
        await client.fetch_challenge_stubs()
    await client.close()


@pytest.mark.asyncio
async def test_missing_credentials_are_reported_not_silently_retried():
    client = TFCClient(base_url="https://tfc.test")
    client._client = httpx.AsyncClient(
        base_url="https://api.tfc.test",
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})),
    )
    with pytest.raises(RuntimeError, match="username and password"):
        await client.fetch_challenge_stubs()
    await client.close()


# --------------------------------------------------------------------- submit
@pytest.mark.asyncio
async def test_submit_correct_flag():
    rec = Recorder()
    client = make_client(rec)
    result = await client.submit_flag("V8 motor", GOOD)
    assert result.status == "correct"
    assert rec.submits[0] == {"challenge_id": CHALL, "flag_id": FLAG, "flag": GOOD}
    await client.close()


@pytest.mark.asyncio
async def test_submit_wrong_flag_is_incorrect_not_an_error():
    rec = Recorder()
    client = make_client(rec)
    result = await client.submit_flag("V8 motor", "TFCCTF{nope}")
    assert result.status == "incorrect"
    assert "invalid flag" in result.display
    await client.close()


@pytest.mark.asyncio
async def test_submit_unknown_challenge_raises():
    rec = Recorder()
    client = make_client(rec)
    with pytest.raises(RuntimeError, match="not found"):
        await client.submit_flag("does not exist", GOOD)
    await client.close()


# ----------------------------------------------------------------------- pull
@pytest.mark.asyncio
async def test_pull_challenge_writes_metadata_and_distfiles(tmp_path):
    import yaml

    rec = Recorder()
    client = make_client(rec)
    client.manage_instances = False  # no instance proxy in tests
    challenges = await client.fetch_all_challenges()
    motor = next(c for c in challenges if c["name"] == "V8 motor")

    ch_dir = await client.pull_challenge(motor, str(tmp_path))
    meta = yaml.safe_load((tmp_path / "v8-motor" / "metadata.yml").read_text(encoding="utf-8"))

    assert ch_dir.endswith("v8-motor")
    assert meta["category"] == "PWN"
    assert meta["value"] == 278
    # HTML description is converted to markdown, as for CTFd.
    assert "deadly laser" in meta["description"]
    assert (tmp_path / "v8-motor" / "distfiles" / "m.zip").read_bytes().startswith(b"PK")
    await client.close()


# ------------------------------------------------------------------ instances
def _broker(handler, **kw) -> InstanceBroker:
    async def auth() -> dict[str, str]:
        return {"Authorization": "Bearer test"}

    return InstanceBroker(
        manager_url="https://cm.tfc.test",
        challenge_domain="challs.tfc.test",
        auth_headers=auth,
        client=httpx.AsyncClient(
            base_url="https://cm.tfc.test", transport=httpx.MockTransport(handler)
        ),
        bind_host="127.0.0.1",
        **kw,
    )


def _iso(minutes: int) -> str:
    return (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.asyncio
async def test_endpoint_is_stable_and_does_not_provision_eagerly():
    started: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            started.append(json.loads(request.content)["name"])
            return httpx.Response(201, json={"deploymentName": "motor-v8-aaaa"})
        return httpx.Response(200, json={"data": []})

    broker = _broker(handler)
    first = await broker.endpoint("motor-v8", "netcat", False)
    second = await broker.endpoint("motor-v8", "netcat", False)
    assert first == second
    assert first.startswith("nc host.docker.internal ")
    # Nothing is started until a solver actually connects.
    assert started == []
    await broker.close()


@pytest.mark.asyncio
async def test_http_challenges_advertise_a_url():
    broker = _broker(lambda r: httpx.Response(200, json={"data": []}))
    endpoint = await broker.endpoint("vaultkeeper", "http", True)
    assert endpoint.startswith("http://host.docker.internal:")
    await broker.close()


@pytest.mark.asyncio
async def test_live_deployment_is_adopted_rather_than_restarted():
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append("post")
            return httpx.Response(201, json={"deploymentName": "motor-v8-new"})
        return httpx.Response(
            200, json={"data": [{"name": "motor-v8-live", "expiresAt": _iso(10)}]}
        )

    broker = _broker(handler)
    from backend.platforms.tfc_instances import _Instance

    inst = _Instance(image="motor-v8", connection_type="netcat", http_only=False)
    dep = await broker.ensure_deployment(inst)
    assert dep.name == "motor-v8-live"
    assert posts == []
    await broker.close()


@pytest.mark.asyncio
async def test_expired_deployment_is_replaced():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            calls.append("delete")
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            calls.append("post")
            return httpx.Response(201, json={"deploymentName": "motor-v8-fresh"})
        # Stale first, fresh once re-created.
        name, expiry = ("motor-v8-fresh", _iso(15)) if "post" in calls else ("motor-v8-old", _iso(0))
        return httpx.Response(200, json={"data": [{"name": name, "expiresAt": expiry}]})

    broker = _broker(handler)
    from backend.platforms.tfc_instances import _Instance

    inst = _Instance(image="motor-v8", connection_type="netcat", http_only=False)
    dep = await broker.ensure_deployment(inst)
    assert dep.name == "motor-v8-fresh"
    assert calls == ["delete", "post"]
    await broker.close()


@pytest.mark.asyncio
async def test_web_instance_waits_for_the_pod_behind_the_ingress(monkeypatch):
    """The ingress accepts TCP before the app serves, so 503 must mean 'not yet'."""
    import backend.platforms.tfc_instances as mod

    broker = _broker(
        lambda r: httpx.Response(
            200, json={"data": [{"name": "vaultkeeper-abc", "expiresAt": _iso(10)}]}
        )
    )
    monkeypatch.setattr(mod, "UPSTREAM_READY_TIMEOUT_S", 0.5)
    monkeypatch.setattr(
        InstanceBroker, "_upstream", lambda self, inst, dep: ("127.0.0.1", 1, False, "")
    )
    codes = iter([503, 503, 200])
    seen: list[int] = []

    async def ready(self, inst, dep):
        code = next(codes, 200)
        seen.append(code)
        return code not in (502, 503, 504)

    monkeypatch.setattr(InstanceBroker, "_http_ready", ready)

    inst = mod._Instance(image="vaultkeeper", connection_type="http", http_only=True)
    with pytest.raises(RuntimeError):
        # Port 1 never connects; we only care that 503s were retried first.
        await broker._connect_upstream(inst)
    assert seen[:2] == [503, 503]
    await broker.close()


def test_deployment_freshness_accounts_for_the_teardown_margin():
    soon = Deployment("x", dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10))
    later = Deployment("x", dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10))
    assert not soon.fresh()
    assert later.fresh()


@pytest.mark.asyncio
async def test_tcp_proxy_pipes_bytes_to_the_current_deployment(monkeypatch):
    """A solver connecting to the stable port reaches the live instance."""
    seen: list[bytes] = []

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(100)
        seen.append(data)
        writer.write(b"flag prompt> ")
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"name": "motor-v8-live", "expiresAt": _iso(10)}]}
        )

    broker = _broker(handler)
    # Point the "netcat" upstream at the local echo server instead of TLS:1337.
    monkeypatch.setattr(
        InstanceBroker, "_upstream", lambda self, inst, dep: ("127.0.0.1", up_port, False, "")
    )

    endpoint = await broker.endpoint("motor-v8", "netcat", False)
    port = int(endpoint.rsplit(" ", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"hello")
    await writer.drain()
    assert await reader.read(100) == b"flag prompt> "
    assert seen == [b"hello"]

    writer.close()
    await broker.close()
    upstream.close()
    await upstream.wait_closed()


@pytest.mark.asyncio
async def test_http_proxy_rewrites_host_for_the_ingress(monkeypatch):
    """Web instances are routed by Host, so the proxy must rewrite it."""
    received: list[bytes] = []

    async def sink(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read(4096))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(sink, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]

    broker = _broker(
        lambda r: httpx.Response(
            200, json={"data": [{"name": "vaultkeeper-abc", "expiresAt": _iso(10)}]}
        )
    )
    monkeypatch.setattr(
        InstanceBroker, "_upstream", lambda self, inst, dep: ("127.0.0.1", up_port, False, "")
    )

    async def ready(self, inst, dep):
        return True

    monkeypatch.setattr(InstanceBroker, "_http_ready", ready)

    endpoint = await broker.endpoint("vaultkeeper", "http", True)
    port = int(endpoint.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /flag HTTP/1.1\r\nHost: host.docker.internal\r\nAccept: */*\r\n\r\n")
    await writer.drain()
    await reader.read(200)

    sent = received[0]
    assert b"Host: vaultkeeper-abc.challs.tfc.test\r\n" in sent
    assert b"Host: host.docker.internal" not in sent
    assert sent.startswith(b"GET /flag HTTP/1.1\r\n")

    writer.close()
    await broker.close()
    upstream.close()
    await upstream.wait_closed()
