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
        #: type(REGISTER/TM/PONG/…)별 수신 건수.
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
        #: REGISTER 응답 대기열에서 상한 초과로 버린 건수.
        self.register_reply_dropped = 0
        self.config_set_sent = 0
        self.config_ack_range = 0
        self.pong_mismatch = 0
        self.mqtt_reconnects = 0
        self.mqtt_publish_failures = 0
        #: 1차 펌웨어(`t` 키)로 보고 있는 단말 수.
        self.legacy_t_devices = 0

    def snapshot(self, *, buffer_pending: int, register_queue: int,
                 mqtt_connected: bool) -> dict[str, Any]:
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
            "flush_failures": self.flush_failures,
            "flush_ms_last": round(self.flush_ms_last, 1),
            "flush_ms_max": round(self.flush_ms_max, 1),
            "buffer_pending": buffer_pending,
            "register_queue": register_queue,
            "register_reply_dropped": self.register_reply_dropped,
            "config_set_sent": self.config_set_sent,
            "config_ack_range": self.config_ack_range,
            "pong_mismatch": self.pong_mismatch,
            "legacy_t_devices": self.legacy_t_devices,
        }

    def record_flush(self, ms: float) -> None:
        self.flush_ms_last = ms
        self.flush_ms_max = max(self.flush_ms_max, ms)


#: 프로세스에 하나. 테스트는 새 Metrics() 를 만들어 주입한다.
metrics = Metrics()
