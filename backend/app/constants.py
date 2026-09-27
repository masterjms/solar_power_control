"""프로토콜 상수. 사양서 §1.1 의 값을 코드 곳곳에 흩뿌리지 않는다."""

from __future__ import annotations

import re
from enum import Enum

#: STM32G0B0 96-bit Unique ID → 24자리 대문자 16진수 (사양서 §1.1.3).
UUID_LENGTH = 24
UUID_RE = re.compile(r"^[0-9A-F]{24}$")

#: 서버 → 단말 발행 상한(ADR-007). 단말 수신 줄 1,024B 가 한계이고 실측 950B 까지 정상
#: (UI_항목_명세 8.4). 예전 384B 는 단말 → 서버 AT 발행 버퍼 이야기였다. 넘으면 발행 전에 막는다.
MQTT_MAX_PAYLOAD_BYTES = 900
#: 이 크기를 넘으면 경고만(SETTINGS_SET 최대 약 560B).
MQTT_WARN_PAYLOAD_BYTES = 800

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
    #: 5차 이전 초안 이름. 받기만 한다(COMMAND_ACK 와 같이 처리).
    CMD_ACK = "CMD_ACK"
    #: 5차 원격 명령 응답(사양서 §3.10.11).
    COMMAND_ACK = "COMMAND_ACK"
    LWT = "LWT"
    EV = "EV"
    # 서버 → 단말
    PING = "PING"
    CONFIG_SET = "CONFIG_SET"
    REGISTER_ACK = "REGISTER_ACK"
    CMD = "CMD"
    #: 5차 원격 제어(사양서 §3.10.7). "CMD" 로 줄이지 않는다.
    COMMAND = "COMMAND"
    #: S-23 단말 설정(UI_항목_명세 8.4, ADR-007). 서버 → 단말 GET/SET, 단말 → 서버 SETTINGS/ACK.
    SETTINGS_GET = "SETTINGS_GET"
    SETTINGS_SET = "SETTINGS_SET"
    SETTINGS = "SETTINGS"
    SETTINGS_ACK = "SETTINGS_ACK"


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
    #: 5차 전 이름. 새로 쓰지 않는다(읽기만 — 이전 이력).
    CMD_ACK = "CMD_ACK"
    #: 5차 COMMAND 개별 발송(첫 발송·재시도). 그룹/전체 발송은 command 행 하나로 충분해 안 쓴다.
    COMMAND_SENT = "COMMAND_SENT"
    COMMAND_ACK = "COMMAND_ACK"
    PONG = "PONG"
    #: S-23 SETTINGS_GET/SET 발송(첫 발송·재발송, payload 그대로 + attempt·by).
    SETTINGS_SENT = "SETTINGS_SENT"
    #: S-23 단말 보고(SETTINGS)·응답(SETTINGS_ACK) 원본.
    SETTINGS = "SETTINGS"
    SETTINGS_ACK = "SETTINGS_ACK"


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


# ── 5차: 법정동 트리 · COMMAND (ADR-005, 사양서 §3.10) ─────────────────────
class RegionLevel(str, Enum):
    """region.level. 말단(그룹)은 dong 뿐이다 — bjd_code 를 가진 노드."""

    SIDO = "sido"
    SIGUNGU = "sigungu"
    DONG = "dong"


#: 법정동코드 10자리(카카오 `b_code` 앞 10자리). group_id = 이것 + 확장 "00" (§3.10.4).
BJD_CODE_RE = re.compile(r"^[0-9]{10}$")
GROUP_SUFFIX = "00"

#: COMMAND.act (사양서 §17).
COMMAND_ACTS = ("on", "off", "pwm", "auto")
#: COMMAND.ch — 1 주등, 2 입간판, 3 예비(§3.10.7). 관리 화면은 1·2 만 보인다.
COMMAND_CHANNELS = (1, 2, 3)
COMMAND_DEFAULT_CH = (1, 2)
#: COMMAND.dur 범위(초). auto 는 dur 이 없다.
DUR_MIN_SEC = 1
DUR_MAX_SEC = 86_400
#: 화면 유지시간 버튼(§3.9.3 #4). tonight 은 suntable 로 계산한다.
DUR_PRESETS = {"30m": 1_800, "1h": 3_600, "3h": 10_800}
DUR_PRESET_TONIGHT = "tonight"


class TargetStatus(str, Enum):
    """command_target.status. pending 외에는 단말 COMMAND_ACK.result 그대로(§3.10.11)."""

    PENDING = "pending"
    OK = "OK"
    LOCAL = "LOCAL"
    EXPIRED = "EXPIRED"
    BAD = "BAD"
    STATE = "STATE"


#: 단말이 보낼 수 있는 result 값.
ACK_RESULTS = frozenset({"OK", "LOCAL", "EXPIRED", "BAD", "STATE"})
#: 다시 보내 볼 만한 상태 — 무응답과 늦게 도착해 버려진 것(ADR-005).
RETRYABLE_STATUSES = frozenset({TargetStatus.PENDING.value, TargetStatus.EXPIRED.value})
#: 받은 것으로 끝난 상태. EXPIRED 는 시도를 다 쓴 경우에만 종결로 본다.
TERMINAL_STATUSES = frozenset({"OK", "LOCAL", "BAD", "STATE"})


class OverrideLevel(str, Enum):
    """device.override_level — 단말 override 슬롯 계층(§3.10.8). 개별 > 그룹 > 전체."""

    DEVICE = "device"
    GROUP = "group"
    ALL = "all"


#: 높을수록 우선.
OVERRIDE_PRIORITY = {"device": 3, "group": 2, "all": 1}
