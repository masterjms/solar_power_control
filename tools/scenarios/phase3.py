"""3차 시나리오 — 승인 게이트 (사양서 §3.9.1 다섯 기준 + ADR-002 retain + 프로필·REJECTED·RETIRED·STATE 재조정).

승인 REST 는 docs/05 `PATCH /api/devices/{uuid}/state {state, site?, reason?}`. 단말 1.4.0 은 승인 게이트가
항상 켜져 있으므로(2cha 기본) 별도 플래그 없이 `--phase-only 3` 으로 돌린다.
5분/30분 REGISTER 재전송과 30초 재접속은 `time_scale=60` 으로 압축한다(5분 → 5초, 30분 → 30초).
"""

from __future__ import annotations

import asyncio
import json
import time

from tools.scenarios.common import (FLUSH_WAIT, approve, check_config_set_shape, make_devices, payload_of,
                                    rx_delay, send_tm_and_wait, tm_in_db, wait_config_set)
from tools.scenarios.framework import Ctx, Fail, scenario
from tools.scenarios.services import error_code
from tools.sim.device import RESEND_FAST_SEC, RESEND_SLOW_SEC

#: 승인 게이트 시나리오는 5분/30분 재전송을 이 배수로 압축한다(5분 → 5초, 30분 → 30초).
SCALE = 60.0
#: §1.1.10 — 단말 송신 직후 서버가 보내는 것의 도착 상한(초).
IMMEDIATE = 2.0


async def _set_state(ctx: Ctx, uuid: str, state: str, *, site: str | None = None, reason: str | None = None) -> dict:
    r = await ctx.s.rest.patch_state(uuid, state, site=site, reason=reason)
    if r.status_code >= 400:
        raise Fail(f"state={state} 설정 실패 {r.status_code} {error_code(r)}: {r.text[:200]}")
    body = r.json()
    ctx.log(f"관리자: {uuid[-6:]} → {state} site={site!r} reason={reason!r} → published={body.get('published')}")
    return body


async def _gate_device(ctx: Ctx, offset: int = 0, **flags):
    (dev,) = await make_devices(ctx, 1, offset=offset, ti=600, mode="2cha", time_scale=SCALE, **flags)
    return dev


async def _wait_gate(ctx: Ctx, dev, state: str | None, timeout: float = 20.0) -> None:
    await ctx.wait_until(lambda: dev.state == state, timeout=timeout, what=f"단말 승인 상태 {state}")


async def _tm_count(ctx: Ctx, uuid: str, since) -> int:
    return await ctx.s.db.telemetry_count(uuid, since)


async def _assert_no_tm(ctx: Ctx, dev, since, seconds: float = 6.0) -> None:
    """seconds 동안 TM 이 하나도 적재되지 않아야 한다. 단말이 주기를 스스로 당겨 보내도록 kick 한다."""
    n0 = await _tm_count(ctx, dev.uuid, since)
    sent0 = dev.stats.tm_sent
    for _ in range(max(1, int(seconds / 2))):
        await dev.send_tm_now()  # 게이트가 닫혀 있으면 None 을 돌려주고 보내지 않는다
        await asyncio.sleep(2.0)
    ctx.check(dev.stats.tm_sent == sent0, f"{seconds:.0f}s 동안 단말이 TM 을 보내지 않음(state={dev.state})")
    ctx.check(await _tm_count(ctx, dev.uuid, since) == n0, f"{seconds:.0f}s 동안 TM 적재 없음")
    ctx.check(dev.stats.tm_suppressed >= 1, "단말이 TM 을 억제함(tm_suppressed)")


async def _retained_ack(ctx: Ctx, dev) -> dict | None:
    raw = await ctx.s.broker.retained(dev.topic("config"))
    if not raw:
        return None
    return json.loads(raw)


# ── S3-01 ────────────────────────────────────────────────────────────────

@scenario("S3-01", "신규 단말 → PENDING(retain, 즉시), Telemetry 없음, PING/PONG, 재전송, 목록 우선", phase=3, timeout=150)
async def s3_01(ctx: Ctx) -> None:
    """§3.9.1-1. REGISTER 에 서버가 REGISTER_ACK state=PENDING 을 config topic 에 retain 으로 **2초 안에** 응답(§1.1.10).
    단말은 TM 을 보내지 않고 5분(→5s) 주기로 REGISTER 를 재전송하며 서버는 매번 응답한다. PING/PONG 은 된다(§3.8).
    `/api/devices` 목록은 PENDING 을 먼저 보여 주고 counts.PENDING ≥ 1."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    delay = rx_delay(dev, "REGISTER_ACK", dev.last_register_sent_at)
    ctx.check(delay is not None and delay < IMMEDIATE, f"REGISTER → REGISTER_ACK {delay if delay is None else round(delay, 3)}s < {IMMEDIATE}s (§1.1.10)")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row["state"], "PENDING", "DB state=PENDING")
    ctx.check(row.get("register_ack_at") is not None, "register_ack_at 기록")
    ack = await _retained_ack(ctx, dev)
    ctx.check(ack is not None and ack.get("state") == "PENDING" and ack.get("type") == "REGISTER_ACK",
              f"config topic 에 REGISTER_ACK PENDING retain: {ack}")
    ctx.check(ack.get("uuid") == dev.uuid, "REGISTER_ACK uuid")
    ctx.check(not ({"cv", "ti", "ka"} & set(ack)), "REGISTER_ACK 에 설정값 없음(§3.3)")
    await _assert_no_tm(ctx, dev, since, seconds=6)
    ctx.check_eq(dev.stats.config_set_rx, 0, "PENDING 동안 CONFIG_SET 없음")

    n = dev.stats.register_sent
    acks = dev.stats.register_ack_rx
    await ctx.wait_until(lambda: dev.stats.register_sent >= n + 1, timeout=RESEND_FAST_SEC / SCALE + 10,
                         what="5분(→5s) 주기 REGISTER 재전송")
    await ctx.wait_until(lambda: dev.stats.register_ack_rx >= acks + 1, timeout=10,
                         what="재전송에도 서버가 매번 REGISTER_ACK 응답(§3.3)")
    delay = rx_delay(dev, "REGISTER_ACK", dev.last_register_sent_at)
    ctx.check(delay is not None and delay < IMMEDIATE, f"재전송 응답도 즉시 ({delay and round(delay, 3)}s)")

    r = await ctx.s.rest.ping(dev.uuid)
    ctx.check(r.status_code < 300, "PENDING 상태에서 ping 허용")
    seq = r.json()["seq"]
    async def acked() -> bool:
        c = await ctx.s.db.command(seq)
        return bool(c and c.get("acked_count") == 1)
    await ctx.wait_until(acked, timeout=60, what="PENDING 에서도 PONG")

    listing = await ctx.s.rest.devices(size=50)
    items = listing.get("items") or []
    ctx.check(bool(items) and items[0].get("state") == "PENDING", f"목록 첫 항목이 PENDING ({items[0].get('state') if items else None})")
    first_non_pending = next((i for i, it in enumerate(items) if it.get("state") != "PENDING"), len(items))
    ctx.check(all(it.get("state") == "PENDING" for it in items[:first_non_pending]) and
              all(it.get("state") != "PENDING" for it in items[first_non_pending:]), "PENDING 이 전부 앞에 몰려 있음")
    counts = listing.get("counts") or {}
    ctx.check(int(counts.get("PENDING", 0)) >= 1, f"counts.PENDING ≥ 1 ({counts})")
    by_state = await ctx.s.rest.devices(state="PENDING", q=dev.uuid)
    ctx.check(any(it.get("uuid") == dev.uuid for it in by_state.get("items", [])), "state=PENDING 필터에 포함")
    ctx.check_eq(dev.stats.config_set_rx, 0, "끝까지 CONFIG_SET 없음")


# ── S3-02 ────────────────────────────────────────────────────────────────

@scenario("S3-02", "관리자 승인 → ACTIVE(site 24자) → TM → 직후 CONFIG_SET cv=1 → ACK OK → 다음 TM cv=1", phase=3, timeout=150)
async def s3_02(ctx: Ctx) -> None:
    """§3.9.1-2 + §3.9.2. site 25자는 422. 승인하면 retain 이 ACTIVE+site 로 바뀌고 단말이 TM 1건을 보내면
    서버는 **그 직후(2초 안)** CONFIG_SET(cv 1 — cv_server 0 이었음) 을 보낸다(§1.1.10). ACK OK, 다음 TM cv=1.
    ACTIVE 전에는 CONFIG_SET 이 나간 적이 없어야 한다."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    ctx.check_eq(dev.stats.config_set_rx, 0, "승인 전 CONFIG_SET 0회")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row["cv_server"], 0, "승인 전 cv_server=0")

    r = await ctx.s.rest.patch_state(dev.uuid, "ACTIVE", site="A" * 25)
    ctx.check(r.status_code == 422 and error_code(r) == "VALIDATION_FAILED", f"site 25자 → 422 VALIDATION_FAILED ({r.status_code} {error_code(r)})")
    ctx.check_eq((await ctx.s.db.device(dev.uuid))["state"], "PENDING", "거부된 승인은 state 를 바꾸지 않음")

    body = await _set_state(ctx, dev.uuid, "ACTIVE", site="A" * 24)
    ctx.check(body.get("published") is True and body.get("state") == "ACTIVE", f"응답 published/state: {body}")
    await _wait_gate(ctx, dev, "ACTIVE")
    ack = await _retained_ack(ctx, dev)
    ctx.check(ack and ack.get("state") == "ACTIVE" and ack.get("site") == "A" * 24, f"retain REGISTER_ACK ACTIVE+site(24자): {ack}")
    ctx.check(not ({"cv", "ti", "ka", "grp"} & set(ack)), "REGISTER_ACK 에 설정값 없음(§3.3)")

    await ctx.wait_until(lambda: dev.stats.tm_sent >= 1, timeout=10, what="ACTIVE 뒤 단말 TM 1건")
    cfg = await wait_config_set(ctx, dev, count=1, timeout=10, what="TM 직후 CONFIG_SET")
    delay = rx_delay(dev, "CONFIG_SET", dev.last_tm_sent_at)
    ctx.check(delay is not None and delay < IMMEDIATE, f"TM → CONFIG_SET {delay and round(delay, 3)}s < {IMMEDIATE}s (§1.1.10)")
    check_config_set_shape(ctx, cfg, cv=1, lat_lon=False)
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=10, what="CONFIG_ACK OK")
    ctx.check_eq(dev.cv, 1, "단말 cv=1")
    payload = await send_tm_and_wait(ctx, dev, since)
    ctx.check_eq(payload["cv"], 1, "다음 TM cv=1")
    async def converged() -> bool:
        r2 = await ctx.s.db.device(dev.uuid)
        return r2["cv_server"] == 1 and r2["cv_device"] == 1
    await ctx.wait_until(converged, timeout=FLUSH_WAIT, what="DB cv_server=1, cv_device=1")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row["state"], "ACTIVE", "DB state=ACTIVE")
    ctx.check_eq(row.get("site"), "A" * 24, "site 저장")
    ctx.check(row.get("state_changed_at") is not None, "state_changed_at")
    ctx.check(dev.gate.resend_needed is False, "ACTIVE 면 REGISTER 재전송 중지")
    ev = await ctx.s.db.events(dev.uuid, "STATE_CHANGE", since)
    ctx.check(len(ev) >= 1, "STATE_CHANGE 이벤트")
    ctx.check_eq(dev.stats.config_set_rx, 1, "CONFIG_SET 은 ACTIVE 뒤 첫 TM 직후 1회뿐")
    rest = await ctx.s.rest.device(dev.uuid)
    ctx.check(rest.get("config_pending") is False, "DeviceOut config_pending=false")


# ── S3-03 ────────────────────────────────────────────────────────────────

@scenario("S3-03", "단말 재부팅 → retain ACK 로 관리자 조작 없이 ACTIVE 복귀", phase=3, timeout=120)
async def s3_03(ctx: Ctx) -> None:
    """§3.9.1-3. 재부팅한 단말은 승인 상태를 잊는다(§3.5). config 구독 직후 브로커가 retain 된
    REGISTER_ACK ACTIVE 를 내려주므로 REST 호출 없이 ACTIVE 로 시작하고, 서버는 REGISTER 에 다시 응답한다."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    await _set_state(ctx, dev.uuid, "ACTIVE", site="B-1")
    await _wait_gate(ctx, dev, "ACTIVE")
    await ctx.wait_until(lambda: _tm_count(ctx, dev.uuid, since), timeout=FLUSH_WAIT, what="TM 시작")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=10, what="초기 동기화 CONFIG_ACK")

    dev.inbox.clear()
    acks_before = dev.stats.register_ack_rx
    await dev.reboot(reconnect_after=0.5)
    ctx.check(dev.state is None, "재부팅 직후 승인 상태 소거")
    await ctx.wait_until(lambda: dev.is_connected, timeout=30, what="재접속")
    await _wait_gate(ctx, dev, "ACTIVE", timeout=10)
    retained_acks = [p for (t, p, r) in dev.inbox if r and isinstance(p, dict) and p.get("type") == "REGISTER_ACK"]
    ctx.check(len(retained_acks) == 1 and retained_acks[0].get("state") == "ACTIVE", "구독 직후 retain ACK ACTIVE 수신")
    await ctx.wait_until(lambda: dev.stats.register_ack_rx >= acks_before + 2, timeout=15,
                         what="retain 1 + 서버 실시간 응답 1 (REGISTER 마다 응답, §3.3)")
    t_reboot = await ctx.s.db.now()
    async def resumed() -> bool:
        return await _tm_count(ctx, dev.uuid, t_reboot) >= 1
    await ctx.wait_until(resumed, timeout=FLUSH_WAIT, what="재부팅 뒤 TM 재개(REST 호출 없음)")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row["state"], "ACTIVE", "DB state 유지")
    ctx.check_eq(row.get("site"), "B-1", "site 유지")
    await asyncio.sleep(1.5)
    ctx.check_eq(dev.stats.config_set_rx, 1, "cv 가 이미 같으므로 재부팅 뒤 CONFIG_SET 없음")


# ── S3-04 ────────────────────────────────────────────────────────────────

@scenario("S3-04", "백엔드 재시작 → 단말 REGISTER 재전송으로 복구", phase=3, timeout=240, docker=True)
async def s3_04(ctx: Ctx) -> None:
    """§3.9.1-4. 백엔드를 재시작해도 단말은 브로커에 연결된 채다. PENDING 단말의 재전송 REGISTER 에 재기동 서버가
    다시 응답하고(DB 정본), ACTIVE 단말의 TM 수집이 이어진다."""
    if not await ctx.s.docker.available():
        ctx.skip("docker compose 를 부를 수 없음")
    since = await ctx.s.db.now()
    active = await _gate_device(ctx)
    await _wait_gate(ctx, active, "PENDING")
    await _set_state(ctx, active.uuid, "ACTIVE")
    await _wait_gate(ctx, active, "ACTIVE")
    pending = await _gate_device(ctx, offset=1)
    await _wait_gate(ctx, pending, "PENDING")

    code, out = await ctx.s.docker.run("restart", "backend", timeout=120)
    ctx.check(code == 0, f"docker compose restart backend → {code} {out[-200:]}")
    await ctx.wait_until(ctx.s.rest.health_ok, timeout=90, interval=2, what="백엔드 재기동 /health ok")
    t_up = await ctx.s.db.now()
    n = pending.stats.register_ack_rx
    await ctx.wait_until(lambda: pending.stats.register_ack_rx > n, timeout=40,
                         what="PENDING 단말 재전송 REGISTER 에 재기동 서버가 응답")
    ctx.check_eq(pending.state, "PENDING", "재기동 서버도 DB 대로 PENDING 응답")
    await active.send_tm_now()
    async def flowing() -> bool:
        return await _tm_count(ctx, active.uuid, t_up) >= 1
    await ctx.wait_until(flowing, timeout=FLUSH_WAIT, what="ACTIVE 단말 TM 수집 재개")
    ctx.check(active.is_connected and pending.is_connected, "단말 연결 유지(브로커는 그대로)")
    ctx.check_eq((await ctx.s.db.device(active.uuid))["state"], "ACTIVE", "DB state 보존")
    ctx.check(await ctx.s.rest.health_ok(), "/health ok")


# ── S3-05 ────────────────────────────────────────────────────────────────

@scenario("S3-05", "SUSPENDED → Telemetry 중지·연결 유지(online), 해제 시 재개", phase=3, timeout=120)
async def s3_05(ctx: Ctx) -> None:
    """§3.9.1-5. SUSPENDED 는 연결을 끊지 않는다(해제 통로). REGISTER 재전송도 하지 않는다(§3.4).
    ACTIVE 로 되돌리면 TM 이 재개된다."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    await _set_state(ctx, dev.uuid, "ACTIVE", site="S")
    await _wait_gate(ctx, dev, "ACTIVE")
    await ctx.wait_until(lambda: _tm_count(ctx, dev.uuid, since), timeout=FLUSH_WAIT, what="TM 시작")

    await _set_state(ctx, dev.uuid, "SUSPENDED", reason="점검")
    await _wait_gate(ctx, dev, "SUSPENDED")
    ack = await _retained_ack(ctx, dev)
    ctx.check(ack and ack.get("state") == "SUSPENDED" and ack.get("site") == "S", f"retain SUSPENDED (site 유지): {ack}")
    await _assert_no_tm(ctx, dev, since)
    ctx.check(dev.is_connected, "SUSPENDED 에서도 연결 유지")
    reg = dev.stats.register_sent
    await asyncio.sleep(RESEND_FAST_SEC / SCALE + 1)
    ctx.check_eq(dev.stats.register_sent, reg, "SUSPENDED 는 REGISTER 재전송 안 함")
    async def online_true() -> bool:
        r = await ctx.s.db.device(dev.uuid)
        return bool(r and r.get("online") is True)
    await ctx.wait_until(online_true, timeout=20, what="SUSPENDED 여도 online=true (브로커 로그)")
    ctx.check_eq((await ctx.s.db.device(dev.uuid))["state_reason"], "점검", "state_reason 저장")

    await _set_state(ctx, dev.uuid, "ACTIVE")
    await _wait_gate(ctx, dev, "ACTIVE")
    t_resume = await ctx.s.db.now()
    async def resumed() -> bool:
        return await _tm_count(ctx, dev.uuid, t_resume) >= 1
    await ctx.wait_until(resumed, timeout=FLUSH_WAIT, what="해제 뒤 TM 재개")
    events = await ctx.s.db.events(dev.uuid, "STATE_CHANGE", since)
    ctx.check(len(events) >= 3, f"STATE_CHANGE 이벤트 3건 이상 ({len(events)})")


# ── S3-06 ────────────────────────────────────────────────────────────────

@scenario("S3-06", "retain REGISTER_ACK 가 비retain CONFIG_SET 에 덮이지 않음 (ADR-002)", phase=3, timeout=120)
async def s3_06(ctx: Ctx) -> None:
    """같은 config topic 으로 CONFIG_SET(retain=0) 이 세 번 나간 뒤에도 브로커 보관본은 REGISTER_ACK ACTIVE 여야 한다.
    새로 구독하는 단말(재부팅)이 CONFIG_SET 을 보관본으로 받으면 안 된다. cmd topic 에는 retain 이 없다."""
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    await _set_state(ctx, dev.uuid, "ACTIVE")
    await _wait_gate(ctx, dev, "ACTIVE")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=15, what="초기 동기화 CONFIG_ACK")
    ok0 = dev.stats.config_ack_ok
    for i, ti in enumerate((300, 600, 900), start=1):
        r = await ctx.s.rest.patch_config(dev.uuid, ti_override=ti)
        ctx.check(r.status_code < 300 and r.json().get("published") is True, f"PATCH ti_override={ti} 즉시 발행")
        await ctx.wait_until(lambda: dev.stats.config_ack_ok >= ok0 + i, timeout=20, what=f"CONFIG_SET {i}회 수신·ACK")
    ack = await _retained_ack(ctx, dev)
    ctx.check_eq(ack and ack.get("type"), "REGISTER_ACK", "보관본은 여전히 REGISTER_ACK")
    ctx.check_eq(ack and ack.get("state"), "ACTIVE", "보관본 state=ACTIVE")
    got = await ctx.s.broker.collect(dev.topic("config"), seconds=2)
    kinds = [(json.loads(p).get("type") if p else None, r) for (_, p, r) in got]
    ctx.check(all(k == "REGISTER_ACK" for k, r in kinds if r), f"retain 으로 온 것은 REGISTER_ACK 뿐: {kinds}")
    ctx.check(await ctx.s.broker.retained(dev.topic("cmd")) is None, "cmd topic 에 retain 없음")
    await ctx.s.rest.ping(dev.uuid)
    await asyncio.sleep(1.0)
    ctx.check(await ctx.s.broker.retained(dev.topic("cmd")) is None, "PING 뒤에도 cmd topic 에 retain 없음")
    # 재부팅한 단말이 보관본으로 CONFIG_SET 을 받지 않는지.
    dev.inbox.clear()
    await dev.reboot(reconnect_after=0.5)
    await _wait_gate(ctx, dev, "ACTIVE", timeout=30)
    retained_kinds = [p.get("type") for (t, p, r) in dev.inbox if r and isinstance(p, dict)]
    ctx.check_eq(retained_kinds, ["REGISTER_ACK"], "재부팅 단말이 retain 으로 받은 것")


# ── S3-07 ────────────────────────────────────────────────────────────────

@scenario("S3-07", "RETIRED 정리 — RETIRED retain 뒤 빈 retain, 단말 무응답 복귀·30분 주기 REGISTER → 다시 PENDING", phase=3, timeout=200)
async def s3_07(ctx: Ctx) -> None:
    """§3.3/§3.4/docs/05 상태 전이. RETIRED 통보를 받은 단말은 TM·재전송을 멈춘다. 서버가 빈 payload 를 retain 으로 보내면
    보관본이 지워지고(Broker.retained → None) 단말은 무응답 상태로 돌아가 30분(→30s) 주기 REGISTER 재전송을 재개한다.
    그 REGISTER 에 서버는 PENDING 으로 되돌리고 PENDING 을 ACK 한다(같은 보드 재설치)."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    empties = dev.stats.register_ack_empty_rx
    body = await _set_state(ctx, dev.uuid, "RETIRED", reason="철거")
    ctx.check(body.get("published") is True, "RETIRED 발행")
    await ctx.wait_until(lambda: any(isinstance(p, dict) and p.get("state") == "RETIRED" for (_, p, _) in dev.inbox),
                         timeout=10, what="단말이 RETIRED ACK 수신")
    async def cleared() -> bool:
        return await ctx.s.broker.retained(dev.topic("config"), timeout=2) is None
    await ctx.wait_until(cleared, timeout=30, interval=2, what="서버가 빈 payload retain 으로 보관본 삭제")
    await ctx.wait_until(lambda: dev.stats.register_ack_empty_rx > empties, timeout=10, what="단말이 빈 payload 수신")
    ctx.check(dev.state is None, "단말 무응답 상태 복귀")
    ctx.check(abs(dev.gate.next_resend_delay() - RESEND_SLOW_SEC / SCALE) < 1e-6, "재전송 주기 30분(→30s)")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row["state"], "RETIRED", "DB state=RETIRED")
    ctx.check(row.get("register_ack_at") is None, "빈 retain 뒤 register_ack_at=null")
    await _assert_no_tm(ctx, dev, since, seconds=4)
    n = dev.stats.register_sent
    await ctx.wait_until(lambda: dev.stats.register_sent > n, timeout=RESEND_SLOW_SEC / SCALE + 15, interval=1,
                         what="30분(→30s) 뒤 REGISTER 재전송 재개")
    await _wait_gate(ctx, dev, "PENDING", timeout=10)
    ctx.check_eq((await ctx.s.db.device(dev.uuid))["state"], "PENDING", "RETIRED 뒤 REGISTER → 서버가 PENDING 으로 되돌림")
    ack = await _retained_ack(ctx, dev)
    ctx.check(ack and ack.get("state") == "PENDING", f"retain 도 PENDING: {ack}")


# ── S3-08 ────────────────────────────────────────────────────────────────

@scenario("S3-08", "프로필 변경 → 그 프로필의 단말 전부 cv_server+1, 다음 TM 에 CONFIG_SET (SUSPENDED 는 해제 뒤)", phase=3, timeout=240)
async def s3_08(ctx: Ctx) -> None:
    """docs/05 프로필. 새 프로필(ti 900/ka 300)을 만들어 3대에 배정 → 승인 → 1대는 SUSPENDED.
    PATCH /api/profiles/{id} ti=1200 → bumped_devices, 즉시 발행 없음. ACTIVE 2대는 다음 TM 에 CONFIG_SET(ti 1200),
    SUSPENDED 1대는 아무것도 못 받다가 해제 뒤 첫 TM 에 받는다. 사용 중 프로필 삭제는 409 PROFILE_IN_USE."""
    since = await ctx.s.db.now()
    name = f"S3-08 {int(time.time())}"
    r = await ctx.s.rest.create_profile(name, 900, 300)
    ctx.check(r.status_code == 201, f"POST /api/profiles → 201 ({r.status_code} {r.text[:100]})")
    profile = r.json()
    pid = profile["id"]
    ctx.defer(lambda: ctx.s.rest.delete_profile(pid))  # 단말 정리(LIFO 뒤에 등록되는 것) 다음에 돈다
    devices = await make_devices(ctx, 3, mode="2cha", ti=600, time_scale=SCALE)
    for d in devices:
        await ctx.wait_until(lambda d=d: d.state == "PENDING", timeout=20, what=f"{d.uuid[-4:]} PENDING")
        r = await ctx.s.rest.patch_config(d.uuid, profile_id=pid)
        ctx.check(r.status_code < 300 and r.json().get("published") is False, f"PENDING 중 프로필 배정 published=false ({r.text[:80]})")
    await approve(ctx, devices, site="P", override=False)  # 프로필 값이 내려와야 하므로 override 없음
    for d in devices:
        cfg = await wait_config_set(ctx, d, count=1, what=f"{d.uuid[-4:]} 승인 뒤 CONFIG_SET")
        check_config_set_shape(ctx, cfg, ti=900, ka=300, lat_lon=False)
        await ctx.wait_until(lambda d=d: d.stats.config_ack_ok >= 1, timeout=10, what=f"{d.uuid[-4:]} ACK OK")
    profiles = await ctx.s.rest.profiles()
    mine = next((p for p in profiles if p["id"] == pid), None)
    ctx.check(mine is not None and mine.get("device_count") == 3, f"GET /api/profiles device_count=3 ({mine})")
    r = await ctx.s.rest.delete_profile(pid)
    ctx.check(r.status_code == 409 and error_code(r) == "PROFILE_IN_USE", f"사용 중 삭제 → 409 PROFILE_IN_USE ({r.status_code} {error_code(r)})")

    suspended = devices[2]
    await _set_state(ctx, suspended.uuid, "SUSPENDED")
    await _wait_gate(ctx, suspended, "SUSPENDED")
    rx = {d.uuid: d.stats.config_set_rx for d in devices}
    cv_before = {d.uuid: (await ctx.s.db.device(d.uuid))["cv_server"] for d in devices}

    r = await ctx.s.rest.patch_profile(pid, ti=1200)
    ctx.check(r.status_code < 300, f"PATCH /api/profiles/{pid} ti=1200 → {r.status_code} {r.text[:100]}")
    body = r.json()
    ctx.check(int(body.get("bumped_devices", -1)) == 3, f"bumped_devices=3 ({body})")
    for d in devices:
        cv_now = (await ctx.s.db.device(d.uuid))["cv_server"]
        ctx.check_eq(cv_now, cv_before[d.uuid] + 1, f"{d.uuid[-4:]} cv_server +1")
    await asyncio.sleep(1.5)
    ctx.check(all(d.stats.config_set_rx == rx[d.uuid] for d in devices), "프로필 변경 직후 즉시 발행 없음(§1.1.10)")

    # ACTIVE 2대: 다음 TM 에 CONFIG_SET. (직전 발행 60초 쿨다운이 걸려 있을 수 있으므로 TM 을 반복하며 최대 75초 본다.)
    t0 = time.monotonic()
    for d in devices[:2]:
        async def got(d=d) -> bool:
            await d.send_tm_now()
            await asyncio.sleep(2.0)
            return d.stats.config_set_rx > rx[d.uuid]
        await ctx.wait_until(got, timeout=75, interval=0.1, what=f"{d.uuid[-4:]} 다음 TM 에 CONFIG_SET")
        check_config_set_shape(ctx, d.last_config_set, ti=1200, ka=300, lat_lon=False)
        await ctx.wait_until(lambda d=d: d.ti == 1200, timeout=10, what=f"{d.uuid[-4:]} ti=1200 적용")
    ctx.log(f"프로필 변경 → ACTIVE 단말 CONFIG_SET 까지 {time.monotonic() - t0:.0f}s (쿨다운 영향 포함)")
    ctx.check_eq(suspended.stats.config_set_rx, rx[suspended.uuid], "SUSPENDED 단말은 CONFIG_SET 없음")

    await _set_state(ctx, suspended.uuid, "ACTIVE")
    await _wait_gate(ctx, suspended, "ACTIVE")
    async def got_s() -> bool:
        await suspended.send_tm_now()
        await asyncio.sleep(2.0)
        return suspended.stats.config_set_rx > rx[suspended.uuid]
    await ctx.wait_until(got_s, timeout=75, interval=0.1, what="해제 뒤 첫 TM 에 CONFIG_SET")
    check_config_set_shape(ctx, suspended.last_config_set, ti=1200, ka=300, lat_lon=False)
    for d in devices:
        async def conv(d=d) -> bool:
            r2 = await ctx.s.db.device(d.uuid)
            return r2["cv_device"] == r2["cv_server"]
        await ctx.wait_until(conv, timeout=FLUSH_WAIT, what=f"{d.uuid[-4:]} DB 수렴")


# ── S3-09 ────────────────────────────────────────────────────────────────

@scenario("S3-09", "REJECTED → ack 에 reason, TM·재전송 없음; PENDING(재검토) → 재전송 재개", phase=3, timeout=150)
async def s3_09(ctx: Ctx) -> None:
    """§3.4: REJECTED 는 TM 도 REGISTER 재전송도 없다. retain 에 reason 이 실린다. 관리자가 PENDING 으로 되돌리면
    단말은 다시 5분(→5s) 주기 재전송을 시작한다."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    await _set_state(ctx, dev.uuid, "REJECTED", reason="unknown device")
    await _wait_gate(ctx, dev, "REJECTED")
    ack = await _retained_ack(ctx, dev)
    ctx.check(ack and ack.get("state") == "REJECTED" and ack.get("reason") == "unknown device", f"retain REJECTED+reason: {ack}")
    ctx.check_eq((await ctx.s.db.device(dev.uuid))["state_reason"], "unknown device", "DB state_reason")
    reg = dev.stats.register_sent
    await _assert_no_tm(ctx, dev, since, seconds=RESEND_FAST_SEC / SCALE + 3)
    ctx.check_eq(dev.stats.register_sent, reg, "REJECTED 는 REGISTER 재전송 없음")
    ctx.check(dev.is_connected, "연결은 유지")

    await _set_state(ctx, dev.uuid, "PENDING")
    await _wait_gate(ctx, dev, "PENDING")
    n = dev.stats.register_sent
    await ctx.wait_until(lambda: dev.stats.register_sent > n, timeout=RESEND_FAST_SEC / SCALE + 10,
                         what="PENDING 복귀 → REGISTER 재전송 재개")
    await ctx.wait_until(lambda: dev.state == "PENDING" and dev.stats.register_ack_rx >= 3, timeout=10,
                         what="재전송에 PENDING 으로 응답")


# ── S3-10 ────────────────────────────────────────────────────────────────

@scenario("S3-10", "PENDING 중 config PATCH → published=false, 승인 뒤 첫 TM 에 새 값으로 CONFIG_SET", phase=3, timeout=150)
async def s3_10(ctx: Ctx) -> None:
    """docs/05 PATCH config: ACTIVE 가 아니면 `published=false, reason=NOT_ACTIVE` 로 DB 에만 둔다(§16.5).
    승인 뒤 단말의 첫 TM 직후 CONFIG_SET 이 새 값(ti 300, lat/lon)으로 나간다."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    r = await ctx.s.rest.patch_config(dev.uuid, ti_override=300, lat=37.3617, lon=126.9352)
    ctx.check(r.status_code < 300, f"PENDING 중 PATCH 허용 ({r.status_code} {r.text[:100]})")
    body = r.json()
    ctx.check(body.get("published") is False, f"published=false: {body}")
    ctx.check_eq(body.get("reason"), "NOT_ACTIVE", "reason")
    ctx.check_eq(body.get("ti_effective"), 300, "ti_effective=300")
    await asyncio.sleep(1.5)
    ctx.check_eq(dev.stats.config_set_rx, 0, "PENDING 이라 CONFIG_SET 은 아직 안 내려감")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check(row["cv_server"] >= 1, f"PATCH 로 cv_server 는 올라감({row['cv_server']})")
    ctx.check(row.get("config_sent_at") is None, "config_sent_at 은 아직 없음")

    await _set_state(ctx, dev.uuid, "ACTIVE", site="X")
    await _wait_gate(ctx, dev, "ACTIVE")
    cfg = await wait_config_set(ctx, dev, count=1, what="승인 뒤 첫 TM 직후 CONFIG_SET")
    delay = rx_delay(dev, "CONFIG_SET", dev.last_tm_sent_at)
    ctx.check(delay is not None and delay < IMMEDIATE, f"TM → CONFIG_SET {delay and round(delay, 3)}s")
    check_config_set_shape(ctx, cfg, ti=300, cv=row["cv_server"], lat_lon=True)
    ctx.check(abs(cfg["lat"] - 37.3617) < 1e-6 and abs(cfg["lon"] - 126.9352) < 1e-6, "lat/lon 값")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=10, what="CONFIG_ACK OK")
    ctx.check(dev.ti == 300 and dev.lat == 37.3617, "단말 적용")
    payload = await send_tm_and_wait(ctx, dev, since)
    ctx.check_eq(payload["cv"], row["cv_server"], "다음 TM cv == cv_server")


# ── S3-11 ────────────────────────────────────────────────────────────────

@scenario("S3-11", "CONFIG_ACK STATE — DB 는 ACTIVE 인데 단말이 STATE 로 답하면 REGISTER_ACK 재발행", phase=3, timeout=150)
async def s3_11(ctx: Ctx) -> None:
    """단말이 승인 상태를 잃었다는 뜻이므로(예: retain 유실) 서버는 DB 상태로 REGISTER_ACK retain 을 다시 발행하고,
    단말은 다음 TM 에 CONFIG_SET 을 다시 받아 OK 한다. `POST /register-ack` 로도 같은 재발행이 된다."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    await _set_state(ctx, dev.uuid, "ACTIVE", site="R")
    await _wait_gate(ctx, dev, "ACTIVE")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=15, what="초기 동기화 CONFIG_ACK OK")

    dev.state_ack_next = 1
    acks = dev.stats.register_ack_rx
    r = await ctx.s.rest.patch_config(dev.uuid, ti_override=300)
    ctx.check(r.status_code < 300 and r.json().get("published") is True, "PATCH → 즉시 CONFIG_SET")
    await ctx.wait_until(lambda: dev.stats.config_ack_state >= 1, timeout=15, what="단말이 STATE 로 응답")
    ctx.check_eq(dev.ti, 600, "STATE 면 아무것도 적용 안 함")
    await ctx.wait_until(lambda: dev.stats.register_ack_rx > acks, timeout=15, what="서버가 REGISTER_ACK 재발행")
    ctx.check_eq(dev.state, "ACTIVE", "재발행된 ACK 는 DB 대로 ACTIVE")
    ack = await _retained_ack(ctx, dev)
    ctx.check(ack and ack.get("state") == "ACTIVE" and ack.get("site") == "R", f"retain ACTIVE+site: {ack}")
    async def state_logged() -> bool:
        return any(payload_of(e).get("result") == "STATE" for e in await ctx.s.db.events(dev.uuid, "CONFIG_ACK", since))
    await ctx.wait_until(state_logged, timeout=FLUSH_WAIT, what="STATE ACK 이벤트 기록")

    # 다음 TM 에 CONFIG_SET 재전송 → 이번엔 OK. (쿨다운이 걸릴 수 있어 최대 75초.)
    ok0 = dev.stats.config_ack_ok
    async def got() -> bool:
        await dev.send_tm_now()
        await asyncio.sleep(2.0)
        return dev.stats.config_ack_ok > ok0
    await ctx.wait_until(got, timeout=75, interval=0.1, what="다음 TM 에 CONFIG_SET 재전송 → OK")
    ctx.check_eq(dev.ti, 300, "ti=300 적용")
    async def converged() -> bool:
        await dev.send_tm_now()
        r2 = await ctx.s.db.device(dev.uuid)
        return r2["cv_device"] == r2["cv_server"]
    await ctx.wait_until(converged, timeout=FLUSH_WAIT, interval=1.0, what="DB 수렴")

    # 수동 재발행 API.
    acks = dev.stats.register_ack_rx
    r = await ctx.s.rest.republish_register_ack(dev.uuid)
    ctx.check(r.status_code < 300, f"POST /register-ack → {r.status_code}")
    await ctx.wait_until(lambda: dev.stats.register_ack_rx > acks, timeout=10, what="재발행 ACK 수신")
    ctx.check_eq(dev.state, "ACTIVE", "여전히 ACTIVE")
    ctx.check(await tm_in_db(ctx, dev.uuid, dev.last_tm["sq"], since), "TM 적재 계속")
