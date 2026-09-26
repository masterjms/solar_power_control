"""2차 시나리오 — 수집 서비스 + DB + HMAC 계정/ACL + CONFIG 전체값 + 브로커 로그 presence.

사양서 2026-09-26 개정판 기준(단말 1.4.0): 승인 게이트가 켜져 있어 Telemetry 가 필요한 시나리오는
setup 에서 `PATCH /api/devices/{uuid}/state ACTIVE` 로 승인한다(`make_devices(approve_now=True)`).
단언은 DB(정본)를 우선하고 REST 로 교차 확인한다. 고정 대기는 2초 이하, 나머지는 wait_until.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import timedelta

from tools.scenarios.common import (FLUSH_WAIT, approve, approve_uuids, check_config_set_shape, last_tm_of,
                                    make_devices, metric_keys, metric_sum, namespace_of, payload_of,
                                    send_tm_and_wait, sim_mode, tm_in_db, uuid_prefix, wait_config_set)
from tools.scenarios.framework import Ctx, Fail, scenario
from tools.scenarios.services import error_code
from tools.sim.device import FORGED_SQ, SQ_MOD, device_password, hmac_key_from_hex, uuid_from_index

_send_tm_and_wait = send_tm_and_wait
_tm_in_db = tm_in_db
_payload = payload_of

#: 거부 계열 metrics 키 패턴(이름이 확정되지 않아 패턴으로 찾는다, docs/06 §가정).
REJECT_RX = r"(invalid|garbage|malformed|mismatch|reject|bad|oversize|too_large|parse|error)"


# ── S2-01 ────────────────────────────────────────────────────────────────

@scenario("S2-01", "신규 단말 REGISTER → 승인 → TELEMETRY → DB 적재", phase=2, timeout=240)
async def s2_01(ctx: Ctx) -> None:
    """50대가 접속해 REGISTER(ka 포함) → PENDING → REST 승인 → TELEMETRY 1건 즉시 + ti=5s 주기.
    합격: device 행이 REGISTER 필드(fw/model/imei/iccid/msisdn/cv/ti/ka_device)로 채워지고 state=ACTIVE,
    telemetry 건수가 기대치 ±2, last_telemetry_at 단조 증가, 적재율 ≥95%,
    REST 상세·**목록** 응답의 last_telemetry 가 시뮬레이터가 마지막에 보낸 것과 같다."""
    since = await ctx.s.db.now()
    count, ti = 50, 5
    devices = await make_devices(ctx, count, ti=ti, mode="2cha")
    uuids = [d.uuid for d in devices]
    d0 = devices[0]

    # 승인 전: PENDING 행, TM 없음.
    async def all_pending() -> bool:
        n = await ctx.s.db.fetchval(
            "SELECT count(*) FROM device WHERE uuid = ANY($1::text[]) AND state = 'PENDING' AND last_register_at >= $2",
            uuids, since)
        return n == count
    await ctx.wait_until(all_pending, timeout=60, what=f"device {count}행 PENDING (REGISTER 반영)")
    sample = await ctx.s.db.device(d0.uuid)
    for col, want in (("fw", d0.fw), ("device_model", d0.device_model), ("modem_model", d0.modem_model),
                      ("imei", d0.imei), ("iccid", d0.iccid), ("msisdn", d0.msisdn),
                      ("cv_device", d0.cv), ("ti_device", d0.ti), ("ka_device", d0.ka)):
        ctx.check_eq(sample.get(col), want, f"device.{col}")
    ctx.check_eq(sample.get("cv_server"), 0, "승인 전 cv_server=0 (한 번도 안 보냄)")
    ctx.check_eq(await ctx.s.db.telemetry_count(d0.uuid, since), 0, "승인 전 telemetry 없음")

    await approve(ctx, devices, site="S2-01")
    t_active = await ctx.s.db.now()
    row = await ctx.s.db.device(d0.uuid)
    ctx.check_eq(row.get("state"), "ACTIVE", "승인 뒤 state=ACTIVE")
    ctx.check_eq(row.get("site"), "S2-01", "site 저장")

    # 주기 3번 이상 돌 때까지 첫 단말의 last_telemetry_at 이 단조 증가하는지 본다.
    seen: list = []
    async def advanced() -> bool:
        r = await ctx.s.db.device(d0.uuid)
        t = r.get("last_telemetry_at") if r else None
        if t is not None and (not seen or t > seen[-1]):
            seen.append(t)
        return len(seen) >= 3
    await ctx.wait_until(advanced, timeout=ti * 5, interval=1.0, what="last_telemetry_at 3회 단조 증가")
    ctx.check(all(a < b for a, b in zip(seen, seen[1:])), f"last_telemetry_at 단조 증가 {seen}")

    # 건수: 승인 직후 1건 + 경과/ti. 승인이 50대에 걸쳐 몇 초 퍼지므로 ±2.
    await ctx.hold(1.5, "마지막 배치 flush")
    elapsed = (await ctx.s.db.now() - t_active).total_seconds()
    expected = 1 + int(elapsed // ti)
    rows = await ctx.s.db.fetch(
        "SELECT uuid, count(*) AS n FROM telemetry WHERE uuid = ANY($1::text[]) AND received_at >= $2 GROUP BY uuid",
        uuids, since)
    counts = {r["uuid"]: r["n"] for r in rows}
    bad = {u: counts.get(u, 0) for u in uuids if not (expected - 2 <= counts.get(u, 0) <= expected + 2)}
    ctx.check(len(counts) == count, f"telemetry 가 있는 단말 {len(counts)}/{count}")
    ctx.check(not bad, f"telemetry 건수 기대 {expected}±2 — 벗어난 단말 {len(bad)}: {list(bad.items())[:5]}")
    total_sent = sum(d.stats.tm_sent for d in devices)
    total_db = sum(counts.values())
    ctx.check(total_db >= total_sent * 0.95, f"전체 적재율 {total_db}/{total_sent} ≥ 95%")
    r = await ctx.s.db.device(d0.uuid)
    ctx.check_eq(r.get("cv_device"), r.get("cv_server"), "승인 초기 동기화 뒤 cv_device == cv_server")
    ctx.check(r.get("cv_server") >= 1, "cv_server ≥ 1")

    # REST 교차 확인(상세 + 목록): 마지막 TM 이 시뮬레이터의 마지막 발행과 같은가(sq 로 비교).
    async def rest_matches() -> bool:
        body = await ctx.s.rest.device(d0.uuid)
        tm = last_tm_of(body)
        return tm is not None and tm.get("sq") == d0.last_tm.get("sq")
    await ctx.wait_until(rest_matches, timeout=FLUSH_WAIT, what="REST 상세 last_telemetry.sq == 시뮬레이터 마지막 sq")
    body = await ctx.s.rest.device(d0.uuid)
    tm = last_tm_of(body)
    ctx.check(tm.get("bv") == d0.last_tm["bv"] and tm.get("ts") == d0.last_tm["ts"], "REST last_telemetry bv/ts 일치")
    ctx.check(body.get("last_sq") in (None, d0.last_tm["sq"]), f"REST last_sq={body.get('last_sq')}")
    for key in ("ti_effective", "ka_effective", "config_pending", "is_online", "profile_name"):
        ctx.check(key in body, f"DeviceOut 계산 필드 {key}")
    ctx.check_eq(body.get("ka_device"), d0.ka, "DeviceOut ka_device")
    listing = await ctx.s.rest.devices(q=d0.uuid)
    items = listing.get("items") or []
    mine = [it for it in items if it.get("uuid") == d0.uuid]
    ctx.check(len(mine) == 1, f"목록 q=uuid 로 1건 ({len(mine)})")
    ltm = last_tm_of(mine[0])
    ctx.check(ltm is not None and ltm.get("sq") == tm.get("sq"), "목록 응답에도 last_telemetry (bv/sc 열용)")
    ctx.check(isinstance(listing.get("counts"), dict) and "ACTIVE" in listing["counts"], f"목록 counts: {listing.get('counts')}")


# ── S2-02 ────────────────────────────────────────────────────────────────

@scenario("S2-02", "REGISTER 유실 — TELEMETRY 만으로 레코드 생성, 늦은 REGISTER 가 보강", phase=2, timeout=150)
async def s2_02(ctx: Ctx) -> None:
    """1.4.0 단말은 ACTIVE 를 받아야 TM 을 보내므로 먼저 정상 REGISTER → REST 승인(retain ACTIVE)을 해 두고,
    device 행만 DB 에서 지운 뒤 REGISTER 를 건너뛰는 재부팅을 시킨다. 단말은 retain ACTIVE 로 곧장 TM 을 보내고
    서버는 topic UUID 만으로 행을 만들어야 한다(§4.1). 이후 REGISTER 가 오면 device_model/imei/ka_device 가 채워진다."""
    (dev,) = await make_devices(ctx, 1, ti=600, mode="2cha", approve_now=True)
    ctx.check((await ctx.s.broker.retained(dev.topic("config"))) is not None, "승인 retain 존재")
    # 서버에는 이 단말이 없었던 것처럼: 행만 지운다(REST DELETE 는 retain 까지 지우므로 쓰지 않는다).
    await ctx.s.db.delete_device_rows(dev.uuid)
    ctx.check(await ctx.s.db.device(dev.uuid) is None, "device 행 삭제(REGISTER 유실 상황 준비)")
    since = await ctx.s.db.now()

    dev.register_on_connect = False
    await dev.reboot(reconnect_after=0.5)
    await ctx.wait_until(lambda: dev.is_connected and dev.state == "ACTIVE", timeout=30,
                         what="REGISTER 없이 재접속 → retain ACTIVE 수신")
    ctx.check_eq(dev.stats.register_sent, 1, "재접속 뒤 REGISTER 를 보내지 않음(유실)")
    await _send_tm_and_wait(ctx, dev, since)
    row = await ctx.s.db.device(dev.uuid)
    ctx.check(row is not None, "TM 만으로 device 행 생성")
    ctx.check(row.get("last_register_at") is None, "REGISTER 전이라 last_register_at NULL")
    ctx.check(row.get("device_model") is None, "REGISTER 전이라 device_model NULL")
    ctx.check(row.get("ka_device") is None, "REGISTER 전이라 ka_device NULL")
    ctx.check(row.get("last_telemetry_at") is not None, "last_telemetry_at 설정")
    ctx.check(row.get("fw") in (None, dev.fw), "fw 는 TM 에서 채워도 되고 비워도 된다")
    ctx.check_eq(row.get("cv_device"), dev.cv, "cv_device 는 TM 에서")

    await dev.send_register()
    async def filled() -> bool:
        r = await ctx.s.db.device(dev.uuid)
        return bool(r and r.get("last_register_at") and r.get("device_model") == dev.device_model)
    await ctx.wait_until(filled, timeout=FLUSH_WAIT, what="늦은 REGISTER 로 보강")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row.get("imei"), dev.imei, "imei")
    ctx.check_eq(row.get("ti_device"), dev.ti, "ti_device")
    ctx.check_eq(row.get("ka_device"), dev.ka, "ka_device")
    ctx.check(row.get("last_telemetry_at") is not None, "last_telemetry_at 유지")
    ctx.check(row.get("register_ack_at") is not None, "REGISTER 에 REGISTER_ACK 응답(register_ack_at)")


# ── S2-03 ────────────────────────────────────────────────────────────────

@scenario("S2-03", "sq 판정 — 유실·재부팅·uint32 wrap", phase=2, timeout=180)
async def s2_03(ctx: Ctx) -> None:
    """sq 건너뜀 3 → lost_count +3. 재부팅(sq 0) → reboot_count +1, REBOOT 이벤트(재부팅 뒤 retain ACTIVE 로 TM 재개).
    wrap 은 별도 단말로 본다: 첫 TM 이 2^32-2 인 단말이 2^32-1 → 0 으로 넘어가면 (backend/app/mqtt/sq.py 규칙대로)
    재부팅이 아니고 유실도 0 이어야 하며, 그 뒤 진짜 재부팅(1 → 0)은 여전히 재부팅으로 잡혀야 한다.
    마지막으로 작은 sq 에서 2^32-2 로 크게 점프하는 경우 lost_count 가 int 범위를 넘거나 flush 가 깨지면 안 된다."""
    since = await ctx.s.db.now()
    dev, wrapper = await make_devices(ctx, 2, ti=600, mode="2cha", approve_now=True)
    await _send_tm_and_wait(ctx, dev, since)
    base = await ctx.s.db.device(dev.uuid)
    lost0, reboot0 = base["lost_count"], base["reboot_count"]

    dev.drop_next_tm(3)
    await _send_tm_and_wait(ctx, dev, since)
    async def lost3() -> bool:
        r = await ctx.s.db.device(dev.uuid)
        return r["lost_count"] == lost0 + 3
    await ctx.wait_until(lost3, timeout=FLUSH_WAIT, what="lost_count +3")
    r = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(r["reboot_count"], reboot0, "유실만으로 reboot_count 불변")
    # 같은 sq 를 두 번 보내면(duplicate) 유실·재부팅 어느 쪽도 아니다.
    dev.sq -= 1
    await dev.send_tm_now()
    await ctx.hold(1.5, "중복 sq 처리")
    r = await ctx.s.db.device(dev.uuid)
    ctx.check(r["lost_count"] == lost0 + 3 and r["reboot_count"] == reboot0, "같은 sq 중복은 카운트 없음")

    # 재부팅: sq 0 부터, REGISTER 다시, retain ACTIVE 로 TM 재개(관리자 조작 없음).
    reg_before = dev.stats.register_sent
    await dev.reboot(reconnect_after=0.5)
    await ctx.wait_until(lambda: dev.is_connected and dev.stats.register_sent > reg_before, timeout=30,
                         what="재부팅 후 재접속 + REGISTER")
    await ctx.wait_until(lambda: dev.state == "ACTIVE", timeout=15, what="retain ACTIVE 복귀")
    async def rebooted() -> bool:
        r = await ctx.s.db.device(dev.uuid)
        return r["reboot_count"] == reboot0 + 1
    await ctx.wait_until(rebooted, timeout=FLUSH_WAIT, what="reboot_count +1")
    events = await ctx.s.db.events(dev.uuid, "REBOOT", since)
    ctx.check(len(events) == 1, f"REBOOT 이벤트 1건 (실제 {len(events)})")
    r = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(r["lost_count"], lost0 + 3, "재부팅은 lost_count 에 영향 없음")
    ctx.check_eq(r["last_sq"], 0, "재부팅 후 last_sq=0")

    # wrap: 처음 보는 단말의 첫 TM 이 2^32-2 → 2^32-1 → 0 (uint32 넘김) → 1 → 진짜 재부팅 0.
    # (승인 초기 동기화 TM 은 이미 나갔으므로 wrapper 행에는 sq 기준이 있다 — 그 뒤 큰 점프는 상한까지 유실.)
    wrapper.sq_wrap()
    await _send_tm_and_wait(ctx, wrapper, since)          # 2^32-2
    await _send_tm_and_wait(ctx, wrapper, since)          # 2^32-1
    ctx.check_eq(wrapper.sq, 0, "시뮬레이터 sq 가 0 으로 wrap")
    await _send_tm_and_wait(ctx, wrapper, since)          # 0
    async def wrapped() -> bool:
        w = await ctx.s.db.device(wrapper.uuid)
        return w["last_sq"] == 0
    await ctx.wait_until(wrapped, timeout=FLUSH_WAIT, what="wrap 뒤 last_sq=0")
    w = await ctx.s.db.device(wrapper.uuid)
    ctx.check_eq(w["reboot_count"], 0, "uint32 wrap 은 재부팅이 아님")
    ctx.check(w["lost_count"] in (0, 100_000),
              f"uint32 wrap 은 유실도 아님(연속 번호): lost_count={w['lost_count']} ∈ {{0, 100000(점프 상한)}}")
    await _send_tm_and_wait(ctx, wrapper, since)          # 1
    tm_before = wrapper.stats.tm_sent
    await wrapper.reboot(reconnect_after=0.5)
    await ctx.wait_until(lambda: wrapper.is_connected and wrapper.stats.tm_sent > tm_before, timeout=30,
                         what="wrap 뒤 진짜 재부팅(retain ACTIVE → sq 0 TM)")
    async def wrapper_rebooted() -> bool:
        w2 = await ctx.s.db.device(wrapper.uuid)
        return w2["reboot_count"] == 1
    await ctx.wait_until(wrapper_rebooted, timeout=FLUSH_WAIT, what="wrap 뒤에도 재부팅 판정 정상")

    # 큰 점프: dev 의 sq(1) → 2^32-2. gap ≈ 4.29e9 — device.lost_count 는 int 라 넘칠 수 있다.
    lost1 = r["lost_count"]
    prev_sq = dev.sq
    dev.sq_wrap()
    gap = (SQ_MOD - 2) - prev_sq - 1
    await _send_tm_and_wait(ctx, dev, since)
    r = await ctx.s.db.device(dev.uuid)
    ctx.check(lost1 <= r["lost_count"] <= lost1 + gap, f"큰 점프 후 lost_count 증가 {r['lost_count'] - lost1} ≤ gap {gap}")
    ctx.check(r["lost_count"] < 2**31, "lost_count 가 int32 안(오버플로 없음)")
    ctx.check_eq(r["reboot_count"], reboot0 + 1, "큰 점프(증가)는 재부팅 아님")


# ── S2-04 ────────────────────────────────────────────────────────────────

@scenario("S2-04", "CONFIG 왕복 — 전체값 CONFIG_SET, 422, 단말 RANGE, cv_device > cv_server", phase=2, timeout=180)
async def s2_04(ctx: Ctx) -> None:
    """사양서 2차 합격 기준 4·5 + S-13. PATCH {ti_override:300} → 즉시 CONFIG_SET **전체값**(cv·ti·ka, 좌표 없으면 키 없음,
    cv ≥ 1) → CONFIG_ACK OK → 다음 TM cv == cv_server → DB 수렴. PATCH ti_override=0/3601, ka_override=1801 은 422
    VALIDATION_FAILED 이고 cv_server 불변·CONFIG_SET 미발행(하한은 백엔드 CONFIG_*_MIN_SEC 라 0 으로 본다).
    서버를 우회해 CONFIG_SET ti=0 을 쏘면 단말이 RANGE. cv=7 로 미리 설정된 단말은 승인 시 cv_server 가 8 이 되어
    첫 TM 뒤 CONFIG_SET cv=8 이 온다."""
    since = await ctx.s.db.now()
    dev, seeded = await make_devices(ctx, 2, ti=600, mode="2cha", start=False)
    seeded.cv = 7
    await asyncio.gather(dev.start(), seeded.start())
    ctx.check(await dev.wait_connected(30) and await seeded.wait_connected(30), "2대 접속")
    await approve(ctx, [dev, seeded])

    # 승인 초기 동기화: cv_server 0 → 1, 첫 TM 뒤 CONFIG_SET cv=1.
    first = await wait_config_set(ctx, dev, count=1, what="승인 뒤 첫 TM 직후 CONFIG_SET")
    check_config_set_shape(ctx, first, cv=1, lat_lon=False)
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=10, what="CONFIG_ACK OK")
    profile_ka = first["ka"]
    rx0, ok0 = dev.stats.config_set_rx, dev.stats.config_ack_ok
    before = await ctx.s.db.device(dev.uuid)

    r = await ctx.s.rest.patch_config(dev.uuid, ti_override=300)
    ctx.check(r.status_code < 300, f"PATCH ti_override=300 → {r.status_code} {r.text[:120]}")
    body = r.json()
    ctx.check(body.get("published") is True, f"ACTIVE 단말이라 즉시 발행 published=true: {body}")
    ctx.check_eq(body.get("ti_effective"), 300, "응답 ti_effective")
    cfg = await wait_config_set(ctx, dev, count=rx0 + 1, what="단말이 CONFIG_SET 수신")
    check_config_set_shape(ctx, cfg, ti=300, ka=profile_ka, lat_lon=False)
    ctx.check(cfg["cv"] > first["cv"], f"cv 증가 {first['cv']} → {cfg['cv']}")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= ok0 + 1, timeout=10, what="CONFIG_ACK OK")
    ctx.check_eq(dev.ti, 300, "단말 ti=300 적용(즉시)")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check(row["cv_server"] > before["cv_server"], f"cv_server 증가 ({before['cv_server']} → {row['cv_server']})")
    ctx.check_eq(dev.cv, row["cv_server"], "단말 cv == cv_server")
    ctx.check_eq(cfg["cv"], row["cv_server"], "CONFIG_SET cv == cv_server")
    ctx.check(row.get("config_sent_at") is not None, "config_sent_at 기록")

    payload = await _send_tm_and_wait(ctx, dev, since)
    ctx.check_eq(payload["cv"], row["cv_server"], "다음 TM 의 cv == cv_server")
    async def converged() -> bool:
        r2 = await ctx.s.db.device(dev.uuid)
        return r2["cv_device"] == r2["cv_server"] and r2["ti_device"] == 300
    await ctx.wait_until(converged, timeout=FLUSH_WAIT, what="DB cv_device == cv_server, ti_device=300")
    acks = [e for e in await ctx.s.db.events(dev.uuid, "CONFIG_ACK", since) if _payload(e).get("cv") == cfg["cv"]]
    ctx.check(len(acks) == 1, f"이번 cv 의 CONFIG_ACK 이벤트 1건 ({len(acks)})")
    ctx.check_eq(_payload(acks[0]).get("result"), "OK", "CONFIG_ACK 이벤트 payload.result")
    rest = await ctx.s.rest.device(dev.uuid)
    ctx.check(rest.get("config_pending") is False, "DeviceOut config_pending=false(수렴)")
    ctx.check(rest.get("config_mismatch") is False, "DeviceOut config_mismatch=false(ti_device == ti_effective)")

    for bad in (0, 3601):
        r = await ctx.s.rest.patch_config(dev.uuid, ti_override=bad)
        ctx.check(r.status_code == 422, f"PATCH ti_override={bad} → 422 (실제 {r.status_code})")
        ctx.check(error_code(r) == "VALIDATION_FAILED", f"오류 코드 VALIDATION_FAILED ({error_code(r) or r.text[:80]})")
    r = await ctx.s.rest.patch_config(dev.uuid, ka_override=1801)
    ctx.check(r.status_code == 422, f"PATCH ka_override=1801 → 422 (실제 {r.status_code})")
    row2 = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row2["cv_server"], row["cv_server"], "거부된 PATCH 는 cv_server 를 올리지 않음")
    ctx.check_eq(dev.stats.config_set_rx, rx0 + 1, "거부된 PATCH 로 CONFIG_SET 이 나가지 않음")

    # 서버 검증을 우회해 단말 자체 검증을 본다(사양서 합격 기준 5). 시험 단말은 ti_min=1 이라 ti=0 으로 범위 밖을 만든다.
    cv_before = dev.cv
    await ctx.s.broker.publish(dev.topic("config"), {"type": "CONFIG_SET", "cv": cv_before + 1, "ti": 0, "ka": 300})
    await ctx.wait_until(lambda: dev.stats.config_ack_range >= 1, timeout=10, what="단말 CONFIG_ACK RANGE")
    ctx.check_eq(dev.cv, cv_before, "RANGE 시 cv 유지")
    ctx.check_eq(dev.ti, 300, "RANGE 시 ti 유지")
    ctx.check_eq(dev.last_result, {"type": "CONFIG_ACK", "uuid": dev.uuid, "cv": cv_before, "result": "RANGE"},
                 "CONFIG_ACK RANGE payload 모양")
    async def range_logged() -> bool:
        evs = await ctx.s.db.events(dev.uuid, "CONFIG_ACK", since)
        return any(_payload(e).get("result") == "RANGE" for e in evs)
    await ctx.wait_until(range_logged, timeout=FLUSH_WAIT, what="서버가 RANGE ACK 를 이벤트로 기록")

    # cv_device(7) > cv_server(0): 승인 때 cv_server = 8, 첫 TM 뒤 CONFIG_SET cv=8.
    cfg7 = await wait_config_set(ctx, seeded, count=1, what="cv=7 단말: 승인 뒤 CONFIG_SET")
    check_config_set_shape(ctx, cfg7, cv=8, lat_lon=False)
    await ctx.wait_until(lambda: seeded.stats.config_ack_ok >= 1, timeout=10, what="cv=7 단말 CONFIG_ACK OK")
    ctx.check_eq(seeded.cv, 8, "단말 cv 가 뒤로 가지 않고 8")
    async def seeded_row() -> bool:
        s = await ctx.s.db.device(seeded.uuid)
        return s["cv_server"] == 8 and s["cv_device"] == 8
    await ctx.wait_until(seeded_row, timeout=FLUSH_WAIT, what="DB cv_server=8, cv_device=8")


# ── S2-05 ────────────────────────────────────────────────────────────────

@scenario("S2-05", "CONFIG 재전송 — 다음 TM 시점, 60초 쿨다운, 수렴, FLASH 는 쿨다운 없이", phase=2, timeout=300)
async def s2_05(ctx: Ctx) -> None:
    """단말이 CONFIG_SET 을 못 받은 척한다. 서버는 타이머 없이 '다음 Telemetry 수신 시점' 에 재전송하되
    같은 단말에 60초 안에는 다시 보내지 않는다(ADR-002). 단말이 받아들이면 더 이상 재전송하지 않는다.
    FLASH: 단말이 `FLASH` 로 답하면 서버는 쿨다운을 기다리지 않고 다음 TM 에 같은 CONFIG 를 다시 보낸다(§1.1.7)."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600, mode="2cha", approve_now=True)
    await wait_config_set(ctx, dev, count=1, what="승인 초기 동기화 CONFIG_SET")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=10, what="초기 CONFIG_ACK OK")
    rx0 = dev.stats.config_set_rx

    dev.ignore_config_set = 1
    r = await ctx.s.rest.patch_config(dev.uuid, ti_override=300)
    ctx.check(r.status_code < 300, f"PATCH ti_override=300 → {r.status_code}")
    await wait_config_set(ctx, dev, count=rx0 + 1, what="즉시 CONFIG_SET 1회(단말은 무시)")
    ctx.check_eq(dev.stats.config_set_ignored, 1, "단말이 CONFIG_SET 무시")
    t_first = time.monotonic()

    # 2초마다 TM. 첫 재전송은 쿨다운(60초) 이후 첫 TM 에 와야 하고, 그 전엔 오지 말아야 한다.
    resend_after = -1.0
    deadline = t_first + 90
    while time.monotonic() < deadline and dev.stats.config_set_rx < rx0 + 2:
        await dev.send_tm_now()
        await asyncio.sleep(2.0)
        if dev.stats.config_set_rx >= rx0 + 2:
            resend_after = time.monotonic() - t_first
    ctx.check(dev.stats.config_set_rx == rx0 + 2, f"쿨다운 뒤 재전송 1회 (수신 {dev.stats.config_set_rx - rx0})")
    ctx.check(resend_after >= 50, f"재전송이 쿨다운(≈60s) 이후에 옴: {resend_after:.0f}s")
    ctx.check(dev.stats.config_ack_ok == 2 and dev.ti == 300, "두 번째 CONFIG_SET 은 적용 (ACK OK)")
    check_config_set_shape(ctx, dev.last_config_set, ti=300, lat_lon=False)

    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(dev.cv, row["cv_server"], "단말 cv == cv_server")
    await _send_tm_and_wait(ctx, dev, since)
    async def converged() -> bool:
        r2 = await ctx.s.db.device(dev.uuid)
        return r2["cv_device"] == r2["cv_server"]
    await ctx.wait_until(converged, timeout=FLUSH_WAIT, what="DB 수렴 cv_device == cv_server")

    # 수렴 뒤 TM 을 더 보내도 CONFIG_SET 이 더 오지 않아야 한다(쿨다운과 무관).
    rx = dev.stats.config_set_rx
    for _ in range(3):
        await dev.send_tm_now()
        await asyncio.sleep(1.5)
    ctx.check_eq(dev.stats.config_set_rx, rx, "수렴 후 재전송 없음")

    # FLASH: 다음 CONFIG_SET 에 FLASH 로 답한다 → 서버는 다음 TM 에 바로 재전송(쿨다운 대기 없음).
    dev.flash_fail_next = 1
    r = await ctx.s.rest.patch_config(dev.uuid, ti_override=600)
    ctx.check(r.status_code < 300, f"PATCH ti_override=600 → {r.status_code}")
    await ctx.wait_until(lambda: dev.stats.config_ack_flash >= 1, timeout=15, what="단말 CONFIG_ACK FLASH")
    ctx.check_eq(dev.ti, 300, "FLASH 면 이전 값 유지")
    cv_target = r.json().get("cv_server")
    t_flash = time.monotonic()
    got = False
    for _ in range(10):
        await dev.send_tm_now()
        await asyncio.sleep(2.0)
        if dev.stats.config_ack_ok >= 3:
            got = True
            break
    ctx.check(got, "FLASH 뒤 다음 TM 에 재전송 → OK (쿨다운 대기 없음)")
    ctx.check(time.monotonic() - t_flash < 30, f"FLASH 재전송이 60초 쿨다운을 기다리지 않음 ({time.monotonic() - t_flash:.0f}s)")
    ctx.check_eq(dev.ti, 600, "재전송 CONFIG 적용 ti=600")
    if cv_target is not None:
        ctx.check_eq(dev.cv, cv_target, "단말 cv == PATCH 응답 cv_server")
    async def flash_logged() -> bool:
        evs = await ctx.s.db.events(dev.uuid, "CONFIG_ACK", since)
        return any(_payload(e).get("result") == "FLASH" for e in evs)
    await ctx.wait_until(flash_logged, timeout=FLUSH_WAIT, what="서버가 FLASH ACK 를 이벤트로 기록")


# ── S2-06 ────────────────────────────────────────────────────────────────

@scenario("S2-06", "단말 계정 — HMAC 비밀번호, 틀린/남의 비밀번호·clientid 거부, ACL, 공용 계정 스위치", phase=2, timeout=200)
async def s2_06(ctx: Ctx) -> None:
    """사양서 §1.1.2.2 / docs/05 `/internal/mqtt/*`. username=UUID + HMAC(K, UUID) 로 접속 성공.
    틀린 비밀번호·다른 UUID 의 비밀번호·비UUID username 은 거부(clientid ≠ username 은 go-auth 캐시 때문에 참고 로그).
    남의 UUID topic 발행은 브로커가 조용히 버려
    DB 에 남지 않고, `iotlight/#` 와일드카드 구독은 거부되거나 아무것도 받지 못한다.
    공용 계정 `solarlte-test` 는 `/health.test_account_enabled` 가 true 일 때만 접속된다."""
    since = await ctx.s.db.now()
    env = ctx.s.env
    key = hmac_key_from_hex(env.mqtt_hmac_key)
    devices = await make_devices(ctx, 5, mode="2cha", ti=600)
    ctx.check(all(d.username == d.uuid and d.password == device_password(key, d.uuid) for d in devices),
              "username=UUID, password=HMAC-SHA256(K, UUID)[:16].hex()")
    ctx.check(all(len(d.password) == 32 and d.password == d.password.lower() for d in devices), "비밀번호 소문자 hex 32자")
    a, b, c = devices[0], devices[1], devices[2]

    ok, err = await ctx.s.broker.can_connect(a.uuid, "0" * 32, f"{a.uuid}")
    ctx.check(not ok, f"틀린 비밀번호 거부 ({err[:60]})")
    wrong = device_password(key, a.uuid)[:-1] + ("0" if device_password(key, a.uuid)[-1] != "0" else "1")
    ok, err = await ctx.s.broker.can_connect(a.uuid, wrong, f"{a.uuid}")
    ctx.check(not ok, f"한 글자 틀린 비밀번호 거부 ({err[:60]})")
    ok, err = await ctx.s.broker.can_connect(a.uuid, device_password(key, b.uuid), f"{a.uuid}")
    ctx.check(not ok, f"다른 UUID 의 비밀번호 거부 ({err[:60]})")
    ok, err = await ctx.s.broker.can_connect(a.uuid, device_password(key, a.uuid), "runner-not-uuid")
    # go-auth 가 (username, password) 결과를 600초 캐시해 두 번째 접속은 백엔드(/internal/mqtt/auth)에 묻지 않는다 →
    # clientid 검사가 캐시에 가려질 수 있어 판정 항목이 아니라 참고 로그로 남긴다.
    ctx.log(f"(참고) clientid ≠ username 접속: {'거부' if not ok else '허용(go-auth 캐시 가능성)'} {err[:60]}")
    ok, err = await ctx.s.broker.can_connect("not-a-uuid", device_password(key, a.uuid), "not-a-uuid")
    ctx.check(not ok, f"UUID 형식이 아닌 username 거부 ({err[:60]})")

    enabled = await ctx.s.rest.test_account_enabled()
    if enabled is None:
        enabled = env.mqtt_test_account_enabled
        ctx.log("(참고) /health 에 test_account_enabled 없음 — env MQTT_TEST_ACCOUNT_ENABLED 로 판정")
    shared_ok, err = await ctx.s.broker.can_connect(env.mqtt_test_user, env.mqtt_test_password, "shared-probe")
    if enabled:
        ctx.check(shared_ok, f"test_account_enabled=true → 공용 계정 접속 허용 ({err[:60]})")
    else:
        ctx.check(not shared_ok, f"test_account_enabled=false → 공용 계정 접속 거부 ({err[:60]})")

    # ACL: A 가 B 의 status topic 에 발행. B 는 승인해 두어 telemetry 기준값을 만든다.
    await approve(ctx, [b])
    await _send_tm_and_wait(ctx, b, since)
    b_row = await ctx.s.db.device(b.uuid)
    result = await a.publish_foreign_topic(b.uuid)
    ctx.log(f"남의 topic 발행 결과: {result['published']=} {result['still_connected']=}")
    ctx.check(result["still_connected"], "ACL 위반 뒤에도 연결 유지(mosquitto 는 조용히 버림)")
    await ctx.hold(2.0, "배치 flush")
    # B 는 자기 주기 TM 을 계속 보내므로 건수 비교는 안 된다 — A 가 넣은 표식(sq=FORGED_SQ)이 B 행에 없어야 한다.
    forged = await ctx.s.db.fetchval(
        "SELECT count(*) FROM telemetry WHERE uuid = $1 AND sq = $2 AND received_at >= $3", b.uuid, FORGED_SQ, since)
    ctx.check_eq(forged, 0, "A 의 위조 TM 이 B 의 telemetry 에 없음 (브로커 ACL 차단)")
    b_row2 = await ctx.s.db.device(b.uuid)
    ctx.check(b_row2["last_sq"] != FORGED_SQ, f"B 의 last_sq 가 위조 sq 로 바뀌지 않음 ({b_row2['last_sq']})")
    ghost = uuid_from_index(999, namespace_of(ctx.scenario.id))
    await a.publish_foreign_topic(ghost)
    await ctx.hold(2.0, "배치 flush")
    ctx.check(await ctx.s.db.device(ghost) is None, "존재하지 않는 UUID 행이 생기지 않음")
    ctx.defer(lambda: ctx.s.db.delete_device_rows(ghost))
    # A 가 남의 config topic 에 REGISTER_ACK 를 쓰려 해도 막힌다(승인 위조).
    forged = await a._publish(a.topic("config", c.uuid), {"type": "REGISTER_ACK", "uuid": c.uuid, "state": "ACTIVE"}, qos=1)
    await ctx.hold(1.0, "위조 ACK 전파 여부")
    ctx.check(c.state != "ACTIVE", f"남의 config topic 에 쓴 REGISTER_ACK 는 전달되지 않음 (C state={c.state}, published={forged})")

    # 와일드카드 구독: 거부되거나(SUBACK 실패) 받는 것이 없어야 한다.
    sub_task = asyncio.create_task(a.subscribe_raw(f"{env.topic_root}/#", seconds=3.0))
    await asyncio.sleep(0.5)
    await b.send_tm_now()
    await ctx.s.broker.publish(c.topic("cmd"), {"type": "PING", "seq": 0})
    sub = await sub_task
    ctx.log(f"iotlight/# 구독 결과: {sub}")
    ctx.check(not sub["subscribed"] or sub["received"] == 0, "단말 계정의 iotlight/# 구독은 거부되거나 아무것도 받지 못함")
    ctx.check(a.is_connected, "구독 시도 뒤에도 연결 유지")


# ── S2-07 ────────────────────────────────────────────────────────────────

@scenario("S2-07", "payload 이상 — 깨진 JSON, uuid 불일치, 옛 type/t 키, 과대 payload", phase=2, timeout=150)
async def s2_07(ctx: Ctx) -> None:
    """서버는 죽지 않고(/health ok) /api/metrics 의 거부 카운터가 올라야 한다.
    구 펌웨어의 `"type":"TM"` 과 1.0.0 의 `"t":"TM"` 은 둘 다 TELEMETRY 로 받아들인다(개발계획 §2)."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600, mode="2cha", approve_now=True)
    (old_type,) = await make_devices(ctx, 1, offset=1, ti=600, mode="2cha", tm_type="TM", fw="1.1.0", approve_now=True)
    (legacy,) = await make_devices(ctx, 1, offset=2, ti=600, mode="2cha", legacy_t_key=True, fw="1.0.0", approve_now=True)
    m0 = await ctx.s.rest.metrics()
    bad0 = metric_sum(m0, REJECT_RX)
    ctx.log(f"거부 계열 카운터 키: {metric_keys(m0, REJECT_RX)}")

    await dev.send_garbage()
    await dev.send_uuid_mismatch()
    await dev.send_oversized(64 * 1024)
    await ctx.s.broker.publish(dev.topic("status"), b"[1,2,3]", qos=0)
    await ctx.s.broker.publish(dev.topic("status"), {"type": "TELEMETRY"}, qos=0)  # 필드 전부 누락
    await ctx.s.broker.publish(dev.topic("result"), {"type": "WHAT", "uuid": dev.uuid}, qos=1)
    await ctx.s.broker.publish(dev.topic("config"), b"\xff\xfe not json", qos=1)  # 단말 쪽 파서도 죽지 않아야
    await ctx.hold(2.0, "서버 처리")
    ctx.check(await ctx.s.rest.health_ok(), "/health ok")
    ctx.check(dev.is_connected, "단말 연결 유지")

    async def counters_up() -> bool:
        m = await ctx.s.rest.metrics()
        return metric_sum(m, REJECT_RX) > bad0
    await ctx.wait_until(counters_up, timeout=FLUSH_WAIT, what="/api/metrics 거부 카운터 증가")

    row = await ctx.s.db.device(dev.uuid)
    ctx.check(row is not None and row.get("device_model") == dev.device_model, "uuid 불일치 REGISTER 로 행이 망가지지 않음")
    ghost = await ctx.s.db.device("FFFFFFFFFFFFFFFFFFFFFFFF")
    ctx.check(ghost is None, "payload uuid(FFFF…) 행이 생기지 않음")
    ctx.defer(lambda: ctx.s.db.delete_device_rows("FFFFFFFFFFFFFFFFFFFFFFFF"))

    p_old = await old_type.send_tm_now()
    ctx.check(p_old["type"] == "TM", '구 펌웨어 "type":"TM" 로 발행')
    await ctx.wait_until(lambda: _tm_in_db(ctx, old_type.uuid, p_old["sq"], since), timeout=FLUSH_WAIT,
                         what='"type":"TM" 이 TELEMETRY 로 적재')
    p_t = await legacy.send_tm_now()
    ctx.check("t" in p_t and "type" not in p_t, '1.0.0 "t":"TM" 로 발행')
    await ctx.wait_until(lambda: _tm_in_db(ctx, legacy.uuid, p_t["sq"], since), timeout=FLUSH_WAIT,
                         what='"t":"TM" 이 TELEMETRY 로 적재')
    # 정상 TELEMETRY 도 여전히 들어온다.
    await _send_tm_and_wait(ctx, dev, since)
    ctx.check(await ctx.s.rest.health_ok(), "/health ok (마지막)")


# ── S2-08 ────────────────────────────────────────────────────────────────

@scenario("S2-08", "QoS1 중복 — 같은 PONG/CONFIG_ACK 두 번 → device_event 1행", phase=2, timeout=120)
async def s2_08(ctx: Ctx) -> None:
    """dedup_key = uuid:type:seq:sha1(payload) 로 QoS1 재전송 중복을 걸러야 한다(docs/03)."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600, mode="2cha", approve_now=True)
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=15, what="승인 초기 동기화 CONFIG_ACK")
    r = await ctx.s.rest.ping(dev.uuid)
    ctx.check(r.status_code < 300, f"POST ping → {r.status_code}")
    seq = r.json()["seq"]
    await ctx.wait_until(lambda: dev.stats.ping_rx >= 1, timeout=60, what="PING 수신")
    ctx.check_eq(dev.last_result, {"type": "PONG", "seq": seq, "uuid": dev.uuid}, "PONG payload")
    for _ in range(3):
        await dev.duplicate_last_result()
    await ctx.hold(2.0, "서버 처리")
    async def one_pong() -> bool:
        evs = [e for e in await ctx.s.db.events(dev.uuid, "PONG", since) if _payload(e).get("seq") == seq]
        return len(evs) == 1
    await ctx.wait_until(one_pong, timeout=FLUSH_WAIT, what="PONG 이벤트 정확히 1행")
    evs = [e for e in await ctx.s.db.events(dev.uuid, "PONG", since) if _payload(e).get("seq") == seq]
    ctx.check(evs[0].get("dedup_key"), f"dedup_key 채워짐: {evs[0].get('dedup_key')}")
    cmd = await ctx.s.db.command(seq)
    ctx.check(cmd is not None and cmd.get("acked_count") == 1, f"command.acked_count == 1 ({cmd and cmd.get('acked_count')})")

    ok0 = dev.stats.config_ack_ok
    r = await ctx.s.rest.patch_config(dev.uuid, ti_override=300)
    ctx.check(r.status_code < 300, "PATCH ti_override=300")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= ok0 + 1, timeout=15, what="CONFIG_ACK OK")
    cv_now = dev.cv
    for _ in range(3):
        await dev.duplicate_last_result()
    await ctx.hold(2.0, "서버 처리")
    async def one_ack() -> bool:
        evs = [e for e in await ctx.s.db.events(dev.uuid, "CONFIG_ACK", since) if _payload(e).get("cv") == cv_now]
        return len(evs) == 1
    await ctx.wait_until(one_ack, timeout=FLUSH_WAIT, what=f"cv={cv_now} CONFIG_ACK 이벤트 정확히 1행")


# ── S2-09 ────────────────────────────────────────────────────────────────

@scenario("S2-09", "PING/PONG — REST ping → seq → 단말 PONG → command OK (PONG 대기 60s)", phase=2, timeout=200)
async def s2_09(ctx: Ctx) -> None:
    """POST /api/devices/{uuid}/ping 이 전역 seq 를 돌려주고, 단말 PONG 으로 command 행이 acked_count=1, result=OK.
    PING/PONG 은 승인 전에도 된다(§3.8). 서버→단말은 수십 초 지연이 정상이라 PONG 대기는 60초(§1.1.10)."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600, mode="2cha")
    (silent,) = await make_devices(ctx, 1, offset=1, ti=600, mode="2cha", silent_results=True)
    await ctx.wait_until(lambda: dev.state == "PENDING", timeout=15, what="PENDING(승인 전) 상태에서 시험")
    r1 = await ctx.s.rest.ping(dev.uuid)
    r2 = await ctx.s.rest.ping(dev.uuid)
    ctx.check(r1.status_code < 300 and r2.status_code < 300, "ping 2회")
    s1, s2 = r1.json()["seq"], r2.json()["seq"]
    ctx.check(isinstance(s1, int) and s2 > s1, f"seq 전역 단조 증가 {s1} < {s2}")
    await ctx.wait_until(lambda: dev.stats.ping_rx >= 2, timeout=60, what="PING 2회 수신(60s 안)")
    async def acked() -> bool:
        c = await ctx.s.db.command(s2)
        return bool(c and c.get("acked_count") == 1 and c.get("result") == "OK")
    await ctx.wait_until(acked, timeout=60, what="command(seq2) acked_count=1 result=OK (PONG 60s 대기)")
    c = await ctx.s.db.command(s2)
    ctx.check_eq(c.get("target_kind"), "device", "target_kind")
    ctx.check_eq(c.get("target_id"), dev.uuid, "target_id")
    ctx.check_eq(c.get("type"), "PING", "type")
    ctx.check(c.get("finished_at") is not None, "finished_at 기록")
    pongs = await ctx.s.db.events(dev.uuid, "PONG", since)
    ctx.check(len(pongs) == 2, f"PONG 이벤트 2건 ({len(pongs)})")

    # 무응답 단말: 응답 없이 시간이 지나면 TIMEOUT 이어야 한다(타임아웃 값은 백엔드 설정 — 상한 90s 로 본다).
    r3 = await ctx.s.rest.ping(silent.uuid)
    s3 = r3.json()["seq"]
    await ctx.wait_until(lambda: silent.stats.ping_rx >= 1, timeout=60, what="무응답 단말도 PING 은 받음")
    async def timed_out() -> bool:
        c3 = await ctx.s.db.command(s3)
        return bool(c3 and c3.get("result") in ("TIMEOUT", "FAILED"))
    try:
        await ctx.wait_until(timed_out, timeout=90, interval=2, what="무응답 → command.result TIMEOUT")
    except Fail as exc:
        ctx.log(f"(참고) PING 타임아웃 판정이 90s 안에 안 남 — 백엔드 타임아웃 설정 확인: {exc}")


# ── S2-10 ────────────────────────────────────────────────────────────────

@scenario("S2-10", "재접속 폭주 — N대 승인 후 강제 절단, 10초 안에 재접속·retain 으로 TM 재개", phase=2, timeout=600)
async def s2_10(ctx: Ctx) -> None:
    """별도 프로세스(fleet, 2cha)로 N대(--storm-count, 기본 1000. Windows Docker 는 300 이하)를 띄운다.
    N대가 REGISTER → PENDING 이면 REST 로 전부 승인(ACTIVE) → retain REGISTER_ACK 가 전부 존재 → TM 시작.
    60초 뒤 전부 강제 절단(LWT 없음) → 10초 안에 재접속·REGISTER → retain 으로 승인 없이 TM 재개.
    합격: N행 last_register_at 재갱신, /health 내내 ok, register 대기열 60초 안에 0, telemetry_dropped 증가 없음,
    절단 뒤 새 TM 이 있는 단말 ≥95%."""
    count = int(getattr(ctx.opt, "storm_count", 1000))
    ns = namespace_of(ctx.scenario.id)
    prefix = uuid_prefix(ctx.scenario.id)
    uuids = [uuid_from_index(i, ns) for i in range(count)]
    cut_after = 60
    args = [sys.executable, "-m", "tools.sim.fleet", "--count", str(count), "--namespace", str(ns),
            "--mode", "2cha", "--ti", "600", "--storm", "--storm-window", "10", "--cut-after", str(cut_after),
            "--duration", "260", "--quiet", "--host", ctx.s.env.mqtt_host, "--port", str(ctx.s.env.mqtt_port)]
    if ctx.s.env.mqtt_hmac_key:
        args += ["--hmac-key", ctx.s.env.mqtt_hmac_key]
    m0 = await ctx.s.rest.metrics()
    dropped0 = metric_sum(m0, r"telemetry_dropped")
    since = await ctx.s.db.now()

    from tools.scenarios.services import REPO_ROOT
    # Windows 셀렉터 루프에서는 asyncio 서브프로세스가 안 된다 → 동기 Popen + 스레드 대기.
    proc = subprocess.Popen(args, cwd=str(REPO_ROOT))
    t_spawn = time.monotonic()

    async def cleanup() -> None:
        if proc.poll() is None:
            if sys.platform == "win32":
                # fleet 는 워커 프로세스를 또 띄운다. Windows 의 terminate 는 부모만 죽여
                # 워커들이 계속 브로커를 두드리므로 트리째 죽인다.
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), 15)
            except asyncio.TimeoutError:
                proc.kill()
        if not getattr(ctx.opt, "keep_rows", False):
            # retain 정리는 REST DELETE 가 한다(빈 retain). 행이 많으니 REST 는 병렬로.
            sem = asyncio.Semaphore(20)
            async def one(u: str) -> None:
                async with sem:
                    try:
                        await ctx.s.rest.delete_device(u)
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.gather(*(one(u) for u in uuids))
            await ctx.s.db.execute("DELETE FROM telemetry WHERE uuid LIKE $1", f"{prefix}%")
            await ctx.s.db.execute("DELETE FROM device_event WHERE uuid LIKE $1", f"{prefix}%")
            await ctx.s.db.execute("DELETE FROM device WHERE uuid LIKE $1", f"{prefix}%")
    ctx.defer(cleanup)

    async def registered_after(t) -> int:
        return await ctx.s.db.fetchval(
            "SELECT count(*) FROM device WHERE uuid LIKE $1 AND last_register_at >= $2", f"{prefix}%", t)

    health_failures = 0
    async def all_registered(t) -> bool:
        nonlocal health_failures
        if not await ctx.s.rest.health_ok():
            health_failures += 1
        n = await registered_after(t)
        ctx.log(f"REGISTER 반영 {n}/{count}")
        return n >= count
    await ctx.wait_until(lambda: all_registered(since), timeout=120, interval=3, what=f"최초 접속 {count}대 REGISTER")
    ok = await approve_uuids(ctx, uuids, site="storm", timeout=30, ti=600, ka=300)
    ctx.check(ok == count, f"REST 승인 {ok}/{count}")
    # MQTT 필터의 `+` 는 한 레벨 전체여야 하므로 device/+/config 로 받아 topic prefix 로 거른다.
    retained = await ctx.s.broker.retained_many(f"{ctx.s.env.topic_root}/device/+/config", seconds=5)
    active_ret = sum(1 for t, p in retained.items()
                     if t.split("/")[2].startswith(prefix) and p and json.loads(p).get("state") == "ACTIVE")
    ctx.check(active_ret >= count, f"REGISTER_ACK ACTIVE retain 전부 존재 ({active_ret}/{count})")

    async def tm_devices(t) -> int:
        return await ctx.s.db.fetchval(
            "SELECT count(DISTINCT uuid) FROM telemetry WHERE uuid LIKE $1 AND received_at >= $2", f"{prefix}%", t)
    await ctx.wait_until(lambda: tm_devices(since), timeout=60, interval=3, what="승인 뒤 TM 시작")

    # fleet 이 cut_after 초에 절단한다. 그 직전 시각을 기준으로 "절단 이후 REGISTER" 를 센다.
    await ctx.wait_until(lambda: time.monotonic() >= t_spawn + cut_after - 1, timeout=cut_after + 5, interval=1,
                         what=f"절단 시점({cut_after}s) 직전")
    t_cut = await ctx.s.db.now()
    await ctx.wait_until(lambda: all_registered(t_cut), timeout=120, interval=3, what=f"폭주 재접속 뒤 {count}대 REGISTER 재반영")
    ctx.check(health_failures == 0, f"/health 실패 {health_failures}회")
    async def resumed() -> bool:
        n = await tm_devices(t_cut)
        ctx.log(f"절단 뒤 새 TM 단말 {n}/{count}")
        return n >= count * 0.95
    await ctx.wait_until(resumed, timeout=90, interval=3, what="절단 뒤 승인 없이(retain) TM 재개 ≥95%")
    active_rows = await ctx.s.db.fetchval(
        "SELECT count(*) FROM device WHERE uuid LIKE $1 AND state = 'ACTIVE'", f"{prefix}%")
    ctx.check_eq(active_rows, count, "재접속 REGISTER 가 state 를 되돌리지 않음(ACTIVE 유지)")

    m1 = await ctx.s.rest.metrics()
    qkeys = metric_keys(m1, r"register.*(queue|backlog|pending)|(queue|backlog|pending).*register")
    if qkeys:
        async def drained() -> bool:
            m = await ctx.s.rest.metrics()
            return metric_sum(m, r"register.*(queue|backlog|pending)|(queue|backlog|pending).*register") == 0
        await ctx.wait_until(drained, timeout=60, interval=2, what=f"register 대기열 {qkeys} 소진")
    else:
        ctx.log("(참고) metrics 에 register 대기열 키가 없어 소진 확인 생략")
    dropped1 = metric_sum(m1, r"telemetry_dropped")
    ctx.check_eq(dropped1, dropped0, "telemetry_dropped 증가 없음")
    ctx.check(await ctx.s.rest.health_ok(), "/health ok (마지막)")


# ── S2-11 ────────────────────────────────────────────────────────────────

@scenario("S2-11", "브로커 재시작 — retain 보존(persistence), 단말 backoff 재접속, 승인 없이 TM 재개", phase=2, timeout=300, docker=True)
async def s2_11(ctx: Ctx) -> None:
    """docker compose restart mosquitto. 단말은 30초(time_scale 로 압축) backoff 로 다시 붙고, 브로커가 보존한
    retain REGISTER_ACK ACTIVE(S-14 persistence) 로 새 승인 없이 TM 을 재개한다. 백엔드는 자동 재구독한다."""
    if not await ctx.s.docker.available():
        ctx.skip("docker compose 를 부를 수 없음")
    since = await ctx.s.db.now()
    devices = await make_devices(ctx, 20, ti=5, mode="2cha", time_scale=10, reconnect_jitter=20, approve_now=True)
    uuids = [d.uuid for d in devices]
    await ctx.wait_until(lambda: ctx.s.db.fetchval(
        "SELECT count(DISTINCT uuid) FROM telemetry WHERE uuid = ANY($1::text[]) AND received_at >= $2",
        uuids, since), timeout=30, what="재시작 전 TM 적재")

    code, out = await ctx.s.docker.run("restart", "mosquitto", timeout=120)
    ctx.check(code == 0, f"docker compose restart mosquitto → {code} {out[-200:]}")
    t_restart = await ctx.s.db.now()
    await ctx.wait_until(lambda: sum(1 for d in devices if not d.is_connected) >= 1, timeout=30,
                         what="단말이 끊김을 감지")
    await ctx.wait_until(lambda: all(d.is_connected for d in devices), timeout=120, interval=1,
                         what="단말 20대 재접속(backoff)")
    ctx.check(all(d.stats.connects >= 2 for d in devices), "각 단말 접속 횟수 ≥ 2")
    await ctx.wait_until(lambda: all(d.state == "ACTIVE" for d in devices), timeout=30,
                         what="재시작 뒤 retain ACTIVE 수신(persistence)")
    retained = await ctx.s.broker.retained(devices[0].topic("config"))
    ctx.check(retained and json.loads(retained).get("state") == "ACTIVE", "브로커 보관본 REGISTER_ACK ACTIVE 생존")

    async def flowing() -> bool:
        n = await ctx.s.db.fetchval(
            "SELECT count(DISTINCT uuid) FROM telemetry WHERE uuid = ANY($1::text[]) AND received_at >= $2",
            uuids, t_restart)
        return n == len(devices)
    await ctx.wait_until(flowing, timeout=90, interval=2, what="재시작 뒤 20대 모두 새 TM 적재 (백엔드 재구독, 승인 없이)")
    async def reregistered() -> bool:
        n = await ctx.s.db.fetchval(
            "SELECT count(*) FROM device WHERE uuid = ANY($1::text[]) AND last_register_at >= $2", uuids, t_restart)
        return n == len(devices)
    await ctx.wait_until(reregistered, timeout=30, what="재접속 REGISTER 반영")
    ctx.check(await ctx.s.rest.health_ok(), "/health ok")


# ── S2-12 ────────────────────────────────────────────────────────────────

@scenario("S2-12", "DB 장애 중 수신 — postgres 30초 정지, 백엔드 생존, 복구 후 적재 재개", phase=2, timeout=360, docker=True)
async def s2_12(ctx: Ctx) -> None:
    """docker compose stop postgres 30초. 100대(승인됨)가 5초마다 TM. 백엔드는 죽지 않고(/health 응답),
    flush 실패분은 버리고 telemetry_dropped 를 올린다. start 뒤 새 TM 행이 보이고 /health ok."""
    if not await ctx.s.docker.available():
        ctx.skip("docker compose 를 부를 수 없음")
    since = await ctx.s.db.now()
    devices = await make_devices(ctx, 100, ti=5, mode="2cha", approve_now=True)
    uuids = [d.uuid for d in devices]
    await ctx.wait_until(lambda: ctx.s.db.fetchval(
        "SELECT count(DISTINCT uuid) FROM telemetry WHERE uuid = ANY($1::text[]) AND received_at >= $2", uuids, since),
        timeout=30, what="정지 전 TM 적재")
    m0 = await ctx.s.rest.metrics()
    dropped0 = metric_sum(m0, r"telemetry_dropped")

    async def reopen_db() -> None:
        # 정리(make_devices 의 행 삭제)가 돌기 전에 DB 를 다시 연다. postgres 가 올라올 때까지 기다린다.
        for _ in range(30):
            try:
                if ctx.s.db.pool is None:
                    await ctx.s.db.open()
                await ctx.s.db.fetchval("SELECT 1")
                return
            except Exception:  # noqa: BLE001
                await ctx.s.db.close()
                await asyncio.sleep(2)
    ctx.defer(reopen_db)
    await ctx.s.db.close()

    code, out = await ctx.s.docker.run("stop", "postgres", timeout=60)
    ctx.check(code == 0, f"docker compose stop postgres → {code} {out[-200:]}")
    restarted = False

    async def start_pg() -> None:
        nonlocal restarted
        if not restarted:
            restarted = True
            await ctx.s.docker.run("start", "postgres", timeout=60)
    ctx.defer(start_pg)

    alive = 0
    for _ in range(15):
        await asyncio.sleep(2.0)
        if await ctx.s.rest.health() is not None:
            alive += 1
    ctx.check(alive >= 12, f"DB 정지 30초 동안 /health 응답 {alive}/15")
    m1 = await ctx.s.rest.metrics()
    dropped1 = metric_sum(m1, r"telemetry_dropped")
    ctx.check(dropped1 > dropped0, f"telemetry_dropped 증가 {dropped0} → {dropped1}")
    ctx.check(all(d.is_connected for d in devices), "단말은 계속 연결 상태")

    await start_pg()
    async def db_back() -> bool:
        try:
            await ctx.s.db.open()
            await ctx.s.db.fetchval("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            await ctx.s.db.close()
            return False
    await ctx.wait_until(db_back, timeout=60, interval=2, what="postgres 복구")
    t_back = await ctx.s.db.now()
    async def resumed() -> bool:
        n = await ctx.s.db.fetchval(
            "SELECT count(DISTINCT uuid) FROM telemetry WHERE uuid = ANY($1::text[]) AND received_at >= $2", uuids, t_back)
        return n >= len(uuids) * 0.9
    await ctx.wait_until(resumed, timeout=90, interval=2, what="복구 뒤 90% 이상 단말의 새 TM 적재")
    await ctx.wait_until(ctx.s.rest.health_ok, timeout=60, interval=2, what="/health ok")


# ── S2-13 ────────────────────────────────────────────────────────────────

@scenario("S2-13", "presence — 브로커 로그로 ONLINE/OFFLINE (LWT 없음), 지연 TM 이 되살리지 않음", phase=2, timeout=200)
async def s2_13(ctx: Ctx) -> None:
    """ADR-004. 단말은 Will 을 넣지 않는다(§16.1). ka=10 인 단말을 강제 절단(DISCONNECT 없음)하면 브로커가
    끊김(FIN 또는 keepalive×1.5 = 15초)을 로그에 남기고 서버가 25초 안에 online=false·offline_at·OFFLINE 이벤트.
    재접속하면 online=true·ONLINE 이벤트. TM 직후 같은 초에 절단해도 online 은 false 로 남고 LWT 이벤트는 없다."""
    since = await ctx.s.db.now()
    a, b = await make_devices(ctx, 2, ti=600, mode="2cha", ka=10, approve_now=True)
    ctx.check(a.lwt is False and a.ka_connected == 10, "Will 없음, keepalive 10")
    await _send_tm_and_wait(ctx, a, since)

    async def online_is(uuid: str, value: bool) -> bool:
        r = await ctx.s.db.device(uuid)
        return bool(r) and r.get("online") is value
    await ctx.wait_until(lambda: online_is(a.uuid, True), timeout=20, what="접속 중 online=true (브로커 로그 ONLINE)")
    ctx.check(len(await ctx.s.db.events(a.uuid, "ONLINE", since)) >= 1, "ONLINE 이벤트")

    await a.disconnect(hard=True, reconnect=True, reconnect_after=35)
    t_cut = time.monotonic()
    await ctx.wait_until(lambda: online_is(a.uuid, False), timeout=25, interval=1, what="절단 뒤 25s 안에 online=false")
    ctx.log(f"OFFLINE 감지까지 {time.monotonic() - t_cut:.1f}s")
    row = await ctx.s.db.device(a.uuid)
    ctx.check(row.get("offline_at") is not None, "offline_at 기록")
    ctx.check(row.get("online_changed_at") is not None, "online_changed_at 기록")
    offs = await ctx.s.db.events(a.uuid, "OFFLINE", since)
    ctx.check(len(offs) >= 1, "OFFLINE 이벤트")
    ctx.check(len(await ctx.s.db.events(a.uuid, "LWT", since)) == 0, "LWT 이벤트 없음(Will 미등록)")
    rest = await ctx.s.rest.device(a.uuid)
    ctx.check(rest.get("is_online") is False, "DeviceOut is_online=false")
    listing = await ctx.s.rest.devices(q=a.uuid, online=False)
    ctx.check(any(it.get("uuid") == a.uuid for it in listing.get("items", [])), "목록 online=false 필터에 포함")

    await ctx.wait_until(lambda: a.is_connected and a.stats.connects >= 2, timeout=60, what="단말 재접속")
    await ctx.wait_until(lambda: online_is(a.uuid, True), timeout=20, interval=1, what="재접속 뒤 online=true")
    ctx.check(len(await ctx.s.db.events(a.uuid, "ONLINE", since)) >= 2, "ONLINE 이벤트 2건 이상(재접속)")
    await ctx.wait_until(lambda: a.state == "ACTIVE", timeout=15, what="retain 으로 ACTIVE 복귀")

    # B: TM 직후 같은 초에 절단(재접속 없음). TM 은 적재되지만 online 은 false 로 남아야 한다.
    await ctx.wait_until(lambda: online_is(b.uuid, True), timeout=20, what="B online=true")
    payload = await b.send_tm_now()
    await b.disconnect(hard=True)
    await ctx.wait_until(lambda: _tm_in_db(ctx, b.uuid, payload["sq"], since), timeout=FLUSH_WAIT, what="B 의 TM 적재")
    await ctx.wait_until(lambda: online_is(b.uuid, False), timeout=25, interval=1, what="B online=false")
    await ctx.hold(2.0, "배치 flush 가 OFFLINE 뒤에 돌 시간")
    row = await ctx.s.db.device(b.uuid)
    ctx.check(row.get("online") is False, "같은 초의 TM 이 online 을 되살리지 않음")
    ctx.check(row.get("offline_at") is not None and row.get("last_telemetry_at") is not None
              and row["last_telemetry_at"] <= row["offline_at"] + timedelta(seconds=2),
              f"OFFLINE 이 last_telemetry_at 을 밀지 않음 (tm={row.get('last_telemetry_at')}, off={row.get('offline_at')})")
    ctx.check(row.get("last_seen_at") is None or row["last_seen_at"] >= row["last_telemetry_at"], "last_seen_at ≥ last_telemetry_at")
    rest = await ctx.s.rest.device(b.uuid)
    ctx.check(rest.get("is_online") is False, "B is_online=false (online 컬럼 false 면 수신 시각과 무관)")


# ── S2-14 ────────────────────────────────────────────────────────────────

@scenario("S2-14", "시각·단위 — ts 원문 보관, 숫자 컬럼은 ÷100 없이 원값, received_at 은 서버 시각", phase=2, timeout=90)
async def s2_14(ctx: Ctx) -> None:
    """telemetry 행의 ts_device 는 단말 문자열 그대로, bv/bi/pp/li/cs/sc 는 raw 정수 그대로, raw jsonb 는 원본 전체."""
    t0 = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600, mode="2cha", approve_now=True)
    payload = await _send_tm_and_wait(ctx, dev, t0)
    t1 = await ctx.s.db.now()
    rows = await ctx.s.db.telemetry_rows(dev.uuid, t0)
    row = next(r for r in rows if r["sq"] == payload["sq"])
    ctx.check_eq(payload["type"], "TELEMETRY", 'type:"TELEMETRY"')
    ctx.check_eq(row["ts_device"], payload["ts"], "ts_device 원문")
    ctx.check(len(payload["ts"]) == 11 and payload["ts"][6] == "T", "ts 형식 YYMMDDThhmm")
    for col in ("bv", "bi", "sc", "pp", "li", "cs", "er", "md", "on", "fw", "ss", "cv"):
        ctx.check_eq(row[col], payload[col], f"telemetry.{col} 원값")
    for i, col in enumerate(("pw1", "pw2", "pw3")):
        ctx.check_eq(row[col], payload["pw"][i], f"telemetry.{col}")
    raw = row["raw"] if isinstance(row["raw"], dict) else json.loads(row["raw"])
    ctx.check_eq(raw, payload, "raw jsonb == 원본 payload")
    ctx.check(t0 - timedelta(seconds=1) <= row["received_at"] <= t1 + timedelta(seconds=1),
              f"received_at 이 서버 시각 범위 안 ({row['received_at']})")
    ctx.check(row["received_at"].tzinfo is not None, "received_at 은 timestamptz")
    # 밤/낮 모델이 그럴듯한지(값 의미가 아니라 부호·범위만).
    ctx.check(-10000 < payload["bi"] < 10000 and 0 <= payload["sc"] <= 100 and payload["bv"] > 2000, "TM 값 범위")
    ctx.check(len(json.dumps(payload, separators=(",", ":"))) < 384, "TM 한 줄 384B 미만")
