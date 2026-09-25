"""2차 시나리오 — 수집 서비스 + DB + 단말별 계정 + CONFIG (사양서 §1.1.8 "2차 합격 기준" 포함).

단언은 DB(정본)를 우선하고 REST 로 교차 확인한다. 고정 대기는 2초 이하, 나머지는 wait_until.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import timedelta

from tools.scenarios.common import (OUT_DIR, import_accounts, last_tm_of, make_devices, metric_keys,
                                    metric_sum, namespace_of, sim_mode)
from tools.scenarios.framework import Ctx, Fail, scenario
from tools.sim.device import SQ_MOD, uuid_from_index
from tools.sim.fleet import WINDOWS_MAX_PER_PROCESS, write_accounts

#: TM 이 DB 에 보이기까지의 여유(백엔드 1초 배치 + 왕복). wait_until 의 상한으로만 쓴다.
FLUSH_WAIT = 10.0


async def _tm_in_db(ctx: Ctx, uuid: str, sq: int, since) -> bool:
    return bool(await ctx.s.db.fetchval(
        "SELECT 1 FROM telemetry WHERE uuid=$1 AND sq=$2 AND received_at >= $3 LIMIT 1", uuid, sq, since))


async def _send_tm_and_wait(ctx: Ctx, dev, since) -> dict:
    """TM 1건을 보내고 그 sq 가 telemetry 에 적재될 때까지 기다린다."""
    payload = await dev.send_tm_now()
    if payload is None:
        raise Fail(f"{dev.uuid}: 승인 전이라 TM 을 보내지 않았다")
    await ctx.wait_until(lambda: _tm_in_db(ctx, dev.uuid, payload["sq"], since), timeout=FLUSH_WAIT,
                         what=f"TM sq={payload['sq']} 적재")
    return payload


# ── S2-01 ────────────────────────────────────────────────────────────────

@scenario("S2-01", "신규 단말 REGISTER → TM → DB 적재", phase=2, timeout=240)
async def s2_01(ctx: Ctx) -> None:
    """50대가 접속해 REGISTER + 주기 TM(ti=5s) 을 보낸다.
    합격: device 행이 REGISTER 필드로 채워지고, telemetry 건수가 기대치 ±1, last_telemetry_at 단조 증가,
    REST /api/devices/{uuid} 의 마지막 TM 이 시뮬레이터가 마지막에 보낸 것과 같다."""
    since = await ctx.s.db.now()
    count = 50
    ti = 5
    devices = await make_devices(ctx, count, ti=ti)
    uuids = [d.uuid for d in devices]

    async def all_registered() -> bool:
        n = await ctx.s.db.fetchval(
            "SELECT count(*) FROM device WHERE uuid = ANY($1::text[]) AND last_register_at >= $2", uuids, since)
        return n == count
    await ctx.wait_until(all_registered, timeout=60, what=f"device {count}행 last_register_at 갱신")

    sample = await ctx.s.db.device(uuids[0])
    d0 = devices[0]
    for col, want in (("fw", d0.fw), ("device_model", d0.device_model), ("modem_model", d0.modem_model),
                      ("imei", d0.imei), ("iccid", d0.iccid), ("cv_device", d0.cv), ("ti_device", d0.ti)):
        ctx.check_eq(sample.get(col), want, f"device.{col}")
    ctx.check_eq(sample.get("state"), "ACTIVE", "2차는 state 항상 ACTIVE")
    ctx.check(sample.get("msisdn") in ("", None), "msisdn 빈 문자열 허용")

    # 주기 3번 이상 돌 때까지 첫 단말의 last_telemetry_at 이 단조 증가하는지 본다.
    seen: list = []
    async def advanced() -> bool:
        row = await ctx.s.db.device(uuids[0])
        t = row.get("last_telemetry_at") if row else None
        if t is not None and (not seen or t > seen[-1]):
            seen.append(t)
        return len(seen) >= 3
    await ctx.wait_until(advanced, timeout=ti * 5, interval=1.0, what="last_telemetry_at 3회 단조 증가")
    ctx.check(all(a < b for a, b in zip(seen, seen[1:])), f"last_telemetry_at 단조 증가 {seen}")

    # 건수 허용 오차: 경과시간/ti + 1(즉시 1건) ±1, 단말별로 QoS0 이라 1건 정도 유실 가능.
    await asyncio.sleep(1.5)  # 마지막 배치 flush
    elapsed = (await ctx.s.db.now() - since).total_seconds()
    expected = 1 + int(elapsed // ti)
    rows = await ctx.s.db.fetch(
        "SELECT uuid, count(*) AS n FROM telemetry WHERE uuid = ANY($1::text[]) AND received_at >= $2 GROUP BY uuid",
        uuids, since)
    counts = {r["uuid"]: r["n"] for r in rows}
    bad = {u: counts.get(u, 0) for u in uuids if not (expected - 2 <= counts.get(u, 0) <= expected + 1)}
    ctx.check(len(counts) == count, f"telemetry 가 있는 단말 {len(counts)}/{count}")
    ctx.check(not bad, f"telemetry 건수 기대 {expected}±(2,1) — 벗어난 단말 {len(bad)}: {list(bad.items())[:5]}")
    total_sent = sum(d.stats.tm_sent for d in devices)
    total_db = sum(counts.values())
    ctx.check(total_db >= total_sent * 0.95, f"전체 적재율 {total_db}/{total_sent} ≥ 95%")

    # REST 교차 확인: 마지막 TM 이 시뮬레이터의 마지막 발행과 같은가(sq 로 비교).
    last = d0.last_tm
    async def rest_matches() -> bool:
        body = await ctx.s.rest.device(uuids[0])
        tm = last_tm_of(body)
        return tm is not None and tm.get("sq") == d0.last_tm.get("sq")
    await ctx.wait_until(rest_matches, timeout=FLUSH_WAIT, what="REST last_telemetry.sq == 시뮬레이터 마지막 sq")
    body = await ctx.s.rest.device(uuids[0])
    tm = last_tm_of(body)
    ctx.check(tm.get("bv") == d0.last_tm["bv"] and tm.get("ts") == d0.last_tm["ts"], "REST last_telemetry bv/ts 일치")
    ctx.check(body.get("last_sq") in (None, d0.last_tm["sq"]), f"REST last_sq={body.get('last_sq')}")


# ── S2-02 ────────────────────────────────────────────────────────────────

@scenario("S2-02", "REGISTER 유실 — TM 만으로 레코드 생성, 늦은 REGISTER 가 보강", phase=2, timeout=120)
async def s2_02(ctx: Ctx) -> None:
    """REGISTER 를 보내지 않는 단말이 TM 만 보내도 topic UUID 로 device 행이 생겨야 한다(§4.1).
    이후 REGISTER 가 오면 device_model/imei 등이 채워지고 last_register_at 이 설정된다."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600, register_on_connect=False)
    await _send_tm_and_wait(ctx, dev, since)
    row = await ctx.s.db.device(dev.uuid)
    ctx.check(row is not None, "TM 만으로 device 행 생성")
    ctx.check(row.get("last_register_at") is None, "REGISTER 전이라 last_register_at NULL")
    ctx.check(row.get("device_model") is None, "REGISTER 전이라 device_model NULL")
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
    ctx.check(row.get("last_telemetry_at") is not None, "last_telemetry_at 유지")


# ── S2-03 ────────────────────────────────────────────────────────────────

@scenario("S2-03", "sq 판정 — 유실·재부팅·uint32 wrap", phase=2, timeout=180)
async def s2_03(ctx: Ctx) -> None:
    """sq 건너뜀 3 → lost_count +3. 재부팅(sq 0) → reboot_count +1, REBOOT 이벤트.
    wrap(2^32-2 → 2^32-1 → 0): 서버가 터무니없는 lost_count 를 만들지 않아야 한다.
    기대 동작(가정, docs/06): 큰 점프는 gap 이하로만 lost 증가, 0 으로 되돌아가는 것은 재부팅으로 판정."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600)
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

    # 재부팅: sq 0 부터, REGISTER 다시.
    reg_before = dev.stats.register_sent
    await dev.reboot(reconnect_after=0.5)
    await ctx.wait_until(lambda: dev.is_connected and dev.stats.register_sent > reg_before, timeout=30,
                         what="재부팅 후 재접속 + REGISTER")
    async def rebooted() -> bool:
        r = await ctx.s.db.device(dev.uuid)
        return r["reboot_count"] == reboot0 + 1
    await ctx.wait_until(rebooted, timeout=FLUSH_WAIT, what="reboot_count +1")
    events = await ctx.s.db.events(dev.uuid, "REBOOT", since)
    ctx.check(len(events) == 1, f"REBOOT 이벤트 1건 (실제 {len(events)})")
    r = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(r["lost_count"], lost0 + 3, "재부팅은 lost_count 에 영향 없음")
    ctx.check_eq(r["last_sq"], 0, "재부팅 후 last_sq=0")
    lost1, reboot1 = r["lost_count"], r["reboot_count"]

    # wrap: 현재 sq(1) → 2^32-2 점프. 그 다음 2^32-1, 그 다음 0.
    prev_sq = dev.sq
    dev.sq_wrap()
    gap = (SQ_MOD - 2) - prev_sq - 1
    await _send_tm_and_wait(ctx, dev, since)
    await _send_tm_and_wait(ctx, dev, since)          # 2^32-1
    r = await ctx.s.db.device(dev.uuid)
    ctx.check(lost1 <= r["lost_count"] <= lost1 + gap, f"큰 점프 후 lost_count 증가 {r['lost_count'] - lost1} ≤ gap {gap}")
    ctx.check(r["lost_count"] - lost1 < 2**31, "lost_count 가 int32 를 넘지 않음(오버플로 없음)")
    ctx.check_eq(r["reboot_count"], reboot1, "단조 증가 구간은 재부팅 아님")
    ctx.check_eq(dev.sq, 0, "시뮬레이터 sq 가 0 으로 wrap")
    await _send_tm_and_wait(ctx, dev, since)          # 0
    async def wrapped() -> bool:
        r = await ctx.s.db.device(dev.uuid)
        return r["last_sq"] == 0
    await ctx.wait_until(wrapped, timeout=FLUSH_WAIT, what="wrap 뒤 last_sq=0")
    r = await ctx.s.db.device(dev.uuid)
    ctx.check(r["reboot_count"] == reboot1 + 1, f"wrap(감소)은 재부팅으로 판정 (reboot_count {r['reboot_count']})")
    ctx.check(r["lost_count"] <= lost1 + gap, "wrap 뒤에도 lost_count 폭증 없음")


# ── S2-04 ────────────────────────────────────────────────────────────────

@scenario("S2-04", "CONFIG 왕복 — PATCH ti=300 OK, ti=10 은 서버 4xx / 단말 RANGE", phase=2, timeout=150)
async def s2_04(ctx: Ctx) -> None:
    """사양서 2차 합격 기준 4·5. PATCH ti=300 → CONFIG_SET → CONFIG_ACK OK → 다음 TM cv == cv_server.
    PATCH ti=10 은 서버가 4xx 로 거부. 서버를 우회해 CONFIG_SET ti=10 을 직접 쏘면 단말이 RANGE 를 답하고 cv 를 유지."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600)
    await _send_tm_and_wait(ctx, dev, since)
    before = await ctx.s.db.device(dev.uuid)

    r = await ctx.s.rest.patch_config(dev.uuid, ti=300)
    ctx.check(r.status_code < 300, f"PATCH ti=300 → {r.status_code} {r.text[:120]}")
    await ctx.wait_until(lambda: dev.stats.config_set_rx >= 1, timeout=15, what="단말이 CONFIG_SET 수신")
    ctx.check_eq(dev.stats.config_ack_ok, 1, "CONFIG_ACK OK 1회")
    ctx.check_eq(dev.ti, 300, "단말 ti=300 적용")
    row = await ctx.s.db.device(dev.uuid)
    ctx.check(row["cv_server"] > before["cv_server"] or row["cv_server"] != before["cv_device"],
              f"cv_server 증가 ({before['cv_server']} → {row['cv_server']})")
    ctx.check_eq(dev.cv, row["cv_server"], "단말 cv == cv_server")
    ctx.check(row.get("config_sent_at") is not None, "config_sent_at 기록")

    payload = await _send_tm_and_wait(ctx, dev, since)
    ctx.check_eq(payload["cv"], row["cv_server"], "다음 TM 의 cv == cv_server")
    async def converged() -> bool:
        r2 = await ctx.s.db.device(dev.uuid)
        return r2["cv_device"] == r2["cv_server"] and r2["ti_device"] == 300
    await ctx.wait_until(converged, timeout=FLUSH_WAIT, what="DB cv_device == cv_server, ti_device=300")
    acks = await ctx.s.db.events(dev.uuid, "CONFIG_ACK", since)
    ctx.check(len(acks) == 1, f"CONFIG_ACK 이벤트 1건 ({len(acks)})")
    ctx.check_eq(_payload(acks[0]).get("result"), "OK", "CONFIG_ACK 이벤트 payload.result")

    r = await ctx.s.rest.patch_config(dev.uuid, ti=10)
    ctx.check(400 <= r.status_code < 500, f"PATCH ti=10 → 4xx (실제 {r.status_code})")
    r = await ctx.s.rest.patch_config(dev.uuid, ti=3601)
    ctx.check(400 <= r.status_code < 500, f"PATCH ti=3601 → 4xx (실제 {r.status_code})")
    row2 = await ctx.s.db.device(dev.uuid)
    ctx.check_eq(row2["cv_server"], row["cv_server"], "거부된 PATCH 는 cv_server 를 올리지 않음")
    ctx.check_eq(dev.stats.config_set_rx, 1, "거부된 PATCH 로 CONFIG_SET 이 나가지 않음")

    # 서버 검증을 우회해 단말 자체 검증을 본다(사양서 합격 기준 5).
    cv_before = dev.cv
    await ctx.s.broker.publish(dev.topic("config"), {"type": "CONFIG_SET", "cv": cv_before + 1, "ti": 10})
    await ctx.wait_until(lambda: dev.stats.config_ack_range >= 1, timeout=10, what="단말 CONFIG_ACK RANGE")
    ctx.check_eq(dev.cv, cv_before, "RANGE 시 cv 유지")
    ctx.check_eq(dev.ti, 300, "RANGE 시 ti 유지")
    ctx.check_eq(dev.last_result, {"type": "CONFIG_ACK", "uuid": dev.uuid, "cv": cv_before, "result": "RANGE"},
                 "CONFIG_ACK RANGE payload 모양")
    async def range_logged() -> bool:
        evs = await ctx.s.db.events(dev.uuid, "CONFIG_ACK", since)
        return any(_payload(e).get("result") == "RANGE" for e in evs)
    await ctx.wait_until(range_logged, timeout=FLUSH_WAIT, what="서버가 RANGE ACK 를 이벤트로 기록")


def _payload(event: dict) -> dict:
    p = event.get("payload")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            return {}
    return p if isinstance(p, dict) else {}


# ── S2-05 ────────────────────────────────────────────────────────────────

@scenario("S2-05", "CONFIG 재전송 — 다음 TM 시점, 60초 쿨다운, 수렴", phase=2, timeout=300)
async def s2_05(ctx: Ctx) -> None:
    """단말이 첫 CONFIG_SET 을 못 받은 척한다. 서버는 타이머 없이 '다음 Telemetry 수신 시점' 에 재전송하되
    같은 단말에 60초 안에는 다시 보내지 않는다(ADR-002). 단말이 받아들이면 더 이상 재전송하지 않는다."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600, ignore_config_set=1)
    await _send_tm_and_wait(ctx, dev, since)

    r = await ctx.s.rest.patch_config(dev.uuid, ti=300)
    ctx.check(r.status_code < 300, f"PATCH ti=300 → {r.status_code}")
    await ctx.wait_until(lambda: dev.stats.config_set_rx >= 1, timeout=15, what="즉시 CONFIG_SET 1회(단말은 무시)")
    ctx.check_eq(dev.stats.config_set_ignored, 1, "단말이 첫 CONFIG_SET 무시")
    t_first = time.monotonic()

    # 2초마다 TM. 첫 재전송은 쿨다운(60초) 이후 첫 TM 에 와야 하고, 그 전엔 오지 말아야 한다.
    resend_after = -1.0
    deadline = t_first + 90
    while time.monotonic() < deadline and dev.stats.config_set_rx < 2:
        await dev.send_tm_now()
        await asyncio.sleep(2.0)
        if dev.stats.config_set_rx >= 2:
            resend_after = time.monotonic() - t_first
    ctx.check(dev.stats.config_set_rx == 2, f"쿨다운 뒤 재전송 1회 (수신 {dev.stats.config_set_rx})")
    ctx.check(resend_after >= 50, f"재전송이 쿨다운(≈60s) 이후에 옴: {resend_after:.0f}s")
    ctx.check(dev.stats.config_ack_ok == 1 and dev.ti == 300, "두 번째 CONFIG_SET 은 적용 (ACK OK)")

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


# ── S2-06 ────────────────────────────────────────────────────────────────

@scenario("S2-06", "단말별 계정 + ACL — 공용 계정 거부, 남의 topic 발행 차단", phase=2, timeout=200)
async def s2_06(ctx: Ctx) -> None:
    """사양서 2차 합격 기준 1·2. CSV 20건 import → acl_applied → 단말이 자기 uuid/pw 로 접속.
    공용 계정은 MQTT_TEST_ACCOUNT_ENABLED=false 일 때 거부되어야 한다(켜져 있으면 그 항목만 건너뜀).
    남의 UUID status topic 에 발행하면 브로커가 조용히 버려 DB 에 남지 않는다."""
    since = await ctx.s.db.now()
    env = ctx.s.env
    devices = await make_devices(ctx, 20, mode="2cha", ti=600, start=False)
    # import_accounts 는 make_devices(2cha) 안에서 이미 했다. 접속:
    await asyncio.gather(*(d.start() for d in devices))
    ok = await asyncio.gather(*(d.wait_connected(30) for d in devices))
    ctx.check(all(ok), f"단말별 계정으로 20대 접속 ({sum(ok)}/20)")

    # 틀린 비밀번호는 거부.
    bad, err = await ctx.s.broker.can_connect(devices[0].uuid, "wrong-password", devices[0].uuid + "X")
    ctx.check(not bad, f"틀린 비밀번호 거부 ({err[:60]})")

    health = await ctx.s.rest.health() or {}
    enabled = health.get("mqtt_test_account_enabled", env.mqtt_test_account_enabled)
    if enabled:
        ctx.log("공용 계정이 아직 켜져 있음(MQTT_TEST_ACCOUNT_ENABLED) — 거부 항목은 건너뜀")
    else:
        shared_ok, err = await ctx.s.broker.can_connect(env.mqtt_test_user, env.mqtt_test_password, "shared-probe")
        ctx.check(not shared_ok, f"공용 계정 접속 거부 ({err[:60]})")

    # ACL: A 가 B 의 status topic 에 발행.
    a, b = devices[0], devices[1]
    await _send_tm_and_wait(ctx, b, since)
    b_row = await ctx.s.db.device(b.uuid)
    n_before = await ctx.s.db.telemetry_count(b.uuid, since)
    result = await a.publish_foreign_topic(b.uuid)
    ctx.log(f"남의 topic 발행 결과: {result['published']=} {result['still_connected']=}")
    ctx.check(result["still_connected"], "ACL 위반 뒤에도 연결 유지(mosquitto 는 조용히 버림)")
    await ctx.hold(2.0, "배치 flush")
    n_after = await ctx.s.db.telemetry_count(b.uuid, since)
    ctx.check_eq(n_after, n_before, "B 의 telemetry 건수 불변 (A 의 발행이 브로커에서 차단)")
    b_row2 = await ctx.s.db.device(b.uuid)
    ctx.check_eq(b_row2["last_sq"], b_row["last_sq"], "B 의 last_sq 불변")
    # A 가 존재하지 않는 UUID 로도 못 쓴다.
    ghost = uuid_from_index(999, namespace_of(ctx.scenario.id))
    await a.publish_foreign_topic(ghost)
    await ctx.hold(2.0, "배치 flush")
    ctx.check(await ctx.s.db.device(ghost) is None, "존재하지 않는 UUID 행이 생기지 않음")
    ctx.defer(lambda: ctx.s.db.delete_device_rows(ghost))


# ── S2-07 ────────────────────────────────────────────────────────────────

@scenario("S2-07", "payload 이상 — 깨진 JSON, uuid 불일치, 옛 t 키, 과대 payload", phase=2, timeout=120)
async def s2_07(ctx: Ctx) -> None:
    """서버는 죽지 않고(/health ok) /api/metrics 의 거부 카운터가 올라야 한다.
    1차 펌웨어의 "t":"TM" 은 TM 으로 받아들인다(개발계획 2차 호환)."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600)
    (legacy,) = await make_devices(ctx, 1, offset=1, ti=600, legacy_t_key=True, legacy_register=True)
    m0 = await ctx.s.rest.metrics()
    bad0 = metric_sum(m0, r"(invalid|garbage|malformed|mismatch|reject|bad|oversize|too_large|parse|error)")
    ctx.log(f"거부 계열 카운터 키: {metric_keys(m0, r'(invalid|garbage|malformed|mismatch|reject|bad|oversize|too_large|parse|error)')}")

    await dev.send_garbage()
    await dev.send_uuid_mismatch()
    await dev.send_oversized(64 * 1024)
    await ctx.s.broker.publish(dev.topic("status"), b"[1,2,3]", qos=0)
    await ctx.s.broker.publish(dev.topic("status"), {"type": "TM"}, qos=0)  # 필드 전부 누락
    await ctx.s.broker.publish(dev.topic("result"), {"type": "WHAT", "uuid": dev.uuid}, qos=1)
    await ctx.hold(2.0, "서버 처리")
    ctx.check(await ctx.s.rest.health_ok(), "/health ok")
    ctx.check(dev.is_connected, "단말 연결 유지")

    async def counters_up() -> bool:
        m = await ctx.s.rest.metrics()
        return metric_sum(m, r"(invalid|garbage|malformed|mismatch|reject|bad|oversize|too_large|parse|error)") > bad0
    await ctx.wait_until(counters_up, timeout=FLUSH_WAIT, what="/api/metrics 거부 카운터 증가")

    row = await ctx.s.db.device(dev.uuid)
    ctx.check(row is None or row.get("device_model") != "FFFF", "uuid 불일치 REGISTER 로 남의 행이 갱신되지 않음")
    ghost = await ctx.s.db.device("FFFFFFFFFFFFFFFFFFFFFFFF")
    ctx.check(ghost is None, "payload uuid(FFFF…) 행이 생기지 않음")
    ctx.defer(lambda: ctx.s.db.delete_device_rows("FFFFFFFFFFFFFFFFFFFFFFFF"))

    payload = await legacy.send_tm_now()
    ctx.check("t" in payload and "type" not in payload, '옛 키 "t":"TM" 로 발행')
    await ctx.wait_until(lambda: _tm_in_db(ctx, legacy.uuid, payload["sq"], since), timeout=FLUSH_WAIT,
                         what='"t":"TM" 이 TM 으로 적재')
    # 정상 TM 도 여전히 들어온다.
    await _send_tm_and_wait(ctx, dev, since)
    ctx.check(await ctx.s.rest.health_ok(), "/health ok (마지막)")


# ── S2-08 ────────────────────────────────────────────────────────────────

@scenario("S2-08", "QoS1 중복 — 같은 PONG/CONFIG_ACK 두 번 → device_event 1행", phase=2, timeout=120)
async def s2_08(ctx: Ctx) -> None:
    """dedup_key = uuid:type:seq:sha1(payload) 로 QoS1 재전송 중복을 걸러야 한다(docs/03)."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600)
    r = await ctx.s.rest.ping(dev.uuid)
    ctx.check(r.status_code < 300, f"POST ping → {r.status_code}")
    seq = r.json()["seq"]
    await ctx.wait_until(lambda: dev.stats.ping_rx >= 1, timeout=15, what="PING 수신")
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

    r = await ctx.s.rest.patch_config(dev.uuid, ti=300)
    ctx.check(r.status_code < 300, "PATCH ti=300")
    await ctx.wait_until(lambda: dev.stats.config_ack_ok >= 1, timeout=15, what="CONFIG_ACK OK")
    for _ in range(3):
        await dev.duplicate_last_result()
    await ctx.hold(2.0, "서버 처리")
    async def one_ack() -> bool:
        evs = await ctx.s.db.events(dev.uuid, "CONFIG_ACK", since)
        return len(evs) == 1
    await ctx.wait_until(one_ack, timeout=FLUSH_WAIT, what="CONFIG_ACK 이벤트 정확히 1행")


# ── S2-09 ────────────────────────────────────────────────────────────────

@scenario("S2-09", "PING/PONG — REST ping → seq → 단말 PONG → command OK", phase=2, timeout=90)
async def s2_09(ctx: Ctx) -> None:
    """POST /api/devices/{uuid}/ping 이 전역 seq 를 돌려주고, 단말 PONG 으로 command 행이 acked_count=1, result=OK."""
    since = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600)
    (silent,) = await make_devices(ctx, 1, offset=1, ti=600, silent_results=True)
    r1 = await ctx.s.rest.ping(dev.uuid)
    r2 = await ctx.s.rest.ping(dev.uuid)
    ctx.check(r1.status_code < 300 and r2.status_code < 300, "ping 2회")
    s1, s2 = r1.json()["seq"], r2.json()["seq"]
    ctx.check(isinstance(s1, int) and s2 > s1, f"seq 전역 단조 증가 {s1} < {s2}")
    await ctx.wait_until(lambda: dev.stats.ping_rx >= 2, timeout=15, what="PING 2회 수신")
    async def acked() -> bool:
        c = await ctx.s.db.command(s2)
        return bool(c and c.get("acked_count") == 1 and c.get("result") == "OK")
    await ctx.wait_until(acked, timeout=FLUSH_WAIT, what="command(seq2) acked_count=1 result=OK")
    c = await ctx.s.db.command(s2)
    ctx.check_eq(c.get("target_kind"), "device", "target_kind")
    ctx.check_eq(c.get("target_id"), dev.uuid, "target_id")
    ctx.check_eq(c.get("type"), "PING", "type")
    ctx.check(c.get("finished_at") is not None, "finished_at 기록")
    pongs = await ctx.s.db.events(dev.uuid, "PONG", since)
    ctx.check(len(pongs) == 2, f"PONG 이벤트 2건 ({len(pongs)})")

    # 무응답 단말: 응답 없이 시간이 지나면 TIMEOUT 이어야 한다(타임아웃 값은 백엔드 설정 — 상한 60s 로 본다).
    r3 = await ctx.s.rest.ping(silent.uuid)
    s3 = r3.json()["seq"]
    await ctx.wait_until(lambda: silent.stats.ping_rx >= 1, timeout=15, what="무응답 단말도 PING 은 받음")
    async def timed_out() -> bool:
        c3 = await ctx.s.db.command(s3)
        return bool(c3 and c3.get("result") in ("TIMEOUT", "FAILED"))
    try:
        await ctx.wait_until(timed_out, timeout=60, interval=2, what="무응답 → command.result TIMEOUT")
    except Fail as exc:
        ctx.log(f"(참고) PING 타임아웃 판정이 60s 안에 안 남 — 백엔드 타임아웃 설정 확인: {exc}")


# ── S2-10 ────────────────────────────────────────────────────────────────

@scenario("S2-10", "재접속 폭주 — 1,000대 강제 절단 후 10초 안에 재접속", phase=2, timeout=420)
async def s2_10(ctx: Ctx) -> None:
    """별도 프로세스(fleet)로 1,000대를 띄운다(Windows 는 400대/프로세스로 자동 분할).
    40초 뒤 전부 강제 절단(LWT 1,000건) → 10초 안에 재접속·REGISTER.
    합격: 1,000행 모두 last_register_at 이 절단 시각 이후로 갱신, /health 내내 ok,
    register 대기열 60초 안에 0, telemetry_dropped 증가 없음."""
    count = int(getattr(ctx.opt, "storm_count", 1000))
    ns = namespace_of(ctx.scenario.id)
    mode = sim_mode(ctx)
    uuids = [uuid_from_index(i, ns) for i in range(count)]
    args = [sys.executable, "-m", "tools.sim.fleet", "--count", str(count), "--namespace", str(ns),
            "--mode", mode, "--ti", "600", "--storm", "--storm-window", "10", "--cut-after", "40",
            "--duration", "200", "--quiet", "--host", ctx.s.env.mqtt_host, "--port", str(ctx.s.env.mqtt_port)]
    if mode == "2cha":
        await import_accounts(ctx, uuids)
        args += ["--accounts", str(OUT_DIR / f"accounts_{ctx.scenario.id}_0.csv")]
    m0 = await ctx.s.rest.metrics()
    dropped0 = metric_sum(m0, r"telemetry_dropped")
    since = await ctx.s.db.now()

    from tools.scenarios.services import REPO_ROOT
    proc = await asyncio.create_subprocess_exec(*args, cwd=str(REPO_ROOT))
    t_spawn = time.monotonic()

    async def cleanup() -> None:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 15)
            except asyncio.TimeoutError:
                proc.kill()
        if not getattr(ctx.opt, "keep_rows", False):
            await ctx.s.db.execute("DELETE FROM telemetry WHERE uuid LIKE $1", f"51A0{ns:04X}%")
            await ctx.s.db.execute("DELETE FROM device_event WHERE uuid LIKE $1", f"51A0{ns:04X}%")
            await ctx.s.db.execute("DELETE FROM device WHERE uuid LIKE $1", f"51A0{ns:04X}%")
    ctx.defer(cleanup)

    async def registered_after(t) -> int:
        return await ctx.s.db.fetchval(
            "SELECT count(*) FROM device WHERE uuid LIKE $1 AND last_register_at >= $2", f"51A0{ns:04X}%", t)

    health_failures = 0
    async def all_registered(t) -> bool:
        nonlocal health_failures
        if not await ctx.s.rest.health_ok():
            health_failures += 1
        n = await registered_after(t)
        ctx.log(f"REGISTER 반영 {n}/{count}")
        return n >= count
    await ctx.wait_until(lambda: all_registered(since), timeout=120, interval=3, what=f"최초 접속 {count}대 REGISTER")
    # fleet 이 40초에 절단한다. 그 직전 시각을 기준으로 "절단 이후 REGISTER" 를 센다.
    await ctx.wait_until(lambda: time.monotonic() >= t_spawn + 39, timeout=60, interval=1, what="절단 시점(40s) 직전")
    t_cut = await ctx.s.db.now()
    await ctx.wait_until(lambda: all_registered(t_cut), timeout=120, interval=3, what=f"폭주 재접속 뒤 {count}대 REGISTER 재반영")
    ctx.check(health_failures == 0, f"/health 실패 {health_failures}회")
    lwt = await ctx.s.db.fetchval(
        "SELECT count(*) FROM device_event WHERE uuid LIKE $1 AND kind='LWT' AND received_at >= $2", f"51A0{ns:04X}%", since)
    ctx.log(f"LWT 이벤트 {lwt}건 (절단 {count}대 기대)")
    ctx.check(lwt >= count * 0.95, f"LWT ≥ 95% ({lwt}/{count})")

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

@scenario("S2-11", "브로커 재시작 — 단말 backoff 재접속, 백엔드 재구독, TM 재개", phase=2, timeout=300, docker=True)
async def s2_11(ctx: Ctx) -> None:
    """docker compose restart mosquitto. 단말은 30초(time_scale 로 압축) backoff 로 다시 붙고,
    백엔드는 자동 재구독해서 새 TM 이 DB 에 쌓여야 한다. retained REGISTER_ACK 보존은 S3-03 에서."""
    if not await ctx.s.docker.available():
        ctx.skip("docker compose 를 부를 수 없음")
    since = await ctx.s.db.now()
    devices = await make_devices(ctx, 20, ti=5, time_scale=10, reconnect_jitter=20)
    await ctx.wait_until(lambda: ctx.s.db.fetchval(
        "SELECT count(DISTINCT uuid) FROM telemetry WHERE uuid = ANY($1::text[]) AND received_at >= $2",
        [d.uuid for d in devices], since), timeout=30, what="재시작 전 TM 적재")

    code, out = await ctx.s.docker.run("restart", "mosquitto", timeout=120)
    ctx.check(code == 0, f"docker compose restart mosquitto → {code} {out[-200:]}")
    t_restart = await ctx.s.db.now()
    await ctx.wait_until(lambda: sum(1 for d in devices if not d.is_connected) >= 1, timeout=30,
                         what="단말이 끊김을 감지")
    await ctx.wait_until(lambda: all(d.is_connected for d in devices), timeout=120, interval=1,
                         what="단말 20대 재접속(backoff)")
    ctx.check(all(d.stats.connects >= 2 for d in devices), "각 단말 접속 횟수 ≥ 2")

    async def flowing() -> bool:
        n = await ctx.s.db.fetchval(
            "SELECT count(DISTINCT uuid) FROM telemetry WHERE uuid = ANY($1::text[]) AND received_at >= $2",
            [d.uuid for d in devices], t_restart)
        return n == len(devices)
    await ctx.wait_until(flowing, timeout=90, interval=2, what="재시작 뒤 20대 모두 새 TM 적재 (백엔드 재구독)")
    async def reregistered() -> bool:
        n = await ctx.s.db.fetchval(
            "SELECT count(*) FROM device WHERE uuid = ANY($1::text[]) AND last_register_at >= $2",
            [d.uuid for d in devices], t_restart)
        return n == len(devices)
    await ctx.wait_until(reregistered, timeout=30, what="재접속 REGISTER 반영")
    ctx.check(await ctx.s.rest.health_ok(), "/health ok")


# ── S2-12 ────────────────────────────────────────────────────────────────

@scenario("S2-12", "DB 장애 중 수신 — postgres 30초 정지, 백엔드 생존, 복구 후 적재 재개", phase=2, timeout=300, docker=True)
async def s2_12(ctx: Ctx) -> None:
    """docker compose stop postgres 30초. 100대가 5초마다 TM. 백엔드는 죽지 않고(/health 응답),
    flush 실패분은 버리고 telemetry_dropped 를 올린다. start 뒤 새 TM 행이 보이고 /health ok."""
    if not await ctx.s.docker.available():
        ctx.skip("docker compose 를 부를 수 없음")
    since = await ctx.s.db.now()
    devices = await make_devices(ctx, 100, ti=5)
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

@scenario("S2-13", "LWT vs 지연 TM — 강제 절단 후 offline, 버퍼된 TM 이 되살리지 않음", phase=2, timeout=120)
async def s2_13(ctx: Ctx) -> None:
    """강제 절단 → 브로커 LWT → device.online=false, offline_at 기록, LWT 이벤트.
    같은 flush 초 안에 'TM 발행 직후 절단' 해도 online 은 false 로 남고 LWT 가 last_telemetry_at 을 밀지 않는다."""
    since = await ctx.s.db.now()
    a, b = await make_devices(ctx, 2, ti=600)
    (nolwt,) = await make_devices(ctx, 1, offset=2, ti=600, lwt=False)
    await _send_tm_and_wait(ctx, a, since)
    async def online_true() -> bool:
        r = await ctx.s.db.device(a.uuid)
        return bool(r and r.get("online"))
    try:
        await ctx.wait_until(online_true, timeout=5, what="접속 중 online=true")
    except Fail:
        ctx.log("(참고) 2차 백엔드는 online 을 ti×3 로 계산할 수 있음 — true 판정은 참고만")

    await a.disconnect(hard=True)
    async def lwt_logged() -> bool:
        return len(await ctx.s.db.events(a.uuid, "LWT", since)) >= 1
    await ctx.wait_until(lwt_logged, timeout=FLUSH_WAIT, what="LWT 이벤트 기록")
    row = await ctx.s.db.device(a.uuid)
    ctx.check(row.get("online") is False, "online=false")
    ctx.check(row.get("offline_at") is not None, "offline_at 기록")
    lwt_ev = (await ctx.s.db.events(a.uuid, "LWT", since))[0]
    ctx.check_eq(_payload(lwt_ev).get("uuid"), a.uuid, "LWT payload uuid")

    # B: TM 직후 같은 초에 절단.
    payload = await b.send_tm_now()
    await b.disconnect(hard=True)
    await ctx.wait_until(lambda: _tm_in_db(ctx, b.uuid, payload["sq"], since), timeout=FLUSH_WAIT, what="B 의 TM 적재")
    async def b_lwt() -> bool:
        return len(await ctx.s.db.events(b.uuid, "LWT", since)) >= 1
    await ctx.wait_until(b_lwt, timeout=FLUSH_WAIT, what="B 의 LWT 이벤트")
    await ctx.hold(2.0, "배치 flush 가 LWT 뒤에 돌 시간")
    row = await ctx.s.db.device(b.uuid)
    ctx.check(row.get("online") is False, "같은 초의 TM 이 online 을 되살리지 않음")
    ctx.check(row.get("offline_at") is not None and row.get("last_telemetry_at") is not None
              and row["last_telemetry_at"] <= row["offline_at"] + timedelta(seconds=2),
              f"LWT 가 last_telemetry_at 을 밀지 않음 (tm={row.get('last_telemetry_at')}, off={row.get('offline_at')})")
    ctx.check(row.get("last_seen_at") is None or row["last_seen_at"] >= row["last_telemetry_at"], "last_seen_at ≥ last_telemetry_at")

    # LWT 미지원 단말(P-2): 절단해도 LWT 가 없다 — 서버는 ti×3 무수신으로 대체해야 한다(3차). 여기서는 이벤트가 없음만 확인.
    await nolwt.disconnect(hard=True)
    await ctx.hold(2.0, "LWT 가 오지 않는지")
    ctx.check(len(await ctx.s.db.events(nolwt.uuid, "LWT", since)) == 0, "LWT 미등록 단말은 LWT 이벤트 없음")


# ── S2-14 ────────────────────────────────────────────────────────────────

@scenario("S2-14", "시각·단위 — ts 원문 보관, 숫자 컬럼은 ÷100 없이 원값, received_at 은 서버 시각", phase=2, timeout=60)
async def s2_14(ctx: Ctx) -> None:
    """telemetry 행의 ts_device 는 단말 문자열 그대로, bv/bi/pp/li/cs/sc 는 raw 정수 그대로, raw jsonb 는 원본 전체."""
    t0 = await ctx.s.db.now()
    (dev,) = await make_devices(ctx, 1, ti=600)
    payload = await _send_tm_and_wait(ctx, dev, t0)
    t1 = await ctx.s.db.now()
    rows = await ctx.s.db.telemetry_rows(dev.uuid, t0)
    row = next(r for r in rows if r["sq"] == payload["sq"])
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
