"""OTA 원격 펌웨어 갱신 (문제점 56번, 사양서 §24, 2026-10-18)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-18

ota_package(펌웨어 pkg 본문 포함) · ota_job · ota_item. ota_item.id 가 OTA_START 의 job_id 다.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_STATUSES = ("waiting", "sending", "running", "DONE", "ROLLBACK", "BAD", "STATE", "BATT", "BUSY",
             "HTTP", "SIZE", "HASH", "FLASH", "NO_RESPONSE", "CANCELLED", "SKIPPED")


def _ts(name: str, nullable: bool = True, default: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=sa.func.now() if default else None)


def upgrade() -> None:
    op.create_table(
        "ota_package",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("version", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column("fw_size", sa.Integer(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", postgresql.CHAR(64), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text()),
        _ts("created_at", nullable=False, default=True),
    )
    op.create_table(
        "ota_job",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("package_id", sa.Integer(), sa.ForeignKey("ota_package.id", ondelete="SET NULL")),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("version", sa.Text()),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", postgresql.CHAR(64), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text()),
        sa.Column("scope_label", sa.Text()),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Text()),
        _ts("created_at", nullable=False, default=True),
        _ts("finished_at"),
        _ts("cancelled_at"),
        sa.Column("cancelled_by", sa.Text()),
    )
    op.create_index("ix_ota_job_package_id", "ota_job", ["package_id"])
    op.create_table(
        "ota_item",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("ota_job.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("uuid", postgresql.CHAR(24), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="waiting"),
        sa.Column("token", sa.Text()),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        _ts("first_sent_at"),
        _ts("sent_at"),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("downloads", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fw_before", sa.Text()),
        sa.Column("fw_after", sa.Text()),
        sa.Column("detail", sa.Text()),
        _ts("updated_at", nullable=False, default=True),
        sa.CheckConstraint("status IN (" + ",".join(f"'{s}'" for s in _STATUSES) + ")",
                           name="ck_ota_item_status"),
        sa.UniqueConstraint("job_id", "uuid", name="uq_ota_item_job_uuid"),
    )
    op.create_index("ix_ota_item_open", "ota_item", ["status"],
                    postgresql_where=sa.text("status IN ('waiting','sending','running')"))
    op.create_index("ix_ota_item_uuid", "ota_item", ["uuid"])


def downgrade() -> None:
    op.drop_table("ota_item")
    op.drop_table("ota_job")
    op.drop_table("ota_package")
