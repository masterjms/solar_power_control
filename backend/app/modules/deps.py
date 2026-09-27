"""라우터 의존성 — app.state 에 붙은 싱글턴을 꺼낸다.

아직 로그인이 없다. REST 는 APP_HOST=127.0.0.1 바인딩(또는 compose 의 포트 비노출)과
nginx Basic auth 로 보호된다. 5차부터 사용자·역할은 `X-Remote-User` 헤더로 판정한다
(app/core/auth.py, ADR-005).
"""

from __future__ import annotations

from fastapi import Request

from app.mqtt.command_retry import CommandRetrier
from app.mqtt.config_sync import ConfigSyncQueue
from app.mqtt.connection import MqttConnection
from app.mqtt.publisher import MqttPublisher
from app.mqtt.telemetry_buffer import TelemetryBuffer
from app.tasks.broker_log import BrokerLogTail


def get_publisher(request: Request) -> MqttPublisher:
    return request.app.state.publisher


def get_connection(request: Request) -> MqttConnection:
    return request.app.state.mqtt


def get_buffer(request: Request) -> TelemetryBuffer:
    return request.app.state.telemetry_buffer


def get_config_sync(request: Request) -> ConfigSyncQueue:
    return request.app.state.config_sync


def get_broker_log(request: Request) -> BrokerLogTail:
    return request.app.state.broker_log


def get_retrier(request: Request) -> CommandRetrier:
    return request.app.state.command_retrier
