"""가짜 단말 한 대 — 사양서를 그대로 흉내낸 소프트웨어 모델.

실물(STM32 + WD-N522S)이 없어도 서버를 개발·시험하려고 만든다. 흉내내는 것:

  · 접속 (§1.1.2): Client ID = UUID 24자리, keepalive 60, clean session, LWT(§16.1)
  · REGISTER (§1.1.4) → Telemetry 1건 즉시 → `ti` 초마다 QoS0 (§1.1.6)
  · PING/PONG (§1.1.5), CONFIG_SET/ACK (§1.1.7), CMD/CMD_ACK (§3.10.7, §3.10.11)
  · 승인 게이트 (§3, 3차): PENDING 이면 Telemetry 를 보내지 않고 REGISTER 재전송(§3.6)
  · 고장 주입: 재부팅(sq 0), TM 유실, 강제 절단(LWT), 잘못된 payload, ACL 위반 …

네트워크와 무관한 부분(TM 값 생성, CONFIG 검증, sq, 승인 상태머신)은 순수 함수·클래스로
떼어 두어 pytest 로 바로 검증한다. 시간 흐름은 `time_scale` 로 압축한다(1 = 실시간).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import aiomqtt

log = logging.getLogger("sim.device")

KST = timezone(timedelta(hours=9))

#: 사양서 §1.1.7 — Telemetry 주기 허용 범위(초)와 CONFIG 버전 범위.
TI_MIN, TI_MAX, TI_DEFAULT = 60, 3600, 600
CV_MIN, CV_MAX = 0, 65535
#: §3.10.7 — override 유지시간 최대 24시간, 0 불가.
DUR_MAX = 86400
#: §0.2 — WD-N522S 모뎀은 구독 topic 을 6개까지만 등록한다.
SUBSCRIBE_LIMIT = 6
#: §3.6 — REGISTER 재전송: 처음 5회는 5분, 이후 30분.
RESEND_FAST_SEC, RESEND_FAST_COUNT, RESEND_SLOW_SEC = 300, 5, 1800
#: 단말 모뎀의 재접속 재시도 주기(1차 README "30초 주기로 정상 반복").
RECONNECT_SEC = 30
#: `sq` 는 uint32 로 가정한다(개발계획 §7 확인 요청 항목).
SQ_MOD = 2**32

APPROVAL_STATES = {"PENDING", "ACTIVE", "SUSPENDED", "REJECTED", "RETIRED"}
CMD_ACTS = {"on", "off", "pwm", "auto"}


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
    fw: str = "1.0.0"

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
    `enabled=False` 면 2차 이전 동작(승인 없이 즉시 Telemetry).
    """

    enabled: bool = False
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
    act: str
    pwm: int | None
    expires_at: float  # time.monotonic()

    def active(self, now: float) -> bool:
        return now < self.expires_at


def validate_config_set(payload: dict[str, Any]) -> str | None:
    """CONFIG_SET 항목 검증(§1.1.7). 문제가 있으면 사유, 없으면 None."""
    cv = payload.get("cv")
    if not isinstance(cv, int) or isinstance(cv, bool) or not (CV_MIN <= cv <= CV_MAX):
        return "cv"
    if "ti" in payload:
        ti = payload["ti"]
        if not isinstance(ti, int) or isinstance(ti, bool) or not (TI_MIN <= ti <= TI_MAX):
            return "ti"
    for key in ("lat", "lon"):
        if key in payload and not isinstance(payload[key], (int, float)):
            return key
    if "lat" in payload and not (-90 <= payload["lat"] <= 90):
        return "lat"
    if "lon" in payload and not (-180 <= payload["lon"] <= 180):
        return "lon"
    if "grp" in payload:
        grp = payload["grp"]
        if not isinstance(grp, list) or len(grp) > 2 or not all(
            isinstance(g, str) and g.isdigit() and 8 <= len(g) <= 16 for g in grp
        ):
            return "grp"
    return None


def validate_cmd(payload: dict[str, Any]) -> str | None:
    """CMD 검증(§3.10.7). `exp`, `dur` 필수. `dur` 0/누락 거부, 최대 86400."""
    seq = payload.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return "seq"
    exp = payload.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool) or exp <= 0:
        return "exp"
    dur = payload.get("dur")
    if not isinstance(dur, int) or isinstance(dur, bool) or not (1 <= dur <= DUR_MAX):
        return "dur"
    act = payload.get("act")
    if act not in CMD_ACTS:
        return "act"
    if act == "pwm":
        pwm = payload.get("pwm")
        if not isinstance(pwm, int) or isinstance(pwm, bool) or not (0 <= pwm <= 100):
            return "pwm"
    return None


@dataclass
class Stats:
    connects: int = 0
    connect_failures: int = 0
    disconnects: int = 0
    register_sent: int = 0
    tm_sent: int = 0
    tm_suppressed: int = 0  # 승인 전이라 보내지 않은 주기
    result_sent: int = 0
    ping_rx: int = 0
    config_set_rx: int = 0
    config_set_ignored: int = 0
    config_ack_ok: int = 0
    config_ack_range: int = 0
    register_ack_rx: int = 0
    cmd_rx: int = 0
    cmd_rejected: int = 0
    unknown_rx: int = 0
    last_error: str = ""


# ── 단말 본체 ─────────────────────────────────────────────────────────────


class SimDevice:
    """가짜 단말 한 대. `start()` 로 띄우고 `stop()` 으로 내린다.

    행동 플래그(생성자):
      mode            "1cha": 공용 계정 + `cv/ss/ti` 없는 REGISTER 허용 / "2cha": uuid 계정 + config 구독
      lwt             False 면 Will 을 등록하지 않는다(모뎀 LWT 미지원, P-2)
      legacy_register REGISTER 에 cv/ss/ti 를 싣지 않는다(1차 펌웨어)
      legacy_t_key    Telemetry 를 `"t":"TM"` 으로 보낸다(1차 펌웨어)
      approval_gate   3차 승인 게이트. REGISTER_ACK state=ACTIVE 전에는 TM 을 보내지 않는다
      ignore_config_set 처음 N 개의 CONFIG_SET 을 못 받은 척한다(재전송 시험)
      silent_results  result 를 아예 보내지 않는다(무응답 단말)
      register_on_connect False 면 접속 직후 REGISTER 를 건너뛴다(유실 흉내)
      time_scale      재전송·재접속 대기를 이 배수로 줄인다(시험용). `ti` 에는 적용하지 않는다
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
        topic_root: str = "iotlight",
        ti: int = TI_DEFAULT,
        cv: int = 0,
        ss: int = 0,
        fw: str = "1.0.0",
        device_model: str = "RMCB-1100M",
        modem_model: str = "WD-N522S",
        lwt: bool = True,
        legacy_register: bool = False,
        legacy_t_key: bool = False,
        approval_gate: bool = False,
        ignore_config_set: int = 0,
        silent_results: bool = False,
        register_on_connect: bool = True,
        time_scale: float = 1.0,
        reconnect_jitter: float = 0.0,
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
        # 2차 모드는 username = uuid(§1.1.2.2). 1차 모드는 공용 계정.
        self.username = username or (uuid if mode == "2cha" else "solarlte-test")
        self.password = password or ("" if mode == "2cha" else "solarlte-test-2026")
        self.root = topic_root
        self.ti, self.cv, self.ss = ti, cv, ss
        self.fw, self.device_model, self.modem_model = fw, device_model, modem_model
        self.lat: float | None = None
        self.lon: float | None = None
        self.grp: list[str] = []
        self.lwt = lwt
        self.legacy_register = legacy_register
        self.legacy_t_key = legacy_t_key
        self.ignore_config_set = ignore_config_set
        self.silent_results = silent_results
        #: False 면 접속 후 REGISTER 를 보내지 않는다(REGISTER 유실 흉내, S2-02).
        self.register_on_connect = register_on_connect
        self.time_scale = max(time_scale, 1e-6)
        self.reconnect_jitter = reconnect_jitter
        self.on_message = on_message

        seed_val = seed if seed is not None else int(uuid[-8:], 16)
        self._rng = random.Random(seed_val)
        self.model = TelemetryModel(seed=seed_val, fw=fw)
        self.gate = ApprovalGate(enabled=approval_gate, time_scale=self.time_scale)
        self.stats = Stats()

        # 단말 식별값(런타임에 안 바뀜, §1.1.4).
        self.imei = f"35{seed_val % 10**13:013d}"
        self.iccid = f"8982{seed_val % 10**15:015d}"
        self.msisdn = ""

        self.sq = 0
        self.er = 0
        self._overrides: dict[str, OverrideSlot] = {}
        self._seen_cmd_seq: set[int] = set()
        self._subscriptions: list[str] = []
        #: 마지막으로 보낸 result payload — QoS1 중복 재전송 흉내에 쓴다.
        self.last_result: dict[str, Any] | None = None
        self.last_tm: dict[str, Any] | None = None
        #: 받은 메시지 이력 (topic, payload dict|None, retained). 시나리오가 들여다본다.
        self.inbox: list[tuple[str, Any, bool]] = []
        self.connected = asyncio.Event()
        self._client: aiomqtt.Client | None = None
        self._run_task: asyncio.Task | None = None
        self._session_task: asyncio.Task | None = None
        self._stopping = False
        self._reconnect_delay_override: float | None = None
        self._tm_kick = asyncio.Event()
        self._register_kick = asyncio.Event()

    # ── topic 도우미 ──────────────────────────────────────────────────────
    def topic(self, kind: str, uuid: str | None = None) -> str:
        return f"{self.root}/device/{uuid or self.uuid}/{kind}"

    @property
    def is_connected(self) -> bool:
        return self.connected.is_set()

    # ── payload 생성 (순수) ───────────────────────────────────────────────
    def build_register(self) -> dict[str, Any]:
        """§1.1.4. 1차 펌웨어(legacy_register)는 cv/ss/ti 를 싣지 않는다."""
        payload: dict[str, Any] = {
            "type": "REGISTER",
            "uuid": self.uuid,
            "fw": self.fw,
        }
        if not self.legacy_register:
            payload.update({"cv": self.cv, "ss": self.ss, "ti": self.ti})
        payload.update({
            "device_model": self.device_model,
            "modem_model": self.modem_model,
            "msisdn": self.msisdn,
            "imei": self.imei,
            "iccid": self.iccid,
        })
        return payload

    def build_tm(self, now: datetime | None = None) -> dict[str, Any]:
        """§1.1.6. 호출할 때마다 `sq` 를 하나 올린다(보내지 못해도 올린다 — 전송 실패 처리)."""
        sample = self.model.sample(now)
        md = 0
        ov = self.current_override()
        if ov is not None:
            md = 2
            if ov.act == "off":
                sample["on"], sample["pw"] = 0, [0, 0, 0]
            elif ov.act == "on":
                sample["on"], sample["pw"] = 1, list(self.model.pwm)
            elif ov.act == "pwm":
                sample["on"] = 1 if (ov.pwm or 0) > 0 else 0
                sample["pw"] = [ov.pwm or 0] * 3
        payload: dict[str, Any] = {
            ("t" if self.legacy_t_key else "type"): "TM",
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

    def current_override(self, now: float | None = None) -> OverrideSlot | None:
        """유효한 슬롯 중 최상위(개별 > 그룹 > 전체, §3.10.8)."""
        now = time.monotonic() if now is None else now
        for layer in ("device", "group", "all"):
            slot = self._overrides.get(layer)
            if slot is not None and slot.active(now):
                return slot
            if slot is not None:
                del self._overrides[layer]
        return None

    # ── 수신 처리 (순수: 응답 payload 를 돌려준다) ───────────────────────
    def handle_ping(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.stats.ping_rx += 1
        return {"type": "PONG", "seq": payload.get("seq"), "uuid": self.uuid}

    def handle_config_set(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """§1.1.7. 범위 밖이면 이전 값 유지·cv 미변경·RANGE. `ignore_config_set` 만큼은 못 받은 척."""
        self.stats.config_set_rx += 1
        if self.ignore_config_set > 0:
            self.ignore_config_set -= 1
            self.stats.config_set_ignored += 1
            return None
        problem = validate_config_set(payload)
        if problem is not None:
            self.stats.config_ack_range += 1
            log.info("[%s] CONFIG_SET 거부(%s) cv 유지 %d", self.uuid, problem, self.cv)
            return {"type": "CONFIG_ACK", "uuid": self.uuid, "cv": self.cv, "result": "RANGE"}
        # 전부 적용하고 Flash 저장(여기서는 인스턴스 필드 — reboot() 에도 남는다).
        self.cv = int(payload["cv"])
        if "ti" in payload:
            self.ti = int(payload["ti"])
        if "lat" in payload:
            self.lat = float(payload["lat"])
        if "lon" in payload:
            self.lon = float(payload["lon"])
        if "grp" in payload:
            self.grp = list(payload["grp"])
        self.stats.config_ack_ok += 1
        return {"type": "CONFIG_ACK", "uuid": self.uuid, "cv": self.cv, "result": "OK"}

    def handle_cmd(self, payload: dict[str, Any], layer: str = "device") -> dict[str, Any] | None:
        """§3.10.7. 검증 실패면 거부하고 로그만 남긴다(응답 없음 — 사양에 거부 ACK 가 없다).

        같은 `seq` 가 다시 오면(QoS1 중복) 다시 실행하지 않고 ACK 만 다시 보낸다.
        """
        self.stats.cmd_rx += 1
        problem = validate_cmd(payload)
        if problem is not None:
            self.stats.cmd_rejected += 1
            log.warning("[%s] CMD 거부(%s): %s", self.uuid, problem, payload)
            return None
        seq = int(payload["seq"])
        act = payload["act"]
        dur = int(payload["dur"])
        if seq not in self._seen_cmd_seq:
            self._seen_cmd_seq.add(seq)
            if act == "auto":
                self._overrides.pop(layer, None)
            else:
                self._overrides[layer] = OverrideSlot(
                    act=act, pwm=payload.get("pwm"), expires_at=time.monotonic() + dur
                )
        ack: dict[str, Any] = {
            "type": "CMD_ACK", "uuid": self.uuid, "seq": seq, "result": "OK",
            "act": act, "dur": dur,
        }
        if act == "pwm":
            ack["pwm"] = payload["pwm"]
        return ack

    def handle_register_ack(self, payload: dict[str, Any] | None) -> None:
        """REGISTER_ACK(§3.3). payload None = 빈 retain(정리)."""
        self.stats.register_ack_rx += 1
        if payload is None:
            self.gate.on_ack(None)
            return
        if payload.get("uuid") not in (None, self.uuid):
            log.warning("[%s] REGISTER_ACK uuid 불일치 %s", self.uuid, payload.get("uuid"))
            return
        self.gate.on_ack(payload.get("state"))
        if self.gate.state == "ACTIVE":
            # 승인되면 곧 CONFIG_SET 이 오고 Telemetry 를 시작한다(§3.1). 주기를 기다리지 않는다.
            self._tm_kick.set()

    # ── 접속 수명주기 ────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._run_task is not None:
            return
        self._stopping = False
        self._run_task = asyncio.create_task(self._run(), name=f"sim-{self.uuid}")

    async def stop(self, *, graceful: bool = True) -> None:
        """단말을 내린다. graceful=False 면 TCP 를 그냥 끊는다(브로커가 LWT 발행)."""
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
            if self._stopping:
                break
            delay = self._next_reconnect_delay()
            await asyncio.sleep(delay)

    def _next_reconnect_delay(self) -> float:
        if self._reconnect_delay_override is not None:
            delay, self._reconnect_delay_override = self._reconnect_delay_override, None
            return delay
        jitter = self._rng.uniform(0, self.reconnect_jitter) if self.reconnect_jitter > 0 else 0.0
        return (RECONNECT_SEC + jitter) / self.time_scale

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
        self.gate.reset()  # §3.5 — 재접속마다 승인 상태를 잊는다
        async with aiomqtt.Client(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            identifier=self.uuid,
            protocol=aiomqtt.ProtocolVersion.V311,
            clean_session=True,
            keepalive=60,
            will=self._will(),
            timeout=30,
        ) as client:
            self._client = client
            await self._subscribe(client, self.topic("cmd"))
            if self.mode == "2cha":
                await self._subscribe(client, self.topic("config"))
                for g in self.grp[:2]:
                    await self._subscribe(client, f"{self.root}/group/{g}/cmd")
                if self.grp:
                    await self._subscribe(client, f"{self.root}/all/cmd")
            self.stats.connects += 1
            self.connected.set()
            log.info("[%s] 접속 (%s)", self.uuid, self.username)

            if self.register_on_connect:
                await self.send_register()
            tm_task = asyncio.create_task(self._tm_loop())
            resend_task = asyncio.create_task(self._register_resend_loop())
            try:
                async for message in client.messages:
                    await self._dispatch(str(message.topic), bytes(message.payload or b""),
                                         bool(message.retain))
            finally:
                for task in (tm_task, resend_task):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

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
        """REGISTER 직후 1건, 이후 `ti` 초마다(§1.1.6). 승인 게이트가 닫혀 있으면 건너뛴다."""
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
        data: Any = None
        if raw:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.stats.unknown_rx += 1
                self.inbox.append((topic, raw, retained))
                return
        self.inbox.append((topic, data, retained))
        if self.on_message is not None:
            result = self.on_message(self, topic, raw)
            if asyncio.iscoroutine(result):
                await result

        leaf = topic.rsplit("/", 1)[-1]
        if leaf == "config":
            if data is None:
                self.handle_register_ack(None)
                return
            kind = data.get("type")
            if kind == "REGISTER_ACK":
                self.handle_register_ack(data)
            elif kind == "CONFIG_SET":
                if self.gate.enabled and self.gate.state != "ACTIVE":
                    # PENDING 에서는 CONFIG 변경 불가(§3.8). 받아도 적용하지 않는다.
                    self.stats.config_set_rx += 1
                    return
                reply = self.handle_config_set(data)
                if reply is not None:
                    await self.send_result(reply)
                    if reply["result"] == "OK":
                        # 승인 직후 CONFIG 적용 → 즉시 Telemetry(§3.1 8단계).
                        self._tm_kick.set()
            else:
                self.stats.unknown_rx += 1
            return

        if leaf == "cmd":
            if not isinstance(data, dict):
                self.stats.unknown_rx += 1
                return
            kind = data.get("type")
            if kind == "PING":
                await self.send_result(self.handle_ping(data))
            elif kind == "CMD":
                if self.gate.enabled and self.gate.state != "ACTIVE":
                    return  # PENDING 에서 원격 제어 불가(§3.8)
                parts = topic.split("/")
                layer = "device" if parts[1] == "device" else ("all" if parts[1] == "all" else "group")
                if layer == "group" and parts[2] not in self.grp:
                    return  # 남의 그룹 명령은 자기 grp 로 걸러낸다(§1.1.2.2)
                reply = self.handle_cmd(data, layer)
                if reply is not None:
                    await self.send_result(reply)
            else:
                self.stats.unknown_rx += 1
            return
        self.stats.unknown_rx += 1

    # ── 고장 주입 ────────────────────────────────────────────────────────
    def _hard_cut(self) -> None:
        """DISCONNECT 없이 TCP 를 끊는다 → 브로커가 LWT 를 대신 발행한다."""
        client = self._client
        if client is None:
            return
        paho = getattr(client, "_client", None)
        sock = paho.socket() if paho is not None else None
        if sock is None:
            return
        # shutdown 만 하고 close 는 하지 않는다. FIN 만 나가도 브로커는 DISCONNECT 패킷 없는
        # 종료로 보고 LWT 를 발행한다. close 까지 하면 paho/aiomqtt 가 아직 들고 있는 fd 가
        # 셀렉터에서 무효가 되어 Windows 에서 WinError 10038 / fd -1 로 루프가 죽는다.
        # 닫기는 paho 가 EOF 를 읽고 자기 절차대로 한다.
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)

    async def disconnect(self, *, hard: bool = True, reconnect: bool = False,
                         reconnect_after: float | None = None) -> None:
        """접속만 끊는다. reconnect=True 면 단말이 `reconnect_after` 초 뒤(기본 30초/time_scale) 다시 붙는다."""
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
        """전원 재인가. sq=0, override·승인 상태 소거, 재접속 후 REGISTER. cv/ti/lat/lon 은 Flash 라 남는다.

        절단을 먼저 하고 상태를 지운다 — 순서를 바꾸면 아직 살아 있는 옛 세션의 TM 루프가
        sq=0 을 한 번 더 보내 서버가 재부팅을 두 번 센다(S2-03 에서 실제 발생)."""
        await self.disconnect(hard=True, reconnect=True, reconnect_after=reconnect_after)
        self.sq = 0
        self._overrides.clear()
        self._seen_cmd_seq.clear()
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
        return await self._publish(self.topic("status"), b'{"type":"TM","sq":', qos=0)

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
        published = await self._publish(self.topic("status", other_uuid), payload, qos=1)
        await asyncio.sleep(1.0)
        return {"published": published, "still_connected": self.is_connected, "payload": payload}

    async def duplicate_last_result(self) -> bool:
        """마지막 result 를 그대로 한 번 더(QoS1 재전송 흉내). dedup_key 가 같아야 한다."""
        if self.last_result is None:
            return False
        return await self._publish(self.topic("result"), self.last_result, qos=1)

    async def send_event(self, payload: dict[str, Any]) -> bool:
        return await self._publish(self.topic("event"), payload, qos=1)
