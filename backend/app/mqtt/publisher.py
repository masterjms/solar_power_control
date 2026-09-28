"""MQTT 발행 단일 창구.

★ 브로커로 나가는 모든 메시지는 이 파일을 거친다. 다른 모듈이
  connection.raw_publish() 를 직접 부르지 않는다.

여기에 몰아둔 것:
  1. payload 직렬화 (compact **한 줄** JSON, ensure_ascii=False — 모뎀은 payload 안의
     줄바꿈을 여러 줄로 넘겨 단말이 한 메시지로 못 읽는다(UI_항목_명세 8.4). 줄바꿈이 섞이면
     발행 전에 막는다)
  2. 크기 검사 (900B 초과 = 발행 전에 실패, 800B 초과 = 경고. 단말 수신 줄 1,024B 한계, ADR-007)
  3. QoS / retain 정책 — cmd 는 절대 retain 하지 않고, retain 은 REGISTER_ACK 하나뿐(ADR-002)

5차 COMMAND(command_payload) · 6차 SCH · 7차 OTA 빌더가 여기 있다/추가된다. 그때도 규칙은 같다 —
빌더는 dict 를 만들고, 실제 전송은 _send() 하나만 쓴다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Mapping
from typing import Any

from app.constants import MQTT_MAX_PAYLOAD_BYTES, MQTT_WARN_PAYLOAD_BYTES, MsgType
from app.core.metrics import metrics
from app.errors import PayloadTooLarge
from app.mqtt import topics
from app.mqtt.connection import MqttConnection

log = logging.getLogger(__name__)

_QOS_CMD = 1
_QOS_CONFIG = 1


def encode(payload: Mapping[str, Any]) -> bytes:
    """구분자에서 공백을 뺀 UTF-8 한 줄 JSON. json.dumps 는 문자열 안의 줄바꿈을 이스케이프하므로
    결과에 실제 줄바꿈 바이트가 있을 수 없다 — 그래도 단말 쪽 사고가 커서 확인한다."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if b"\n" in raw or b"\r" in raw:
        raise ValueError("MQTT payload 에 줄바꿈이 있다 — 단말이 한 메시지로 읽지 못한다")
    return raw


def check_size(topic: str, raw: bytes) -> None:
    """900B 초과면 PayloadTooLarge. 800B 초과면 경고만."""
    if len(raw) > MQTT_MAX_PAYLOAD_BYTES:
        raise PayloadTooLarge(
            detail={"topic": topic, "size_bytes": len(raw), "limit_bytes": MQTT_MAX_PAYLOAD_BYTES}
        )
    if len(raw) > MQTT_WARN_PAYLOAD_BYTES:
        log.warning("MQTT payload %dB — 단말 한계 %dB 에 근접: %s",
                    len(raw), MQTT_MAX_PAYLOAD_BYTES, topic)


# ── payload 빌더 (순수 함수 — 테스트가 직접 검사한다) ───────────────────
def config_set_payload(
    *, cv: int, ti: int, ka: int, lat: float | None, lon: float | None
) -> dict[str, Any]:
    """CONFIG_SET (사양서 §1.1.7 S-13) — **항상 전체값** `cv` `ti` `ka`, 좌표는 있을 때만.

    일부만 보내면 빠진 항목은 단말의 옛 값이 남는데 cv 는 같아져 서버가 어긋남을 영영 모른다.
    cv 0 은 보내지 않는다(호출부가 next_cv_server 로 1 이상을 만든다) — 여기서 한 번 더 막는다.
    lat/lon 이 NULL 이면 키 자체를 뺀다(하나만 있으면 둘 다 뺀다 — 반쪽 좌표는 못 쓴다).
    """
    if cv < 1:
        raise ValueError(f"CONFIG_SET cv 는 1 이상이어야 합니다 (cv={cv}) — S-13 규칙 2")
    payload: dict[str, Any] = {"type": MsgType.CONFIG_SET.value, "cv": cv, "ti": ti, "ka": ka}
    if lat is not None and lon is not None:
        payload["lat"] = lat
        payload["lon"] = lon
    return payload


def ping_payload(*, seq: int) -> dict[str, Any]:
    return {"type": MsgType.PING.value, "seq": seq}


#: COMMAND.ts 형식 — 보낸 시각 KST `YYMMDDThhmmss` (사양서 §3.10.7).
KST = dt.timezone(dt.timedelta(hours=9), "KST")


def kst_ts(at: dt.datetime) -> str:
    """aware datetime → `YYMMDDThhmmss`(KST). 단말은 RTC - ts > exp 면 EXPIRED 로 버린다."""
    return at.astimezone(KST).strftime("%y%m%dT%H%M%S")


def parse_kst_ts(value: str) -> dt.datetime:
    """`YYMMDDThhmmss` → aware datetime(KST). 형식이 틀리면 ValueError."""
    return dt.datetime.strptime(value, "%y%m%dT%H%M%S").replace(tzinfo=KST)


def command_payload(
    *, seq: int, ts: str, exp: int, act: str, ch: list[int] | tuple[int, ...],
    pwm: list[int] | tuple[int, ...] | None = None, dur: int | None = None,
) -> dict[str, Any]:
    """COMMAND (사양서 §3.10.7) — 키 순서 `type seq ts exp act ch [pwm] [dur]` 고정.

    `pwm` 은 act=pwm 일 때만, `dur` 은 auto 가 아닐 때만 싣는다(auto 는 dur 이 없다). 값 검증은
    호출부(core/command_rules)가 끝낸 뒤다 — 여기서는 모양만 만든다.
    """
    payload: dict[str, Any] = {
        "type": MsgType.COMMAND.value, "seq": seq, "ts": ts, "exp": exp, "act": act,
        "ch": list(ch),
    }
    if act == "pwm" and pwm is not None:
        payload["pwm"] = list(pwm)
    if act != "auto" and dur is not None:
        payload["dur"] = dur
    return payload


def register_ack_payload(
    *, uuid: str, state: str, site: str | None = None, reason: str | None = None,
    grp: str | None = None,
) -> dict[str, Any]:
    """REGISTER_ACK (사양서 §3.3). state·site·grp(·reason) 만 싣는다 — cv/ti/ka 는 절대
    넣지 않는다. retain 메시지에 설정값이 섞이면 재부팅 시 옛 값이 먼저 도착한다.

    **state·site·grp 는 언제나 셋 다**, 없으면 `""` (2026-09-27 연동 시험 지적). 단말은
    site·grp 가 빠진 ACK 를 "없음"으로 받아 그룹 구독을 해제한다. reason 만 있을 때 싣는다."""
    payload: dict[str, Any] = {
        "type": MsgType.REGISTER_ACK.value, "uuid": uuid, "state": state,
        "site": site or "", "grp": grp or "",
    }
    if reason:
        payload["reason"] = reason
    return payload


class MqttPublisher:
    def __init__(self, connection: MqttConnection) -> None:
        self._conn = connection

    @property
    def connection(self) -> MqttConnection:
        """연결 상태 조회용(/health). 발행에는 쓰지 않는다."""
        return self._conn

    # ── 최하위 전송 ─────────────────────────────────────────────────────
    async def _send(
        self, topic: str, payload: Mapping[str, Any], *, qos: int, retain: bool
    ) -> None:
        raw = encode(payload)
        check_size(topic, raw)
        try:
            await self._conn.raw_publish(topic, raw, qos=qos, retain=retain)
        except Exception:
            metrics.mqtt_publish_failures += 1
            raise
        log.info("MQTT → %s (%dB, qos=%d, retain=%s)", topic, len(raw), qos, retain)

    # ── CONFIG ──────────────────────────────────────────────────────────
    async def publish_config_set(
        self, *, uuid: str, cv: int, ti: int, ka: int, lat: float | None, lon: float | None
    ) -> dict[str, Any]:
        """CONFIG_SET. retain=False — 단말이 cv 를 Flash 에 저장하고 Telemetry 에 echo
        하므로 retain 이 필요 없고, 사양서가 retain 에 설정값 섞는 것을 금지한다(ADR-002).
        보낸 payload 를 돌려준다(device_event(CONFIG_SET) 에 그대로 남긴다)."""
        payload = config_set_payload(cv=cv, ti=ti, ka=ka, lat=lat, lon=lon)
        await self._send(topics.device_config(uuid), payload, qos=_QOS_CONFIG, retain=False)
        metrics.config_set_sent += 1
        return payload

    async def publish_register_ack(
        self, *, uuid: str, state: str, site: str | None = None, reason: str | None = None,
        grp: str | None = None,
    ) -> dict[str, Any]:
        """REGISTER_ACK — 유일한 retain 메시지 (사양서 §3.3).

        빈 payload 를 retain 으로 보내면 브로커가 보관본을 지운다(RETIRED 정리) —
        그건 clear_register_ack() 다. 보낸 payload 를 돌려준다.
        """
        payload = register_ack_payload(uuid=uuid, state=state, site=site, reason=reason, grp=grp)
        await self._send(topics.device_config(uuid), payload, qos=_QOS_CONFIG, retain=True)
        metrics.register_ack_sent += 1
        return payload

    async def clear_register_ack(self, *, uuid: str) -> None:
        """retain 보관본 삭제 (사양서 §3.3, RETIRED 정리·단말 삭제). 빈 payload + retain."""
        try:
            await self._conn.raw_publish(
                topics.device_config(uuid), b"", qos=_QOS_CONFIG, retain=True
            )
        except Exception:
            metrics.mqtt_publish_failures += 1
            raise
        log.info("MQTT → %s (retain 삭제)", topics.device_config(uuid))

    # ── CMD ─────────────────────────────────────────────────────────────
    async def publish_ping(self, *, uuid: str, seq: int) -> None:
        await self.publish_device_cmd(uuid=uuid, payload=ping_payload(seq=seq))

    async def publish_device_cmd(self, *, uuid: str, payload: Mapping[str, Any]) -> None:
        """개별 cmd. retain=False 고정 — cmd 에 retain 을 걸면 재접속 단말에 옛 명령이
        되살아난다(5차 소등 명령이면 사고다)."""
        await self._send(topics.device_cmd(uuid), payload, qos=_QOS_CMD, retain=False)

    async def publish_settings_get(self, *, uuid: str, payload: Mapping[str, Any]) -> int:
        """S-23 SETTINGS_GET — 개별 cmd topic 만(그룹·전체면 단말이 무시한다). 보낸 바이트 수."""
        return await self._publish_settings(uuid, payload, "SETTINGS_GET")

    async def publish_settings_set(self, *, uuid: str, payload: Mapping[str, Any]) -> int:
        """S-23 SETTINGS_SET(25개 전부 + 선택 tbl). 최대 약 560B. 보낸 바이트 수."""
        return await self._publish_settings(uuid, payload, "SETTINGS_SET")

    async def _publish_settings(
        self, uuid: str, payload: Mapping[str, Any], kind: str
    ) -> int:
        if payload.get("type") != kind:
            raise ValueError(f"{kind} 가 아닌 payload: {payload.get('type')}")
        raw = encode(payload)
        await self._send(topics.device_cmd(uuid), payload, qos=_QOS_CMD, retain=False)
        return len(raw)

    async def publish_command(self, *, topic: str, payload: Mapping[str, Any]) -> None:
        """5차 COMMAND — 개별·그룹·전체 cmd topic 공용. QoS1, **retain=False 고정**.

        topic 은 topics.device_cmd / group_cmd / all_cmd 로 만든 것만 받는다. 다른 topic 으로
        새지 않게 여기서 한 번 더 본다(오타 하나가 "명령이 조용히 안 감"이 된다)."""
        if not topic.endswith("/cmd"):
            raise ValueError(f"COMMAND 는 cmd topic 으로만 보낸다: {topic}")
        await self._send(topic, payload, qos=_QOS_CMD, retain=False)
        metrics.command_published += 1
