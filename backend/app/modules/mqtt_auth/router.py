"""/internal/mqtt/* — 브로커 인증 플러그인(go-auth http 백엔드)이 부르는 API (ADR-003, docs/05).

go-auth 설정: `params_mode json`, `response_mode status` — 200 이면 허용, 그 외는 거부.
본문은 JSON 이고 응답 본문은 보지 않는다(로그용으로만 짧게 준다).

    POST /internal/mqtt/auth      {username, password, clientid}  → 200 / 403
    POST /internal/mqtt/acl       {username, clientid, topic, acc} → 200 / 403
    POST /internal/mqtt/superuser {username}                       → 항상 403

규칙(사양서 §1.1.2.2):
  · username 이 UUID 형식이 아니면 403 — `server`/`solarlte-test` 는 go-auth files 백엔드 몫이다.
    (go-auth 는 백엔드를 순서대로 물어보고 하나라도 200 이면 허용한다.)
  · password == HMAC(K_i, username) 인 활성 키가 하나라도 있으면 200. 상수 시간 비교.
  · clientid 가 username 과 다르면 403 (Client ID = UUID).
  · **승인 상태로 접속을 막지 않는다.** DB 를 보지 않는다 — 처음 보는 UUID 도 200.
  · `MQTT_AUTH_SHARED_SECRET` 이 비어 있지 않으면 `X-Auth-Secret` 헤더가 같아야 한다.

비밀번호·키는 절대 로그에 남기지 않는다. 실패 로그에도 username 과 사유만 적는다.
이 라우터는 DB 세션을 쓰지 않는다 — 브로커 재시작 뒤 1만 대 재접속 폭주에서 DB 왕복이
인증 경로에 끼면 안 된다(go-auth 캐시가 있어도 첫 접속은 전부 여기로 온다).
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import BaseModel, Field

from app.config import settings
from app.constants import UUID_RE
from app.core import device_password
from app.core.metrics import metrics
from app.core.mqtt_acl import device_acl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/mqtt", tags=["mqtt-auth"], include_in_schema=False)

_DENY = status.HTTP_403_FORBIDDEN
_ALLOW = status.HTTP_200_OK


class AuthIn(BaseModel):
    username: str = ""
    password: str = ""
    clientid: str = ""


class AclIn(BaseModel):
    username: str = ""
    clientid: str = ""
    topic: str = ""
    acc: int = Field(default=0)


class SuperuserIn(BaseModel):
    username: str = ""


def _hmac_keys() -> dict[str, bytes]:
    """settings 문자열 → 키 dict. 설정이 틀리면 기동 때(main.py) 이미 죽었으므로 여기서는
    파싱 실패를 빈 dict(전부 403)로 흡수한다."""
    try:
        return device_password.parse_keys(settings.mqtt_hmac_keys)
    except ValueError:
        log.error("MQTT_HMAC_KEYS 형식 오류 — 단말 인증 전부 거부")
        return {}


def active_key_ids() -> list[str]:
    """/health 표시용. 키 값은 절대 내보내지 않는다."""
    return list(_hmac_keys())


def _secret_ok(header: str | None) -> bool:
    expected = settings.mqtt_auth_shared_secret
    if not expected:
        return True
    return header is not None and hmac.compare_digest(header, expected)


def _respond(code: int, body: dict[str, Any]) -> Response:
    # go-auth 는 상태 코드만 본다. 본문은 curl 진단용.
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=code, media_type="application/json",
    )


@router.post("/auth")
async def mqtt_auth(
    body: AuthIn, request: Request, x_auth_secret: str | None = Header(default=None)
) -> Response:
    if not _secret_ok(x_auth_secret):
        metrics.mqtt_auth_fail += 1
        log.warning("/internal/mqtt/auth 공유 비밀 불일치 (from %s)", request.client)
        return _respond(_DENY, {"ok": False, "reason": "bad_secret"})
    username = body.username.strip()
    if not UUID_RE.match(username):
        metrics.mqtt_auth_fail += 1
        return _respond(_DENY, {"ok": False, "reason": "not_uuid"})
    if body.clientid and body.clientid.strip() != username:
        metrics.mqtt_auth_fail += 1
        log.warning("MQTT 인증 거부 %s: clientid 불일치", username)
        return _respond(_DENY, {"ok": False, "reason": "clientid_mismatch"})
    key_id = device_password.verify(username, body.password, _hmac_keys())
    if key_id is None:
        metrics.mqtt_auth_fail += 1
        log.warning("MQTT 인증 거부 %s: 비밀번호 불일치", username)
        return _respond(_DENY, {"ok": False, "reason": "bad_password"})
    metrics.mqtt_auth_ok += 1
    log.debug("MQTT 인증 허용 %s (key=%s)", username, key_id)
    return _respond(_ALLOW, {"ok": True, "key": key_id})


@router.post("/acl")
async def mqtt_acl(
    body: AclIn, request: Request, x_auth_secret: str | None = Header(default=None)
) -> Response:
    if not _secret_ok(x_auth_secret):
        metrics.mqtt_acl_deny += 1
        return _respond(_DENY, {"ok": False, "reason": "bad_secret"})
    username = body.username.strip()
    allowed = device_acl(username, body.topic, body.acc, root=settings.mqtt_topic_root)
    if not allowed:
        metrics.mqtt_acl_deny += 1
        log.info("MQTT ACL 거부 %s acc=%d %s", username or "-", body.acc, body.topic)
        return _respond(_DENY, {"ok": False})
    return _respond(_ALLOW, {"ok": True})


@router.post("/superuser")
async def mqtt_superuser(body: SuperuserIn) -> Response:
    """단말에 superuser 는 없다. 항상 403."""
    return _respond(_DENY, {"ok": False})
