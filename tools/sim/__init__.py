"""단말 시뮬레이터 — 사양서 §1.1, §3, §3.10.7, §16.1 을 소프트웨어로 흉내낸 가짜 단말."""

from tools.sim.device import ApprovalGate, SimDevice, TelemetryModel, uuid_from_index

__all__ = ["ApprovalGate", "SimDevice", "TelemetryModel", "uuid_from_index"]
