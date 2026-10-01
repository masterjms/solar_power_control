"""모델 패키지. Alembic env 가 Base.metadata 를 잡을 수 있게 전부 import 한다."""

from __future__ import annotations

from app.models.alarm import Alarm
from app.models.base import Base
from app.models.command import Command, CommandAck, CommandTarget
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.profile import ConfigProfile
from app.models.region import Region
from app.models.schedule import (
    DeployItem,
    DeployJob,
    DeviceSchedule,
    ScheduleAssign,
    ScheduleProfile,
)
from app.models.settings import DeviceSettings, DeviceSettingsHistory
from app.models.system import AdminUser, MqttAccountExport, ServerSetting
from app.models.telemetry import Telemetry, TelemetryDaily

__all__ = [
    "AdminUser",
    "Alarm",
    "Base",
    "Command",
    "CommandAck",
    "CommandTarget",
    "ConfigProfile",
    "Device",
    "DeployItem",
    "DeployJob",
    "DeviceEvent",
    "DeviceSchedule",
    "DeviceSettings",
    "DeviceSettingsHistory",
    "MqttAccountExport",
    "Region",
    "ScheduleAssign",
    "ScheduleProfile",
    "Telemetry",
    "TelemetryDaily",
]
