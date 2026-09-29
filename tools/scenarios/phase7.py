"""알람(S-24) · 스케줄 배포(S-25) 시나리오 — ADR-009, ADR-010, 사양서 §16.6·§13.1.

`--phase 7`(또는 `--phase-only 7`) 없이는 SKIP(`alarm_schedule` 플래그). REST 가 404/405 면 SKIP.
단말은 F/W 2026-09-29-1 모델: `set_er()` = er 가 바뀌면 2초 뒤 Telemetry(1분 모으기, 10분 5건),
SETTINGS_SET `tbl` 은 단말이 같은 식으로 계산해 crc 가 같을 때만 저장.

개발 스택(docker-compose.dev.yml)은 알람 재조정 3초, CONFIG 불일치 20초, 현장 조작 15초로 줄여 둔다
(ALARM_EVAL_SEC / ALARM_CONFIG_HOLD_SEC / ALARM_LOCAL_HOLD_SEC 환경 변수로 같은 값을 준다).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from tools.scenarios.common import approve, make_devices
from tools.scenarios.framework import Ctx, Fail, scenario
from tools.scenarios.phase5 import devices_in, make_tree
from tools.scenarios.services import error_code
from tools.sim import settings as st
from tools.sim.device import SimDevice

EVAL = float(os.environ.get("ALARM_EVAL_SEC") or 3)
CONFIG_HOLD = float(os.environ.get("ALARM_CONFIG_HOLD_SEC") or 20)
LOCAL_HOLD = float(os.environ.get("ALARM_LOCAL_HOLD_SEC") or 15)
#: 조건 → 알람 반영 상한(초): 단말 2초 + flush 1초 + 재조정 1~2판.
ALARM_WAIT = 2 + 1 + EVAL * 3 + 5
DEPLOY_WAIT = 25.0
REQ = "alarm_schedule"


def _describe(r) -> str:
    return f"{r.status_code} {error_code(r) or r.text[:200]}"


def _skip_if_missing(ctx: Ctx, r, what: str) -> None:
    if r.status_code in (404, 405) and not error_code(r):
        ctx.skip(f"REST 미구현/미배포 — {what}: {_describe(r)}")


async def alarms_of(ctx: Ctx, uuid: str) -> list[dict[str, Any]]:
    r = await ctx.s.rest.device_alarms(uuid, 100)
    _skip_if_missing(ctx, r, "GET /api/devices/{uuid}/alarms")
    if r.status_code != 200:
        raise Fail(f"device alarms → {_describe(r)}")
    return r.json()


def open_kinds(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {a["kind"]: a for a in rows if not a.get("closed_at")}


async def wait_open(ctx: Ctx, d: SimDevice, kind: str, *, present: bool = True,
                    timeout: float = ALARM_WAIT) -> dict[str, Any] | None:
    got: dict[str, Any] | None = None

    async def ok() -> bool:
        nonlocal got
        got = open_kinds(await alarms_of(ctx, d.uuid)).get(kind)
        return (got is not None) == present
    await ctx.wait_until(ok, timeout=timeout, interval=1.0,
                         what=f"{d.uuid[-4:]} 알람 {kind} {'열림' if present else '닫힘'}")
    return got


async def db_alarm_rows(ctx: Ctx, uuid: str, kind: str) -> list[dict[str, Any]]:
    rows = await ctx.s.db.fetch("SELECT * FROM alarm WHERE uuid = $1 AND kind = $2 ORDER BY id", uuid, kind)
    return [dict(r) for r in rows]


# ── S7-01 고장(er) ───────────────────────────────────────────────────────

@scenario("S7-01", "알람 고장 — er 변화 2초 Telemetry → 열림·값 갱신(1건 유지)·예약/LTE bit 무시·해제 → 이력·재발생 새 건",
          phase=7, requires=REQ, timeout=150)
async def s7_01(ctx: Ctx) -> None:
    """§16.6.1·16.6.2 · ADR-009. er 0x0004 → LED_FAULT(경고). 0x0005 → BATT_LOW 추가, LED_FAULT 는 같은 행 값만 갱신.
    0x0008(예약)을 더해도 새 알람 없음. 0x0040(LTE_OFFLINE)만 남기면 둘 다 해제(이력, 지속 시간). 다시 0x0004 → 새 행."""
    # ti 를 길게 — 주기 보고가 먼저 er 를 실어 가면 앞당김이 없는 것이 정상이라(§16.6.1) 앞당김 경로를 보려는 것.
    (d,) = await make_devices(ctx, 1, approve_now=True, ti=600)
    await ctx.wait_until(lambda: d.last_tm is not None, timeout=10, what="승인 뒤 첫 TM")
    d.er_merge_sec = 0.5
    d.set_er(0x0004)
    # 2초 앞당김이든, 그 전에 나간 다른 Telemetry 든 er 4 가 실려 가면 된다(먼저 나간 보고가 있으면 앞당김은
    # 취소하는 것이 펌웨어 규칙 — 앞당김 경로 자체는 tools/tests 단위 시험이 본다).
    await ctx.wait_until(lambda: (d.last_tm or {}).get("er") == 4, timeout=6, what="er 4 가 실린 Telemetry")
    a = await wait_open(ctx, d, "LED_FAULT")
    ctx.check(a["severity"] == "warn" and (a.get("value") or {}).get("er") == 4, f"LED_FAULT 경고·er 4: {a}")
    first_id = a["id"]

    d.set_er(0x0005)
    b = await wait_open(ctx, d, "BATT_LOW")
    ctx.check_eq(b["severity"], "warn", "BATT_LOW 등급")

    async def led_value(v: int) -> bool:
        x = open_kinds(await alarms_of(ctx, d.uuid)).get("LED_FAULT")
        return bool(x) and (x.get("value") or {}).get("er") == v
    await ctx.wait_until(lambda: led_value(5), timeout=ALARM_WAIT, what="LED_FAULT 값 er 5 로 갱신")
    rows = await db_alarm_rows(ctx, d.uuid, "LED_FAULT")
    ctx.check(len([r for r in rows if r["closed_at"] is None]) == 1 and rows[-1]["id"] == first_id,
              f"한 단말·한 항목 열린 알람 1건(같은 행): {[r['id'] for r in rows]}")

    d.set_er(0x0005 | 0x0008)
    await ctx.wait_until(lambda: (d.last_tm or {}).get("er") == 0x000D, timeout=10, what="er 0x000D Telemetry")
    await asyncio.sleep(EVAL * 2)
    ctx.check_eq(set(open_kinds(await alarms_of(ctx, d.uuid))), {"LED_FAULT", "BATT_LOW"}, "0x0008(예약)은 알람 아님")

    d.set_er(0x0040)
    await wait_open(ctx, d, "LED_FAULT", present=False)
    await wait_open(ctx, d, "BATT_LOW", present=False)
    ctx.check(not open_kinds(await alarms_of(ctx, d.uuid)), "LTE_OFFLINE(0x0040)만 남으면 열린 알람 없음")
    r = await ctx.s.rest.alarms(status="closed", q=d.uuid)
    hist = [x for x in r.json().get("items", []) if x["uuid"] == d.uuid]
    ctx.check({x["kind"] for x in hist} >= {"LED_FAULT", "BATT_LOW"} and all(x["closed_at"] for x in hist),
              f"해제 → 이력: {[(x['kind'], x['duration_sec']) for x in hist]}")

    d.set_er(0x0004)
    again = await wait_open(ctx, d, "LED_FAULT")
    ctx.check(again["id"] != first_id, f"다시 생기면 새 건: {first_id} → {again['id']}")


# ── S7-02 통신 두절 · 승인 대기 · SUSPENDED ─────────────────────────────

@scenario("S7-02", "알람 통신 두절·승인 대기 — PENDING 정보 알람 → 승인 해제, 끊김 → 두절(경고) → 재접속 해제, SUSPENDED 는 두절 없음",
          phase=7, requires=REQ, timeout=180)
async def s7_02(ctx: Ctx) -> None:
    (a,) = await make_devices(ctx, 1, approve_now=True)
    (b,) = await make_devices(ctx, 1, offset=1)
    p = await wait_open(ctx, b, "PENDING")
    ctx.check_eq(p["severity"], "info", "승인 대기 = 정보")
    await approve(ctx, [b])
    await wait_open(ctx, b, "PENDING", present=False)

    await a.disconnect(hard=False, reconnect=True, reconnect_after=25)

    async def offline() -> bool:
        r = await ctx.s.db.device(a.uuid)
        return bool(r) and r.get("online") is False
    await ctx.wait_until(offline, timeout=20, interval=1, what="끊은 뒤 online=false")
    o = await wait_open(ctx, a, "OFFLINE")
    ctx.check_eq(o["severity"], "warn", "통신 두절 = 경고")
    await ctx.wait_until(lambda: a.is_connected, timeout=40, what="재접속")
    await wait_open(ctx, a, "OFFLINE", present=False, timeout=ALARM_WAIT + 10)

    r = await ctx.s.rest.patch_state(b.uuid, "SUSPENDED")
    ctx.check(r.status_code < 300, f"b SUSPENDED: {_describe(r)}")
    await b.disconnect(hard=False, reconnect=False)
    await ctx.wait_until(lambda: _offline(ctx, b.uuid), timeout=20, interval=1, what="b online=false")
    await asyncio.sleep(EVAL * 3)
    ctx.check("OFFLINE" not in open_kinds(await alarms_of(ctx, b.uuid)), "SUSPENDED 는 통신 두절을 만들지 않는다")


async def _offline(ctx: Ctx, uuid: str) -> bool:
    r = await ctx.s.db.device(uuid)
    return bool(r) and r.get("online") is False


# ── S7-03 지속 조건(관찰 → 열림) ────────────────────────────────────────

@scenario("S7-03", "알람 지속 조건 — 현장 조작 md 1: 기준 전엔 관찰(안 보임)·끝나면 지움(이력 없음), 기준 넘으면 열림; CONFIG 불일치 기준 뒤 열림",
          phase=7, requires=REQ, timeout=240)
async def s7_03(ctx: Ctx) -> None:
    t0 = (await ctx.s.db.now()).timestamp()
    (d,) = await make_devices(ctx, 1, approve_now=True)
    d.start_local()
    await ctx.wait_until(lambda: (d.last_tm or {}).get("md") == 1, timeout=10, what="md 1 Telemetry")

    async def watching() -> bool:
        rows = await db_alarm_rows(ctx, d.uuid, "LOCAL_OPERATION")
        return any(r["opened_at"] is None and r["closed_at"] is None for r in rows)
    await ctx.wait_until(watching, timeout=ALARM_WAIT, interval=1, what="관찰 행(opened_at NULL)")
    ctx.check("LOCAL_OPERATION" not in open_kinds(await alarms_of(ctx, d.uuid)), "기준 전에는 화면에 안 보임")
    d.end_local()
    await d.send_tm_now()

    async def dropped() -> bool:   # 관찰 행이 지워졌고 새 이력(닫힌 행)도 안 생겼다
        rows = await db_alarm_rows(ctx, d.uuid, "LOCAL_OPERATION")
        return not [r for r in rows if r["closed_at"] is None or r["first_seen_at"].timestamp() >= t0 - 1]
    await ctx.wait_until(dropped, timeout=ALARM_WAIT, interval=1, what="기준 전에 끝남 → 관찰 행 지움(이력 없음)")

    d.start_local()
    a = await wait_open(ctx, d, "LOCAL_OPERATION", timeout=LOCAL_HOLD + ALARM_WAIT + 5)
    held = (_ts(a["opened_at"]) - _ts(a["first_seen_at"]))
    ctx.check(held >= LOCAL_HOLD - 1, f"기준({LOCAL_HOLD:.0f}s) 뒤 열림: {held:.1f}s")
    d.end_local()
    await d.send_tm_now()
    await wait_open(ctx, d, "LOCAL_OPERATION", present=False)

    # CONFIG 불일치 — 단말이 CONFIG_SET 을 못 받는다(cv 그대로) → 기준 뒤 열림
    d.ignore_config_set = 10**6
    r = await ctx.s.rest.patch_config(d.uuid, ti_override=7)
    ctx.check(r.status_code < 300, f"config 변경(cv_server +1): {_describe(r)}")
    await d.send_tm_now()
    c = await wait_open(ctx, d, "CONFIG_MISMATCH", timeout=CONFIG_HOLD + ALARM_WAIT + 10)
    ctx.check("cv_server" in (c.get("value") or {}), f"CONFIG 불일치 값: {c.get('value')}")


def _ts(v: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


# ── S7-04 재부팅 잦음 ────────────────────────────────────────────────────

@scenario("S7-04", "알람 재부팅 잦음 — 오늘 sq 감소(REBOOT) 3회 → 주의 알람(횟수)", phase=7, requires=REQ, timeout=180)
async def s7_04(ctx: Ctx) -> None:
    (d,) = await make_devices(ctx, 1, approve_now=True, ti=600)
    await ctx.wait_until(lambda: d.last_tm is not None, timeout=10, what="첫 TM")
    for i in range(3):
        before = len(await ctx.s.db.events(d.uuid, "REBOOT"))
        await d.send_tm_now()
        await asyncio.sleep(1.5)
        await d.reboot(reconnect_after=0.5)
        await ctx.wait_until(lambda: d.state == "ACTIVE" and d.is_connected, timeout=20, what=f"재부팅 {i + 1} 뒤 ACTIVE")
        await ctx.wait_until(lambda b=before: _events_more(ctx, d.uuid, b), timeout=15, interval=1,
                             what=f"REBOOT 이벤트 {i + 1}")
    a = await wait_open(ctx, d, "REBOOT_FREQUENT")
    ctx.check(a["severity"] == "caution" and (a.get("value") or {}).get("count_today", 0) >= 3,
              f"재부팅 잦음 주의·횟수: {a.get('value')}")


async def _events_more(ctx: Ctx, uuid: str, n: int) -> bool:
    return len(await ctx.s.db.events(uuid, "REBOOT")) > n


# ── 스케줄 배포 도우미 ───────────────────────────────────────────────────

async def profile(ctx: Ctx, name: str, *, on: int = 0, off: int = 0, **values: int) -> dict[str, Any]:
    r = await ctx.s.rest.schedule_keys()
    _skip_if_missing(ctx, r, "GET /api/schedule/profile-keys")
    defaults = r.json()["defaults"]
    body = {"name": f"{ctx.scenario.id} {name}", "region": " 시험 서울 ", "lat": 37.5665, "lon": 126.978,
            "on": on, "off": off, "values": {**defaults, **values}}
    r = await ctx.s.rest.create_schedule_profile(body, ctx.s.env.admin_user)
    if r.status_code != 201:
        raise Fail(f"프로필 만들기 → {_describe(r)}")
    p = r.json()

    async def cleanup() -> None:
        try:
            await ctx.s.db.execute("DELETE FROM schedule_assign WHERE profile_id = $1", p["id"])
            await ctx.s.db.execute("DELETE FROM device_schedule WHERE profile_id = $1", p["id"])
            await ctx.s.rest.delete_schedule_profile(p["id"])
        except Exception:  # noqa: BLE001
            pass
    ctx.defer(cleanup)
    return p


async def deploy(ctx: Ctx, profile_id: int, scope: str, scope_id: str | None = None, *, expect: int = 201) -> dict:
    r = await ctx.s.rest.create_deploy({"profile_id": profile_id, "scope": scope, "scope_id": scope_id},
                                       ctx.s.env.admin_user)
    if r.status_code != expect:
        raise Fail(f"배포 {scope} {scope_id} → {_describe(r)} (기대 {expect})")
    return r.json()


async def job_items(ctx: Ctx, job_id: int) -> dict[str, dict[str, Any]]:
    r = await ctx.s.rest.deploy_job(job_id)
    return {i["uuid"]: i for i in r.json().get("items") or []}


async def wait_item(ctx: Ctx, job_id: int, d: SimDevice, status: str, *, timeout: float = DEPLOY_WAIT) -> dict:
    last: dict[str, Any] = {}

    async def ok() -> bool:
        nonlocal last
        last = (await job_items(ctx, job_id)).get(d.uuid, {})
        return last.get("status") == status
    try:
        await ctx.wait_until(ok, timeout=timeout, interval=0.5, what=f"배포 #{job_id} {d.uuid[-4:]} → {status}")
    except Fail:
        raise Fail(f"배포 #{job_id} {d.uuid[-4:]}: 마지막 {last}") from None
    return last


def last_set(d: SimDevice) -> dict[str, Any] | None:
    sets = d.settings_requests("SETTINGS_SET")
    return sets[-1] if sets else None


# ── S7-05 배포 정상 흐름 ─────────────────────────────────────────────────

@scenario("S7-05", "스케줄 배포 — 노드 배정 → 배포: 안 읽은 단말은 읽기 먼저, 단말별 10개 보존·프로필 15개 덮기·tbl crc, 오프라인은 재접속 때 자동 → 전부 적용됨",
          phase=7, requires=REQ, timeout=240)
async def s7_05(ctx: Ctx) -> None:
    tree = await make_tree(ctx, [1])
    leaf = tree.leaves[0]
    a, b, c = await devices_in(ctx, 3, leaf, offset=0)
    # b: 현장 기준 밝기·배터리 값이 기본과 다르고, 서버가 이미 읽어 안다(synced)
    b.settings.update({"manual_40w": 55, "fade": 3, "cut24": 2400, "rtn24": 2700})
    r = await ctx.s.rest.read_settings(b.uuid, ctx.s.env.admin_user)
    _skip_if_missing(ctx, r, "POST settings/read")

    async def synced(u: str) -> bool:
        s = (await ctx.s.rest.get_settings(u, ctx.s.env.admin_user)).json()
        return s.get("sync") == "synced" and not s.get("pending")
    await ctx.wait_until(lambda: synced(b.uuid), timeout=10, what="b 읽기 → synced")
    await c.disconnect(hard=False, reconnect=True, reconnect_after=20)
    await ctx.wait_until(lambda: _offline(ctx, c.uuid), timeout=20, interval=1, what="c online=false")

    p = await profile(ctx, "기본", on=5, off=-5, stage1_pwm=55, start_pwm=90)
    ctx.check(p["region"] == "시험 서울" and p["crc"] == st.table_crc(37566500, 126978000, 5, -5),
              f"지역 공백 제거·crc = suntable: {p['region']!r} {p['crc']}")
    r = await ctx.s.rest.set_schedule_assign({"node_id": leaf.id, "profile_id": p["id"]}, ctx.s.env.admin_user)
    ctx.check(r.status_code < 300, f"노드 배정: {_describe(r)}")

    gets_a = len(a.settings_requests("SETTINGS_GET"))
    gets_b = len(b.settings_requests("SETTINGS_GET"))
    job = await deploy(ctx, p["id"], "node", str(leaf.id))
    ctx.check_eq(job["total"], 3, "대상 3대(노드 상속)")
    jid = job["id"]

    await wait_item(ctx, jid, b, "OK")
    ctx.check_eq(len(b.settings_requests("SETTINGS_GET")), gets_b, "이미 읽은 단말은 다시 읽지 않고 바로 쓰기")
    sb = last_set(b) or {}
    v = sb.get("v") or {}
    ctx.check(v.get("manual_40w") == 55 and v.get("fade") == 3 and v.get("cut24") == 2400 and v.get("rtn24") == 2700,
              f"단말별 10개 보존: {[v.get(k) for k in ('manual_40w', 'fade', 'cut24', 'rtn24')]}")
    ctx.check(v.get("stage1_pwm") == 55 and v.get("start_pwm") == 90, "프로필 15개 덮기")
    t = sb.get("tbl") or {}
    ctx.check(t.get("crc") == p["crc"] and t.get("region") == "시험 서울" and t.get("on") == 5 and t.get("off") == -5,
              f"tbl 조건·crc: {t}")

    await wait_item(ctx, jid, a, "OK")
    ctx.check(len(a.settings_requests("SETTINGS_GET")) > gets_a, "안 읽은 단말은 SETTINGS_GET 먼저(§13.1 ③)")
    ctx.check(a.tbl.get("crc") == p["crc"] and a.tbl.get("src") == st.SRC_SERVER, f"단말 표 저장(src 2): {a.tbl}")

    items = await job_items(ctx, jid)
    ctx.check_eq(items.get(c.uuid, {}).get("status"), "waiting", "오프라인 단말은 대기")
    await ctx.wait_until(lambda: c.is_connected, timeout=40, what="c 재접속")
    await wait_item(ctx, jid, c, "OK", timeout=DEPLOY_WAIT + 10)

    r = await ctx.s.rest.deploy_job(jid)
    ctx.check(bool(r.json().get("finished_at")), "열린 항목 없음 → 작업 끝")
    r = await ctx.s.rest.schedule_devices(profile_id=p["id"])
    rows = r.json()["items"]
    ctx.check(len(rows) == 3 and all(x["applied_ok"] for x in rows), f"적용됨 3대: {[(x['uuid'][-4:], x['applied_ok']) for x in rows]}")
    profs = {x["id"]: x for x in (await ctx.s.rest.schedule_profiles()).json()}
    ctx.check((profs[p["id"]]["targets"], profs[p["id"]]["applied"]) == (3, 3), "프로필 대상 3 · 적용 3")
    await asyncio.sleep(EVAL * 2)
    for d in (a, b, c):
        ctx.check("SCHEDULE_MISMATCH" not in open_kinds(await alarms_of(ctx, d.uuid)), f"{d.uuid[-4:]} 스케줄 불일치 없음")


# ── S7-06 실패·재시도·예외·불일치 알람 ─────────────────────────────────────

@scenario("S7-06", "스케줄 배포 예외 — 단말 예외 배정 우선, 무응답 3회 → 응답 없음 → 다시 → 적용, 현장 표 변경 → 스케줄 불일치 알람 → 재배포 해제, 대상 없음 409, 취소",
          phase=7, requires=REQ, timeout=300)
async def s7_06(ctx: Ctx) -> None:
    tree = await make_tree(ctx, [1])
    leaf = tree.leaves[0]
    x, y = await devices_in(ctx, 2, leaf, offset=0)
    p1 = await profile(ctx, "노드")
    p2 = await profile(ctx, "예외", stage2_pwm=20)
    p3 = await profile(ctx, "안 씀")
    user = ctx.s.env.admin_user
    await ctx.s.rest.set_schedule_assign({"node_id": leaf.id, "profile_id": p1["id"]}, user)
    await ctx.s.rest.set_schedule_assign({"uuid": y.uuid, "profile_id": p2["id"]}, user)

    r = await ctx.s.rest.create_deploy({"profile_id": p3["id"], "scope": "profile"}, user)
    ctx.check(r.status_code == 409 and error_code(r) == "DEPLOY_NO_TARGETS", f"배정 없는 프로필 → 409: {_describe(r)}")

    x.settings_silent_next = 10**6
    job = await deploy(ctx, p1["id"], "profile")
    ctx.check_eq(job["total"], 1, "노드 프로필 배포 대상 = x 만(y 는 단말 예외)")

    async def kick() -> None:   # 단말이 보낸 직후 재발송(5초 이상) — 30초를 기다리지 않게
        while True:
            await asyncio.sleep(3)
            await x.send_tm_now()
    task = asyncio.create_task(kick())
    try:
        await wait_item(ctx, job["id"], x, "NO_RESPONSE", timeout=90)
    finally:
        task.cancel()
    ctx.check(x.stats.settings_silenced >= 3, f"새 seq 로 3회 보냄: silenced {x.stats.settings_silenced}")
    x.settings_silent_next = 0
    r = await ctx.s.rest.retry_deploy(job["id"])
    ctx.check(r.status_code == 200 and r.json().get("retried") == 1, f"응답 없는 단말만 다시: {r.text[:120]}")
    await wait_item(ctx, job["id"], x, "OK")

    j2 = await deploy(ctx, p2["id"], "device", y.uuid)
    await wait_item(ctx, j2["id"], y, "OK")
    ctx.check(y.tbl.get("crc") == p2["crc"] and (last_set(y) or {}).get("v", {}).get("stage2_pwm") == 20, "예외 프로필 적용")

    # 현장에서 표 조건을 바꿔 저장 → 다시 읽으면 적용 crc 와 다름 → 스케줄 불일치
    y.local_save({"tbl": {"on": 10}})
    await y.send_tm_now()
    await wait_open(ctx, y, "LOCAL_SAVED")
    r = await ctx.s.rest.read_settings(y.uuid, user)
    ctx.check(r.status_code == 202, f"다시 읽기: {_describe(r)}")
    m = await wait_open(ctx, y, "SCHEDULE_MISMATCH")
    ctx.check((m.get("value") or {}).get("applied_crc") == p2["crc"], f"불일치 값: {m.get('value')}")
    await wait_open(ctx, y, "LOCAL_SAVED", present=False)

    j3 = await deploy(ctx, p2["id"], "device", y.uuid)
    await wait_item(ctx, j3["id"], y, "OK")
    await wait_open(ctx, y, "SCHEDULE_MISMATCH", present=False)

    # 취소 — 응답을 막아 둔 채 배포 → 진행 중 취소
    x.settings_silent_next = 10**6
    j4 = await deploy(ctx, p1["id"], "device", x.uuid)
    await ctx.wait_until(lambda: _item_in(ctx, j4["id"], x.uuid, ("reading", "sent")), timeout=10, interval=0.5,
                         what="진행 중")
    r = await ctx.s.rest.cancel_deploy(j4["id"])
    ctx.check(r.status_code == 200 and r.json().get("cancelled_at"), f"취소: {_describe(r)}")
    ctx.check_eq((await job_items(ctx, j4["id"]))[x.uuid]["status"], "CANCELLED", "항목 CANCELLED")
    x.settings_silent_next = 0


async def _item_in(ctx: Ctx, job_id: int, uuid: str, statuses: tuple[str, ...]) -> bool:
    return (await job_items(ctx, job_id)).get(uuid, {}).get("status") in statuses
