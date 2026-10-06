"""/api/schedule/* — 스케줄 프로필·배정·배포(S-25, ADR-010)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import schedule_rules as sr
from app.core.auth import Principal, current_user
from app.db import get_db
from app.modules.schedule import service
from app.modules.schedule.schemas import (
    AssignIn,
    AssignOut,
    DeployIn,
    DeployJobOut,
    DeployJobPage,
    DeviceScheduleOut,
    DevicesPage,
    ProfileIn,
    ProfileOut,
    ProfilePatch,
    RetryIn,
    RetryOut,
)
from app.mqtt.deploy_runner import DeployRunner

router = APIRouter(prefix="/api/schedule", tags=["schedule"])
device_router = APIRouter(tags=["schedule"])


def get_runner(request: Request) -> DeployRunner | None:
    return getattr(request.app.state, "deploy_runner", None)


@router.get("/profile-keys")
async def profile_keys() -> dict[str, Any]:
    """프로필에 들어가는 운전 15개 키와 기본값(나머지 10개는 단말별)."""
    return {"keys": list(sr.profile_keys()), "defaults": sr.profile_defaults()}


@router.get("/profiles", response_model=list[ProfileOut])
async def list_profiles(db: AsyncSession = Depends(get_db)) -> list[ProfileOut]:
    return await service.list_profiles(db)


@router.post("/profiles", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(body: ProfileIn, db: AsyncSession = Depends(get_db),
                         me: Principal = Depends(current_user)) -> ProfileOut:
    return await service.create_profile(db, body, me)


@router.patch("/profiles/{pid}", response_model=ProfileOut)
async def patch_profile(pid: int, body: ProfilePatch, db: AsyncSession = Depends(get_db),
                        me: Principal = Depends(current_user)) -> ProfileOut:
    """조건·15개가 바뀌면 version +1(이미 적용된 단말은 "적용됨" 이 풀린다 — 다시 배포)."""
    return await service.patch_profile(db, pid, body, me)


@router.delete("/profiles/{pid}")
async def delete_profile(pid: int, db: AsyncSession = Depends(get_db),
                         me: Principal = Depends(current_user)) -> dict[str, Any]:
    return await service.delete_profile(db, pid, me)


@router.get("/assign", response_model=list[AssignOut])
async def list_assign(db: AsyncSession = Depends(get_db)) -> list[AssignOut]:
    return await service.list_assign(db)


@router.put("/assign")
async def set_assign(body: AssignIn, db: AsyncSession = Depends(get_db),
                     me: Principal = Depends(current_user)) -> dict[str, Any]:
    """노드(하위가 물려받음) 또는 단말 하나(예외)에 프로필. profile_id null = 해제. 배포는 따로."""
    return await service.set_assign(db, body, me)


@router.get("/devices", response_model=DevicesPage)
async def devices(
    node_id: int | None = Query(default=None), profile_id: int | None = Query(default=None),
    q: str | None = Query(default=None, max_length=64), page: int = Query(default=1, ge=1),
    size: int = Query(default=100, ge=1, le=1000), db: AsyncSession = Depends(get_db),
) -> DevicesPage:
    """운영(ACTIVE) 단말별 배정·적용 상태."""
    return await service.devices(db, node_id=node_id, profile_id=profile_id, q=q, page=page,
                                 size=size)


@router.post("/deploy", response_model=DeployJobOut, status_code=status.HTTP_201_CREATED)
async def create_deploy(body: DeployIn, db: AsyncSession = Depends(get_db),
                        me: Principal = Depends(current_user),
                        runner: DeployRunner | None = Depends(get_runner)) -> DeployJobOut:
    return await service.create_deploy(db, body, me, runner)


@router.get("/deploy", response_model=list[DeployJobOut] | DeployJobPage)
async def list_jobs(limit: int = Query(default=30, ge=1, le=200),
                    profile_id: int | None = Query(default=None),
                    uuid: str | None = Query(default=None),
                    page: int | None = Query(default=None, ge=1),
                    size: int = Query(default=20, ge=1, le=100),
                    db: AsyncSession = Depends(get_db)) -> list[DeployJobOut] | DeployJobPage:
    """page 를 주면 {items,total,page,size}(문제점 39번), 없으면 예전처럼 목록만(limit)."""
    u = uuid.strip().upper() if uuid else None
    if page is None:
        items, _ = await service.list_jobs(db, limit=limit, profile_id=profile_id, uuid=u)
        return items
    items, total = await service.list_jobs(db, limit=size, profile_id=profile_id, uuid=u,
                                           offset=(page - 1) * size)
    return DeployJobPage(items=items, total=total, page=page, size=size)


@router.get("/deploy/{job_id}", response_model=DeployJobOut)
async def get_job(job_id: int, db: AsyncSession = Depends(get_db)) -> DeployJobOut:
    return await service.get_job(db, job_id)


@router.post("/deploy/{job_id}/retry", response_model=RetryOut)
async def retry_job(job_id: int, body: RetryIn, db: AsyncSession = Depends(get_db),
                    runner: DeployRunner | None = Depends(get_runner)) -> RetryOut:
    """응답 없는(또는 실패한) 단말만 다시 — 항목당 DEPLOY_MAX_ROUNDS 번까지."""
    return await service.retry_job(db, job_id, body.uuids, runner, settings.deploy_max_rounds)


@router.post("/deploy/{job_id}/cancel", response_model=DeployJobOut)
async def cancel_job(job_id: int, db: AsyncSession = Depends(get_db)) -> DeployJobOut:
    """대기·진행 항목을 취소. 이미 보낸 요청은 단말이 적용할 수 있다(결과는 무시)."""
    return await service.cancel_job(db, job_id)


@device_router.get("/api/devices/{uuid}/schedule", response_model=DeviceScheduleOut)
async def device_schedule(uuid: str, db: AsyncSession = Depends(get_db)) -> DeviceScheduleOut:
    return await service.device_schedule(db, uuid.strip().upper())
