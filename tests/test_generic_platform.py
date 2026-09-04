"""Offline tests for the config-driven generic platform adapter + prober.

Uses httpx.MockTransport so no network is touched. Covers CTFd-shaped,
rCTF-shaped, and a fully custom API, plus probe detection and validation.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.platforms.generic import GenericHTTPClient, ctfd_adapter, rctf_adapter
from backend.platforms.probe import probe_platform, validate_adapter

GOOD = "SAS{good}"


# --------------------------------------------------------------------------- CTFd
def ctfd_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/challenges":
        assert request.headers.get("Authorization") == "Token tok123"
        return httpx.Response(200, json={"success": True, "data": [
            {"id": 1, "name": "baby-rop", "category": "pwn", "value": 100, "solves": 42, "type": "standard"},
            {"id": 2, "name": "quantum", "category": "crypto", "value": 300, "solves": 9},
            {"id": 3, "name": "hidden-one", "category": "misc", "value": 50, "type": "hidden"},
        ]})
    if path == "/api/v1/challenges/1":
        return httpx.Response(200, json={"data": {
            "id": 1, "name": "baby-rop", "category": "pwn", "value": 100,
            "description": "smash it", "connection_info": "nc host 1337", "files": ["/files/vuln"]}})
    if path == "/api/v1/challenges/attempt":
        body = json.loads(request.content)
        status = "correct" if body.get("submission") == GOOD else "incorrect"
        return httpx.Response(200, json={"data": {"status": status, "message": ""}})
    return httpx.Response(404, json={"success": False})


def ctfd_client(base="https://ctf.example") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(ctfd_handler), base_url=base)


async def test_ctfd_adapter_list_and_submit():
    gc = GenericHTTPClient(base_url="https://ctf.example", token="tok123",
                           adapter=ctfd_adapter(), _client=ctfd_client())
    stubs = await gc.fetch_challenge_stubs()
    names = {s["name"] for s in stubs}
    assert {"baby-rop", "quantum"} <= names
    assert stubs[0]["value"] == 100

    ok = await gc.submit_flag("baby-rop", GOOD)
    assert ok.status == "correct"
    bad = await gc.submit_flag("baby-rop", "SAS{nope}")
    assert bad.status == "incorrect"
    await gc.close()


async def test_ctfd_detail_fetch():
    gc = GenericHTTPClient(base_url="https://ctf.example", token="tok123",
                           adapter=ctfd_adapter(), _client=ctfd_client())
    full = await gc.fetch_all_challenges()
    baby = next(c for c in full if c["name"] == "baby-rop")
    assert "smash it" in baby["description"]
    assert baby["connection_info"] == "nc host 1337"
    await gc.close()


async def test_probe_detects_ctfd():
    res = await probe_platform("https://ctf.example", token="tok123", client=ctfd_client())
    assert res.kind == "ctfd"
    assert res.confidence >= 0.8
    assert res.adapter is not None


async def test_validate_adapter_rejects_wrong_flag():
    ok, msg = await validate_adapter("https://ctf.example", "tok123", ctfd_adapter(), client=ctfd_client())
    assert ok, msg
    assert "rejected" in msg.lower() or "challenges" in msg.lower()


async def test_validate_flags_broken_success_matcher():
    # A matcher that treats every response as correct must be caught.
    bad_adapter = ctfd_adapter()
    bad_adapter["submit"]["success"] = {"status_path": "data.status",
                                        "correct_values": ["correct", "incorrect"]}
    ok, msg = await validate_adapter("https://ctf.example", "tok123", bad_adapter, client=ctfd_client())
    assert not ok
    assert "wrong flag was accepted" in msg.lower()


# --------------------------------------------------------------------------- rCTF
def rctf_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/challs":
        return httpx.Response(200, json={"kind": "goodChallenges", "data": [
            {"id": "abc", "name": "web1", "category": "web", "points": 50, "files": [{"url": "/f/a"}]},
        ]})
    if path == "/api/v1/challs/abc/submit":
        body = json.loads(request.content)
        kind = "goodFlag" if body.get("flag") == GOOD else "badFlag"
        return httpx.Response(200, json={"kind": kind, "message": kind})
    if path == "/api/v1/users/me":
        return httpx.Response(200, json={"data": {"solves": [{"name": "web1"}]}})
    return httpx.Response(404)


def rctf_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(rctf_handler), base_url="https://rctf.example")


async def test_rctf_adapter():
    gc = GenericHTTPClient(base_url="https://rctf.example", token="teamtok",
                           adapter=rctf_adapter(), _client=rctf_client())
    stubs = await gc.fetch_challenge_stubs()
    assert stubs[0]["name"] == "web1"
    assert stubs[0]["value"] == 50
    assert stubs[0]["files"] == ["/f/a"]
    solved = await gc.fetch_solved_names()
    assert "web1" in solved
    good = await gc.submit_flag("web1", GOOD)
    assert good.status == "correct"
    await gc.close()


async def test_probe_detects_rctf():
    res = await probe_platform("https://rctf.example", token="teamtok", client=rctf_client())
    assert res.kind == "rctf"


# ------------------------------------------------------------------------- custom
def custom_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/challenges":
        if request.headers.get("X-API-Key") != "k":
            return httpx.Response(401)
        return httpx.Response(200, json=[
            {"id": 7, "title": "Custom One", "cat": "misc", "score": 10},
            {"id": 8, "title": "Custom Two", "cat": "web", "score": 20},
        ])
    if path == "/api/solve/7":
        body = json.loads(request.content)
        return httpx.Response(200, json={"result": "correct" if body.get("answer") == GOOD else "wrong"})
    return httpx.Response(404)


def custom_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(custom_handler), base_url="https://custom.example")


async def test_probe_discovers_custom_and_asks_for_submit():
    res = await probe_platform("https://custom.example", token="k", client=custom_client())
    assert res.kind == "generic"
    assert res.adapter is not None
    # name should be aliased from "title"
    assert res.adapter["list"]["fields"]["name"] == "title"
    # it cannot infer the submit endpoint, so it must ask
    q_ids = {q.id for q in res.questions}
    assert "submit.path" in q_ids


async def test_custom_adapter_after_operator_fills_submit():
    # Operator answers the questions -> a complete adapter that works.
    adapter = {
        "auth": {"mode": "token_header", "header": "X-API-Key", "prefix": ""},
        "list": {"method": "GET", "path": "/api/challenges", "items_path": "",
                 "fields": {"name": "title", "points": "score", "category": "cat"}},
        "submit": {"method": "POST", "path": "/api/solve/{id}",
                   "body_template": {"answer": "{flag}"},
                   "success": {"status_path": "result", "correct_values": ["correct"],
                               "incorrect_values": ["wrong"]}},
    }
    gc = GenericHTTPClient(base_url="https://custom.example", token="k", adapter=adapter, _client=custom_client())
    stubs = await gc.fetch_challenge_stubs()
    assert {s["name"] for s in stubs} == {"Custom One", "Custom Two"}
    assert stubs[0]["value"] == 10
    good = await gc.submit_flag("Custom One", GOOD)
    assert good.status == "correct"
    bad = await gc.submit_flag("Custom One", "x")
    assert bad.status == "incorrect"
    await gc.close()


async def test_probe_unknown_when_nothing_found():
    def empty(request): return httpx.Response(404)
    client = httpx.AsyncClient(transport=httpx.MockTransport(empty), base_url="https://nope.example")
    res = await probe_platform("https://nope.example", token="", client=client)
    assert res.kind == "unknown"
    assert res.adapter is None
    assert any(q.id == "list.path" for q in res.questions)


@pytest.mark.parametrize("hint,expect", [("ctfd", "ctfd"), ("rctf", "rctf")])
async def test_probe_forced_hint(hint, expect):
    res = await probe_platform("https://x.example", token="t", platform_hint=hint,
                               client=ctfd_client())
    assert res.kind == expect
