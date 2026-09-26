"""설정 프로필 API 스키마 (docs/05 프로필)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from app.constants import KA_MAX_SEC, KA_MIN_SEC, TI_MAX_SEC, TI_MIN_SEC


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    ti: int
    ka: int
    #: 이 프로필을 쓰는 단말 수.
    device_count: int = 0
    created_at: dt.datetime
    updated_at: dt.datetime


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    ti: int = Field(ge=TI_MIN_SEC, le=TI_MAX_SEC)
    ka: int = Field(ge=KA_MIN_SEC, le=KA_MAX_SEC)


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    ti: int | None = Field(default=None, ge=TI_MIN_SEC, le=TI_MAX_SEC)
    ka: int | None = Field(default=None, ge=KA_MIN_SEC, le=KA_MAX_SEC)


class ProfilePatchOut(ProfileOut):
    #: ti/ka 가 바뀌어 cv_server 를 올린 단말 수(cv_server 0 인 단말은 제외).
    bumped_devices: int = 0


class ProfileDeleteOut(BaseModel):
    id: int
    deleted: bool
