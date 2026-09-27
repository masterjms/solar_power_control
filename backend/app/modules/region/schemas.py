"""법정동 트리·주소 검색 API 스키마 (docs/05 "5차 API — 법정동 트리")."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.constants import BJD_CODE_RE
from app.core.kakao_geo import GeoResult


class RegionOut(BaseModel):
    id: int
    parent_id: int | None
    #: sido / sigungu / dong
    level: str
    name: str
    bjd_code: str | None
    #: bjd_code + "00". 말단만.
    grp: str | None
    lat: float | None
    lon: float | None
    #: 그 노드 아래(자신 포함) 단말 수 / 그중 ACTIVE.
    device_count: int
    active_count: int


class ManualRegion(BaseModel):
    """개발 환경(APP_ENV=dev) 전용 직접 입력 — 카카오 키 없이 트리를 만들어 시험한다."""

    sido: str = Field(min_length=1, max_length=40)
    sigungu: str = Field(min_length=1, max_length=40)
    dong: str = Field(min_length=1, max_length=40)
    bjd_code: str
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("bjd_code")
    @classmethod
    def _code(cls, v: str) -> str:
        v = v.strip()
        if not BJD_CODE_RE.match(v):
            raise ValueError("bjd_code 는 숫자 10자리")
        return v


class FromAddress(BaseModel):
    """`{"query"}` · `{"pick":GeoResult}` · (dev 만) `{"sido","sigungu","dong","bjd_code",…}`."""

    query: str | None = Field(default=None, min_length=2, max_length=200)
    pick: GeoResult | None = None
    # dev 직접 입력 — 평평한 키로 받는다(docs/05).
    sido: str | None = None
    sigungu: str | None = None
    dong: str | None = None
    bjd_code: str | None = None
    lat: float | None = None
    lon: float | None = None

    def manual(self) -> ManualRegion | None:
        if self.bjd_code is None and self.dong is None:
            return None
        return ManualRegion(sido=self.sido or "", sigungu=self.sigungu or "",
                            dong=self.dong or "", bjd_code=self.bjd_code or "",
                            lat=self.lat, lon=self.lon)


class RegionPatch(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class RegionDeleteOut(BaseModel):
    id: int
    deleted: bool
