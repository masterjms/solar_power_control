"""단말 서비스 — 조회 · 승인(상태) · CONFIG 변경 · PING · 삭제 · 브로커 계정 내보내기 (docs/05).

서버 → 단말 발행 시점 원칙(사양서 §1.1.10): 관리자 조작은 **즉시 발행**하되 단말 도착은 다음
단말 송신 이후일 수 있다. 승인(REGISTER_ACK)은 retain 이라 재접속 때 반드시 받는다. CONFIG_SET
은 비retain 이라 즉시 1회 보내고, 못 받았으면 다음 TELEMETRY 의 cv 불일치가 다시 보낸다.
발행 실패(브로커 끊김)는 예외로 올리지 않고 `published=false` 로 알린다 — DB 는 커밋한다.

5차(ADR-005): 말단 법정동 배정(node_id) → grp·bjd_code 동기화 → REGISTER_ACK 재발행(grp 가
거기 실린다). 승인에는 말단이 필요하다(APPROVE_REQUIRES_NODE). DeviceOut 에 트리 경로와
원격 제어 표시(override·remote_active)가 붙는다.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import Select, case, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import settings
from app.constants import STATE_TRANSITIONS, DeviceState, EventKind, MsgType
from app.core import energy, ids, mqtt_accounts, presence
from app.core.command_rules import channel_remaining, remote_status
from app.core.config_rules import (
    bump_cv_server,
    effective_config,
    next_cv_server,
    should_send_config,
)
from app.core.region_tree import Tree, group_id
from app.errors import (
    DeviceNotFound,
    InvalidStateTransition,
    NodeNotLeaf,
    NodeRequired,
    ProfileNotFound,
    RegionNotFound,
)
from app.models.command import Command
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.profile import ConfigProfile
from app.models.region import Region
from app.models.settings import DeviceSettings
from app.models.system import MqttAccountExport
from app.models.telemetry import Telemetry
from app.modules.device.schemas import (
    EnergyToday,
    MapPoint,
    ConfigPatch,
    ConfigPatchOut,
    DeleteOut,
    DeviceOut,
    DevicePage,
    EventOut,
    PingOut,
    RegisterAckOut,
    StateOut,
    StatePatch,
    TelemetryOut,
)
from app.modules.region.service import load_tree
from app.mqtt.config_sync import ConfigSyncQueue, register_ack_job_for
from app.mqtt.publisher import MqttPublisher, config_set_payload, register_ack_payload

log = logging.getLogger(__name__)

#: 브로커 passwd/aclfile 내보내기를 직렬화하는 어드바이저리 락 키. 기동 시와 5분 재조정이
#: 겹쳐도 파일을 반쯤 쓴 상태로 두 번 설치하지 않게 한다.
_BROKER_EXPORT_LOCK_KEY = 0x696F746C_69676874  # "iotlight"

#: site 가 바뀌면 REGISTER_ACK 를 다시 retain 하는 상태(site 가 거기 실린다). RETIRED 는 retain
#: 이 비어 있어야 하므로 제외.
_SITE_REPUBLISH_STATES = frozenset({"ACTIVE", "PENDING", "SUSPENDED", "REJECTED"})


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ── 출력 변환 ────────────────────────────────────────────────────────────
def _to_out(
    device: Device, profile: ConfigProfile | None, now: dt.datetime, tree: Tree | None = None,
    settings_sync: str | None = None,
) -> DeviceOut:
    data: dict[str, Any] = {c.name: getattr(device, c.name) for c in Device.__table__.columns}
    # S-23 device_settings.sync — 행이 없으면(한 번도 안 읽음) unknown.
    data["settings_sync"] = settings_sync or "unknown"
    node = tree.get(device.node_id) if tree is not None else None
    data["node_name"] = node.name if node else None
    data["node_path"] = tree.path_name(device.node_id) if tree is not None else None
    data["remote_active"], data["remote_remaining_sec"] = remote_status(
        device.last_telemetry, device.override_until, now
    )
    data["override_ch"] = channel_remaining(device.override_ch, now)
    # FK 가 보장하지만 프로필을 못 찾으면(방금 지움) 화면이 죽지 않게 0 으로 표시한다.
    p_ti, p_ka = (profile.ti, profile.ka) if profile else (0, 0)
    eff = effective_config(
        ti_override=device.ti_override, ka_override=device.ka_override,
        profile_ti=p_ti, profile_ka=p_ka,
    )
    data["profile_name"] = profile.name if profile else None
    data["ti_effective"] = eff.ti
    data["ka_effective"] = eff.ka
    data["is_online"] = presence.is_online(device, now, eff.ti)
    data["config_pending"] = device.cv_server > 0 and device.cv_device != device.cv_server
    data["config_mismatch"] = (
        (device.ti_device is not None and device.ti_device != eff.ti)
        or (device.ka_device is not None and device.ka_device != eff.ka)
    )
    return DeviceOut(**data)


# ── 조회 ─────────────────────────────────────────────────────────────────
def _filtered(
    state: str | None, online: bool | None, q: str | None, now: dt.datetime, *,
    node_ids: list[int] | None = None, remote: bool | None = None,
) -> Select:
    stmt = select(Device, ConfigProfile, DeviceSettings.sync).join(
        ConfigProfile, ConfigProfile.id == Device.profile_id, isouter=True
    ).join(DeviceSettings, DeviceSettings.uuid == Device.uuid, isouter=True)
    if node_ids is not None:
        # 그 노드 아래 전체(자신 포함). 트리는 메모리에서 펼쳤다 — 말단 수천 개여도 IN 이면 된다.
        stmt = stmt.where(Device.node_id.in_(node_ids or [-1]))
    if remote is not None:
        # remote_active 의 SQL 판(core/command_rules.remote_status 와 같은 규칙).
        active = (Device.last_telemetry["md"].astext == "2") & (Device.override_until > now)
        stmt = stmt.where(active if remote else ~func.coalesce(active, False))
    if state:
        stmt = stmt.where(Device.state == state.upper())
    if online is True:
        stmt = stmt.where(presence.online_clause(now))
    elif online is False:
        stmt = stmt.where(~presence.online_clause(now))
    if q and q.strip():
        # 한 글자·숫자 몇 개로도 찾는다(부분 일치, 문제점 #8). 시설명·UUID·주소·지역(법정동 이름).
        # % _ \ 는 글자 그대로 찾도록 이스케이프한다.
        raw = q.strip()
        esc = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        needle = f"%{esc}%"
        # 지역은 조상 이름도 본다(시·군·구 이름으로 그 아래 말단 단말). 트리는 3단(시도>시군구>동).
        leaf, mid, top = aliased(Region), aliased(Region), aliased(Region)
        region_hit = (
            select(leaf.id)
            .join(mid, mid.id == leaf.parent_id, isouter=True)
            .join(top, top.id == mid.parent_id, isouter=True)
            .where(leaf.name.ilike(needle, escape="\\") | mid.name.ilike(needle, escape="\\")
                   | top.name.ilike(needle, escape="\\"))
        )
        stmt = stmt.where(
            Device.uuid.ilike(needle, escape="\\") | Device.site.ilike(needle, escape="\\")
            | Device.address.ilike(needle, escape="\\") | Device.node_id.in_(region_hit)
        )
    return stmt


async def _counts(db: AsyncSession, now: dt.datetime) -> dict[str, int]:
    """필터와 무관한 전체 집계 — 화면 상단 탭(PENDING n 건)용."""
    counts = {s.value: 0 for s in DeviceState}
    for state, n in await db.execute(select(Device.state, func.count()).group_by(Device.state)):
        counts[state] = int(n)
    counts["online"] = int(
        await db.scalar(select(func.count()).select_from(Device).where(presence.online_clause(now)))
        or 0
    )
    return counts


async def list_devices(
    db: AsyncSession, *, page: int, size: int, state: str | None, online: bool | None,
    q: str | None, node_id: int | None = None, remote: bool | None = None,
) -> DevicePage:
    now = _now()
    tree = await load_tree(db)
    node_ids = None
    if node_id is not None:
        if tree.get(node_id) is None:
            raise RegionNotFound(detail={"id": node_id})
        node_ids = tree.subtree_ids(node_id)
    base = _filtered(state, online, q, now, node_ids=node_ids, remote=remote)
    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    # PENDING 먼저(승인 대기가 화면 맨 위), 그다음 최근 수신 순, NULL(한 번도 안 옴) 마지막.
    pending_first = case((Device.state == DeviceState.PENDING.value, 0), else_=1)
    rows = (
        await db.execute(
            base.order_by(pending_first, Device.last_seen_at.desc().nulls_last(), Device.uuid)
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    return DevicePage(
        items=[_to_out(d, p, now, tree, sync) for d, p, sync in rows], total=int(total),
        page=page, size=size,
        counts=await _counts(db, now),
    )


async def map_points(db: AsyncSession, *, state: str | None) -> list[MapPoint]:
    """좌표가 있는 단말 전부(지도). 온라인 판정은 목록과 같은 SQL(presence.online_clause)."""
    now = _now()
    stmt = (
        select(Device.uuid, Device.site, Device.lat, Device.lon, Device.state,
               presence.online_clause(now).label("is_online"),
               Device.last_telemetry["on"].astext.label("on"), Region.name)
        .join(Region, Region.id == Device.node_id, isouter=True)
        .where(Device.lat.is_not(None), Device.lon.is_not(None))
    )
    if state:
        stmt = stmt.where(Device.state == state.upper())
    out: list[MapPoint] = []
    for uuid, site, lat, lon, st, online, on, node_name in await db.execute(stmt):
        try:
            on_v = int(on) if on is not None else None
        except ValueError:
            on_v = None
        out.append(MapPoint(uuid=uuid, site=site, lat=lat, lon=lon, state=st,
                            is_online=bool(online), on=on_v, node_name=node_name))
    return out


async def energy_today(db: AsyncSession, uuids: list[str]) -> dict[str, EnergyToday]:
    rows = await energy.today(db, uuids, _now())
    return {u: EnergyToday(**v) for u, v in rows.items()}


async def _get_with_profile(db: AsyncSession, uuid: str) -> tuple[Device, ConfigProfile | None]:
    row = (
        await db.execute(
            select(Device, ConfigProfile)
            .join(ConfigProfile, ConfigProfile.id == Device.profile_id, isouter=True)
            .where(Device.uuid == uuid)
        )
    ).first()
    if row is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    return row[0], row[1]


async def get_device(db: AsyncSession, uuid: str) -> DeviceOut:
    device, profile = await _get_with_profile(db, uuid)
    sync = await db.scalar(select(DeviceSettings.sync).where(DeviceSettings.uuid == uuid))
    return _to_out(device, profile, _now(), await load_tree(db), sync)


async def list_telemetry(
    db: AsyncSession, uuid: str, *, since: dt.datetime | None, until: dt.datetime | None,
    limit: int,
) -> list[TelemetryOut]:
    if await db.get(Device, uuid) is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    stmt = select(Telemetry).where(Telemetry.uuid == uuid)
    if since is not None:
        stmt = stmt.where(Telemetry.received_at >= since)
    if until is not None:
        stmt = stmt.where(Telemetry.received_at < until)
    rows = (
        await db.execute(stmt.order_by(Telemetry.received_at.desc()).limit(limit))
    ).scalars().all()
    return [
        TelemetryOut(
            received_at=r.received_at, ts_device=r.ts_device, sq=r.sq, fw=r.fw, ss=r.ss,
            cv=r.cv, er=r.er, on=r.on_, md=r.md, pw=[r.pw1, r.pw2, r.pw3], bv=r.bv, bi=r.bi,
            sc=r.sc, pp=r.pp, li=r.li, cs=r.cs,
        )
        for r in rows
    ]


async def list_events(
    db: AsyncSession, uuid: str, *, kind: str | None, limit: int
) -> list[EventOut]:
    stmt = select(DeviceEvent).where(DeviceEvent.uuid == uuid)
    if kind:
        stmt = stmt.where(DeviceEvent.kind == kind.upper())
    rows = (
        await db.execute(stmt.order_by(DeviceEvent.received_at.desc(), DeviceEvent.id.desc())
                         .limit(limit))
    ).scalars().all()
    return [EventOut.model_validate(r) for r in rows]


# ── REGISTER_ACK (관리자 경로 — 즉시 발행) ───────────────────────────────
async def _event(
    db: AsyncSession, uuid: str, kind: EventKind, payload: dict[str, Any], now: dt.datetime
) -> None:
    await db.execute(pg_insert(DeviceEvent).values(
        uuid=uuid, kind=kind.value, payload=payload, received_at=now
    ))


async def _publish_register_ack(
    db: AsyncSession, device: Device, publisher: MqttPublisher, now: dt.datetime
) -> tuple[bool, dict[str, Any]]:
    """DB 상태 그대로 REGISTER_ACK 를 retain 발행. RETIRED 는 발행 뒤 빈 retain 으로 지운다.

    반환 (발행 성공, payload). 실패는 로그만 — 다음 REGISTER 때 큐가 다시 답한다.
    """
    # 모양(reason 은 REJECTED 만, grp 는 배정돼 있으면 항상)은 수신 경로와 같은 함수로 정한다.
    job = register_ack_job_for(uuid=device.uuid, state=device.state, site=device.site,
                               reason=device.state_reason, grp=device.grp)
    retired = job.clear_after
    payload = register_ack_payload(
        uuid=job.uuid, state=job.state, site=job.site, reason=job.reason, grp=job.grp
    )
    try:
        await publisher.publish_register_ack(
            uuid=job.uuid, state=job.state, site=job.site, reason=job.reason, grp=job.grp
        )
        if retired:
            await publisher.clear_register_ack(uuid=device.uuid)
    except Exception:  # noqa: BLE001
        log.exception("REGISTER_ACK 발행 실패 %s (state=%s) — DB 는 커밋, 재발행 필요",
                      device.uuid, device.state)
        return False, payload
    device.register_ack_at = None if retired else now
    event = dict(payload)
    if retired:
        event["retain_cleared"] = True
    await _event(db, device.uuid, EventKind.REGISTER_ACK, event, now)
    return True, payload


async def _assign_node(db: AsyncSession, device: Device, node_id: int | None) -> bool:
    """말단 배정/해제 → grp·bjd_code 동기화(ADR-005). grp 가 바뀌었으면 True(REGISTER_ACK 재발행).

    말단(법정동)만 된다 — 상위 노드는 422 NODE_NOT_LEAF(단말은 그룹 하나만 구독, §3.10.4).
    """
    old_grp = device.grp
    if node_id is None:
        device.node_id = None
        device.grp = None
        device.bjd_code = None
    else:
        node = await db.get(Region, node_id)
        if node is None:
            raise RegionNotFound(detail={"id": node_id})
        if node.level != "dong" or not node.bjd_code:
            raise NodeNotLeaf(detail={"node_id": node_id, "level": node.level})
        device.node_id = node.id
        device.bjd_code = node.bjd_code
        device.grp = group_id(node.bjd_code)
    return (old_grp or None) != (device.grp or None)


async def set_state(
    db: AsyncSession, uuid: str, patch: StatePatch, publisher: MqttPublisher
) -> StateOut:
    """승인·거부·중지·해제·폐기 (사양서 §3.9.2, docs/05 PATCH /state).

    1. 전이 표 검사 → DB state/site/reason + device_event(STATE_CHANGE)
    2. REGISTER_ACK retain 발행(RETIRED 는 발행 후 빈 retain)
    3. ACTIVE 로 갈 때 cv_server 를 1 이상·단말보다 크게. CONFIG_SET 은 **여기서 안 보낸다** —
       단말이 ACTIVE 를 받고 보내는 첫 TELEMETRY 때 cv 비교로 나간다(§1.1.10, S-10).
    """
    device = await db.get(Device, uuid)
    if device is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    old = device.state
    if patch.state not in STATE_TRANSITIONS.get(old, frozenset()):
        raise InvalidStateTransition(
            detail={"from": old, "to": patch.state, "allowed": sorted(STATE_TRANSITIONS[old])}
        )
    if "node_id" in patch.model_fields_set:
        await _assign_node(db, device, patch.node_id)
    if (patch.state == DeviceState.ACTIVE.value and device.node_id is None
            and settings.approve_requires_node):
        # 주소 없는 단말은 두지 않는다(§3.10.4) — 그룹 명령을 영영 못 받는다.
        raise NodeRequired(detail={"uuid": uuid})
    now = _now()
    device.state = patch.state
    device.state_changed_at = now
    device.state_reason = patch.reason
    if patch.site is not None:
        device.site = patch.site or None
    if patch.state == DeviceState.ACTIVE.value:
        device.cv_server = next_cv_server(device.cv_server, device.cv_device)
    await _event(db, uuid, EventKind.STATE_CHANGE, {
        "from": old, "to": patch.state, "site": device.site, "reason": patch.reason, "by": "admin",
        "grp": device.grp,
    }, now)
    await db.flush()

    published, payload = await _publish_register_ack(db, device, publisher, now)
    return StateOut(uuid=uuid, state=device.state, site=device.site, node_id=device.node_id,
                    grp=device.grp, published=published, register_ack=payload)


async def republish_register_ack(
    db: AsyncSession, uuid: str, publisher: MqttPublisher
) -> RegisterAckOut:
    """DB 상태 그대로 재발행(재조정용). RETIRED 면 빈 retain 만."""
    device = await db.get(Device, uuid)
    if device is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    retired = device.state == DeviceState.RETIRED.value
    published, payload = await _publish_register_ack(db, device, publisher, _now())
    return RegisterAckOut(uuid=uuid, state=device.state, published=published, cleared=retired,
                          register_ack=None if retired else payload)


# ── CONFIG ───────────────────────────────────────────────────────────────
async def patch_config(
    db: AsyncSession, uuid: str, patch: ConfigPatch, publisher: MqttPublisher,
    config_sync: ConfigSyncQueue,
) -> ConfigPatchOut:
    """프로필/override/좌표/site/주소 변경 (docs/05 PATCH /config).

    적용값(ti/ka/lat/lon)이 바뀌면 `cv_server = max(cv_server, cv_device)+1`. site/address/bjd 만
    바꾸면 cv 그대로. ACTIVE 면 CONFIG_SET 즉시 1회(전체값), 아니면 승인 뒤 첫 TELEMETRY 때.
    site 가 바뀌면 REGISTER_ACK retain 을 다시 발행한다(site 가 거기 실린다).
    """
    device, profile = await _get_with_profile(db, uuid)
    sent = patch.model_fields_set
    now = _now()

    new_pid = patch.profile_id if "profile_id" in sent else None
    if new_pid is not None and new_pid != device.profile_id:
        new_profile = await db.get(ConfigProfile, new_pid)
        if new_profile is None:
            raise ProfileNotFound(detail={"profile_id": new_pid})
        device.profile_id = new_profile.id
        profile = new_profile
    if profile is None:  # FK 상 없을 수 없지만 방어
        raise ProfileNotFound(detail={"profile_id": device.profile_id})

    before = effective_config(
        ti_override=device.ti_override, ka_override=device.ka_override,
        profile_ti=profile.ti, profile_ka=profile.ka,
    )
    old_lat, old_lon, old_site = device.lat, device.lon, device.site
    old_profile_ti, old_profile_ka = before.ti, before.ka

    if "ti_override" in sent:
        device.ti_override = patch.ti_override
    if "ka_override" in sent:
        device.ka_override = patch.ka_override
    if "lat" in sent:
        device.lat = patch.lat
    if "lon" in sent:
        device.lon = patch.lon
    if "site" in sent:
        device.site = patch.site or None
    if "address" in sent:
        device.address = patch.address
    if "bjd_code" in sent:
        device.bjd_code = patch.bjd_code
    grp_changed = False
    if "node_id" in sent:
        # node_id 가 bjd_code 보다 우선 — 같이 오면 말단의 코드로 덮는다.
        grp_changed = await _assign_node(db, device, patch.node_id)

    after = effective_config(
        ti_override=device.ti_override, ka_override=device.ka_override,
        profile_ti=profile.ti, profile_ka=profile.ka,
    )
    changed = (
        (after.ti, after.ka) != (old_profile_ti, old_profile_ka)
        or device.lat != old_lat or device.lon != old_lon
    )
    if changed:
        device.cv_server = bump_cv_server(device.cv_server, device.cv_device)
    await db.flush()

    # site·grp 변경 → REGISTER_ACK 재발행 (RETIRED 제외). 둘 다 retain ACK 에 실린다.
    ack_republished = False
    if (device.site != old_site or grp_changed) and device.state in _SITE_REPUBLISH_STATES:
        ack_republished, _ = await _publish_register_ack(db, device, publisher, now)

    published = False
    reason: str | None = None
    payload: dict[str, Any] | None = None
    active = device.state == DeviceState.ACTIVE.value
    if not active:
        reason = "NOT_ACTIVE"
        if device.cv_server > 0:
            payload = config_set_payload(cv=device.cv_server, ti=after.ti, ka=after.ka,
                                         lat=device.lat, lon=device.lon)
    elif changed or should_send_config(device.state, device.cv_server, device.cv_device):
        device.cv_server = next_cv_server(device.cv_server, device.cv_device)
        payload = config_set_payload(cv=device.cv_server, ti=after.ti, ka=after.ka,
                                     lat=device.lat, lon=device.lon)
        try:
            await publisher.publish_config_set(
                uuid=uuid, cv=device.cv_server, ti=after.ti, ka=after.ka,
                lat=device.lat, lon=device.lon,
            )
            published = True
            device.config_sent_at = now
            config_sync.mark_sent(uuid)
            await _event(db, uuid, EventKind.CONFIG_SET, payload, now)
        except Exception:  # noqa: BLE001
            reason = "PUBLISH_FAILED"
            log.exception("CONFIG_SET 즉시 발행 실패 %s — 다음 TELEMETRY 때 재전송", uuid)
    else:
        reason = "NOT_NEEDED"
        if device.cv_server > 0:
            payload = config_set_payload(cv=device.cv_server, ti=after.ti, ka=after.ka,
                                         lat=device.lat, lon=device.lon)

    return ConfigPatchOut(
        uuid=uuid, state=device.state, cv_server=device.cv_server, profile_id=device.profile_id,
        ti_override=device.ti_override, ka_override=device.ka_override,
        ti_effective=after.ti, ka_effective=after.ka, lat=device.lat, lon=device.lon,
        site=device.site, address=device.address, bjd_code=device.bjd_code,
        node_id=device.node_id, grp=device.grp, cv_bumped=changed, published=published,
        reason=reason, payload=payload,
        register_ack_republished=ack_republished,
    )


# ── PING ─────────────────────────────────────────────────────────────────
async def ping(db: AsyncSession, uuid: str, publisher: MqttPublisher) -> PingOut:
    """seq 발번 → command 행 → 발행. 발행이 실패하면 예외가 올라가고 트랜잭션은 롤백된다
    (미들웨어). 시퀀스는 롤백돼도 되감기지 않으므로 seq 에 구멍이 생기는데, 그게 맞다 —
    단말이 봤을지도 모르는 번호를 다시 쓰지 않는다."""
    if await db.get(Device, uuid) is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    seq = await ids.next_cmd_seq(db)
    payload = {"type": MsgType.PING.value, "seq": seq}
    db.add(Command(seq=seq, target_kind="device", target_id=uuid, type=MsgType.PING.value,
                   payload=payload, expected_count=1, sent_at=_now()))
    # 발행 **전에** 커밋한다. 단말(시뮬레이터)의 PONG 은 수 ms 안에 오는데, 커밋이 응답 뒤라면
    # 수신 처리가 command 행을 못 보고 "모르는 seq" 로 버린다(S2-08 에서 실제 발생 — COMMAND 와
    # 같은 이유, docs/02 §16). 발행이 실패하면 행을 지우고 예외를 올린다.
    await db.commit()
    try:
        await publisher.publish_ping(uuid=uuid, seq=seq)
    except Exception:
        await db.execute(delete(Command).where(Command.seq == seq))
        await db.commit()
        raise
    return PingOut(uuid=uuid, seq=seq)


# ── 삭제 ─────────────────────────────────────────────────────────────────
async def delete_device(db: AsyncSession, uuid: str, publisher: MqttPublisher) -> DeleteOut:
    """행 삭제 + REGISTER_ACK retain 삭제. 이력(telemetry/device_event)은 남긴다.
    계정 파일은 손대지 않는다(단말 계정은 파일에 없다 — ADR-003)."""
    if await db.get(Device, uuid) is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    await db.execute(delete(Device).where(Device.uuid == uuid))
    await db.flush()
    cleared = True
    try:
        await publisher.clear_register_ack(uuid=uuid)
    except Exception:  # noqa: BLE001
        cleared = False
        log.exception("단말 삭제 %s: retain 삭제 실패 — 브로커에 옛 REGISTER_ACK 가 남는다", uuid)
    return DeleteOut(uuid=uuid, deleted=True, retain_cleared=cleared)


# ── 브로커 계정 파일 (server 계정만) ─────────────────────────────────────
async def export_broker_accounts(
    db: AsyncSession, *, wait_applied: bool = False
) -> tuple[mqtt_accounts.ExportResult, bool]:
    """server(+시험) 계정 passwd + aclfile 을 내보내고 mqtt_account_export 에 남긴다.

    기동 시 + 5분 재조정. 실패해도 예외를 던지지 않는다. 반환: (결과, 적용 확인 여부).
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BROKER_EXPORT_LOCK_KEY}
    )
    result = mqtt_accounts.export_all()
    if not result.enabled:
        return result, False

    now = _now()
    values: dict[str, Any] = {"exported_at": now}
    if result.passwd_ok:
        values["passwd_md5"] = result.passwd_md5
    if result.acl_ok:
        values["acl_md5"] = result.acl_md5
    await db.execute(update(MqttAccountExport).where(MqttAccountExport.id == 1).values(**values))

    applied = False
    if result.acl_ok and result.passwd_ok and wait_applied:
        applied = await mqtt_accounts.wait_applied(result.passwd_md5, result.acl_md5)
    applied_md5 = mqtt_accounts.read_applied_md5()
    if applied_md5:
        await db.execute(
            update(MqttAccountExport)
            .where(MqttAccountExport.id == 1)
            .values(acl_applied_md5=applied_md5, applied_at=now if applied else None)
        )
    return result, applied
