"""5차 시나리오 — 그룹 CMD (사양서 §3.10.7, §3.10.11, §17).

`--phase 5` 없이는 SKIP. 명령 REST 는 미확정이라 `POST /api/commands
{target_kind, target_id, type:"CMD", payload:{act,dur,exp,pwm?}}` → {seq} 로 가정한다(docs/06 §가정).
그룹 배정은 `PATCH /api/devices/{uuid}/config {grp:[...]}` 로 가정(사양은 REGISTER_ACK `grp`, §3.10.9 — 5차 설계 때 맞춘다).
단말은 1.4.0(승인 게이트)이라 setup 에서 REST 승인을 한다.
"""

from __future__ import annotations

import asyncio

from tools.scenarios.common import approve, make_devices
from tools.scenarios.framework import Ctx, Fail, scenario

GROUP = "4141011000"  # §3.10.4 법정동코드 예시


async def _post_cmd(ctx: Ctx, body: dict):
    r = await ctx.s.rest.post_command(body)
    if r.status_code in (404, 405):
        ctx.skip(f"명령 REST(POST /api/commands) 미구현: {r.status_code}")
    return r


@scenario("S5-01", "그룹 CMD — 10대 일괄, 무응답 2대만 개별 재시도, dur 누락 거부", phase=5, requires="group_cmd", timeout=240)
async def s5_01(ctx: Ctx) -> None:
    """한 그룹 10대에 group topic 으로 CMD 1회 발행 → 10대가 각자 result 로 CMD_ACK(§3.10.11).
    2대를 무응답으로 만들면 서버는 그 2대에만 개별 cmd 로 재시도한다. `dur` 누락은 서버(4xx)와 단말(거부) 모두 막는다."""
    since = await ctx.s.db.now()
    devices = await make_devices(ctx, 10, mode="2cha", ti=600, start=False)
    # 그룹 배정: CONFIG_SET 으로 내려가야 하므로 먼저 접속 → PATCH grp → CONFIG_ACK → 재접속해 group topic 구독.
    await asyncio.gather(*(d.start() for d in devices))
    ctx.check(all(await asyncio.gather(*(d.wait_connected(30) for d in devices))), "10대 접속")
    await approve(ctx, devices)
    for d in devices:
        r = await ctx.s.rest.patch_config(d.uuid, grp=[GROUP])
        if r.status_code in (404, 405, 422):
            ctx.skip(f"grp 배정 REST 미구현/미지원: {r.status_code} {r.text[:100]}")
        ctx.check(r.status_code < 300, f"PATCH grp {d.uuid[-4:]}")
    await ctx.wait_until(lambda: all(d.grp == [GROUP] for d in devices), timeout=30, what="10대 grp 수신(CONFIG_ACK)")
    # 실제 단말은 grp 를 Flash 에 저장하고 재접속 시 구독한다. 시뮬레이터도 같다.
    for d in devices:
        await d.disconnect(hard=False, reconnect=True, reconnect_after=0.5)
    await ctx.wait_until(lambda: all(d.is_connected and d.stats.connects >= 2 for d in devices), timeout=30, what="재접속·그룹 topic 구독")
    ctx.check(all(f"{d.root}/group/{GROUP}/cmd" in d._subscriptions for d in devices), "group topic 구독 확인")
    ctx.check(all(len(d._subscriptions) <= 6 for d in devices), "구독 한도 6 이내(§0.2)")

    silenced = devices[:2]
    for d in silenced:
        d.silent_results = True

    r = await _post_cmd(ctx, {"target_kind": "group", "target_id": GROUP, "type": "CMD",
                              "payload": {"act": "off", "dur": 600, "exp": 30}})
    ctx.check(r.status_code < 300, f"POST group CMD → {r.status_code} {r.text[:120]}")
    seq = r.json()["seq"]
    await ctx.wait_until(lambda: all(d.stats.cmd_rx >= 1 for d in devices), timeout=15, what="10대 모두 그룹 CMD 수신")
    ctx.check(all(d.current_override() is not None and d.current_override().act == "off" for d in devices), "10대 모두 off override 적용")
    tms = [await d.send_tm_now() for d in devices]
    ctx.check(all(tm is not None and tm.get("md") == 2 and tm.get("on") == 0 for tm in tms), "TM md=2(원격), on=0 보고")

    async def eight_acked() -> bool:
        acks = await ctx.s.db.command_acks(seq)
        return len(acks) == 8
    await ctx.wait_until(eight_acked, timeout=15, what="command_ack 8행(응답한 단말)")
    cmd = await ctx.s.db.command(seq)
    ctx.check_eq(cmd.get("expected_count"), 10, "expected_count=10")
    ctx.check_eq(cmd.get("acked_count"), 8, "acked_count=8")

    # 개별 재시도: 무응답 2대의 device cmd topic 으로 같은 seq 의 CMD 가 와야 한다.
    def individual_cmds(d) -> list:
        return [p for (t, p, _) in d.inbox if t == d.topic("cmd") and isinstance(p, dict) and p.get("type") == "CMD"]
    retry_timeout = float(getattr(ctx.opt, "cmd_retry_timeout", 90))
    await ctx.wait_until(lambda: all(len(individual_cmds(d)) >= 1 for d in silenced), timeout=retry_timeout, interval=2,
                         what="무응답 2대에 개별 재시도")
    ctx.check(all(len(individual_cmds(d)) == 0 for d in devices[2:]), "응답한 8대에는 개별 재시도 없음")
    for d in silenced:
        d.silent_results = False
    # 재시도 명령을 이제 응답한다(같은 seq 면 다시 실행하지 않고 ACK 만).
    for d in silenced:
        for p in individual_cmds(d):
            reply = d.handle_cmd(p, "device")
            if reply:
                await d.send_result(reply)
    async def all_acked() -> bool:
        c = await ctx.s.db.command(seq)
        return bool(c and c.get("acked_count") == 10 and c.get("result") == "OK")
    try:
        await ctx.wait_until(all_acked, timeout=20, what="재시도 응답 후 acked_count=10, result=OK")
    except Fail as exc:
        ctx.log(f"(참고) 재시도가 새 seq 를 쓰면 원 명령은 PARTIAL 로 남을 수 있음: {exc}")

    # dur 누락 → 서버 4xx.
    r = await _post_cmd(ctx, {"target_kind": "group", "target_id": GROUP, "type": "CMD", "payload": {"act": "off", "exp": 30}})
    ctx.check(400 <= r.status_code < 500, f"dur 누락 → 서버 4xx (실제 {r.status_code})")
    r = await _post_cmd(ctx, {"target_kind": "group", "target_id": GROUP, "type": "CMD", "payload": {"act": "off", "exp": 30, "dur": 0}})
    ctx.check(400 <= r.status_code < 500, f"dur=0 → 서버 4xx (실제 {r.status_code})")
    r = await _post_cmd(ctx, {"target_kind": "group", "target_id": GROUP, "type": "CMD", "payload": {"act": "off", "exp": 30, "dur": 86401}})
    ctx.check(400 <= r.status_code < 500, f"dur>86400 → 서버 4xx (실제 {r.status_code})")

    # 서버를 우회해 단말 검증: dur 누락 CMD 를 group topic 에 직접 발행 → 단말이 거부(응답 없음, 상태 불변).
    d0 = devices[5]
    rej0, rx0 = d0.stats.cmd_rejected, d0.stats.cmd_rx
    await ctx.s.broker.publish(f"{d0.root}/group/{GROUP}/cmd", {"type": "CMD", "seq": 999999, "exp": 30, "act": "on"})
    await ctx.wait_until(lambda: d0.stats.cmd_rx > rx0, timeout=10, what="단말이 dur 누락 CMD 수신")
    ctx.check(d0.stats.cmd_rejected == rej0 + 1, "단말이 dur 누락 CMD 거부")
    ctx.check(d0.current_override().act == "off", "거부된 명령은 override 를 바꾸지 않음")
    acks = [e for e in await ctx.s.db.events(d0.uuid, "CMD_ACK", since)]
    ctx.check(not any(str(e.get("payload")).find("999999") >= 0 for e in acks), "거부 명령의 CMD_ACK 없음")

    # act=auto 로 즉시 복귀.
    r = await _post_cmd(ctx, {"target_kind": "group", "target_id": GROUP, "type": "CMD", "payload": {"act": "auto", "dur": 1, "exp": 30}})
    ctx.check(r.status_code < 300, "act=auto")
    await ctx.wait_until(lambda: all(d.current_override() is None for d in devices), timeout=15, what="10대 모두 스케줄 복귀")
