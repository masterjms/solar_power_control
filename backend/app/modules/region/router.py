"""/api/regions · /api/geo — docs/05 "5차 API — 법정동 트리"."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import kakao_geo
from app.core.auth import Principal, current_user, require_super
from app.core.kakao_geo import GeoResult
from app.db import get_db
from app.modules.region import service
from app.modules.region.schemas import FromAddress, RegionDeleteOut, RegionOut, RegionPatch

router = APIRouter(prefix="/api/regions", tags=["regions"])
geo_router = APIRouter(prefix="/api/geo", tags=["regions"])


@router.get("", response_model=list[RegionOut])
async def list_regions(db: AsyncSession = Depends(get_db)) -> list[RegionOut]:
    """평면 목록(화면이 트리로 조립). 관리자 누구나."""
    return await service.list_regions(db)


@router.post("/from-address", response_model=RegionOut)
async def from_address(
    body: FromAddress,
    response: Response,
    db: AsyncSession = Depends(get_db),
    me: Principal = Depends(current_user),
) -> RegionOut:
    """말단 find-or-create. 새로 만들었으면 201, 이미 있으면 200."""
    require_super(me, action="region.create")
    out, created = await service.from_address(db, body)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return out


@router.patch("/{region_id}", response_model=RegionOut)
async def rename_region(
    region_id: int,
    body: RegionPatch,
    db: AsyncSession = Depends(get_db),
    me: Principal = Depends(current_user),
) -> RegionOut:
    require_super(me, action="region.rename")
    return await service.rename(db, region_id, body)


@router.delete("/{region_id}", response_model=RegionDeleteOut)
async def delete_region(
    region_id: int,
    db: AsyncSession = Depends(get_db),
    me: Principal = Depends(current_user),
) -> RegionDeleteOut:
    require_super(me, action="region.delete")
    return await service.delete(db, region_id)


@geo_router.get("/search", response_model=list[GeoResult])
async def geo_search(
    query: str = Query(min_length=2, max_length=200),
) -> list[GeoResult]:
    """카카오 로컬 주소 검색 프록시. 키가 없으면 503 GEO_UNAVAILABLE."""
    return await kakao_geo.search_address(query)
