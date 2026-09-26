"""설정 프로필 API 스키마 (docs/05 프로필)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.constants import KA_MAX_SEC, TI_MAX_SEC


def _check_min(value: int | None, name: str, minimum: int) -> int | None:
    # 하한은 settings(개발 환경에서만 낮춤). pydantic Field(ge=) 는 import 시점 상수라 여기서 본다.
    if value is not None and value < minimum:
        raise ValueError(f"{name} 은 {minimum} 이상이어야 한다")
    return value


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


class _TiKaMin(BaseModel):
    @field_validator("ti", check_fields=False)
    @classmethod
    def _ti_min(cls, v: int | None) -> int | None:
        return _check_min(v, "ti", settings.config_ti_min_sec)

    @field_validator("ka", check_fields=False)
    @classmethod
    def _ka_min(cls, v: int | None) -> int | None:
        return _check_min(v, "ka", settings.config_ka_min_sec)


class ProfileCreate(_TiKaMin):
    name: str = Field(min_length=1, max_length=50)
    ti: int = Field(ge=1, le=TI_MAX_SEC)
    ka: int = Field(ge=1, le=KA_MAX_SEC)


class ProfilePatch(_TiKaMin):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    ti: int | None = Field(default=None, ge=1, le=TI_MAX_SEC)
    ka: int | None = Field(default=None, ge=1, le=KA_MAX_SEC)


class ProfilePatchOut(ProfileOut):
    #: ti/ka 가 바뀌어 cv_server 를 올린 단말 수(cv_server 0 인 단말은 제외).
    bumped_devices: int = 0


class ProfileDeleteOut(BaseModel):
    id: int
    deleted: bool
