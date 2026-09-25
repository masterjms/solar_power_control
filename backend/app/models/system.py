"""시스템 테이블 — mqtt_account_export · admin_user (docs/03)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, DateTime, Integer, Text, func
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
    """관리자 계정(3차). 2차는 테이블만 만들어 두고 쓰지 않는다."""

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: bcrypt
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="admin")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
