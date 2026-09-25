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
#: CONFIG_SET.cv 범위. 단말은 uint16 으로 든다.
CV_MAX = 65535
#: 전역 seq 는 uint32 (사양서 §1.1.5).
SEQ_MAX = 4_294_967_295


class MsgType(str, Enum):
    """payload.type 값."""

    REGISTER = "REGISTER"
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
    LWT = "LWT"
    REBOOT = "REBOOT"
    LOST = "LOST"
    ERR = "ERR"
    STATE_CHANGE = "STATE_CHANGE"
    CONFIG_ACK = "CONFIG_ACK"
    CMD_ACK = "CMD_ACK"
    PONG = "PONG"


class DeviceState(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class TopicKind(str, Enum):
    REGISTER = "register"
    STATUS = "status"
    RESULT = "result"
    EVENT = "event"
