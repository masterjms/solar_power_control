"""프로세스 내 카운터. /health · /api/metrics 가 읽는다.

Prometheus 는 4차 이후 검토(docs/00 §5). 그때까지는 JSON 으로 충분하다 — 재시작하면 0 으로
돌아가는 값이며, 추세가 아니라 "지금 뭔가 새고 있나"를 보는 용도다.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


class Metrics:
    def __init__(self) -> None:
        #: kind(register/status/result/event)별 수신 건수.
        self.received: Counter[str] = Counter()
        #: type(REGISTER/TELEMETRY/PONG/…)별 수신 건수.
        self.received_type: Counter[str] = Counter()
        self.malformed_topic = 0
        self.malformed_payload = 0
        self.uuid_mismatch = 0
        self.unknown_type = 0
        #: flush 실패로 버린 telemetry 행 수.
        self.telemetry_dropped = 0
        self.telemetry_flushed = 0
        self.flush_ms_last = 0.0
        self.flush_ms_max = 0.0
        self.flush_failures = 0
        #: ACTIVE 가 아닌 단말이 보낸 TELEMETRY (사양서 §3.8 — 보내면 안 되는 상태). 저장은 한다.
        self.telemetry_not_active = 0
        #: REGISTER 응답 대기열에서 상한 초과로 버린 건수.
        self.register_reply_dropped = 0
        #: REGISTER_ACK retain 발행 수 (REGISTER 응답 + 관리자 상태 변경 + 재발행).
        self.register_ack_sent = 0
        self.config_set_sent = 0
        self.config_ack_ok = 0
        self.config_ack_range = 0
        #: 단말이 "승인 전" 이라 거부. DB 가 ACTIVE 면 REGISTER_ACK 를 다시 retain 한다.
        self.config_ack_state = 0
        #: 단말 Flash 기록 실패. 쿨다운을 풀어 다음 송신 때 바로 재전송한다.
        self.config_ack_flash = 0
        self.pong_mismatch = 0
        self.mqtt_reconnects = 0
        self.mqtt_publish_failures = 0
        #: 1차 펌웨어(`t` 키)로 보고 있는 단말 수.
        self.legacy_t_devices = 0
        # ── 브로커 인증 API (/internal/mqtt/*) ──
        self.mqtt_auth_ok = 0
        self.mqtt_auth_fail = 0
        self.mqtt_acl_deny = 0
        # ── 브로커 로그 tail (ADR-004) ──
        self.broker_log_lines = 0
        self.broker_log_online = 0
        self.broker_log_offline = 0
        self.broker_log_errors = 0
        #: `disconnected, not authorised.` 줄 수 — HMAC 키 불일치·옛 펌웨어 단말.
        self.broker_log_not_authorised = 0

    def snapshot(self, *, buffer_pending: int, register_queue: int,
                 mqtt_connected: bool, broker_log_tail: bool) -> dict[str, Any]:
        return {
            "mqtt_connected": mqtt_connected,
            "mqtt_reconnects": self.mqtt_reconnects,
            "mqtt_publish_failures": self.mqtt_publish_failures,
            "received": dict(self.received),
            "received_type": dict(self.received_type),
            "malformed_topic": self.malformed_topic,
            "malformed_payload": self.malformed_payload,
            "uuid_mismatch": self.uuid_mismatch,
            "unknown_type": self.unknown_type,
            "telemetry_flushed": self.telemetry_flushed,
            "telemetry_dropped": self.telemetry_dropped,
            "telemetry_not_active": self.telemetry_not_active,
            "flush_failures": self.flush_failures,
            "flush_ms_last": round(self.flush_ms_last, 1),
            "flush_ms_max": round(self.flush_ms_max, 1),
            "buffer_pending": buffer_pending,
            "register_queue": register_queue,
            "register_reply_dropped": self.register_reply_dropped,
            "register_ack_sent": self.register_ack_sent,
            "config_set_sent": self.config_set_sent,
            "config_ack_ok": self.config_ack_ok,
            "config_ack_range": self.config_ack_range,
            "config_ack_state": self.config_ack_state,
            "config_ack_flash": self.config_ack_flash,
            "pong_mismatch": self.pong_mismatch,
            "legacy_t_devices": self.legacy_t_devices,
            "mqtt_auth_ok": self.mqtt_auth_ok,
            "mqtt_auth_fail": self.mqtt_auth_fail,
            "mqtt_acl_deny": self.mqtt_acl_deny,
            "broker_log_tail": broker_log_tail,
            "broker_log_lines": self.broker_log_lines,
            "broker_log_online": self.broker_log_online,
            "broker_log_offline": self.broker_log_offline,
            "broker_log_errors": self.broker_log_errors,
            "broker_log_not_authorised": self.broker_log_not_authorised,
        }

    def record_flush(self, ms: float) -> None:
        self.flush_ms_last = ms
        self.flush_ms_max = max(self.flush_ms_max, ms)


#: 프로세스에 하나. 테스트는 새 Metrics() 를 만들어 주입한다.
metrics = Metrics()
