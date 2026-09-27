"""모델 패키지. Alembic env 가 Base.metadata 를 잡을 수 있게 전부 import 한다."""

from __future__ import annotations

from app.models.base import Base
from app.models.command import Command, CommandAck, CommandTarget
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.profile import ConfigProfile
from app.models.region import Region
from app.models.system import AdminUser, MqttAccountExport
from app.models.telemetry import Telemetry, TelemetryDaily

__all__ = [
    "AdminUser",
    "Base",
    "Command",
    "CommandAck",
    "CommandTarget",
    "ConfigProfile",
    "Device",
    "DeviceEvent",
    "MqttAccountExport",
    "Region",
    "Telemetry",
    "TelemetryDaily",
]
