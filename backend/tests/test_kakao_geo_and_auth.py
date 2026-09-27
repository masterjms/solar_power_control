"""카카오 주소 응답 매핑 · 역할 판정 · REGISTER_ACK grp (ADR-005)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.core import kakao_geo
from app.core.auth import ROLE_ADMIN, ROLE_SUPER, resolve_principal
from app.errors import GeoUnavailable
from app.mqtt.config_sync import register_ack_job_for
from app.mqtt.publisher import register_ack_payload

FIXTURE = Path(__file__).parent / "fixtures" / "kakao_address_geumjeong.json"
U = "00112233445566778899AABB"


def _data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_kakao_mapping_geumsan_ro_91():
    results = kakao_geo.map_documents(_data())
    # 같은 법정동 두 번째 행과 b_code 없는 도로명 행은 빠진다
    assert [r.bjd_code for r in results] == ["4141010400", "3611025023"]
    first = results[0]
    assert first.model_dump() == {
        "address_name": "경기 군포시 금산로 91", "bjd_code": "4141010400",
        "sido": "경기도", "sigungu": "군포시", "dong": "금정동",
        "lat": 37.3617, "lon": 126.9352,
    }


def test_kakao_mapping_sejong_has_no_sigungu():
    sejong = kakao_geo.map_documents(_data())[1]
    assert sejong.sido == "세종특별자치시" and sejong.sigungu == "세종특별자치시"
    assert sejong.dong == "조치원읍 원리"


def test_kakao_mapping_rejects_bad_code_and_coords():
    assert kakao_geo.map_document({"address": {"b_code": "12345", "x": "1", "y": "2"}}) is None
    doc = {"address": {"b_code": "4141010400", "region_1depth_name": "경기",
                       "region_3depth_name": "금정동"}, "x": "abc", "y": "1"}
    assert kakao_geo.map_document(doc) is None
    assert kakao_geo.map_documents({"documents": None}) == []


def test_normalize_sido():
    assert kakao_geo.normalize_sido("서울") == "서울특별시"
    assert kakao_geo.normalize_sido("강원") == "강원특별자치도"
    assert kakao_geo.normalize_sido("경기도") == "경기도"


async def test_search_without_key_is_503(monkeypatch):
    monkeypatch.setattr(kakao_geo.settings, "kakao_rest_api_key", "")
    with pytest.raises(GeoUnavailable) as e:
        await kakao_geo.search_address("금산로 91")
    assert e.value.status_code == 503 and e.value.detail["reason"] == "NO_KEY"


async def test_search_sends_kakaoak_header_and_maps(monkeypatch):
    monkeypatch.setattr(kakao_geo.settings, "kakao_rest_api_key", "secret-key")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["query"] = request.url.params.get("query")
        return httpx.Response(200, json=_data())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await kakao_geo.search_address("경기도 군포시 금산로 91", client=client)
    assert seen == {"auth": "KakaoAK secret-key", "query": "경기도 군포시 금산로 91"}
    assert results[0].bjd_code == "4141010400"


async def test_search_http_error_is_503_without_key_in_detail(monkeypatch):
    monkeypatch.setattr(kakao_geo.settings, "kakao_rest_api_key", "secret-key")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    ) as client:
        with pytest.raises(GeoUnavailable) as e:
            await kakao_geo.search_address("x" * 5, client=client)
    assert e.value.detail == {"reason": "HTTP", "status": 401}
    assert "secret-key" not in e.value.message


# ── 역할 ─────────────────────────────────────────────────────────────────
SUPERS = frozenset({"admin", "boss"})


@pytest.mark.parametrize("header, env, user, role", [
    ("admin", "prod", "admin", ROLE_SUPER),
    (" boss ", "prod", "boss", ROLE_SUPER),
    ("operator", "prod", "operator", ROLE_ADMIN),
    ("operator", "dev", "operator", ROLE_ADMIN),
    (None, "dev", "local", ROLE_SUPER),
    ("", "dev", "local", ROLE_SUPER),
    (None, "prod", "anonymous", ROLE_ADMIN),
    ("  ", "prod", "anonymous", ROLE_ADMIN),
])
def test_resolve_principal(header, env, user, role):
    p = resolve_principal(header, super_users=SUPERS, app_env=env)
    assert (p.user, p.role) == (user, role)
    assert p.is_super == (role == ROLE_SUPER)


def test_settings_super_users_and_approve_default(monkeypatch):
    monkeypatch.delenv("APPROVE_REQUIRES_NODE", raising=False)
    s = Settings(_env_file=None, SUPER_ADMIN_USERS="admin, boss ,", app_env="prod")
    assert s.super_admin_users == frozenset({"admin", "boss"})
    assert s.approve_requires_node is True
    assert Settings(_env_file=None, app_env="dev").approve_requires_node is False
    monkeypatch.setenv("APPROVE_REQUIRES_NODE", "true")
    assert Settings(_env_file=None, app_env="dev").approve_requires_node is True
    monkeypatch.setenv("APPROVE_REQUIRES_NODE", "false")
    assert Settings(_env_file=None, app_env="prod").approve_requires_node is False


# ── REGISTER_ACK grp ─────────────────────────────────────────────────────
def test_register_ack_carries_grp_whenever_assigned():
    job = register_ack_job_for(uuid=U, state="PENDING", site=None, reason=None,
                               grp="411711010000")
    assert job.grp == "411711010000"
    p = register_ack_payload(uuid=U, state="ACTIVE", site="금정역 앞 공원", grp=job.grp)
    assert list(p) == ["type", "uuid", "state", "site", "grp"]
    assert register_ack_job_for(uuid=U, state="ACTIVE", site=None, reason=None, grp="").grp is None
    assert register_ack_job_for(uuid=U, state="ACTIVE", site=None, reason=None).grp is None
