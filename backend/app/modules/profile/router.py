"""/api/profiles — docs/05_API.md 프로필"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.modules.profile import service
from app.modules.profile.schemas import (
    ProfileCreate,
    ProfileDeleteOut,
    ProfileOut,
    ProfilePatch,
    ProfilePatchOut,
)

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileOut])
async def list_profiles(db: AsyncSession = Depends(get_db)) -> list[ProfileOut]:
    return await service.list_profiles(db)


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(body: ProfileCreate, db: AsyncSession = Depends(get_db)) -> ProfileOut:
    return await service.create_profile(db, body)


@router.patch("/{profile_id}", response_model=ProfilePatchOut)
async def patch_profile(
    profile_id: int, body: ProfilePatch, db: AsyncSession = Depends(get_db)
) -> ProfilePatchOut:
    return await service.patch_profile(db, profile_id, body)


@router.delete("/{profile_id}", response_model=ProfileDeleteOut)
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db)) -> ProfileDeleteOut:
    return await service.delete_profile(db, profile_id)
