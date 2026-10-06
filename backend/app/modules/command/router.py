"""/api/commands — docs/05 "5차 API — 명령"."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, current_user
from app.db import get_db
from app.modules.command import service
from app.modules.command.schemas import (
    CommandPage,
    CommandDetail,
    CommandIn,
    CommandItem,
    CommandOut,
    PreviewOut,
    RetryIn,
    RetryOut,
)
from app.modules.deps import get_publisher, get_retrier
from app.mqtt.command_retry import CommandRetrier
from app.mqtt.publisher import MqttPublisher

router = APIRouter(prefix="/api/commands", tags=["commands"])


@router.post("/preview", response_model=PreviewOut)
async def preview(
    body: CommandIn,
    db: AsyncSession = Depends(get_db),
    me: Principal = Depends(current_user),
) -> PreviewOut:
    """보내기 전 확인 — 대상 수·온라인·저전압·topic·확정 유지시간. 아무것도 쓰지 않는다."""
    return await service.preview(db, body, me)


@router.post("", response_model=CommandOut, status_code=status.HTTP_201_CREATED)
async def create(
    body: CommandIn,
    db: AsyncSession = Depends(get_db),
    me: Principal = Depends(current_user),
    publisher: MqttPublisher = Depends(get_publisher),
    retrier: CommandRetrier = Depends(get_retrier),
) -> CommandOut:
    return await service.create(db, body, me, publisher, retrier)


@router.get("", response_model=list[CommandItem] | CommandPage)
async def list_commands(
    limit: int = Query(default=50, ge=1, le=500),
    uuid: str | None = Query(default=None, max_length=24),
    node_id: int | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[CommandItem] | CommandPage:
    """page 를 주면 {items,total,page,size}(문제점 39번 쪽 넘기기), 없으면 예전처럼 목록만(limit)."""
    if page is None:
        items, _ = await service.list_commands(db, limit=limit, uuid=uuid, node_id=node_id)
        return items
    items, total = await service.list_commands(db, limit=size, uuid=uuid, node_id=node_id,
                                               offset=(page - 1) * size)
    return CommandPage(items=items, total=total, page=page, size=size)


@router.get("/{seq}", response_model=CommandDetail)
async def get_command(seq: int, db: AsyncSession = Depends(get_db)) -> CommandDetail:
    return await service.get_command(db, seq)


@router.post("/{seq}/retry", response_model=RetryOut)
async def retry(
    seq: int,
    body: RetryIn | None = None,
    db: AsyncSession = Depends(get_db),
    publisher: MqttPublisher = Depends(get_publisher),
    retrier: CommandRetrier = Depends(get_retrier),
) -> RetryOut:
    return await service.retry(db, seq, body or RetryIn(), publisher, retrier)
