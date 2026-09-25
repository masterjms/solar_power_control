"""라우터 의존성 — app.state 에 붙은 싱글턴을 꺼낸다.

2차는 인증이 없다. REST 는 APP_HOST=127.0.0.1 바인딩(또는 compose 의 포트 비노출)으로만
보호된다(config.py 참고). 3차에서 admin_user 기반 `require_admin` 의존성이 여기 추가된다.
"""

from __future__ import annotations

from fastapi import Request

from app.mqtt.config_sync import ConfigSyncQueue
from app.mqtt.connection import MqttConnection
from app.mqtt.publisher import MqttPublisher
from app.mqtt.telemetry_buffer import TelemetryBuffer


def get_publisher(request: Request) -> MqttPublisher:
    return request.app.state.publisher


def get_connection(request: Request) -> MqttConnection:
    return request.app.state.mqtt


def get_buffer(request: Request) -> TelemetryBuffer:
    return request.app.state.telemetry_buffer


def get_config_sync(request: Request) -> ConfigSyncQueue:
    return request.app.state.config_sync
