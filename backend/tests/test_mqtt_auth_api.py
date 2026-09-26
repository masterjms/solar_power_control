"""/internal/mqtt/* — go-auth http 백엔드 규약(200/403). DB·브로커 없이 앱만 띄운다."""

from __future__ import annotations

import httpx
import pytest

from app.config import settings
from app.main import app

U = "20363930594D50170004003A"
PW = "70e8a87fba4a997f7c24d1f98300753a"


@pytest.fixture
async def client():
    # lifespan 을 돌리지 않는다(DB·MQTT 필요). /internal/* 는 app.state 를 안 쓴다.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_auth_ok_and_fail(client):
    r = await client.post("/internal/mqtt/auth",
                          json={"username": U, "password": PW, "clientid": U})
    assert r.status_code == 200
    r = await client.post("/internal/mqtt/auth",
                          json={"username": U, "password": PW[:-1] + "0", "clientid": U})
    assert r.status_code == 403
    r = await client.post("/internal/mqtt/auth",
                          json={"username": U, "password": PW, "clientid": "other"})
    assert r.status_code == 403
    # 서버/공용 계정은 files 백엔드 몫 → 여기서는 403
    r = await client.post("/internal/mqtt/auth",
                          json={"username": "server", "password": "x", "clientid": "backend"})
    assert r.status_code == 403
    # clientid 비어 있으면 검사하지 않는다
    r = await client.post("/internal/mqtt/auth", json={"username": U, "password": PW})
    assert r.status_code == 200


async def test_auth_does_not_echo_password(client):
    r = await client.post("/internal/mqtt/auth",
                          json={"username": U, "password": PW, "clientid": U})
    assert PW not in r.text


async def test_acl_table(client):
    async def acl(topic: str, acc: int) -> int:
        r = await client.post("/internal/mqtt/acl",
                              json={"username": U, "clientid": U, "topic": topic, "acc": acc})
        return r.status_code

    assert await acl(f"iotlight/device/{U}/status", 2) == 200
    assert await acl(f"iotlight/device/{U}/config", 4) == 200
    assert await acl("iotlight/group/4141011000/cmd", 1) == 200
    assert await acl("iotlight/all/cmd", 4) == 200
    assert await acl("iotlight/#", 4) == 403
    assert await acl("iotlight/device/+/status", 4) == 403
    assert await acl("iotlight/device/00112233445566778899AABB/status", 2) == 403
    assert await acl("iotlight/all/cmd", 2) == 403


async def test_superuser_always_denied(client):
    r = await client.post("/internal/mqtt/superuser", json={"username": U})
    assert r.status_code == 403


async def test_shared_secret_header(client, monkeypatch):
    monkeypatch.setattr(settings, "mqtt_auth_shared_secret", "s3cret")
    body = {"username": U, "password": PW, "clientid": U}
    assert (await client.post("/internal/mqtt/auth", json=body)).status_code == 403
    r = await client.post("/internal/mqtt/auth", json=body, headers={"X-Auth-Secret": "wrong"})
    assert r.status_code == 403
    r = await client.post("/internal/mqtt/auth", json=body, headers={"X-Auth-Secret": "s3cret"})
    assert r.status_code == 200
    r = await client.post("/internal/mqtt/acl",
                          json={"username": U, "clientid": U, "topic": "iotlight/all/cmd",
                                "acc": 1})
    assert r.status_code == 403
