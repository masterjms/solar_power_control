"""가짜 단말 한 대 — 사양서(2026-09-26 개정, 펌웨어 1.4.0)를 그대로 흉내낸 소프트웨어 모델.

실물(STM32 + WD-N522S)이 없어도 서버를 개발·시험하려고 만든다. 흉내내는 것:

  · 접속 (§1.1.2): Client ID = UUID 24자리, keepalive = `ka`(기본 300), clean session.
    2cha 모드는 username = UUID, password = HMAC-SHA256(K, UUID)[:16].hex() (§1.1.2.2)
  · REGISTER (§1.1.4, `ka` 포함) → 승인(§3) 뒤 Telemetry 1건 즉시 → `ti` 초마다 QoS0 (§1.1.6)
  · PING/PONG (§1.1.5), CONFIG_SET/ACK OK·RANGE·STATE·FLASH (§1.1.7)
  · 5차 (펌웨어 2026-09-27-3): COMMAND/COMMAND_ACK OK·LOCAL·EXPIRED·BAD·STATE (§3.10.7, §3.10.11),
    계층별 override 슬롯 개별>그룹>전체 (§3.10.8), REGISTER_ACK `grp` → `group/<grp>/cmd` 구독(§3.10.9),
    구독 6개 한도(§3.10.10), 최근 8개 seq 중복 방지
  · 승인 게이트 (§3, 기본 ON): REGISTER_ACK state=ACTIVE 전에는 Telemetry 를 보내지 않고 REGISTER 재전송(§3.6)
  · 재접속 30초×5 → 5분×5 → 30분 (서버_MQTT_안내_README "단말 동작")
  · LWT 는 모뎀이 못 넣으므로 기본 없음(§16.1). `lwt=True` 로 켤 수 있다
  · 고장 주입: 재부팅(sq 0), TM 유실, 강제 절단, 잘못된 payload, ACL 위반, Flash 실패, STATE 오답 …

네트워크와 무관한 부분(TM 값 생성, CONFIG 검증, sq, 승인 상태머신, 재접속 표)은 순수 함수·클래스로
떼어 두어 pytest 로 바로 검증한다. 시간 흐름은 `time_scale` 로 압축한다(1 = 실시간).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import socket
from collections import Counter, OrderedDict
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import aiomqtt

log = logging.getLogger("sim.device")

KST = timezone(timedelta(hours=9))

#: 사양서 §1.1.7 — Telemetry 주기·keepalive 허용 범위(초)와 CONFIG 버전 범위.
TI_MIN, TI_MAX, TI_DEFAULT = 60, 3600, 600
KA_MIN, KA_MAX, KA_DEFAULT = 60, 1800, 300
CV_MIN, CV_MAX = 0, 65535
#: §3.10.7 — override 유지시간 최대 24시간, 0 불가.
DUR_MAX = 86400
#: §0.2 — WD-N522S 모뎀은 구독 topic 을 6개까지만 등록한다.
SUBSCRIBE_LIMIT = 6
#: §3.6 — REGISTER 재전송: 처음 5회는 5분, 이후 30분.
RESEND_FAST_SEC, RESEND_FAST_COUNT, RESEND_SLOW_SEC = 300, 5, 1800
#: 단말 재접속 간격(README "단말 동작"): 30초×5 → 5분×5 → 30분.
RECONNECT_SCHEDULE: tuple[tuple[int, int], ...] = ((30, 5), (300, 5))
RECONNECT_SLOW_SEC = 1800
#: `sq` 는 uint32 로 가정한다(개발계획 §7 확인 요청 항목).
FORGED_SQ = 777_777  # publish_foreign_topic 표식
SQ_MOD = 2**32
#: 시뮬레이터가 흉내내는 펌웨어. 1cha 모드는 1차 펌웨어 모양(type:"TM", ka 없음).
FW_DEFAULT, FW_LEGACY = "1.4.0", "1.0.0"

APPROVAL_STATES = {"PENDING", "ACTIVE", "SUSPENDED", "REJECTED", "RETIRED"}
#: §3.10.7 COMMAND — act 네 가지, 채널 1(주등)·2(입간판)·3, 결과 다섯 가지(§3.10.11).
CMD_ACTS = {"on", "off", "pwm", "auto"}
CHANNELS: tuple[int, ...] = (1, 2, 3)
COMMAND_RESULTS: tuple[str, ...] = ("OK", "LOCAL", "EXPIRED", "BAD", "STATE")
#: override 계층(우선순위 순, §3.10.8).
LEVELS: tuple[str, ...] = ("device", "group", "all")
#: §3.10.7 — 단말은 최근 8개 seq 를 기억해 같은 seq 는 다시 실행하지 않고 처음 결과로 ACK.
SEQ_MEMORY = 8
#: 기억하는 결과. EXPIRED/STATE 는 "실행하지 않았다"라 기억하지 않는다 — 같은 seq·새 ts 재시도(ADR-005)가
#: 먹히려면 EXPIRED 를 기억하면 안 된다(가정, docs/06 §5 B3).
REMEMBERED_RESULTS = {"OK", "LOCAL", "BAD"}
#: §3.10.3/§3.10.4 — group_id 는 숫자 12자리.
_GRP_RE = re.compile(r"^\d{12}$")
#: §3.10.7 COMMAND `ts` = 보낸 시각 `YYMMDDThhmmss` KST.
CMD_TS_FORMAT = "%y%m%dT%H%M%S"
_CMD_TS_RE = re.compile(r"^\d{6}T\d{6}$")
#: Telemetry `er` 비트 0 = BATT_LOW(저전압 차단, §3.10.8 "순위 밖").
ER_BATT_LOW = 0x0001

#: 사양서 §1.1.2.2 공개 시험 키(운영 키 아님). 환경 변수 MQTT_HMAC_KEY 가 없을 때 쓴다.
TEST_HMAC_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"


def device_password(key: bytes, uuid: str) -> str:
    """§1.1.2.2 — HMAC-SHA256(K, UUID) 앞 16바이트를 소문자 hex 32자로."""
    if len(key) != 32:
        raise ValueError("HMAC 키는 32바이트")
    return hmac.new(key, uuid.upper().encode("ascii"), hashlib.sha256).hexdigest()[:32]


def hmac_key_from_hex(text: str | None) -> bytes:
    """hex 64자 → 32바이트. 비어 있으면 사양서 시험 키."""
    text = (text or "").strip()
    if not text:
        text = TEST_HMAC_KEY_HEX
    key = bytes.fromhex(text)
    if len(key) != 32:
        raise ValueError("MQTT_HMAC_KEY 는 hex 64자(32바이트)여야 한다")
    return key


def hmac_key_from_env() -> bytes:
    return hmac_key_from_hex(os.environ.get("MQTT_HMAC_KEY"))


def reconnect_delay(attempt: int) -> float:
    """연속 `attempt` 번째(0부터) 재접속 대기(초). 30초×5 → 5분×5 → 30분."""
    n = max(0, attempt)
    for sec, count in RECONNECT_SCHEDULE:
        if n < count:
            return float(sec)
        n -= count
    return float(RECONNECT_SLOW_SEC)


def uuid_from_index(index: int, namespace: int = 0) -> str:
    """결정적 UUID. `51A0` + 네임스페이스 4자리 + 순번 16자리 = 24자리 대문자 16진수.

    앞 4자리를 고정해 두면 브로커 로그·DB 에서 시뮬레이터 단말을 한눈에 가려낼 수 있다.
    namespace 는 시나리오별로 다르게 주어 서로의 레코드를 건드리지 않게 한다.
    """
    if not (0 <= index < 16**16) or not (0 <= namespace < 16**4):
        raise ValueError("index/namespace 범위 초과")
    return f"51A0{namespace:04X}{index:016X}"


def kst_ts(now: datetime | None = None) -> str:
    """단말 RTC 시각 `YYMMDDThhmm` — KST, 오프셋 없음(§1.1.6 payload 규약)."""
    now = (now or datetime.now(tz=KST)).astimezone(KST)
    return now.strftime("%y%m%dT%H%M")


def kst_ts_sec(now: datetime | None = None) -> str:
    """COMMAND `ts` 모양 `YYMMDDThhmmss` — KST, 초까지(§3.10.7)."""
    now = (now or datetime.now(tz=KST)).astimezone(KST)
    return now.strftime(CMD_TS_FORMAT)


def parse_cmd_ts(text: Any) -> datetime | None:
    """`YYMMDDThhmmss`(KST) → aware datetime. 모양이 틀리면 None."""
    if not isinstance(text, str) or not _CMD_TS_RE.match(text):
        return None
    try:
        return datetime.strptime(text, CMD_TS_FORMAT).replace(tzinfo=KST)
    except ValueError:
        return None


def valid_grp(grp: Any) -> bool:
    """§3.10.3 — 숫자 12자리 문자열만 그룹. 그 밖은 거절(로그, 이전 값 유지)."""
    return isinstance(grp, str) and bool(_GRP_RE.match(grp))


# ── Telemetry 값 모델 ─────────────────────────────────────────────────────


@dataclass
class TelemetryModel:
    """MPPT(0x3201)·LED 상태를 벽시계 기준으로 그럴듯하게 만든다.

    · 낮(06~18시): PV 발전 곡선(pp), 배터리 충전(bi 양수), 만충 부근 톱니 전압(bv)
    · 밤: 점등(on=1, pw 유효), 방전(bi 음수), pp=0, cs 의 Running 비트 0
    실제 단말도 100 배 정수를 보낸다(2612 = 26.12V). 여기서도 나누지 않는다.
    """

    seed: int = 0
    pwm: tuple[int, int, int] = (70, 64, 64)
    fw: str = FW_DEFAULT

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        # 단말마다 배터리 상태가 조금씩 다르게 시작한다.
        self._sc = self._rng.randint(60, 100)
        self._saw = 0

    @staticmethod
    def is_night(now: datetime) -> bool:
        return now.hour >= 18 or now.hour < 6

    def sample(self, now: datetime | None = None) -> dict[str, int | list[int]]:
        """`type`/`sq`/`ts`/`ss`/`cv`/`er`/`md` 를 뺀 계측값만 만든다."""
        now = (now or datetime.now(tz=KST)).astimezone(KST)
        night = self.is_night(now)
        hour = now.hour + now.minute / 60

        if night:
            pp = 0
            # 부하 전류 2.3A 정도, 방전이므로 bi 음수.
            li = 230 + self._rng.randint(-10, 10)
            bi = -(li - self._rng.randint(0, 40))
            self._sc = max(20, self._sc - (1 if self._rng.random() < 0.3 else 0))
            bv = 2500 + self._sc + self._rng.randint(-5, 5)
            # D15-D14=00 정상, D0 Running=0 (밤이라 발전 없음)
            cs = 0x0000
        else:
            # 정오 최대 40W 정도의 사인 곡선(x100 → 4000).
            pp = int(max(0.0, math.sin((hour - 6) / 12 * math.pi)) * 4000)
            pp += self._rng.randint(-50, 50) if pp > 0 else 0
            li = 0
            bi = int(pp / 26) if pp > 0 else 0
            self._sc = min(100, self._sc + (1 if self._rng.random() < 0.5 else 0))
            # 만충 부근 톱니: CV ↔ 완료를 오가며 약 1V 폭으로 진동(§1.1.6 서버 유의점).
            self._saw = (self._saw + 1) % 4
            bv = 2680 + (self._saw * 25 if self._sc >= 95 else 0) + self._rng.randint(-5, 5)
            # D0 Running=1, D3-D2=01 Float(만충) 또는 10 Boost, D11-D10 charging → 예시값 3073.
            cs = 0x0C01 if self._sc < 95 else 0x0C05

        on = 1 if night else 0
        pw = list(self.pwm) if on else [0, 0, 0]
        return {
            "on": on, "pw": pw,
            "bv": max(0, bv), "bi": bi, "sc": self._sc,
            "pp": max(0, pp), "li": li, "cs": cs,
        }


# ── 승인 상태머신 (§3, 3차) ────────────────────────────────────────────────


@dataclass
class ApprovalGate:
    """REGISTER_ACK `state` 에 따른 단말 동작(§3.4)과 REGISTER 재전송 주기(§3.6).

    상태는 메모리에만 있다(§3.5) — `reset()` 이 곧 재부팅/재접속이다.
    `enabled=False` 면 1차 펌웨어 동작(승인 없이 즉시 Telemetry).
    """

    enabled: bool = True
    time_scale: float = 1.0
    state: str | None = None
    resend_count: int = 0
    #: REJECTED/RETIRED 처럼 더 이상 REGISTER 를 보내지 않는 상태로 잠겼는가.
    silenced: bool = False

    def reset(self) -> None:
        """재접속·재부팅. 카운터를 처음으로 되돌린다(§3.6)."""
        self.state = None
        self.resend_count = 0
        self.silenced = False

    def on_register_sent(self) -> None:
        self.resend_count += 1

    def on_ack(self, state: str | None) -> None:
        """REGISTER_ACK 수신. `None` 은 빈 payload(retain 삭제, §3.3)."""
        if state is None:
            # 무응답 상태로 돌아가 30분 주기 재전송 재개(§3.3). 카운터는 느린 구간으로.
            self.state = None
            self.silenced = False
            self.resend_count = max(self.resend_count, RESEND_FAST_COUNT)
            return
        if state not in APPROVAL_STATES:
            log.warning("모르는 승인 상태 %r — 무시", state)
            return
        self.state = state
        self.silenced = state in {"REJECTED", "RETIRED"}

    @property
    def telemetry_allowed(self) -> bool:
        return (not self.enabled) or self.state == "ACTIVE"

    @property
    def config_allowed(self) -> bool:
        """CONFIG 변경은 ACTIVE 에서만(§3.8). 아니면 CONFIG_ACK `STATE`."""
        return (not self.enabled) or self.state == "ACTIVE"

    @property
    def resend_needed(self) -> bool:
        """무응답·PENDING 만 재전송한다(§3.4). ACTIVE/SUSPENDED/REJECTED/RETIRED 는 아니다."""
        if not self.enabled or self.silenced:
            return False
        return self.state in (None, "PENDING")

    def next_resend_delay(self) -> float:
        base = RESEND_FAST_SEC if self.resend_count < RESEND_FAST_COUNT else RESEND_SLOW_SEC
        return base / self.time_scale


# ── 원격 제어 override 슬롯 (§3.10.8, 5차) ────────────────────────────────


@dataclass
class OverrideSlot:
    """계층 하나(개별/그룹/전체)의 (값, 만료시각). `ch` 에 든 채널만 건드린다(§3.10.7 "없는 채널은 건드리지 않는다")."""

    act: str                       # on / off / pwm (auto 는 슬롯을 지운다)
    ch: tuple[int, ...]
    pwm: tuple[int, ...] | None    # act=pwm 일 때 ch 와 같은 순서의 %
    expires_at: float              # time.monotonic()
    seq: int = -1
    level: str = "device"

    def active(self, now: float) -> bool:
        return now < self.expires_at

    def percent(self, channel: int) -> int | None:
        """이 슬롯이 `channel` 에 거는 밝기 %(설치 기준 밝기에 곱함). 그 채널을 안 건드리면 None."""
        if channel not in self.ch:
            return None
        if self.act == "off":
            return 0
        if self.act == "on":
            return 100
        assert self.pwm is not None
        return int(self.pwm[self.ch.index(channel)])


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_config_set(payload: dict[str, Any], *, ti_min: int = TI_MIN, ka_min: int = KA_MIN) -> str | None:
    """CONFIG_SET 항목 검증(§1.1.7). 문제가 있으면 항목 이름, 없으면 None.

    `cv` 0~65535, `ti` 60~3600, `ka` 60~1800, `lat`/`lon` WGS84 범위. `grp` 는 REGISTER_ACK 로만 온다(§3.10.9) —
    CONFIG_SET 에 실려 와도 모르는 키로 보고 무시한다(검사하지도 저장하지도 않는다).
    `ti_min`/`ka_min` 은 시험용 하한(백엔드 `CONFIG_TI_MIN_SEC`/`CONFIG_KA_MIN_SEC` 와 짝) — 기본은 사양값.
    """
    cv = payload.get("cv")
    if not _is_int(cv) or not (CV_MIN <= cv <= CV_MAX):
        return "cv"
    if "ti" in payload and (not _is_int(payload["ti"]) or not (ti_min <= payload["ti"] <= TI_MAX)):
        return "ti"
    if "ka" in payload and (not _is_int(payload["ka"]) or not (ka_min <= payload["ka"] <= KA_MAX)):
        return "ka"
    for key in ("lat", "lon"):
        if key in payload and (not isinstance(payload[key], (int, float)) or isinstance(payload[key], bool)):
            return key
    if "lat" in payload and not (-90 <= payload["lat"] <= 90):
        return "lat"
    if "lon" in payload and not (-180 <= payload["lon"] <= 180):
        return "lon"
    return None


def validate_command_header(payload: dict[str, Any]) -> str | None:
    """COMMAND 의 늦음 판정 재료(`seq`, `ts`, `exp`) 모양. 틀리면 항목 이름(→ `BAD`).

    `ts` 는 없어도 된다(없으면 늦음 판정을 안 한다, §3.10.7). `exp` 는 필수(§17 "exp, dur 은 필수")·0 이상 정수.
    """
    seq = payload.get("seq")
    if not _is_int(seq) or seq < 0:
        return "seq"
    if "ts" in payload and parse_cmd_ts(payload["ts"]) is None:
        return "ts"
    exp = payload.get("exp")
    if not _is_int(exp) or exp < 0:
        return "exp"
    return None


def validate_command_body(payload: dict[str, Any]) -> str | None:
    """COMMAND 의 동작 항목(§3.10.7). 틀리면 항목 이름(→ `BAD`).

    · `act` ∈ on/off/pwm/auto
    · `ch` 있으면 1~3 중복 없는 비어 있지 않은 정수 배열. 없으면 모든 채널
    · `act != auto` 면 `dur` 1~86400 정수(누락·0·초과 = BAD). `auto` 의 dur 은 보지 않는다
    · `act = pwm` 이면 `pwm` 이 `ch`(없으면 채널 3개)와 같은 길이, 각 0~100 정수
    """
    act = payload.get("act")
    if act not in CMD_ACTS:
        return "act"
    ch = payload.get("ch")
    if ch is not None:
        if (not isinstance(ch, list) or not ch or len(set(map(repr, ch))) != len(ch)
                or not all(_is_int(c) and c in CHANNELS for c in ch)):
            return "ch"
    if act != "auto":
        dur = payload.get("dur")
        if not _is_int(dur) or not (1 <= dur <= DUR_MAX):
            return "dur"
    if act == "pwm":
        pwm = payload.get("pwm")
        n = len(ch) if isinstance(ch, list) else len(CHANNELS)
        if not isinstance(pwm, list) or len(pwm) != n or not all(_is_int(p) and 0 <= p <= 100 for p in pwm):
            return "pwm"
    return None


def validate_command(payload: dict[str, Any]) -> str | None:
    """헤더 + 동작 검증 한 번에(시험용). 단말은 헤더 → 늦음(EXPIRED) → 동작 순으로 본다."""
    return validate_command_header(payload) or validate_command_body(payload)


def command_age_sec(payload: dict[str, Any], rtc_now: datetime) -> float | None:
    """단말 RTC − `ts`(초). `ts` 가 없으면 None(늦음 판정 안 함). RTC 는 초 단위로 자른다."""
    ts = parse_cmd_ts(payload.get("ts")) if "ts" in payload else None
    if ts is None:
        return None
    return (rtc_now.astimezone(KST).replace(microsecond=0) - ts).total_seconds()


def command_expired(payload: dict[str, Any], rtc_now: datetime) -> bool:
    """§3.10.7 — RTC − ts 가 exp 보다 크면 폐기(EXPIRED). 같으면 아직 유효."""
    age = command_age_sec(payload, rtc_now)
    return age is not None and age > int(payload.get("exp", 0))


@dataclass
class Stats:
    connects: int = 0
    connect_failures: int = 0
    disconnects: int = 0
    reconnect_attempts: int = 0
    register_sent: int = 0
    tm_sent: int = 0
    tm_suppressed: int = 0  # 승인 전이라 보내지 않은 주기
    result_sent: int = 0
    ping_rx: int = 0
    config_set_rx: int = 0
    config_set_ignored: int = 0
    config_ack_ok: int = 0
    config_ack_range: int = 0
    config_ack_state: int = 0
    config_ack_flash: int = 0
    register_ack_rx: int = 0
    register_ack_empty_rx: int = 0
    #: COMMAND 처리(§3.10.7) — 처리한 건수(무시·지연 대기 제외), 계층별 수신, 결과별 ACK.
    cmd_rx: int = 0
    cmd_rejected: int = 0          # = cmd_ack["BAD"] (옛 이름 유지)
    cmd_rx_by_level: Counter = field(default_factory=Counter)
    cmd_ack: Counter = field(default_factory=Counter)
    cmd_dup: int = 0               # 최근 8개 seq 에 걸려 첫 결과로 다시 ACK
    cmd_silenced: int = 0          # silent_commands 로 통째로 무시
    cmd_delayed: int = 0           # delay_commands_sec 로 늦게 처리
    cmd_foreign_group: int = 0     # 지금 구독 중이 아닌 그룹 topic 으로 온 것(해제 직후 등) — 무시
    cmd_legacy_rx: int = 0         # 옛 `type:"CMD"` — 1.4.0 이후 없음, 무시
    local_applied: int = 0         # 현장 조작 종료 뒤 적용한 보류 명령
    group_subscribe: int = 0
    group_unsubscribe: int = 0
    grp_rejected: int = 0          # 12자리 숫자가 아닌 grp — 이전 값 유지
    unknown_rx: int = 0
    last_error: str = ""


# ── 단말 본체 ─────────────────────────────────────────────────────────────


class SimDevice:
    """가짜 단말 한 대. `start()` 로 띄우고 `stop()` 으로 내린다.

    모드:
      "2cha"  펌웨어 1.4.0. username=UUID + HMAC 비밀번호, `config` 구독, 승인 게이트 ON,
              REGISTER 에 cv/ss/ti/ka, Telemetry `type:"TELEMETRY"`, msisdn 실제 모양
      "1cha"  1차 펌웨어. 공용 계정, 승인 게이트 OFF, Telemetry `type:"TM"`, REGISTER 에 ka 없음

    행동 플래그(생성자, None 이면 모드 기본값):
      hmac              2cha 에서 False 면 공용 계정으로 붙는다(HMAC 펌웨어 이전 단말)
      hmac_key          32바이트. None 이면 env MQTT_HMAC_KEY → 사양서 시험 키
      ka                CONNECT keepalive(초). CONFIG_SET `ka` 는 다음 접속부터 적용(§1.1.7)
      approval_gate     승인 게이트. 2cha 기본 True, 1cha 기본 False
      lwt               Will 등록. 기본 False(모뎀 미지원, §16.1). 1cha 기본 True
      tm_type           Telemetry 의 type 값. 2cha "TELEMETRY", 1cha "TM". `legacy_t_key` 면 `"t":"TM"`
      legacy_register   REGISTER 에 cv/ss/ti/ka 를 싣지 않는다(1차 펌웨어)
      ignore_config_set 처음 N 개의 CONFIG_SET 을 못 받은 척한다(재전송 시험)
      flash_fail_next   다음 N 개의 CONFIG_SET 에 `FLASH` 로 답하고 이전 값을 유지한다(1.3.0~)
      state_ack_next    다음 N 개의 CONFIG_SET 에 ACTIVE 여도 `STATE` 로 답한다(서버 재조정 시험)
      silent_results    result 를 아예 보내지 않는다(무응답 단말)
      register_on_connect False 면 접속 직후 REGISTER 를 건너뛴다(유실 흉내)
      time_scale        재전송·재접속 대기를 이 배수로 줄인다(시험용). `ti`/`ka` 에는 적용하지 않는다
      ti_min, ka_min    CONFIG_SET 검증 하한(기본 60 = 사양). 시나리오는 1 로 두어 ti=5 같은 시험값을 RANGE 로 거부하지 않는다
      silent_commands   다음 N 개의 COMMAND 를 통째로 무시한다(처리·ACK·seq 기억 없음 — 서버 자동 재시도 시험)
      delay_commands_sec COMMAND 를 받고 이 초만큼 늦게 처리한다(모뎀 전달 지연 흉내 → EXPIRED). 0 = 즉시
      clock_skew_sec    단말 RTC 를 이만큼 앞당긴다(+) / 늦춘다(-). EXPIRED 판정에만 쓴다
      local_mode        현장 조작 중(DIP/엔코더/OLED 메뉴). COMMAND 는 `LOCAL` 로 답하고 보류 → `end_local()` 이 적용
    """

    def __init__(
        self,
        uuid: str,
        *,
        host: str = "localhost",
        port: int = 1883,
        mode: str = "2cha",
        username: str | None = None,
        password: str | None = None,
        hmac: bool | None = None,
        hmac_key: bytes | None = None,
        topic_root: str = "iotlight",
        ti: int = TI_DEFAULT,
        ka: int = KA_DEFAULT,
        cv: int = 0,
        ss: int = 0,
        fw: str | None = None,
        device_model: str = "RMCB-1100M",
        modem_model: str = "WD-N522S",
        lwt: bool | None = None,
        tm_type: str | None = None,
        legacy_register: bool = False,
        legacy_t_key: bool = False,
        approval_gate: bool | None = None,
        ignore_config_set: int = 0,
        flash_fail_next: int = 0,
        state_ack_next: int = 0,
        silent_results: bool = False,
        register_on_connect: bool = True,
        time_scale: float = 1.0,
        reconnect_jitter: float = 0.0,
        ti_min: int = TI_MIN,
        ka_min: int = KA_MIN,
        silent_commands: int = 0,
        delay_commands_sec: float = 0.0,
        clock_skew_sec: float = 0.0,
        local_mode: bool = False,
        seed: int | None = None,
        on_message: Callable[["SimDevice", str, bytes], Awaitable[None] | None] | None = None,
    ) -> None:
        if len(uuid) != 24 or any(c not in "0123456789ABCDEF" for c in uuid):
            raise ValueError(f"UUID 는 24자리 대문자 16진수여야 한다: {uuid!r}")
        if mode not in {"1cha", "2cha"}:
            raise ValueError("mode 는 1cha 또는 2cha")
        self.uuid = uuid
        self.host, self.port = host, port
        self.mode = mode
        legacy = mode == "1cha"
        self.hmac = (not legacy) if hmac is None else hmac
        self.hmac_key = hmac_key
        if self.hmac:
            # §1.1.2.2 — username = UUID, password 는 계산값. 아무도 저장하지 않는다.
            self.username = uuid
            self.password = password or device_password(hmac_key or hmac_key_from_env(), uuid)
        else:
            self.username = username or "solarlte-test"
            self.password = password or "solarlte-test-2026"
        self.root = topic_root
        self.ti, self.cv, self.ss = ti, cv, ss
        #: Flash 의 keepalive 값. 접속 중인 세션의 값은 `ka_connected`(다음 접속부터 적용).
        self.ka = ka
        self.ka_connected: int | None = None
        self.fw = fw or (FW_LEGACY if legacy else FW_DEFAULT)
        self.device_model, self.modem_model = device_model, modem_model
        self.lat: float | None = None
        self.lon: float | None = None
        #: REGISTER_ACK 로 받은 group_id(12자리). 저장하지 않는다(§3.10.9) — 접속마다 retain 으로 다시 받는다.
        self.grp: str | None = None
        self.lwt = legacy if lwt is None else lwt
        self.tm_type = tm_type or ("TM" if legacy else "TELEMETRY")
        self.legacy_register = legacy_register
        self.legacy_t_key = legacy_t_key
        self.ignore_config_set = ignore_config_set
        self.flash_fail_next = flash_fail_next
        self.state_ack_next = state_ack_next
        self.silent_results = silent_results
        #: False 면 접속 후 REGISTER 를 보내지 않는다(REGISTER 유실 흉내, S2-02).
        self.register_on_connect = register_on_connect
        self.time_scale = max(time_scale, 1e-6)
        self.reconnect_jitter = reconnect_jitter
        self.ti_min, self.ka_min = ti_min, ka_min
        self.silent_commands = silent_commands
        self.delay_commands_sec = delay_commands_sec
        self.clock_skew_sec = clock_skew_sec
        self.local_mode = local_mode
        self.on_message = on_message

        seed_val = seed if seed is not None else int(uuid[-8:], 16)
        self._rng = random.Random(seed_val)
        self.model = TelemetryModel(seed=seed_val, fw=self.fw)
        gate_on = (not legacy) if approval_gate is None else approval_gate
        self.gate = ApprovalGate(enabled=gate_on, time_scale=self.time_scale)
        self.stats = Stats()

        # 단말 식별값(런타임에 안 바뀜, §1.1.4).
        self.imei = f"35{seed_val % 10**13:013d}"
        self.iccid = f"8982{seed_val % 10**15:015d}"
        # 1.1.0 실기 로그 모양 "01248324427". 1차 모양(빈 문자열)도 사양이 허용한다.
        self.msisdn = "" if legacy else f"010{seed_val % 10**8:08d}"

        self.sq = 0
        self.er = 0
        self._overrides: dict[str, OverrideSlot] = {}
        #: 최근 8개 seq → 첫 ACK(§3.10.7). 순서 = 받은 순.
        self._seq_memory: OrderedDict[int, dict[str, Any]] = OrderedDict()
        #: 현장 조작 중 받은 명령 (level, payload, 받은 시각 monotonic). `end_local()` 이 적용.
        self._pending_local: list[tuple[str, dict[str, Any], float]] = []
        self._subscriptions: list[str] = []
        #: 지금 구독 중인 그룹 topic(없으면 None). REGISTER_ACK 의 state·grp 로 정해진다(§3.10.9).
        self._group_topic: str | None = None
        #: 그룹 구독 변경 이력 (monotonic, 이전 topic, 새 topic).
        self.group_log: list[tuple[float, str | None, str | None]] = []
        #: 보낸(보내려 한) COMMAND_ACK 이력 (monotonic, 받은 topic, ack). silent_results 여도 남는다.
        self.ack_log: list[tuple[float, str, dict[str, Any]]] = []
        self._cmd_tasks: set[asyncio.Task] = set()
        #: 마지막으로 보낸 result payload — QoS1 중복 재전송 흉내에 쓴다.
        self.last_result: dict[str, Any] | None = None
        self.last_tm: dict[str, Any] | None = None
        self.last_config_set: dict[str, Any] | None = None
        #: 받은 메시지 이력 (topic, payload dict|None, retained). 시나리오가 들여다본다.
        self.inbox: list[tuple[str, Any, bool]] = []
        #: §1.1.10 검증용 시각(time.monotonic). 마지막 수신, type 별 마지막 수신, 마지막 송신.
        self.last_rx_at_monotonic: float | None = None
        self.last_rx_by_type: dict[str, float] = {}
        self.last_register_sent_at: float | None = None
        self.last_tm_sent_at: float | None = None
        self.connected_at_monotonic: float | None = None
        self.connected = asyncio.Event()
        self._client: aiomqtt.Client | None = None
        self._run_task: asyncio.Task | None = None
        self._session_task: asyncio.Task | None = None
        self._stopping = False
        self._reconnect_attempt = 0
        self._reconnect_delay_override: float | None = None
        self._tm_kick = asyncio.Event()
        self._register_kick = asyncio.Event()

    # ── topic 도우미 ──────────────────────────────────────────────────────
    def topic(self, kind: str, uuid: str | None = None) -> str:
        return f"{self.root}/device/{uuid or self.uuid}/{kind}"

    @property
    def is_connected(self) -> bool:
        return self.connected.is_set()

    @property
    def state(self) -> str | None:
        """승인 상태(REGISTER_ACK 로 받은 값). None = 무응답."""
        return self.gate.state

    # ── payload 생성 (순수) ───────────────────────────────────────────────
    def build_register(self) -> dict[str, Any]:
        """§1.1.4. 1차 펌웨어(legacy_register/1cha)는 cv/ss/ti/ka 를 싣지 않는다."""
        payload: dict[str, Any] = {
            "type": "REGISTER",
            "uuid": self.uuid,
            "fw": self.fw,
        }
        if not self.legacy_register:
            payload.update({"cv": self.cv, "ss": self.ss, "ti": self.ti})
            if self.mode == "2cha":
                payload["ka"] = self.ka
        payload.update({
            "device_model": self.device_model,
            "modem_model": self.modem_model,
            "msisdn": self.msisdn,
            "imei": self.imei,
            "iccid": self.iccid,
        })
        return payload

    def build_tm(self, now: datetime | None = None) -> dict[str, Any]:
        """§1.1.6. 호출할 때마다 `sq` 를 하나 올린다(보내지 못해도 올린다 — 전송 실패 처리).

        `md`: 1 현장 조작 중 / 2 원격 슬롯이 하나라도 유효 / 0 스케줄(§3.10.8). 채널별 출력은 `channel_output()`.
        """
        sample = self.model.sample(now)
        md = self.md
        if md == 2:
            pw = list(sample["pw"])
            for channel, pct in self.channel_output().items():
                if pct is not None:
                    pw[channel - 1] = round(self.model.pwm[channel - 1] * pct / 100)
            sample["pw"] = pw
        if self.er & ER_BATT_LOW:
            # 저전압 차단은 순위 밖(§3.10.8) — 어떤 명령이 와도 켜지지 않는다. md 는 경로 보고라 그대로 둔다(가정 B6).
            sample["pw"] = [0, 0, 0]
        sample["on"] = 1 if any(sample["pw"]) else 0
        payload: dict[str, Any] = {
            ("t" if self.legacy_t_key else "type"): ("TM" if self.legacy_t_key else self.tm_type),
            "sq": self.sq,
            "ts": kst_ts(now),
            "fw": self.fw,
            "ss": self.ss,
            "cv": self.cv,
            "er": self.er,
            "on": sample["on"],
            "md": md,
            "pw": sample["pw"],
            "bv": sample["bv"], "bi": sample["bi"], "sc": sample["sc"],
            "pp": sample["pp"], "li": sample["li"], "cs": sample["cs"],
        }
        self.sq = (self.sq + 1) % SQ_MOD
        return payload

    # ── override 슬롯 (§3.10.8) ──────────────────────────────────────────
    def _prune(self, now: float) -> None:
        for level in list(self._overrides):
            if not self._overrides[level].active(now):
                del self._overrides[level]

    def active_slots(self, now: float | None = None) -> dict[str, OverrideSlot]:
        """만료되지 않은 슬롯 {level: slot}. 만료된 것은 여기서 지운다(dur 경과 → 스케줄/하위 계층 복귀)."""
        self._prune(time.monotonic() if now is None else now)
        return dict(self._overrides)

    def current_override(self, now: float | None = None) -> OverrideSlot | None:
        """유효한 슬롯 중 최상위(개별 > 그룹 > 전체, §3.10.8). 없으면 None(스케줄)."""
        slots = self.active_slots(now)
        for level in LEVELS:
            if level in slots:
                return slots[level]
        return None

    def channel_output(self, now: float | None = None) -> dict[int, int | None]:
        """채널별 원격 밝기 %. 채널마다 그 채널을 건드리는 유효 슬롯 중 최상위가 정한다. None = 스케줄."""
        slots = self.active_slots(now)
        out: dict[int, int | None] = {}
        for channel in CHANNELS:
            out[channel] = None
            for level in LEVELS:
                slot = slots.get(level)
                if slot is not None and (pct := slot.percent(channel)) is not None:
                    out[channel] = pct
                    break
        return out

    @property
    def effective_act(self) -> str:
        """최상위 유효 슬롯의 act(`on`/`off`/`pwm`), 없으면 `schedule`. 현장 조작 중이면 `local`."""
        if self.local_mode:
            return "local"
        slot = self.current_override()
        return slot.act if slot is not None else "schedule"

    @property
    def md(self) -> int:
        """Telemetry `md`: 0 스케줄 / 1 현장 수동 / 2 원격(§3.10.8)."""
        if self.local_mode:
            return 1
        return 2 if self.current_override() is not None else 0

    def slot_dump(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        """시나리오 로그용 슬롯 상태."""
        now = time.monotonic() if now is None else now
        return {level: {"act": s.act, "ch": list(s.ch), "pwm": list(s.pwm) if s.pwm else None, "seq": s.seq,
                        "remaining_sec": round(s.expires_at - now, 1)}
                for level, s in self.active_slots(now).items()}

    def rtc_now(self) -> datetime:
        """단말 RTC(KST). `clock_skew_sec` 만큼 어긋나 있다."""
        return datetime.now(tz=KST) + timedelta(seconds=self.clock_skew_sec)

    # ── 수신 처리 (순수: 응답 payload 를 돌려준다) ───────────────────────
    def handle_ping(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.stats.ping_rx += 1
        return {"type": "PONG", "seq": payload.get("seq"), "uuid": self.uuid}

    def _config_ack(self, result: str) -> dict[str, Any]:
        return {"type": "CONFIG_ACK", "uuid": self.uuid, "cv": self.cv, "result": result}

    def handle_config_set(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """§1.1.7 CONFIG_SET.

        · 승인 전(ACTIVE 아님) → `STATE`, 아무것도 바꾸지 않음(§3.8)
        · 범위 밖 → `RANGE`, 이전 값 유지·cv 미변경
        · Flash 실패(`flash_fail_next`) → `FLASH`, 이전 값 유지
        · OK → cv/ti/ka/lat/lon 저장. `ti` 즉시, `ka` 다음 접속부터. `grp` 는 CONFIG 로 받지 않는다(§3.10.9)
        `ignore_config_set` 만큼은 못 받은 척(None). `state_ack_next` 만큼은 ACTIVE 여도 STATE.
        """
        self.stats.config_set_rx += 1
        self.last_config_set = dict(payload)
        if self.ignore_config_set > 0:
            self.ignore_config_set -= 1
            self.stats.config_set_ignored += 1
            return None
        if not self.gate.config_allowed or self.state_ack_next > 0:
            if self.state_ack_next > 0:
                self.state_ack_next -= 1
            self.stats.config_ack_state += 1
            log.info("[%s] CONFIG_SET 거부(STATE, 승인 상태 %s)", self.uuid, self.gate.state)
            return self._config_ack("STATE")
        problem = validate_config_set(payload, ti_min=self.ti_min, ka_min=self.ka_min)
        if problem is not None:
            self.stats.config_ack_range += 1
            log.info("[%s] CONFIG_SET 거부(RANGE:%s) cv 유지 %d", self.uuid, problem, self.cv)
            return self._config_ack("RANGE")
        if self.flash_fail_next > 0:
            self.flash_fail_next -= 1
            self.stats.config_ack_flash += 1
            log.info("[%s] CONFIG_SET Flash 기록 실패 흉내(FLASH) cv 유지 %d", self.uuid, self.cv)
            return self._config_ack("FLASH")
        # 전부 적용하고 Flash 저장(여기서는 인스턴스 필드 — reboot() 에도 남는다).
        self.cv = int(payload["cv"])
        if "ti" in payload:
            self.ti = int(payload["ti"])       # 즉시 적용
        if "ka" in payload:
            self.ka = int(payload["ka"])       # 다음 접속부터(§1.1.7)
        if "lat" in payload:
            self.lat = float(payload["lat"])
        if "lon" in payload:
            self.lon = float(payload["lon"])
        self.stats.config_ack_ok += 1
        return self._config_ack("OK")

    # ── COMMAND (§3.10.7 ~ §3.10.11) ────────────────────────────────────
    def _command_ack(self, payload: dict[str, Any], result: str) -> dict[str, Any]:
        """§3.10.11 `{"type":"COMMAND_ACK","uuid","seq","result","act","dur"}`. act/dur 는 받은 값을 되돌려 준다
        (없으면 뺀다 — auto 는 dur 없음, BAD 는 누락된 채로)."""
        ack: dict[str, Any] = {"type": "COMMAND_ACK", "uuid": self.uuid, "seq": payload.get("seq"), "result": result}
        for key in ("act", "dur"):
            if key in payload:
                ack[key] = payload[key]
        return ack

    def _decide(self, payload: dict[str, Any], level: str, now: float, rtc_now: datetime) -> str:
        """판정 순서: 헤더 모양(BAD) → 늦음(EXPIRED) → 동작 항목(BAD) → 승인(STATE) → 현장(LOCAL) → 적용(OK)."""
        problem = validate_command_header(payload)
        if problem is None and command_expired(payload, rtc_now):
            log.info("[%s] COMMAND 늦음(EXPIRED) seq=%s age=%.0fs exp=%s", self.uuid, payload.get("seq"),
                     command_age_sec(payload, rtc_now) or 0, payload.get("exp"))
            return "EXPIRED"
        problem = problem or validate_command_body(payload)
        if problem is not None:
            log.warning("[%s] COMMAND 거부(BAD:%s): %s", self.uuid, problem, payload)
            return "BAD"
        if self.gate.enabled and self.gate.state != "ACTIVE":
            return "STATE"  # 승인 전 원격 제어 불가(§3.8)
        if self.local_mode:
            self._pending_local.append((level, dict(payload), now))
            return "LOCAL"
        self._apply(level, payload, received_at=now)
        return "OK"

    def _apply(self, level: str, payload: dict[str, Any], *, received_at: float) -> None:
        """그 계층 슬롯에 적용. 만료 = 받은 시각 + dur. `auto` 는 그 계층 슬롯만 지운다(가정 B4)."""
        act = payload["act"]
        if act == "auto":
            self._overrides.pop(level, None)
            return
        ch = tuple(payload.get("ch") or CHANNELS)
        pwm = tuple(int(p) for p in payload["pwm"]) if act == "pwm" else None
        self._overrides[level] = OverrideSlot(act=act, ch=ch, pwm=pwm, expires_at=received_at + int(payload["dur"]),
                                              seq=int(payload["seq"]), level=level)

    def handle_command(self, payload: dict[str, Any], level: str = "device", *, now: float | None = None,
                       rtc_now: datetime | None = None, topic: str = "") -> dict[str, Any]:
        """COMMAND 하나를 처리하고 COMMAND_ACK 를 돌려준다(모든 결과에 응답한다, §3.10.11).

        같은 seq 가 최근 8개 안에 있으면 다시 실행하지 않고 **처음 결과**를 그대로 돌려준다(§3.10.7).
        `now` 는 monotonic(슬롯 만료 기준), `rtc_now` 는 KST 벽시계(EXPIRED 판정 기준, 기본 `rtc_now()`).
        """
        now = time.monotonic() if now is None else now
        rtc_now = self.rtc_now() if rtc_now is None else rtc_now
        self.stats.cmd_rx += 1
        self.stats.cmd_rx_by_level[level] += 1
        seq = payload.get("seq")
        if _is_int(seq) and seq in self._seq_memory:
            self.stats.cmd_dup += 1
            ack = dict(self._seq_memory[seq])
            log.info("[%s] COMMAND seq=%s 중복 — 재실행 없이 첫 결과 %s", self.uuid, seq, ack.get("result"))
        else:
            result = self._decide(payload, level, now, rtc_now)
            ack = self._command_ack(payload, result)
            if _is_int(seq) and result in REMEMBERED_RESULTS:
                self._seq_memory[seq] = dict(ack)
                while len(self._seq_memory) > SEQ_MEMORY:
                    self._seq_memory.popitem(last=False)
        result = ack["result"]
        self.stats.cmd_ack[result] += 1
        if result == "BAD":
            self.stats.cmd_rejected += 1
        self.ack_log.append((now, topic, ack))
        return ack

    def end_local(self, now: float | None = None) -> list[int]:
        """현장 조작 종료. 그동안 `LOCAL` 로 답한 명령 중 **아직 유효한 것**(받은 시각 + dur 전)을 받은 순서대로
        적용한다(§3.10.11 "현장 조작이 끝났을 때 아직 유효하면 그때 적용"). 적용한 seq 목록을 돌려준다."""
        now = time.monotonic() if now is None else now
        self.local_mode = False
        applied: list[int] = []
        for level, payload, received in self._pending_local:
            if payload["act"] == "auto" or received + int(payload["dur"]) > now:
                self._apply(level, payload, received_at=received)
                applied.append(int(payload["seq"]))
        self._pending_local.clear()
        self.stats.local_applied += len(applied)
        return applied

    def acks_for(self, seq: int) -> list[dict[str, Any]]:
        """이 seq 로 보낸(보내려 한) COMMAND_ACK 전부."""
        return [a for (_, _, a) in self.ack_log if a.get("seq") == seq]

    def commands_received(self, seq: int | None = None, topic: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        """받은 COMMAND (topic, payload). 무시·지연된 것도 포함(inbox 기준)."""
        return [(t, p) for (t, p, _) in self.inbox
                if isinstance(p, dict) and p.get("type") == "COMMAND"
                and (seq is None or p.get("seq") == seq) and (topic is None or t == topic)]

    def level_of(self, topic: str) -> str | None:
        """수신 topic → 계층(§3.10.10 "어느 경로로 온 명령인지 단말이 구분한다")."""
        parts = topic.split("/")
        if len(parts) == 4 and parts[1] == "device" and parts[3] == "cmd":
            return "device"
        if len(parts) == 4 and parts[1] == "group" and parts[3] == "cmd":
            return "group"
        if len(parts) == 3 and parts[1] == "all" and parts[2] == "cmd":
            return "all"
        return None

    # ── REGISTER_ACK · 그룹 구독 (§3.3, §3.10.9) ─────────────────────────
    def handle_register_ack(self, payload: dict[str, Any] | None) -> None:
        """REGISTER_ACK(§3.3). payload None = 빈 retain(정리). `grp` 도 여기서 받는다(§3.10.9).

        구독 변경은 네트워크 일이라 `_sync_group_subscription()` 이 한다(`desired_group_topic` 을 본다).
        """
        self.stats.register_ack_rx += 1
        if payload is None:
            self.stats.register_ack_empty_rx += 1
            self.gate.on_ack(None)
            self.grp = None
            return
        if payload.get("uuid") not in (None, self.uuid):
            log.warning("[%s] REGISTER_ACK uuid 불일치 %s", self.uuid, payload.get("uuid"))
            return
        was_active = self.gate.state == "ACTIVE"
        self.gate.on_ack(payload.get("state"))
        grp = payload.get("grp")
        if grp is None or grp == "":
            self.grp = None                      # 미배정
        elif valid_grp(grp):
            self.grp = grp
        else:
            self.stats.grp_rejected += 1         # 로그 남기고 이전 값 유지(§3.10.3)
            log.warning("[%s] REGISTER_ACK grp 거절(12자리 숫자 아님) %r — 이전 값 %r 유지", self.uuid, grp, self.grp)
        if self.gate.state == "ACTIVE" and not was_active:
            # 승인되면 곧바로 Telemetry 1건(§3.9.2 "단말 ACTIVE → Telemetry 1건"). 주기를 기다리지 않는다.
            self._tm_kick.set()

    @property
    def desired_group_topic(self) -> str | None:
        """구독해야 할 그룹 topic. ACTIVE 이고 grp 가 있을 때만(§3.10.9). 1cha(1차 펌웨어)는 그룹 없음."""
        if self.mode != "2cha" or not self.grp:
            return None
        if self.gate.enabled and self.gate.state != "ACTIVE":
            return None
        return f"{self.root}/group/{self.grp}/cmd"

    @property
    def group_topic(self) -> str | None:
        """지금 구독 중인 그룹 topic."""
        return self._group_topic

    @property
    def subscriptions(self) -> list[str]:
        return list(self._subscriptions)

    async def _sync_group_subscription(self) -> None:
        """이전 그룹 topic 구독 해제 → 새 topic 구독(§3.10.9). 같으면 아무것도 안 한다."""
        client = self._client
        desired, current = self.desired_group_topic, self._group_topic
        if client is None or desired == current:
            return
        if current is not None:
            with contextlib.suppress(aiomqtt.MqttError):
                await client.unsubscribe(current)
            if current in self._subscriptions:
                self._subscriptions.remove(current)
            self._group_topic = None
            self.stats.group_unsubscribe += 1
        if desired is not None:
            await self._subscribe(client, desired)
            self._group_topic = desired
            self.stats.group_subscribe += 1
        self.group_log.append((time.monotonic(), current, desired))
        log.info("[%s] 그룹 구독 %s → %s", self.uuid, current, desired)

    # ── 접속 수명주기 ────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._run_task is not None:
            return
        self._stopping = False
        self._run_task = asyncio.create_task(self._run(), name=f"sim-{self.uuid}")

    async def stop(self, *, graceful: bool = True) -> None:
        """단말을 내린다. graceful=False 면 TCP 를 그냥 끊는다(DISCONNECT 없음)."""
        self._stopping = True
        if not graceful:
            self._hard_cut()
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._run_task
            self._run_task = None
        self.connected.clear()
        self._client = None

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.connected.wait(), timeout)
        return self.connected.is_set()

    async def wait_state(self, state: str | None, timeout: float = 30.0) -> bool:
        """승인 상태가 `state` 가 될 때까지(None = 무응답) 기다린다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.gate.state == state:
                return True
            await asyncio.sleep(0.1)
        return self.gate.state == state

    async def _run(self) -> None:
        while not self._stopping:
            self._session_task = asyncio.create_task(self._session())
            try:
                await self._session_task
            except asyncio.CancelledError:
                if self._stopping:
                    raise
            except aiomqtt.MqttError as exc:
                self.stats.last_error = str(exc)
                if not self.connected.is_set():
                    self.stats.connect_failures += 1
                log.info("[%s] 세션 종료: %s", self.uuid, exc)
            except Exception as exc:  # noqa: BLE001 — 단말은 어떤 오류에도 죽지 않고 재접속한다
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("[%s] 세션 오류", self.uuid)
            finally:
                if self.connected.is_set():
                    self.stats.disconnects += 1
                self.connected.clear()
                self._client = None
                self.ka_connected = None
            if self._stopping:
                break
            delay = self._next_reconnect_delay()
            self._reconnect_attempt += 1
            self.stats.reconnect_attempts += 1
            await asyncio.sleep(delay)

    def _next_reconnect_delay(self) -> float:
        if self._reconnect_delay_override is not None:
            delay, self._reconnect_delay_override = self._reconnect_delay_override, None
            return delay
        jitter = self._rng.uniform(0, self.reconnect_jitter) if self.reconnect_jitter > 0 else 0.0
        return (reconnect_delay(self._reconnect_attempt) + jitter) / self.time_scale

    def _will(self) -> aiomqtt.Will | None:
        if not self.lwt:
            return None
        return aiomqtt.Will(
            topic=self.topic("event"),
            payload=json.dumps({"type": "LWT", "uuid": self.uuid}, separators=(",", ":")),
            qos=1,
            retain=False,
        )

    async def _session(self) -> None:
        self._subscriptions = []
        self._group_topic = None
        self.grp = None    # §3.10.9 — grp 는 저장하지 않는다. 접속마다 retain REGISTER_ACK 로 다시 받는다
        self.gate.reset()  # §3.5 — 재접속마다 승인 상태를 잊는다
        keepalive = self.ka
        async with aiomqtt.Client(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            identifier=self.uuid,
            protocol=aiomqtt.ProtocolVersion.V311,
            clean_session=True,
            keepalive=keepalive,
            will=self._will(),
            timeout=30,
        ) as client:
            self._client = client
            self.ka_connected = keepalive
            # §3.10.10 구독 표: 1 device cmd, 2 device config, 3 group(ACTIVE+grp 일 때만, REGISTER_ACK 뒤), 4 all/cmd.
            # 1차 펌웨어(1cha)는 device cmd 하나뿐.
            await self._subscribe(client, self.topic("cmd"))
            if self.mode == "2cha":
                await self._subscribe(client, self.topic("config"))
                await self._subscribe(client, f"{self.root}/all/cmd")
            self.stats.connects += 1
            self._reconnect_attempt = 0  # 접속 성공 → 재접속 표 처음부터
            self.connected_at_monotonic = time.monotonic()
            self.connected.set()
            log.info("[%s] 접속 (%s, ka=%d)", self.uuid, self.username, keepalive)

            if self.register_on_connect:
                await self.send_register()
            tm_task = asyncio.create_task(self._tm_loop())
            resend_task = asyncio.create_task(self._register_resend_loop())
            try:
                async for message in client.messages:
                    await self._dispatch(str(message.topic), bytes(message.payload or b""),
                                         bool(message.retain))
            finally:
                for task in (tm_task, resend_task, *self._cmd_tasks):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                self._cmd_tasks.clear()

    async def _subscribe(self, client: aiomqtt.Client, topic: str) -> None:
        """모뎀 한도 6개(§0.2)를 넘기면 실제 단말처럼 등록에 실패한다."""
        if len(self._subscriptions) >= SUBSCRIBE_LIMIT:
            raise RuntimeError(f"구독 topic 한도 {SUBSCRIBE_LIMIT} 초과: {topic}")
        await client.subscribe(topic, qos=1)
        self._subscriptions.append(topic)

    # ── 발행 ─────────────────────────────────────────────────────────────
    async def _publish(self, topic: str, payload: bytes | str | dict, qos: int) -> bool:
        client = self._client
        if client is None:
            return False
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if isinstance(payload, str):
            payload = payload.encode()
        try:
            await client.publish(topic, payload, qos=qos)
            return True
        except aiomqtt.MqttError as exc:
            self.stats.last_error = str(exc)
            return False

    async def send_register(self) -> bool:
        self.last_register_sent_at = time.monotonic()
        ok = await self._publish(self.topic("register"), self.build_register(), qos=1)
        if ok:
            self.stats.register_sent += 1
            self.gate.on_register_sent()
        return ok

    async def send_tm_now(self) -> dict[str, Any] | None:
        """주기를 기다리지 않고 Telemetry 1건. 승인 전(게이트)이면 보내지 않고 None."""
        if not self.gate.telemetry_allowed:
            self.stats.tm_suppressed += 1
            return None
        payload = self.build_tm()
        self.last_tm = payload
        self.last_tm_sent_at = time.monotonic()
        if await self._publish(self.topic("status"), payload, qos=0):
            self.stats.tm_sent += 1
        return payload

    async def send_result(self, payload: dict[str, Any]) -> bool:
        if self.silent_results:
            return False
        self.last_result = payload
        ok = await self._publish(self.topic("result"), payload, qos=1)
        if ok:
            self.stats.result_sent += 1
        return ok

    async def _tm_loop(self) -> None:
        """REGISTER 직후 1건, 이후 `ti` 초마다(§1.1.6). 승인 게이트가 닫혀 있으면 건너뛴다.

        `ti` 는 CONFIG_SET 으로 바뀌면 다음 대기부터 즉시 반영된다.
        """
        await self.send_tm_now()
        while True:
            self._tm_kick.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._tm_kick.wait(), timeout=self.ti)
            await self.send_tm_now()

    async def _register_resend_loop(self) -> None:
        """§3.6 — 무응답/PENDING 이면 5분×5회, 이후 30분 주기로 REGISTER 재전송."""
        while True:
            self._register_kick.clear()
            delay = self.gate.next_resend_delay()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._register_kick.wait(), timeout=delay)
            if self.gate.resend_needed:
                await self.send_register()

    # ── 수신 분기 ────────────────────────────────────────────────────────
    async def _dispatch(self, topic: str, raw: bytes, retained: bool) -> None:
        now = time.monotonic()
        self.last_rx_at_monotonic = now
        data: Any = None
        if raw:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.stats.unknown_rx += 1
                self.inbox.append((topic, raw, retained))
                return
        self.inbox.append((topic, data, retained))
        kind = data.get("type") if isinstance(data, dict) else None
        self.last_rx_by_type[kind or ("EMPTY" if not raw else "?")] = now
        if self.on_message is not None:
            result = self.on_message(self, topic, raw)
            if asyncio.iscoroutine(result):
                await result

        leaf = topic.rsplit("/", 1)[-1]
        if leaf == "config":
            if data is None:
                self.handle_register_ack(None)
                await self._sync_group_subscription()
                return
            if not isinstance(data, dict):
                self.stats.unknown_rx += 1
                return
            if kind == "REGISTER_ACK":
                self.handle_register_ack(data)
                await self._sync_group_subscription()
            elif kind == "CONFIG_SET":
                reply = self.handle_config_set(data)
                if reply is not None:
                    await self.send_result(reply)
                    if reply["result"] == "OK":
                        # 적용 뒤 바로 Telemetry 로 cv 를 echo 한다(§3.9.2 "다음 Telemetry cv 일치 확인").
                        self._tm_kick.set()
            else:
                self.stats.unknown_rx += 1
            return

        if leaf == "cmd":
            if not isinstance(data, dict):
                self.stats.unknown_rx += 1
                return
            if kind == "PING":
                await self.send_result(self.handle_ping(data))
            elif kind == "COMMAND":
                await self._on_command(topic, data)
            elif kind == "CMD":
                self.stats.cmd_legacy_rx += 1  # 2차 전 초안 이름. 1.4.0 이후 단말은 모른다(§3.10.7 "CMD 로 줄이지 않는다")
                self.stats.unknown_rx += 1
            else:
                self.stats.unknown_rx += 1
            return
        self.stats.unknown_rx += 1

    async def _on_command(self, topic: str, data: dict[str, Any]) -> None:
        level = self.level_of(topic)
        if level is None:
            self.stats.unknown_rx += 1
            return
        if level == "group" and topic != self._group_topic:
            # 구독 해제 직후 브로커에 남아 있던 것 등 — 지금 내 그룹이 아니면 버린다.
            self.stats.cmd_foreign_group += 1
            return
        if self.silent_commands > 0:
            self.silent_commands -= 1
            self.stats.cmd_silenced += 1
            log.info("[%s] COMMAND seq=%s 무시(silent_commands)", self.uuid, data.get("seq"))
            return
        if self.delay_commands_sec > 0:
            self.stats.cmd_delayed += 1
            task = asyncio.create_task(self._delayed_command(topic, level, data, self.delay_commands_sec))
            self._cmd_tasks.add(task)
            task.add_done_callback(self._cmd_tasks.discard)
            return
        await self._send_command_ack(topic, level, data)

    async def _delayed_command(self, topic: str, level: str, data: dict[str, Any], delay: float) -> None:
        await asyncio.sleep(delay)
        await self._send_command_ack(topic, level, data)

    async def _send_command_ack(self, topic: str, level: str, data: dict[str, Any]) -> None:
        ack = self.handle_command(data, level, topic=topic)
        await self.send_result(ack)   # §3.10.11 — 그룹·전체로 받아도 각자 device/<uuid>/result, QoS1

    # ── 고장 주입 ────────────────────────────────────────────────────────
    def _hard_cut(self) -> None:
        """DISCONNECT 패킷 없이 TCP 를 끊는다(모뎀 전원 차단 흉내).

        LWT 가 없으므로(§16.1) 서버는 브로커 로그로 끊김을 안다(ADR-004)."""
        client = self._client
        if client is None:
            return
        paho = getattr(client, "_client", None)
        sock = paho.socket() if paho is not None else None
        if sock is None:
            return
        # shutdown 만 하고 close 는 하지 않는다. FIN 만 나가도 브로커는 DISCONNECT 패킷 없는
        # 종료로 본다. close 까지 하면 paho/aiomqtt 가 아직 들고 있는 fd 가
        # 셀렉터에서 무효가 되어 Windows 에서 WinError 10038 / fd -1 로 루프가 죽는다.
        # 닫기는 paho 가 EOF 를 읽고 자기 절차대로 한다.
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)

    async def disconnect(self, *, hard: bool = True, reconnect: bool = False,
                         reconnect_after: float | None = None) -> None:
        """접속만 끊는다. reconnect=True 면 단말이 `reconnect_after` 초 뒤(기본 재접속 표/time_scale) 다시 붙는다."""
        if reconnect_after is not None:
            self._reconnect_delay_override = reconnect_after
        if not reconnect:
            await self.stop(graceful=not hard)
            return
        if hard:
            self._hard_cut()
        elif self._session_task is not None:
            self._session_task.cancel()

    async def reboot(self, *, reconnect_after: float = 1.0) -> None:
        """전원 재인가. sq=0, override·승인 상태 소거, 재접속 후 REGISTER. cv/ti/ka/lat/lon 은 Flash 라 남는다.

        절단을 먼저 하고 상태를 지운다 — 순서를 바꾸면 아직 살아 있는 옛 세션의 TM 루프가
        sq=0 을 한 번 더 보내 서버가 재부팅을 두 번 센다(S2-03 에서 실제 발생)."""
        await self.disconnect(hard=True, reconnect=True, reconnect_after=reconnect_after)
        self.sq = 0
        self._overrides.clear()        # §3.10.8 재부팅 → 슬롯 전부 소거
        self._seq_memory.clear()
        self._pending_local.clear()
        self.grp = None                # 저장하지 않는 값(§3.10.9) — 접속 뒤 retain 으로 다시 받는다
        self.gate.reset()
        self.er = 0

    def drop_next_tm(self, count: int = 1) -> None:
        """다음 `count` 건을 못 보낸 것처럼 sq 만 건너뛴다(전송 실패 시에도 sq 는 올라간다)."""
        self.sq = (self.sq + count) % SQ_MOD

    def sq_wrap(self) -> None:
        """uint32 끝으로 점프. 두 건 뒤에 0 으로 되돌아간다."""
        self.sq = SQ_MOD - 2

    async def send_garbage(self) -> bool:
        """JSON 이 아닌 payload 를 status 로."""
        return await self._publish(self.topic("status"), b'{"type":"TELEMETRY","sq":', qos=0)

    async def send_uuid_mismatch(self) -> bool:
        """payload 의 uuid 가 topic 과 다른 REGISTER (§1.1.4 서버가 대조해야 함)."""
        payload = self.build_register()
        payload["uuid"] = "FFFFFFFFFFFFFFFFFFFFFFFF"
        return await self._publish(self.topic("register"), payload, qos=1)

    async def send_oversized(self, size: int = 64 * 1024) -> bool:
        """비정상적으로 큰 Telemetry (AT 버퍼 384B 를 한참 넘는다)."""
        payload = self.build_tm()
        payload["pad"] = "x" * size
        return await self._publish(self.topic("status"), payload, qos=0)

    async def publish_foreign_topic(self, other_uuid: str) -> dict[str, Any]:
        """남의 UUID topic 에 Telemetry 발행 시도(ACL 위반, §1.1.2.2).

        MQTT 3.1.1 에서 mosquitto 는 ACL 위반 발행을 조용히 버리고 연결은 유지한다.
        결과 dict: published(전송 자체 성공 여부), still_connected(1초 뒤 연결 유지 여부).
        """
        payload = self.build_tm()
        # 남의 topic 에 넣은 건이 DB 에 섞였는지 sq 로 가려낼 수 있게 표식을 단다(상대 단말의 자기 TM 과 구분).
        payload["sq"] = FORGED_SQ
        published = await self._publish(self.topic("status", other_uuid), payload, qos=1)
        await asyncio.sleep(1.0)
        return {"published": published, "still_connected": self.is_connected, "payload": payload}

    async def subscribe_raw(self, topic_filter: str, *, seconds: float = 3.0) -> dict[str, Any]:
        """단말 계정으로 임의 topic 필터를 구독해 본다(ACL 시험 — `iotlight/#` 같은 와일드카드).

        결과: subscribed(SUBACK 가 실패가 아니었는가), received(seconds 동안 받은 건수).
        구독 한도(§0.2)는 셈하지 않는다 — 시험용 구독이라 실제 모뎀 슬롯과 무관.
        """
        client = self._client
        if client is None:
            return {"subscribed": False, "received": 0, "error": "not connected"}
        before = len(self.inbox)
        try:
            await client.subscribe(topic_filter, qos=1)
        except aiomqtt.MqttError as exc:
            return {"subscribed": False, "received": 0, "error": str(exc)}
        await asyncio.sleep(seconds)
        foreign = [t for (t, _, _) in self.inbox[before:] if f"/device/{self.uuid}/" not in t]
        with contextlib.suppress(aiomqtt.MqttError):
            await client.unsubscribe(topic_filter)
        return {"subscribed": True, "received": len(foreign), "error": ""}

    async def duplicate_last_result(self) -> bool:
        """마지막 result 를 그대로 한 번 더(QoS1 재전송 흉내). dedup_key 가 같아야 한다."""
        if self.last_result is None:
            return False
        return await self._publish(self.topic("result"), self.last_result, qos=1)

    async def send_event(self, payload: dict[str, Any]) -> bool:
        return await self._publish(self.topic("event"), payload, qos=1)
