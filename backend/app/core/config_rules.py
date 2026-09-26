"""CONFIG 값 규칙 — 적용값·cv 판정 (사양서 §1.1.7 S-13, §4.2, ADR-002).

순수 함수만 둔다. DB·MQTT 는 모른다. handlers / telemetry_buffer / device.service 세 곳이
같은 규칙을 써야 하므로 한 파일에 모은다.

    적용값  ti = ti_override ?? profile.ti,  ka = ka_override ?? profile.ka
    cv 규칙 (보내기로 결정한 순간에만 적용):
        cv_server == 0            → 1        (0 은 "한 번도 받은 적 없음"이라 보내지 않는다)
        cv_device > cv_server     → cv_device + 1   (PC 도구로 시험값을 넣었거나 DB 복구)
        그 외                     → 그대로
    보낼 조건: state == ACTIVE 이고 cv_device != cv_server (cv_device 가 없으면 보내지 않는다 —
              1차 펌웨어는 cv 를 안 싣고, 그 값 없이 보내면 뭘 맞추는지 알 수 없다)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.constants import CV_MAX, DeviceState


@dataclass(frozen=True)
class EffectiveConfig:
    ti: int
    ka: int


def effective_config(
    *, ti_override: int | None, ka_override: int | None, profile_ti: int, profile_ka: int
) -> EffectiveConfig:
    return EffectiveConfig(
        ti=profile_ti if ti_override is None else ti_override,
        ka=profile_ka if ka_override is None else ka_override,
    )


def next_cv_server(cv_server: int, cv_device: int | None) -> int:
    """보내기 직전의 cv_server. 규칙 2·3 (S-13). 65535 를 넘으면 1 로 감는다(0 은 건너뛴다)."""
    cv = cv_server
    if cv <= 0:
        cv = 1
    if cv_device is not None and cv_device > cv:
        cv = cv_device + 1
    if cv > CV_MAX:
        cv = 1
    return cv


def bump_cv_server(cv_server: int, cv_device: int | None) -> int:
    """관리자가 적용값을 바꿨을 때: `max(cv_server, cv_device or 0) + 1` (docs/05 PATCH config)."""
    cv = max(cv_server, cv_device or 0) + 1
    return 1 if cv > CV_MAX else cv


def should_send_config(state: str, cv_server: int, cv_device: int | None) -> bool:
    """단말 송신 직후 CONFIG_SET 을 보낼지. ACTIVE 가 아니면 절대 보내지 않는다(단말이 STATE
    로 거부하고, 사양서 S-10 이 금지한다)."""
    if state != DeviceState.ACTIVE.value:
        return False
    if cv_device is None:
        return False
    return cv_device != cv_server
