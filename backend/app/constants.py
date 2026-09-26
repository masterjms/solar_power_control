"""프로토콜 상수. 사양서 §1.1 의 값을 코드 곳곳에 흩뿌리지 않는다."""

from __future__ import annotations

import re
from enum import Enum

#: STM32G0B0 96-bit Unique ID → 24자리 대문자 16진수 (사양서 §1.1.3).
UUID_LENGTH = 24
UUID_RE = re.compile(r"^[0-9A-F]{24}$")

#: WD-N522S AT 버퍼 한계(사양서 §1.1.6 payload 규약). 넘으면 단말이 못 받는다.
MQTT_MAX_PAYLOAD_BYTES = 384
#: 이 크기를 넘으면 경고만 — 6차 SCHEDULE chunk 설계 때 여유를 확인할 근거가 된다.
MQTT_WARN_PAYLOAD_BYTES = 300

#: CONFIG_SET.ti 허용 범위(초). 사양서 §1.1.6 주기.
TI_MIN_SEC = 60
TI_MAX_SEC = 3600
#: CONFIG_SET.ka 허용 범위(초). 사양서 §1.1.7 (F/W 1.3.0~ 1초 단위). 운영 권장 상한은 600.
KA_MIN_SEC = 60
KA_MAX_SEC = 1800
#: `site` 는 단말 OLED 에 표시된다 — 24자 이내 (사양서 §3.9.2).
SITE_MAX_LEN = 24
#: CONFIG_SET.cv 범위. 단말은 uint16 으로 든다.
CV_MAX = 65535
#: 전역 seq 는 uint32 (사양서 §1.1.5).
SEQ_MAX = 4_294_967_295


class MsgType(str, Enum):
    """payload.type 값."""

    REGISTER = "REGISTER"
    #: 1.1.0 부터 "TELEMETRY". 1.0.0 은 "t":"TM" / 초기 2차 코드는 "type":"TM" — 셋 다 받는다.
    TELEMETRY = "TELEMETRY"
    TM = "TM"
    PONG = "PONG"
    CONFIG_ACK = "CONFIG_ACK"
    CMD_ACK = "CMD_ACK"
    LWT = "LWT"
    EV = "EV"
    # 서버 → 단말
    PING = "PING"
    CONFIG_SET = "CONFIG_SET"
    REGISTER_ACK = "REGISTER_ACK"
    CMD = "CMD"


class EventKind(str, Enum):
    """device_event.kind (docs/03)."""

    REGISTER = "REGISTER"
    #: REGISTER_ACK retain 발행 (REGISTER 응답 또는 관리자 상태 변경).
    REGISTER_ACK = "REGISTER_ACK"
    #: 브로커 로그(ADR-004) 또는 LWT 로 online 플래그가 바뀐 순간. 플래그가 안 바뀌면 안 쓴다.
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    LWT = "LWT"
    REBOOT = "REBOOT"
    LOST = "LOST"
    ERR = "ERR"
    STATE_CHANGE = "STATE_CHANGE"
    #: CONFIG_SET 발행(payload 그대로).
    CONFIG_SET = "CONFIG_SET"
    CONFIG_ACK = "CONFIG_ACK"
    CMD_ACK = "CMD_ACK"
    PONG = "PONG"


class DeviceState(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


#: 관리자 상태 전이 표 (docs/05 "상태 전이"). 여기 없는 전이는 409 INVALID_STATE_TRANSITION.
#: RETIRED → ACTIVE 는 PENDING 을 거쳐야 한다 — 빈 retain 상태에서 다시 붙는 보드는
#: "새 설치"이므로 승인 절차를 다시 밟는다.
STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    DeviceState.PENDING.value: frozenset({"ACTIVE", "REJECTED", "RETIRED"}),
    DeviceState.ACTIVE.value: frozenset({"SUSPENDED", "RETIRED", "PENDING"}),
    DeviceState.SUSPENDED.value: frozenset({"ACTIVE", "RETIRED"}),
    DeviceState.REJECTED.value: frozenset({"PENDING", "RETIRED"}),
    DeviceState.RETIRED.value: frozenset({"PENDING"}),
}


class TopicKind(str, Enum):
    REGISTER = "register"
    STATUS = "status"
    RESULT = "result"
    EVENT = "event"
