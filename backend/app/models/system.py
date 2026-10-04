"""시스템 테이블 — mqtt_account_export · admin_user (docs/03)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MqttAccountExport(Base):
    """계정 파일 내보내기 상태. 단일 행(id=1).

    passwd/aclfile 을 마지막으로 어떤 내용으로 내보냈고, 브로커 entrypoint 가 어떤
    내용을 적용했다고 보고했는지를 남긴다. `acl_md5 != acl_applied_md5` 면 브로커가
    아직 옛 ACL 로 돌고 있다는 뜻이다.
    """

    __tablename__ = "mqtt_account_export"
    __table_args__ = (CheckConstraint("id = 1", name="ck_mqtt_account_export_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    passwd_md5: Mapped[str | None] = mapped_column(Text)
    acl_md5: Mapped[str | None] = mapped_column(Text)
    acl_applied_md5: Mapped[str | None] = mapped_column(Text)
    exported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AdminUser(Base):
    """관리 화면 계정(문제점 21번, ADR-013). 규칙은 core/accounts.
    .env 의 ADMIN_USER·OPERATOR_USER 는 여기에 없다(화면에서 못 바꾼다)."""

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: pbkdf2_sha256$반복$salt$hash (core/accounts.hash_password)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: super_admin / region_admin / guest
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="guest")
    #: 맡은 시·도(트리 최상위 region.id). 최고관리자는 NULL(전 지역).
    region_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    #: NULL = 무기한
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 비밀번호를 바꾼 시각 — 이보다 먼저 발급된 로그인 쿠키는 끊는다.
    password_changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LoginLog(Base):
    """로그인 기록(성공·실패). 1년 보관(tasks/partitions)."""

    __tablename__ = "login_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    username: Mapped[str] = mapped_column(Text, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: ok / bad_password / expired / disabled
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    ip: Mapped[str | None] = mapped_column(Text)


class ServerSetting(Base):
    """운영 중 화면에서 바꾸는 서버 설정(문제점 14번, ADR-012). 항목 정의는 core/server_settings.ITEMS.

    행이 없는 항목은 기본값이다. 값은 정수(jsonb 가 아니라 integer — 지금 항목이 모두 정수)."""

    __tablename__ = "server_setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 바꾼 관리자(X-Remote-User)
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
