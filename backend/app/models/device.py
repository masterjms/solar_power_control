"""단말 — device (docs/03 §device).

최신값 캐시(last_telemetry 등)와 서버 의도값(cv_server, 프로필+override, lat, lon, site)을
한 행에 둔다. 이력은 telemetry / device_event 로 간다.

2026-09-26(ADR-003/004, 사양서 개정) 로 바뀐 것:
  · `mqtt_password_hash` 삭제 — 비밀번호는 HMAC 계산값이라 아무도 저장하지 않는다
  · `ti_server` → `profile_id` + `ti_override`/`ka_override` (적용값 = override ?? profile)
  · `grp0`/`grp1` → `grp` 하나
  · 새 단말 기본 state = PENDING (승인 게이트)
  · `online` 은 브로커 로그 tail 이 갱신한다. `online_changed_at` 으로 전이 시각을 남긴다
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import (
    KA_MAX_SEC,
    SITE_MAX_LEN,
    TI_MAX_SEC,
    UUID_LENGTH,
    DeviceState,
)
from app.models.base import Base
from app.models.profile import DEFAULT_PROFILE_ID


class Device(Base):
    __tablename__ = "device"
    __table_args__ = (
        CheckConstraint("uuid ~ '^[0-9A-F]{24}$'", name="ck_device_uuid_format"),
        CheckConstraint(
            f"ti_override IS NULL OR ti_override BETWEEN 1 AND {TI_MAX_SEC}",
            name="ck_device_ti_override_range",
        ),
        CheckConstraint(
            f"ka_override IS NULL OR ka_override BETWEEN 1 AND {KA_MAX_SEC}",
            name="ck_device_ka_override_range",
        ),
        CheckConstraint(
            "state IN ('PENDING','ACTIVE','SUSPENDED','REJECTED','RETIRED')",
            name="ck_device_state",
        ),
    )

    #: 24자리 대문자 16진수. topic 의 UUID 가 그대로 PK 다.
    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), primary_key=True)

    #: 새 UUID 는 PENDING 으로 생긴다(사양서 §2, §3.4). 관리자가 ACTIVE 로 올린다.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=DeviceState.PENDING.value, index=True
    )
    #: REJECTED 사유 등. REGISTER_ACK `reason` 으로 단말에 나간다.
    state_reason: Mapped[str | None] = mapped_column(Text)
    state_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 마지막 REGISTER_ACK retain 발행 시각. RETIRED 정리(빈 retain) 뒤에는 NULL.
    register_ack_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # ── REGISTER 보강 정보 ──────────────────────────────
    fw: Mapped[str | None] = mapped_column(Text)
    device_model: Mapped[str | None] = mapped_column(Text)
    modem_model: Mapped[str | None] = mapped_column(Text)
    imei: Mapped[str | None] = mapped_column(Text)
    iccid: Mapped[str | None] = mapped_column(Text)
    msisdn: Mapped[str | None] = mapped_column(Text)

    # ── 단말 보고값 (REGISTER / Telemetry) ──────────────
    cv_device: Mapped[int | None] = mapped_column(Integer)
    ss_device: Mapped[int | None] = mapped_column(Integer)
    ti_device: Mapped[int | None] = mapped_column(Integer)
    #: REGISTER 에만 실린다(사양서 §4.2). CONFIG_ACK OK 로도 서버 의도값을 적는다.
    ka_device: Mapped[int | None] = mapped_column(Integer)

    # ── 서버 의도값 ─────────────────────────────────────
    #: 0 = 아직 한 번도 보낸 적 없음. 보낼 때는 1 이상(사양서 §1.1.7 S-13).
    #: `cv_server != cv_device` 면 단말의 다음 송신 시점에 CONFIG_SET 재전송(ADR-002).
    cv_server: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("config_profile.id"), nullable=False,
        server_default=str(DEFAULT_PROFILE_ID), index=True,
    )
    #: 이 단말만 다르게. NULL 이면 프로필 값.
    ti_override: Mapped[int | None] = mapped_column(Integer)
    ka_override: Mapped[int | None] = mapped_column(Integer)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    #: 장소명. REGISTER_ACK 로 단말에 내려간다(OLED 표시). CONFIG 아님. 24자 이내.
    site: Mapped[str | None] = mapped_column(String(SITE_MAX_LEN))
    #: 설치 주소(화면용). 단말에 안 내려간다.
    address: Mapped[str | None] = mapped_column(Text)
    #: 법정동코드 10자리. 5차 group_id 재료.
    bjd_code: Mapped[str | None] = mapped_column(CHAR(10))
    #: 5차 그룹 12자리. 단말당 1개. REGISTER_ACK 로 보낸다. node_id 배정 시 같이 쓴다(비정규화 —
    #: REGISTER 마다 트리를 조인하지 않고 ACK 를 만들려고).
    grp: Mapped[str | None] = mapped_column(CHAR(12), index=True)
    #: 5차 말단 법정동(region.level='dong'). 승인 때 고른다(§3.9.3 #2).
    node_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("region.id"), index=True)

    # ── 원격 제어 표시 (5차, §3.10.8 S-19) ──────────────
    #: 마지막 OK 명령의 act(on/off/pwm). auto 로 해제되면 NULL.
    override_act: Mapped[str | None] = mapped_column(Text)
    #: device / group / all — 그 명령이 어느 계층 슬롯에 들어갔나.
    override_level: Mapped[str | None] = mapped_column(Text)
    override_seq: Mapped[int | None] = mapped_column(BigInteger)
    #: 보낸 시각(ts) + dur. 재부팅(sq 감소)이면 단말이 잃으므로 NULL 로 지운다.
    override_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # ── 최신값 캐시 ─────────────────────────────────────
    last_register_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_telemetry_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    #: 어떤 메시지든 마지막 수신. LWT 는 브로커가 대신 보내는 것이라 갱신하지 않는다.
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_sq: Mapped[int | None] = mapped_column(BigInteger)
    #: 마지막 TELEMETRY payload 원본.
    last_telemetry: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: 브로커 로그(ADR-004) 또는 LWT 로 갱신. 화면의 is_online 은 여기에 수신 시각 보조 규칙을
    #: AND 한 값이다(core/presence.py).
    online: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: 마지막 ONLINE/OFFLINE 전이 시각.
    online_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 마지막 OFFLINE 시각.
    offline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    lost_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reboot_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: 마지막 CONFIG_SET 발행 시각.
    config_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
