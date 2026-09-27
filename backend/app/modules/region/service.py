"""법정동 트리 서비스 — 목록(단말 수 집계) · 주소로 find-or-create · 이름 변경 · 삭제 (ADR-005).

트리 편집은 최고관리자만(라우터가 막는다). 말단(법정동)은 카카오 주소 검색 결과로만 만든다 —
운영자가 코드를 손으로 치지 않는다(§3.10.5). 개발 환경만 직접 입력을 허용한다.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import RegionLevel
from app.core import kakao_geo
from app.core.kakao_geo import GeoResult
from app.core.region_tree import Node, Tree, group_id
from app.errors import RegionInUse, RegionNotFound, ValidationFailed
from app.models.device import Device
from app.models.region import Region
from app.modules.region.schemas import FromAddress, RegionDeleteOut, RegionOut, RegionPatch

log = logging.getLogger(__name__)

#: 노드마다 "자신 + 하위 전부" 의 단말 수. 1만 대여도 device 는 node_id 로 한 번만 GROUP BY 하고,
#: 트리 폐포(closure)는 region 수천 행 × 깊이 3 이라 작다. 쿼리 하나.
_LIST_SQL = text("""
WITH RECURSIVE closure(ancestor, descendant) AS (
    SELECT id, id FROM region
    UNION ALL
    SELECT c.ancestor, r.id FROM closure c JOIN region r ON r.parent_id = c.descendant
),
dc AS (
    SELECT node_id, count(*) AS n, count(*) FILTER (WHERE state = 'ACTIVE') AS a
    FROM device WHERE node_id IS NOT NULL GROUP BY node_id
)
SELECT r.id, r.parent_id, r.level, r.name, r.bjd_code, r.lat, r.lon,
       coalesce(sum(dc.n), 0) AS device_count, coalesce(sum(dc.a), 0) AS active_count
FROM region r
JOIN closure c ON c.ancestor = r.id
LEFT JOIN dc ON dc.node_id = c.descendant
WHERE (CAST(:only_id AS integer) IS NULL OR r.id = CAST(:only_id AS integer))
GROUP BY r.id
ORDER BY r.parent_id NULLS FIRST, r.name
""")


def _out_row(row) -> RegionOut:
    bjd = row.bjd_code.strip() if row.bjd_code else None
    return RegionOut(
        id=row.id, parent_id=row.parent_id, level=row.level, name=row.name, bjd_code=bjd,
        grp=group_id(bjd) if bjd else None, lat=row.lat, lon=row.lon,
        device_count=int(row.device_count), active_count=int(row.active_count),
    )


async def list_regions(db: AsyncSession, *, only_id: int | None = None) -> list[RegionOut]:
    rows = (await db.execute(_LIST_SQL, {"only_id": only_id})).all()
    return [_out_row(r) for r in rows]


async def load_tree(db: AsyncSession) -> Tree:
    """트리 전체를 메모리로(수천 행). 경로 이름·하위 말단·topic 계산용."""
    rows = await db.execute(
        select(Region.id, Region.parent_id, Region.level, Region.name, Region.bjd_code,
               Region.lat, Region.lon)
    )
    return Tree(
        Node(id=r[0], parent_id=r[1], level=r[2], name=r[3],
             bjd_code=r[4].strip() if r[4] else None, lat=r[5], lon=r[6])
        for r in rows
    )


async def _region_out(db: AsyncSession, region_id: int) -> RegionOut:
    items = await list_regions(db, only_id=region_id)
    if not items:
        raise RegionNotFound(detail={"id": region_id})
    return items[0]


# ── find-or-create ───────────────────────────────────────────────────────
async def _find_child(db: AsyncSession, parent_id: int | None, name: str) -> Region | None:
    cond = Region.parent_id.is_(None) if parent_id is None else Region.parent_id == parent_id
    return (await db.execute(select(Region).where(cond, Region.name == name))).scalar_one_or_none()


async def _ensure(db: AsyncSession, parent_id: int | None, level: RegionLevel, name: str) -> Region:
    """상위 노드(시도·시군구) find-or-create. 이름이 같으면 같은 노드다."""
    found = await _find_child(db, parent_id, name)
    if found is not None:
        if found.level != level.value:
            raise ValidationFailed(
                f"'{name}' 이(가) 이미 다른 단계({found.level})로 있습니다.",
                detail={"id": found.id, "level": found.level},
            )
        return found
    node = Region(parent_id=parent_id, level=level.value, name=name)
    db.add(node)
    await db.flush()
    log.info("법정동 트리 노드 추가: %s %s (id=%d)", level.value, name, node.id)
    return node


async def find_or_create_leaf(db: AsyncSession, geo: GeoResult) -> tuple[Region, bool]:
    """시도 > 시군구 > 법정동 을 차례로 find-or-create. (말단, 새로 만들었나).

    말단은 **법정동코드로** 찾는다(이름은 바뀔 수 있어도 코드가 정체성). 같은 시군구 아래 같은
    이름의 다른 코드가 있으면(리 이름 겹침 등) 이름 뒤에 코드를 붙여 구분한다.
    """
    existing = (
        await db.execute(select(Region).where(Region.bjd_code == geo.bjd_code))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    sido = await _ensure(db, None, RegionLevel.SIDO, geo.sido.strip())
    sigungu = await _ensure(db, sido.id, RegionLevel.SIGUNGU, geo.sigungu.strip())
    name = geo.dong.strip()
    if await _find_child(db, sigungu.id, name) is not None:
        name = f"{name} ({geo.bjd_code})"
    leaf = Region(parent_id=sigungu.id, level=RegionLevel.DONG.value, name=name,
                  bjd_code=geo.bjd_code, lat=geo.lat, lon=geo.lon)
    db.add(leaf)
    await db.flush()
    log.info("법정동 말단 추가: %s > %s > %s (%s, id=%d)",
             sido.name, sigungu.name, name, geo.bjd_code, leaf.id)
    return leaf, True


async def from_address(db: AsyncSession, body: FromAddress) -> tuple[RegionOut, bool]:
    """docs/05 POST /api/regions/from-address. (말단, 새로 만들었나)."""
    manual = body.manual()
    if manual is not None:
        if settings.app_env != "dev":
            raise ValidationFailed(
                "법정동코드 직접 입력은 개발 환경(APP_ENV=dev)에서만 됩니다. 주소 검색을 쓰세요.",
                detail={"reason": "MANUAL_NOT_ALLOWED"},
            )
        geo = GeoResult(address_name=f"{manual.sido} {manual.sigungu} {manual.dong}",
                        bjd_code=manual.bjd_code, sido=kakao_geo.normalize_sido(manual.sido),
                        sigungu=manual.sigungu, dong=manual.dong,
                        lat=manual.lat if manual.lat is not None else 0.0,
                        lon=manual.lon if manual.lon is not None else 0.0)
        leaf, created = await find_or_create_leaf(db, geo)
        if created and (manual.lat is None or manual.lon is None):
            # 좌표를 안 줬으면 비워 둔다("오늘 밤" 은 기본 좌표로 계산). 0,0 을 남기지 않는다.
            leaf.lat, leaf.lon = manual.lat, manual.lon
            await db.flush()
    elif body.pick is not None:
        leaf, created = await find_or_create_leaf(db, body.pick)
    elif body.query:
        results = await kakao_geo.search_address(body.query)
        if not results:
            raise ValidationFailed("주소 검색 결과가 없습니다.",
                                   detail={"reason": "NO_RESULT", "query": body.query})
        leaf, created = await find_or_create_leaf(db, results[0])
    else:
        raise ValidationFailed("query 또는 pick 이 필요합니다.")
    return await _region_out(db, leaf.id), created


# ── 편집 ─────────────────────────────────────────────────────────────────
async def rename(db: AsyncSession, region_id: int, body: RegionPatch) -> RegionOut:
    node = await db.get(Region, region_id)
    if node is None:
        raise RegionNotFound(detail={"id": region_id})
    node.name = body.name.strip()
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as e:
        raise ValidationFailed("같은 상위 노드 아래에 같은 이름이 있습니다.",
                               detail={"name": body.name}) from e
    return await _region_out(db, region_id)


async def delete(db: AsyncSession, region_id: int) -> RegionDeleteOut:
    """자식이나 배정된 단말이 있으면 409 REGION_IN_USE — 그룹 topic 을 구독 중인 단말을 고아로
    만들지 않는다(단말 쪽 retain REGISTER_ACK 의 grp 가 남는다)."""
    node = await db.get(Region, region_id)
    if node is None:
        raise RegionNotFound(detail={"id": region_id})
    children = int(await db.scalar(
        select(func.count()).select_from(Region).where(Region.parent_id == region_id)
    ) or 0)
    devices = int(await db.scalar(
        select(func.count()).select_from(Device).where(Device.node_id == region_id)
    ) or 0)
    if children or devices:
        raise RegionInUse(detail={"id": region_id, "children": children, "devices": devices})
    await db.delete(node)
    await db.flush()
    return RegionDeleteOut(id=region_id, deleted=True)
