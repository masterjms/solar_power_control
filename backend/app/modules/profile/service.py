"""설정 프로필 서비스 (docs/05 프로필, 사양서 §4.2).

프로필의 ti/ka 를 바꾸면 그 프로필을 쓰는 **모든 단말의 cv_server 를 +1** 한다. 단, 0 인
단말(아직 한 번도 보낸 적 없음 = 승인 전)은 0 그대로 — 승인 때 1 로 시작한다.
ACTIVE 단말에 즉시 CONFIG_SET 을 보내지 않는다 — 각 단말의 다음 송신 때 cv 불일치로 나간다
(사양서 §1.1.10). 1만 대가 한 프로필이면 즉시 발행은 폭주다.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import CV_MAX
from app.errors import ProfileInUse, ProfileNotFound, ValidationFailed
from app.models.device import Device
from app.models.profile import DEFAULT_PROFILE_ID, ConfigProfile
from app.modules.profile.schemas import (
    ProfileCreate,
    ProfileDeleteOut,
    ProfileOut,
    ProfilePatch,
    ProfilePatchOut,
)

log = logging.getLogger(__name__)


async def _device_count(db: AsyncSession, profile_id: int) -> int:
    stmt = select(func.count()).select_from(Device).where(Device.profile_id == profile_id)
    return int(await db.scalar(stmt) or 0)


def _out(profile: ConfigProfile, count: int) -> ProfileOut:
    return ProfileOut(id=profile.id, name=profile.name, ti=profile.ti, ka=profile.ka,
                      device_count=count, created_at=profile.created_at,
                      updated_at=profile.updated_at)


async def list_profiles(db: AsyncSession) -> list[ProfileOut]:
    count_stmt = select(Device.profile_id, func.count()).group_by(Device.profile_id)
    counts = dict((await db.execute(count_stmt)).all())
    rows = (await db.execute(select(ConfigProfile).order_by(ConfigProfile.id))).scalars().all()
    return [_out(p, int(counts.get(p.id, 0))) for p in rows]


async def create_profile(db: AsyncSession, body: ProfileCreate) -> ProfileOut:
    profile = ConfigProfile(name=body.name.strip(), ti=body.ti, ka=body.ka)
    db.add(profile)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ValidationFailed(
            "같은 이름의 프로필이 있습니다.", detail={"name": body.name}
        ) from exc
    await db.refresh(profile)
    return _out(profile, 0)


async def patch_profile(db: AsyncSession, profile_id: int, body: ProfilePatch) -> ProfilePatchOut:
    profile = await db.get(ConfigProfile, profile_id)
    if profile is None:
        raise ProfileNotFound(detail={"profile_id": profile_id})
    values_changed = False
    if body.name is not None and body.name.strip() != profile.name:
        profile.name = body.name.strip()
    if body.ti is not None and body.ti != profile.ti:
        profile.ti = body.ti
        values_changed = True
    if body.ka is not None and body.ka != profile.ka:
        profile.ka = body.ka
        values_changed = True
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ValidationFailed(
            "같은 이름의 프로필이 있습니다.", detail={"name": body.name}
        ) from exc

    bumped = 0
    if values_changed:
        # cv_server > 0 인 단말만. 65535 를 넘으면 1 로 감는다(0 은 건너뛴다).
        result = await db.execute(
            update(Device)
            .where(Device.profile_id == profile_id, Device.cv_server > 0)
            .values(cv_server=func.greatest(1, (Device.cv_server + 1) % (CV_MAX + 1)))
        )
        bumped = int(result.rowcount or 0)
        log.info("프로필 %d(%s) ti/ka 변경 → 단말 %d대 cv_server +1",
                 profile_id, profile.name, bumped)
    await db.refresh(profile)
    count = await _device_count(db, profile_id)
    return ProfilePatchOut(**_out(profile, count).model_dump(), bumped_devices=bumped)


async def delete_profile(db: AsyncSession, profile_id: int) -> ProfileDeleteOut:
    profile = await db.get(ConfigProfile, profile_id)
    if profile is None:
        raise ProfileNotFound(detail={"profile_id": profile_id})
    if profile_id == DEFAULT_PROFILE_ID:
        raise ProfileInUse("기본 프로필(id 1)은 지울 수 없습니다.", detail={"reason": "default"})
    count = await _device_count(db, profile_id)
    if count:
        raise ProfileInUse(detail={"profile_id": profile_id, "device_count": count})
    await db.delete(profile)
    await db.flush()
    return ProfileDeleteOut(id=profile_id, deleted=True)
