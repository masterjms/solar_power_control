"""3차 시나리오 — 승인 게이트 (사양서 §3.9.1 다섯 기준 + ADR-002 retain 검증).

지금은 백엔드에 승인 기능이 없으므로 `--phase 3` 없이는 전부 SKIP 된다.
승인 조작 REST 는 아직 확정 전이라 `PATCH /api/devices/{uuid}/state {state, site?, reason?}` 로
가정한다(docs/06 §가정). 404/405 면 이유를 남기고 SKIP.
"""

from __future__ import annotations

import asyncio
import json

from tools.scenarios.common import make_devices
from tools.scenarios.framework import Ctx, Fail, scenario

#: 승인 게이트 시나리오는 5분/30분 재전송을 이 배수로 압축한다(5분 → 5초, 30분 → 30초).
SCALE = 60.0
FLUSH_WAIT = 10.0


async def _set_state(ctx: Ctx, uuid: str, state: str, **extra) -> None:
    r = await ctx.s.rest.set_state(uuid, state, **extra)
    if r.status_code in (404, 405):
        ctx.skip(f"승인 REST(PATCH /api/devices/{{uuid}}/state) 미구현: {r.status_code}")
    if r.status_code >= 400:
        raise Fail(f"state={state} 설정 실패 {r.status_code}: {r.text[:200]}")
    ctx.log(f"관리자: {uuid} → {state} {extra or ''}")


async def _gate_device(ctx: Ctx, **flags):
    (dev,) = await make_devices(ctx, 1, ti=600, approval_gate=True, time_scale=SCALE, **flags)
    return dev


async def _wait_gate(ctx: Ctx, dev, state: str | None, timeout: float = 20.0) -> None:
    await ctx.wait_until(lambda: dev.gate.state == state, timeout=timeout, what=f"단말 승인 상태 {state}")


async def _tm_count(ctx: Ctx, uuid: str, since) -> int:
    return await ctx.s.db.telemetry_count(uuid, since)


async def _assert_no_tm(ctx: Ctx, dev, since, seconds: float = 6.0) -> None:
    """seconds 동안 TM 이 하나도 적재되지 않아야 한다. 단말이 주기를 스스로 당겨 보내도록 kick 한다."""
    n0 = await _tm_count(ctx, dev.uuid, since)
    for _ in range(int(seconds / 2)):
        await dev.send_tm_now()  # 게이트가 닫혀 있으면 None 을 돌려주고 보내지 않는다
        await asyncio.sleep(2.0)
    ctx.check(await _tm_count(ctx, dev.uuid, since) == n0, f"{seconds:.0f}s 동안 TM 적재 없음")
    ctx.check(dev.stats.tm_suppressed >= 1, "단말이 TM 을 억제함(tm_suppressed)")


# ── S3-01 ────────────────────────────────────────────────────────────────

@scenario("S3-01", "신규 단말 → PENDING, Telemetry 없음, PING/PONG 됨", phase=3, requires="approval_gate", timeout=120)
async def s3_01(ctx: Ctx) -> None:
    """§3.9.1-1. REGISTER 에 서버가 REGISTER_ACK state=PENDING 을 retain 으로 응답.
    단말은 TM 을 보내지 않고 5분(→5s) 주기로 REGISTER 를 재전송한다. PING/PONG 은 된다(§3.8)."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row["state"], "PENDING", "DB state=PENDING")
    retained = await ctx.s.broker.retained(dev.topic("config"))
    ctx.check(retained and json.loads(retained).get("state") == "PENDING", "config topic 에 REGISTER_ACK PENDING retain")
    await _assert_no_tm(ctx, dev, since)

    n = dev.stats.register_sent
    await ctx.wait_until(lambda: dev.stats.register_sent >= n + 1, timeout=15, what="5분(→5s) 주기 REGISTER 재전송")
    await ctx.wait_until(lambda: dev.stats.register_ack_rx >= 2, timeout=15, what="재전송에도 서버가 매번 REGISTER_ACK 응답(§3.3)")

    r = await ctx.s.rest.ping(dev.uuid)
    ctx.check(r.status_code < 300, "PENDING 상태에서 ping 허용")
    seq = r.json()["seq"]
    async def acked() -> bool:
        c = await ctx.s.db.command(seq)
        return bool(c and c.get("acked_count") == 1)
    await ctx.wait_until(acked, timeout=FLUSH_WAIT, what="PENDING 에서도 PONG")
    # CONFIG 는 PENDING 에서 내려가면 안 된다(§3.8, §16.5).
    ctx.check_eq(dev.stats.config_set_rx, 0, "PENDING 동안 CONFIG_SET 없음")


# ── S3-02 ────────────────────────────────────────────────────────────────

@scenario("S3-02", "관리자 승인 → ACTIVE → CONFIG_SET → Telemetry 시작", phase=3, requires="approval_gate", timeout=120)
async def s3_02(ctx: Ctx) -> None:
    """§3.9.1-2. 승인하면 retain 이 ACTIVE 로 바뀌고, cv 가 다르면 CONFIG_SET → CONFIG_ACK, 이어서 TM 이 쌓인다."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    # cv 를 달리 해 두어 승인 직후 CONFIG_SET 이 나가게 한다.
    r = await ctx.s.rest.patch_config(dev.uuid, ti=300)
    ctx.check(r.status_code < 300, "PENDING 중 서버 의도값 변경(PATCH) 은 허용")
    await asyncio.sleep(1.0)
    ctx.check_eq(dev.stats.config_set_rx, 0, "PENDING 이라 CONFIG_SET 은 아직 안 내려감(§16.5)")

    await _set_state(ctx, dev.uuid, "ACTIVE", site="A-12")
    await _wait_gate(ctx, dev, "ACTIVE")
    retained = await ctx.s.broker.retained(dev.topic("config"))
    body = json.loads(retained) if retained else {}
    ctx.check(body.get("state") == "ACTIVE" and body.get("site") == "A-12", f"retain REGISTER_ACK ACTIVE+site: {body}")
    ctx.check("cv" not in body and "grp" not in body and "ti" not in body, "REGISTER_ACK 에 설정값 없음(§3.3)")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=15, what="승인 직후 CONFIG_SET → ACK OK")
    ctx.check_eq(dev.ti, 300, "ti=300 적용")
    async def flowing() -> bool:
        return await _tm_count(ctx, dev.uuid, since) >= 1
    await ctx.wait_until(flowing, timeout=FLUSH_WAIT, what="Telemetry 시작")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row["state"], "ACTIVE", "DB state=ACTIVE")
    ctx.check_eq(row.get("site"), "A-12", "site 저장")
    ctx.check(dev.gate.resend_needed is False, "ACTIVE 면 REGISTER 재전송 중지")
    ev = await ctx.s.db.events(dev.uuid, "STATE_CHANGE", since)
    ctx.check(len(ev) >= 1, "STATE_CHANGE 이벤트")


# ── S3-03 ────────────────────────────────────────────────────────────────

@scenario("S3-03", "단말 재부팅 → retain ACK 로 관리자 조작 없이 ACTIVE 복귀", phase=3, requires="approval_gate", timeout=120)
async def s3_03(ctx: Ctx) -> None:
    """§3.9.1-3. 재부팅한 단말은 승인 상태를 잊는다(§3.5). config 구독 직후 브로커가 retain 된
    REGISTER_ACK ACTIVE 를 내려주므로 서버 응답 전에도 ACTIVE 로 시작하고, 서버는 REGISTER 에 다시 응답한다."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    await _set_state(ctx, dev.uuid, "ACTIVE", site="B-1")
    await _wait_gate(ctx, dev, "ACTIVE")
    await ctx.wait_until(lambda: _tm_count(ctx, dev.uuid, since), timeout=FLUSH_WAIT, what="TM 시작")

    dev.inbox.clear()
    acks_before = dev.stats.register_ack_rx
    await dev.reboot(reconnect_after=0.5)
    ctx.check(dev.gate.state is None, "재부팅 직후 승인 상태 소거")
    await ctx.wait_until(lambda: dev.is_connected, timeout=30, what="재접속")
    await _wait_gate(ctx, dev, "ACTIVE", timeout=10)
    retained_acks = [p for (t, p, r) in dev.inbox if r and isinstance(p, dict) and p.get("type") == "REGISTER_ACK"]
    ctx.check(len(retained_acks) == 1 and retained_acks[0].get("state") == "ACTIVE", "구독 직후 retain ACK ACTIVE 수신")
    await ctx.wait_until(lambda: dev.stats.register_ack_rx >= acks_before + 2, timeout=15,
                         what="retain 1 + 서버 실시간 응답 1 (REGISTER 마다 응답, §3.3)")
    t_reboot = await ctx.s.db.now()
    async def resumed() -> bool:
        return await _tm_count(ctx, dev.uuid, t_reboot) >= 1
    await ctx.wait_until(resumed, timeout=FLUSH_WAIT, what="재부팅 뒤 TM 재개(관리자 조작 없음)")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row["state"], "ACTIVE", "DB state 유지")


# ── S3-04 ────────────────────────────────────────────────────────────────

@scenario("S3-04", "서버 프로세스 재시작 → 단말 REGISTER 재전송으로 복구", phase=3, requires="approval_gate", timeout=240, docker=True)
async def s3_04(ctx: Ctx) -> None:
    """§3.9.1-4. 백엔드를 재시작해도 단말은 연결을 유지한 채 REGISTER 를 재전송하지 않는다(ACTIVE 라서).
    서버는 DB 를 정본으로 복구하고 TM 수집을 이어간다. PENDING 단말은 재전송으로 다시 목록에 나타난다."""
    if not await ctx.s.docker.available():
        ctx.skip("docker compose 를 부를 수 없음")
    since = await ctx.s.db.now()
    active = await _gate_device(ctx)
    await _wait_gate(ctx, active, "PENDING")
    await _set_state(ctx, active.uuid, "ACTIVE")
    await _wait_gate(ctx, active, "ACTIVE")
    (pending,) = await make_devices(ctx, 1, offset=1, ti=600, approval_gate=True, time_scale=SCALE)
    await _wait_gate(ctx, pending, "PENDING")

    code, out = await ctx.s.docker.run("restart", "backend", timeout=120)
    ctx.check(code == 0, f"docker compose restart backend → {code} {out[-200:]}")
    await ctx.wait_until(ctx.s.rest.health_ok, timeout=90, interval=2, what="백엔드 재기동 /health ok")
    t_up = await ctx.s.db.now()
    n = pending.stats.register_ack_rx
    await ctx.wait_until(lambda: pending.stats.register_ack_rx > n, timeout=40,
                         what="PENDING 단말 재전송 REGISTER 에 재기동 서버가 응답")
    await active.send_tm_now()
    async def flowing() -> bool:
        return await _tm_count(ctx, active.uuid, t_up) >= 1
    await ctx.wait_until(flowing, timeout=FLUSH_WAIT, what="ACTIVE 단말 TM 수집 재개")
    ctx.check(active.is_connected and pending.is_connected, "단말 연결 유지(브로커는 그대로)")
    ctx.check_eq((await ctx.s.db.device(active.uuid))["state"], "ACTIVE", "DB state 보존")


# ── S3-05 ────────────────────────────────────────────────────────────────

@scenario("S3-05", "SUSPENDED → Telemetry 중지·연결 유지, 해제 시 재개", phase=3, requires="approval_gate", timeout=120)
async def s3_05(ctx: Ctx) -> None:
    """§3.9.1-5. SUSPENDED 는 연결을 끊지 않는다(해제 통로). REGISTER 재전송도 하지 않는다(§3.4)."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    await _set_state(ctx, dev.uuid, "ACTIVE")
    await _wait_gate(ctx, dev, "ACTIVE")
    await ctx.wait_until(lambda: _tm_count(ctx, dev.uuid, since), timeout=FLUSH_WAIT, what="TM 시작")

    await _set_state(ctx, dev.uuid, "SUSPENDED", reason="점검")
    await _wait_gate(ctx, dev, "SUSPENDED")
    await _assert_no_tm(ctx, dev, since)
    ctx.check(dev.is_connected, "SUSPENDED 에서도 연결 유지")
    reg = dev.stats.register_sent
    await asyncio.sleep(2.0)
    ctx.check_eq(dev.stats.register_sent, reg, "SUSPENDED 는 REGISTER 재전송 안 함")

    await _set_state(ctx, dev.uuid, "ACTIVE")
    await _wait_gate(ctx, dev, "ACTIVE")
    t_resume = await ctx.s.db.now()
    async def resumed() -> bool:
        return await _tm_count(ctx, dev.uuid, t_resume) >= 1
    await ctx.wait_until(resumed, timeout=FLUSH_WAIT, what="해제 뒤 TM 재개")


# ── S3-06 ────────────────────────────────────────────────────────────────

@scenario("S3-06", "retain REGISTER_ACK 가 비retain CONFIG_SET 에 덮이지 않음 (ADR-002)", phase=3, requires="approval_gate", timeout=120)
async def s3_06(ctx: Ctx) -> None:
    """같은 config topic 으로 CONFIG_SET(retain=0) 이 나간 뒤에도 브로커 보관본은 REGISTER_ACK ACTIVE 여야 한다.
    새로 구독하는 단말(재부팅)이 CONFIG_SET 을 보관본으로 받으면 안 된다."""
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    await _set_state(ctx, dev.uuid, "ACTIVE")
    await _wait_gate(ctx, dev, "ACTIVE")
    for ti in (300, 600, 900):
        r = await ctx.s.rest.patch_config(dev.uuid, ti=ti)
        ctx.check(r.status_code < 300, f"PATCH ti={ti}")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 3, timeout=20, what="CONFIG_SET 3회 수신·ACK")
    retained = await ctx.s.broker.retained(dev.topic("config"))
    body = json.loads(retained) if retained else {}
    ctx.check_eq(body.get("type"), "REGISTER_ACK", "보관본은 여전히 REGISTER_ACK")
    ctx.check_eq(body.get("state"), "ACTIVE", "보관본 state=ACTIVE")
    got = await ctx.s.broker.collect(dev.topic("config"), seconds=2)
    kinds = [(json.loads(p).get("type") if p else None, r) for (_, p, r) in got]
    ctx.check(all(k == "REGISTER_ACK" for k, r in kinds if r), f"retain 으로 온 것은 REGISTER_ACK 뿐: {kinds}")
    ctx.check(await ctx.s.broker.retained(dev.topic("cmd")) is None, "cmd topic 에 retain 없음")


# ── S3-07 ────────────────────────────────────────────────────────────────

@scenario("S3-07", "RETIRED 정리 — 빈 payload retain, 단말은 30분 주기 REGISTER 재개", phase=3, requires="approval_gate", timeout=150)
async def s3_07(ctx: Ctx) -> None:
    """§3.3/§3.4. RETIRED 통보를 받은 단말은 TM·재전송을 멈춘다. 서버가 빈 payload 를 retain 으로 보내면
    보관본이 지워지고 단말은 무응답 상태로 돌아가 30분(→30s) 주기 REGISTER 재전송을 재개한다."""
    since = await ctx.s.db.now()
    dev = await _gate_device(ctx)
    await _wait_gate(ctx, dev, "PENDING")
    await _set_state(ctx, dev.uuid, "RETIRED", reason="철거")
    await _wait_gate(ctx, dev, "RETIRED")
    reg = dev.stats.register_sent
    await asyncio.sleep(2.0)
    ctx.check_eq(dev.stats.register_sent, reg, "RETIRED 는 REGISTER 재전송 중지")
    await _assert_no_tm(ctx, dev, since, seconds=4)

    async def cleared() -> bool:
        return await ctx.s.broker.retained(dev.topic("config"), timeout=2) is None
    await ctx.wait_until(cleared, timeout=30, interval=2, what="서버가 빈 payload retain 으로 보관본 삭제")
    await ctx.wait_until(lambda: dev.gate.state is None, timeout=15, what="단말이 빈 payload 로 무응답 상태 복귀")
    ctx.check(abs(dev.gate.next_resend_delay() - 1800 / SCALE) < 1e-6, "재전송 주기 30분(→30s)")
    n = dev.stats.register_sent
    await ctx.wait_until(lambda: dev.stats.register_sent > n, timeout=1800 / SCALE + 15, interval=1,
                         what="30분(→30s) 뒤 REGISTER 재전송 재개")
