"""MQTT 토픽 문자열 (사양서 §1.1.3).

토픽을 문자열 리터럴로 흩뿌리지 않는다. 오타 하나가 "명령이 조용히 안 감"으로 나타나서
디버깅이 매우 어렵기 때문이다.

    iotlight/device/<uuid>/register   D→S  단말 기본정보      QoS1
    iotlight/device/<uuid>/status     D→S  Telemetry(TM)     QoS0 (서버는 QoS1 로 구독)
    iotlight/device/<uuid>/result     D→S  PONG/CONFIG_ACK/CMD_ACK  QoS1
    iotlight/device/<uuid>/event      D→S  LWT / EV          QoS1
    iotlight/device/<uuid>/cmd        S→D  PING/CMD/SCH/OTA  QoS1  retain=False (절대)
    iotlight/device/<uuid>/config     S→D  CONFIG_SET(비retain) · REGISTER_ACK(retain, 3차)
    iotlight/group/<grp>/cmd          S→D  5차
    iotlight/all/cmd                  S→D  5차

uuid 는 24자리 **대문자** 16진수. 소문자가 오면 형식 위반으로 버린다 — DB CHECK 와
브로커 ACL(%u = username) 모두 대문자를 전제한다.
"""

from __future__ import annotations

import re

from app.config import settings
from app.constants import TopicKind

ROOT = settings.mqtt_topic_root

_UUID = r"[0-9A-F]{24}"
_INBOUND_RE = re.compile(
    rf"^{re.escape(ROOT)}/device/({_UUID})/(register|status|result|event)$"
)


# ── 서버 → 단말 ──────────────────────────────────────────────────────────
def device_cmd(uuid: str) -> str:
    return f"{ROOT}/device/{uuid}/cmd"


def device_config(uuid: str) -> str:
    return f"{ROOT}/device/{uuid}/config"


def group_cmd(group_id: str) -> str:
    return f"{ROOT}/group/{group_id}/cmd"


def all_cmd() -> str:
    return f"{ROOT}/all/cmd"


# ── 단말 → 서버 ──────────────────────────────────────────────────────────
def device_register(uuid: str) -> str:
    return f"{ROOT}/device/{uuid}/register"


def device_status(uuid: str) -> str:
    return f"{ROOT}/device/{uuid}/status"


def device_result(uuid: str) -> str:
    return f"{ROOT}/device/{uuid}/result"


def device_event(uuid: str) -> str:
    return f"{ROOT}/device/{uuid}/event"


#: 백엔드가 구독하는 패턴. (토픽, QoS). status 는 단말이 QoS0 으로 보내지만 구독 QoS 는
#: 상한일 뿐이라 1 로 둬도 QoS0 메시지는 QoS0 으로 온다.
SUBSCRIPTIONS: tuple[tuple[str, int], ...] = (
    (f"{ROOT}/device/+/register", 1),
    (f"{ROOT}/device/+/status", 1),
    (f"{ROOT}/device/+/result", 1),
    (f"{ROOT}/device/+/event", 1),
)


def parse_inbound(topic: str) -> tuple[str, TopicKind] | None:
    """수신 토픽 → (uuid, kind). 형식이 안 맞으면 None.

    브로커에 엉뚱한 토픽이 섞여도 수신 루프가 죽지 않도록 예외 대신 None 을 준다.
    호출부가 malformed_topic 을 센다.
    """
    match = _INBOUND_RE.match(topic)
    if match is None:
        return None
    return match.group(1), TopicKind(match.group(2))
