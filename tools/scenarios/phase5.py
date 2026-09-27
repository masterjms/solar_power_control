"""5차 시나리오 — 법정동 트리·COMMAND·COMMAND_ACK·자동 재시도·권한 (ADR-005, docs/05 "5차 API",
사양서 §3.9.3, §3.10.7 ~ §3.10.11, §17).

`--phase 5` 없이는 SKIP(`group_cmd` 플래그). REST 는 docs/05 5차 API 그대로:
  · 지역: `POST /api/regions/from-address` 개발 직접 입력 `{sido,sigungu,dong,bjd_code,lat,lon}`(APP_ENV=dev)
  · 배정: `PATCH /api/devices/{u}/state {"state":"ACTIVE","node_id":..}` / `PATCH …/config {"node_id":..|null}`
  · 명령: `POST /api/commands/preview`, `POST /api/commands`, `GET /api/commands[/{seq}]`, `POST /api/commands/{seq}/retry`
  · 사용자: 헤더 `X-Remote-User` — 최고관리자 `SCENARIO_ADMIN_USER`(기본 admin), 관리자 `SCENARIO_OPERATOR_USER`(기본 operator)
명령·지역 REST 가 404/405 면 SKIP(백엔드 5차 미배포).

시나리오마다 가짜 법정동코드를 쓴다: `99` + 단계 2자리 + 번호 2자리 + 시군구 2자리 + 말단 2자리(예 S5-02 → `9905020101`).
실제 시·도 코드는 11~50 이라 겹치지 않는다. 지역 이름은 전부 `시험S5-xx` 로 시작해 정리 때 이름으로 찾아 지운다.

단말은 펌웨어 2026-09-27-3 모델(`tools/sim/device.py`): `all/cmd` 상시 구독, ACTIVE + grp 일 때만 `group/<grp>/cmd`.
모든 COMMAND 에 COMMAND_ACK(OK/LOCAL/EXPIRED/BAD/STATE)를 `device/<u>/result` 로 보낸다.
현장 우선 개정(2026-09-27-5 이후): LOCAL 은 버림, 현장 시작 = 원격 취소, 원격 OK·만료·현장 취소 뒤 2초에 추가 Telemetry.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from tools.scenarios.common import approve, make_devices, payload_of, send_tm_and_wait
from tools.scenarios.framework import Ctx, Fail, scenario
from tools.scenarios.services import Sniffer, error_code
from tools.sim.device import CMD_TS_FORMAT, KST, SimDevice, kst_ts_sec

#: ACK 가 와야 하는 상한(초). 로컬 스택에서 서버 발행 → 단말 → result 는 1초 안팎.
ACK_WAIT = 5.0
#: 서버 명령 상태(command_target / GET /api/commands/{seq}) 반영 상한.
SERVER_WAIT = 10.0
#: 재시도를 유도하려고 Telemetry 를 보내는 간격(초). 서버는 "단말이 뭔가 보낸 직후" 재시도한다(ADR-005).
KICK_EVERY = 3.0
#: 백엔드 COMMAND_RETRY_MIN_SEC / COMMAND_MAX_ATTEMPTS 와 같게(docs/06 §1 환경 변수).
RETRY_MIN_SEC = float(os.environ.get("COMMAND_RETRY_MIN_SEC") or 20)
MAX_ATTEMPTS = int(os.environ.get("COMMAND_MAX_ATTEMPTS") or 3)
#: 이 이름으로 시작하는 404 코드는 "기능 없음"이 아니라 정상 응답이다.
_REAL_404 = {"DEVICE_NOT_FOUND", "REGION_NOT_FOUND", "COMMAND_NOT_FOUND"}


# ── 공통 도우미 ──────────────────────────────────────────────────────────

def _admin(ctx: Ctx) -> str:
    return ctx.s.env.admin_user


def _operator(ctx: Ctx) -> str:
    return ctx.s.env.operator_user


def _root(ctx: Ctx) -> str:
    return ctx.s.env.topic_root


def _describe(r) -> str:
    return f"{r.status_code} {error_code(r) or r.text[:200]}"


def _skip_if_missing(ctx: Ctx, r, what: str) -> None:
    if r.status_code in (404, 405) and error_code(r) not in _REAL_404:
        ctx.skip(f"5차 REST 미구현/미배포 — {what}: {_describe(r)}")


def _jsonb(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


@dataclass
class Leaf:
    id: int
    name: str
    bjd_code: str

    @property
    def grp(self) -> str:
        return self.bjd_code + "00"

    def topic(self, root: str) -> str:
        return f"{root}/group/{self.grp}/cmd"


@dataclass
class Sigungu:
    id: int
    name: str
    leaves: list[Leaf] = field(default_factory=list)


@dataclass
class Tree:
    sido_id: int
    sigungus: list[Sigungu]

    @property
    def leaves(self) -> list[Leaf]:
        return [leaf for sg in self.sigungus for leaf in sg.leaves]


def _name_prefix(ctx: Ctx) -> str:
    return f"시험{ctx.scenario.id}"


def fake_bjd_code(scenario_id: str, sigungu_no: int, leaf_no: int) -> str:
    """`99` + 단계(2) + 번호(2) + 시군구(2) + 말단(2) = 10자리."""
    phase, num = scenario_id[1:].split("-")
    code = f"99{int(phase):02d}{int(num):02d}{sigungu_no:02d}{leaf_no:02d}"
    if len(code) != 10 or not code.isdigit():
        raise ValueError(code)
    return code


async def _purge_regions(ctx: Ctx) -> None:
    """이 시나리오 이름으로 시작하는 지역을 DB 에서 지운다(이전 실행 잔여·정리 실패 대비). 배정된 단말은 해제."""
    like = _name_prefix(ctx) + "%"
    try:
        ids = [r["id"] for r in await ctx.s.db.fetch("SELECT id FROM region WHERE name LIKE $1", like)]
        if not ids:
            return
        await ctx.s.db.execute("UPDATE device SET node_id = NULL, grp = NULL WHERE node_id = ANY($1::int[])", ids)
        for level in ("dong", "sigungu", "sido"):
            await ctx.s.db.execute("DELETE FROM region WHERE id = ANY($1::int[]) AND level = $2", ids, level)
    except Exception as exc:  # noqa: BLE001 — 5차 테이블이 없으면 여기서는 조용히(REST 가 SKIP 을 낸다)
        ctx.log(f"지역 정리 생략: {type(exc).__name__}: {exc}")


async def make_tree(ctx: Ctx, layout: list[int]) -> Tree:
    """시도 1 > 시군구 len(layout) > 시군구 j 마다 layout[j] 개 말단. 최고관리자 REST(개발 직접 입력)로 만든다."""
    await _purge_regions(ctx)
    prefix = _name_prefix(ctx)
    sido = f"{prefix}도"
    created: list[tuple[str, dict]] = []   # (기대 bjd_code, 응답)
    for j, n in enumerate(layout, start=1):
        for k in range(1, n + 1):
            code = fake_bjd_code(ctx.scenario.id, j, k)
            body = {"sido": sido, "sigungu": f"{prefix}시{j}구", "dong": f"{prefix}-{j}-{k}동", "bjd_code": code,
                    "lat": 37.36 + j * 0.01, "lon": 126.93 + k * 0.01}
            r = await ctx.s.rest.region_from_address(body, _admin(ctx))
            _skip_if_missing(ctx, r, "POST /api/regions/from-address")
            if r.status_code not in (200, 201):
                raise Fail(f"말단 생성 실패 {body['dong']}: {_describe(r)} — APP_ENV=dev·SUPER_ADMIN_USERS 확인")
            created.append((code, r.json()))

    async def cleanup() -> None:
        # 단말 정리가 뒤에 돌 수 있으므로(LIFO) REST 삭제가 409 면 DB 로 마무리한다.
        rr = await ctx.s.rest.regions(_admin(ctx))
        if rr.status_code == 200:
            order = {"dong": 0, "sigungu": 1, "sido": 2}
            mine = [x for x in rr.json() if str(x.get("name", "")).startswith(prefix)]
            for x in sorted(mine, key=lambda x: order.get(x.get("level"), 3)):
                await ctx.s.rest.delete_region(int(x["id"]), _admin(ctx))
        await _purge_regions(ctx)
    ctx.defer(cleanup)

    r = await ctx.s.rest.regions(_admin(ctx))
    ctx.check(r.status_code == 200, f"GET /api/regions → {r.status_code}")
    rows = {int(x["id"]): x for x in r.json()}
    sido_id: int | None = None
    by_sg: dict[int, Sigungu] = {}
    for code, leaf in created:
        row = rows.get(int(leaf["id"]), leaf)
        ctx.check(row.get("level") == "dong" and row.get("bjd_code") == code,
                  f"말단 {row.get('name')}: level dong·bjd_code {code} ({row.get('level')} {row.get('bjd_code')})")
        ctx.check(row.get("grp") in (None, code + "00"), f"grp = bjd_code+'00': {row.get('grp')}")
        sg_row = rows[int(row["parent_id"])]
        ctx.check(sg_row.get("level") == "sigungu", f"상위 = 시군구: {sg_row.get('level')}")
        sido_id = int(sg_row["parent_id"])
        by_sg.setdefault(int(sg_row["id"]), Sigungu(int(sg_row["id"]), sg_row["name"])).leaves.append(
            Leaf(int(row["id"]), row["name"], code))
    assert sido_id is not None
    ctx.check(rows[sido_id].get("level") == "sido" and rows[sido_id].get("parent_id") is None, "최상위 = 시도")
    sgs = sorted(by_sg.values(), key=lambda s: s.name)
    ctx.log(f"트리: {sido} > " + ", ".join(f"{sg.name}[{', '.join(x.bjd_code for x in sg.leaves)}]" for sg in sgs))
    return Tree(sido_id, sgs)


async def wait_group(ctx: Ctx, devices: list[SimDevice], leaf: Leaf | None, timeout: float = 15.0) -> None:
    """단말이 retain REGISTER_ACK 의 grp 로 그룹 topic 을 구독(leaf=None 이면 해제)할 때까지. 그룹 구독은 최대 1개."""
    want = leaf.topic(_root(ctx)) if leaf else None
    await ctx.wait_until(lambda: all(d.group_topic == want for d in devices), timeout=timeout,
                         what=f"{len(devices)}대 그룹 구독 = {want}")
    for d in devices:
        groups = [t for t in d.subscriptions if "/group/" in t]
        if groups != ([want] if want else []) or len(d.subscriptions) > 6:
            raise Fail(f"{d.uuid[-4:]} 구독 {d.subscriptions}")


async def devices_in(ctx: Ctx, count: int, leaf: Leaf | None, *, offset: int, **flags: Any) -> list[SimDevice]:
    """시나리오 네임스페이스 단말 count 대를 띄우고 말단 leaf 로 승인(ACTIVE+node_id). leaf=None 이면 승인하지 않는다(PENDING)."""
    flags.setdefault("ti", 600)
    devs = await make_devices(ctx, count, offset=offset, **flags)
    if leaf is not None:
        await approve(ctx, devs, node_id=leaf.id)
        await wait_group(ctx, devs, leaf)
        await settle(ctx, devs)
    return devs


async def settle(ctx: Ctx, devs: list[SimDevice], timeout: float = 20.0) -> None:
    """승인 직후 단말이 보낸 TM 이 DB 에 반영(last_sq)될 때까지 기다린다.

    서버는 sq 캐시(메모리)로 재부팅을 판정하고, 재부팅이면 flush 때 override 필드를 지운다. 같은 UUID 로 시나리오를
    다시 돌리면 첫 TM(sq 0)이 "재부팅"으로 판정되는데, 그 flush(1초 배치)가 명령 ACK 뒤에 끝나면 방금 기록한 override 를
    지워 버린다(2026-09-27 실행에서 S5-01 이 이것으로 실패 — docs/06 §5 B11). 명령 전에 TM 이 전부 적재되게 한다.
    """
    # CONFIG_SET 왕복은 기다리지 않는다 — 같은 UUID 를 방금 지우고 다시 만들면 서버의 CONFIG 쿨다운(메모리, uuid 기준)
    # 때문에 CONFIG_SET 이 60초 늦게 올 수 있다(2026-09-27 실행, S5-03 연속 실행). 재부팅 판정만 흡수하면 된다.
    await ctx.wait_until(lambda: all(d.last_tm is not None for d in devs), timeout=timeout,
                         what=f"{len(devs)}대 승인 뒤 첫 TM")
    await asyncio.sleep(0.3)  # CONFIG 를 곧바로 받은 단말의 echo TM 이 나갈 틈

    async def flushed() -> bool:
        rows = await ctx.s.db.fetch("SELECT uuid, last_sq FROM device WHERE uuid = ANY($1::text[])",
                                    [d.uuid for d in devs])
        got = {r["uuid"]: r["last_sq"] for r in rows}
        return all(got.get(d.uuid) == d.last_tm["sq"] for d in devs)
    await ctx.wait_until(flushed, timeout=timeout, what=f"{len(devs)}대 마지막 TM 적재(last_sq)")


async def published(ctx: Ctx, sn: Sniffer, seq: int, at_least: int, timeout: float = 5.0) -> list:
    """sniffer 가 이 seq 의 COMMAND 를 at_least 건 볼 때까지 기다린 뒤(구독 전달이 단말보다 늦을 수 있다)
    늦은 중복이 더 오는지 0.5초 더 보고 전부 돌려준다. 정확한 횟수 판정은 호출자가 한다."""
    await ctx.wait_until(lambda: len(sn.find(type_="COMMAND", seq=seq, retained=None)) >= at_least,
                         timeout=timeout, interval=0.1, what=f"sniffer 가 seq {seq} 발행 {at_least}건 관찰")
    await asyncio.sleep(0.5)
    return sn.find(type_="COMMAND", seq=seq, retained=None)


async def sniff(ctx: Ctx, name: str) -> Sniffer:
    sn = await ctx.s.broker.sniffer(name=f"{ctx.scenario.id}-{name}")
    ctx.defer(sn.stop)
    return sn


def target(kind: str, ident: Any = None) -> dict[str, Any]:
    return {"kind": kind, "id": None if ident is None else str(ident)}


def body_of(tgt: dict, act: str, **fields: Any) -> dict[str, Any]:
    return {"target": tgt, "act": act, **fields}


async def preview(ctx: Ctx, body: dict, *, user: str | None = None) -> dict:
    r = await ctx.s.rest.command_preview(body, user or _admin(ctx))
    _skip_if_missing(ctx, r, "POST /api/commands/preview")
    if r.status_code != 200:
        raise Fail(f"preview {body} → {_describe(r)}")
    return r.json()


async def send(ctx: Ctx, tgt: dict, act: str, *, user: str | None = None, **fields: Any) -> dict:
    """`POST /api/commands` → 201 응답 JSON. 404/405 면 SKIP(백엔드 5차 없음)."""
    body = body_of(tgt, act, **fields)
    r = await ctx.s.rest.post_command(body, user or _admin(ctx))
    _skip_if_missing(ctx, r, "POST /api/commands")
    if r.status_code != 201:
        raise Fail(f"POST /api/commands {body} → {_describe(r)} (기대 201)")
    out = r.json()
    ctx.log(f"명령 seq={out.get('seq')} {tgt['kind']}:{tgt['id']} act={act} topics={out.get('topics')}")
    return out


def check_payload(ctx: Ctx, p: dict, *, seq: int, act: str, ch: list[int] | None = None, dur: int | None = None,
                  exp: int = 30, pwm: list[int] | None = None) -> None:
    """§3.10.7 공통 포맷: type COMMAND, seq, ts(YYMMDDThhmmss KST, 지금 ±5초), exp, act, ch, (pwm), (dur)."""
    ctx.check_eq(p.get("type"), "COMMAND", "payload type")
    ctx.check_eq(p.get("seq"), seq, "payload seq")
    ts = p.get("ts")
    ctx.check(isinstance(ts, str) and len(ts) == 13, f"ts 13자 YYMMDDThhmmss: {ts!r}")
    sent = datetime.strptime(ts, CMD_TS_FORMAT).replace(tzinfo=KST)
    skew = abs((datetime.now(tz=KST) - sent).total_seconds())
    ctx.check(skew <= 5, f"ts = 지금 KST ±5s (차 {skew:.1f}s)")
    ctx.check_eq(p.get("exp"), exp, "payload exp")
    ctx.check_eq(p.get("act"), act, "payload act")
    ctx.check_eq(p.get("ch"), ch or [1, 2], "payload ch")
    if act == "auto":
        ctx.check("dur" not in p, f"auto 에는 dur 없음: {p}")
    else:
        ctx.check_eq(p.get("dur"), dur, "payload dur")
    if act == "pwm":
        ctx.check_eq(p.get("pwm"), pwm, "payload pwm")
    else:
        ctx.check("pwm" not in p, f"act≠pwm 에는 pwm 없음: {p}")
    extra = set(p) - {"type", "seq", "ts", "exp", "act", "ch", "pwm", "dur"}
    ctx.check(not extra, f"사양 밖 키 없음: {extra}")
    ctx.check(len(json.dumps(p, separators=(",", ":"))) < 384, "384B 미만(AT 버퍼)")


def _acked(d: SimDevice, seq: int, result: str) -> bool:
    return any(a["result"] == result for a in d.acks_for(seq))


async def wait_acks(ctx: Ctx, devices: list[SimDevice], seq: int, result: str = "OK", *,
                    timeout: float = ACK_WAIT, what: str = "") -> None:
    await ctx.wait_until(lambda: all(_acked(d, seq, result) for d in devices), timeout=timeout, interval=0.2,
                         what=what or f"{len(devices)}대 COMMAND_ACK {result} (seq {seq})")


async def detail(ctx: Ctx, seq: int) -> dict:
    r = await ctx.s.rest.command(seq, _admin(ctx))
    _skip_if_missing(ctx, r, "GET /api/commands/{seq}")
    if r.status_code != 200:
        raise Fail(f"GET /api/commands/{seq} → {_describe(r)}")
    return r.json()


async def wait_counts(ctx: Ctx, seq: int, want: dict[str, int], *, timeout: float = SERVER_WAIT,
                      finished: bool | None = None) -> dict:
    """GET /api/commands/{seq} 의 counts 가 want 와 같아질 때까지(없는 키는 0). finished=True/False 면 finished_at 도."""
    last: dict = {}

    async def ok() -> bool:
        nonlocal last
        last = await detail(ctx, seq)
        counts = last.get("counts") or {}
        if any(int(counts.get(k, 0) or 0) != v for k, v in want.items()):
            return False
        if finished is True and not last.get("finished_at"):
            return False
        if finished is False and last.get("finished_at"):
            return False
        return True
    try:
        await ctx.wait_until(ok, timeout=timeout, what=f"seq {seq} counts {want}" + (" + 종료" if finished else ""))
    except Fail:
        raise Fail(f"seq {seq} counts {want}: 마지막 {last.get('counts')} finished_at={last.get('finished_at')} "
                   f"result={last.get('result')}") from None
    return last


def targets_by_uuid(d: dict) -> dict[str, dict]:
    return {t["uuid"]: t for t in d.get("targets") or []}


async def target_status(ctx: Ctx, seq: int, uuid: str) -> str | None:
    return targets_by_uuid(await detail(ctx, seq)).get(uuid, {}).get("status")


async def kick_until(ctx: Ctx, devices: list[SimDevice], pred: Callable[[], bool], *, timeout: float,
                     what: str) -> None:
    """devices 가 KICK_EVERY 초마다 Telemetry 를 보내게 하면서 pred 가 참이 될 때까지(서버 자동 재시도 유도)."""
    started = time.monotonic()
    while not pred():
        if time.monotonic() - started >= timeout:
            raise Fail(f"{what}: {timeout:.0f}s 안에 충족되지 않음")
        for d in devices:
            await d.send_tm_now()
        await asyncio.sleep(KICK_EVERY)
    ctx.log(f"{what} ({time.monotonic() - started:.0f}s)")


def device_cmds(d: SimDevice, seq: int) -> list[dict]:
    """개별 topic 으로 받은 이 seq 의 COMMAND(그룹·전체 명령에서는 = 재시도)."""
    return [p for (_, p) in d.commands_received(seq=seq, topic=d.topic("cmd"))]


async def tm_now(ctx: Ctx, d: SimDevice) -> dict:
    """TM 1건 보내고 적재까지 기다린다(서버 last_telemetry.md 갱신)."""
    return await send_tm_and_wait(ctx, d, await ctx.s.db.now())


async def rest_device(ctx: Ctx, uuid: str) -> dict:
    dev = await ctx.s.rest.device(uuid)
    if dev is None:
        raise Fail(f"GET /api/devices/{uuid} 404")
    return dev


async def retained_ack(ctx: Ctx, d: SimDevice) -> dict | None:
    raw = await ctx.s.broker.retained(d.topic("config"))
    return json.loads(raw) if raw else None


def raw_seq() -> int:
    """러너가 서버 계정으로 직접 발행하는 COMMAND 의 seq — 서버 DB 시퀀스와 안 겹치게 큰 값."""
    return 3_000_000_000 + random.randrange(1, 900_000_000)


# ── S5-01 개별 명령 ──────────────────────────────────────────────────────

@scenario("S5-01", "개별 명령 — preview·payload(ts/exp/ch/dur)·ACK OK·md=2·remote_active·auto 해제", phase=5,
          requires="group_cmd", timeout=150)
async def s5_01(ctx: Ctx) -> None:
    """개별 `device/<u>/cmd` 명령(§3.10.1). preview(expected 1·not_active 0·topic 1·payload 에 seq 없음) → 201 응답 payload 가
    `type COMMAND`·`ts` 13자(지금 KST ±5s)·`exp 30`·`ch [1,2]`·`dur`, 발행은 개별 topic 1회(retain 없음), created_by = X-Remote-User →
    단말 ACK OK 5s 안 → 명령 종료 result OK(counts OK 1, target attempts 1) → 다음 TM `md 2` → REST `remote_active true`·
    `remote_remaining_sec ≈ dur`·`override_act/level/seq` → pwm ch[1] 50% 는 pw1 = 기준×0.5, seq 단조 증가 →
    `auto`(payload 에 dur 없음) → 단말 슬롯 없음·md 0 → `remote_active false`, override 필드 NULL(개별 auto = 전부 해제)."""
    tree = await make_tree(ctx, [1])
    leaf = tree.leaves[0]
    (d,) = await devices_in(ctx, 1, leaf, offset=0)
    sn = await sniff(ctx, "a")
    tgt = target("device", d.uuid)

    pv = await preview(ctx, body_of(tgt, "on", ch=[1, 2], dur=120))
    ctx.check_eq(pv.get("expected"), 1, "preview expected")
    ctx.check_eq(pv.get("not_active"), 0, "preview not_active")
    ctx.check_eq(pv.get("topics"), [d.topic("cmd")], "preview topics = 개별 topic")
    ctx.check_eq(pv.get("dur"), 120, "preview dur")
    ctx.check(all(k in pv for k in ("online", "offline", "low_battery")), f"preview 모양: {sorted(pv)}")
    ppl = pv.get("payload") or {}
    ctx.check(ppl.get("type") == "COMMAND" and "seq" not in ppl, f"preview payload COMMAND·seq 없음: {ppl}")

    t0 = time.monotonic()
    body = await send(ctx, tgt, "on", ch=[1, 2], dur=120)
    seq = body["seq"]
    ctx.check(isinstance(seq, int) and seq > 0, f"seq 정수: {seq}")
    check_payload(ctx, body["payload"], seq=seq, act="on", ch=[1, 2], dur=120)
    ctx.check_eq(body.get("topics"), [d.topic("cmd")], "응답 topics")
    ctx.check_eq(body.get("expected"), 1, "응답 expected")
    ctx.check_eq(body.get("created_by"), _admin(ctx), "created_by = X-Remote-User")
    ctx.check_eq((body.get("target") or {}).get("kind"), "device", "응답 target.kind")

    await wait_acks(ctx, [d], seq, "OK")
    ctx.check(time.monotonic() - t0 < ACK_WAIT + 1, "POST → 단말 ACK 5s 안")
    got = d.commands_received(seq=seq)
    ctx.check(len(got) == 1 and got[0][0] == d.topic("cmd"), f"단말은 개별 topic 으로 1회 수신: {[t for t, _ in got]}")
    ctx.check_eq(got[0][1], body["payload"], "단말이 받은 payload = 응답 payload")
    pubs = await published(ctx, sn, seq, 1)
    ctx.check(len(pubs) == 1 and pubs[0][1] == d.topic("cmd") and not pubs[0][3],
              f"브로커 발행 1회·개별 topic·retain 없음: {[(p[1], p[3]) for p in pubs]}")
    ctx.check_eq(d.effective_act, "on", "단말 적용(개별 슬롯 on)")

    det = await wait_counts(ctx, seq, {"OK": 1, "pending": 0}, finished=True)
    ctx.check_eq(det.get("result"), "OK", "command result")
    ctx.check_eq(det.get("expected_count"), 1, "expected_count")
    t = targets_by_uuid(det).get(d.uuid) or {}
    ctx.check(t.get("status") == "OK" and t.get("attempts") == 1 and bool(t.get("acked_at")), f"target: {t}")
    ct = await ctx.s.db.command_targets(seq)
    ctx.check(ct.get(d.uuid, {}).get("status") == "OK", f"DB command_target OK: {ct.get(d.uuid)}")

    tm = await tm_now(ctx, d)
    ctx.check_eq(tm["md"], 2, "TM md=2(원격)")
    ctx.check(tm["pw"][:2] == list(d.model.pwm[:2]) and tm["on"] == 1, f"on → pw1/pw2 = 설치 기준: {tm['pw']}")

    async def remote_on() -> dict | None:
        dev = await rest_device(ctx, d.uuid)
        return dev if dev.get("remote_active") is True else None
    dev = await ctx.wait_until(remote_on, timeout=SERVER_WAIT, what="REST remote_active=true")
    rem = dev.get("remote_remaining_sec")
    ctx.check(isinstance(rem, (int, float)) and 90 <= rem <= 121, f"remote_remaining_sec ≈ 120: {rem}")
    ctx.check(dev.get("override_act") == "on" and dev.get("override_level") == "device"
              and dev.get("override_seq") == seq and bool(dev.get("override_until")),
              f"override_*: {dev.get('override_act')}/{dev.get('override_level')}/{dev.get('override_seq')}/"
              f"{dev.get('override_until')}")
    row = await ctx.s.db.device(d.uuid)
    ctx.check_eq(_jsonb(row.get("last_telemetry")).get("md"), 2, "DB last_telemetry.md")

    body2 = await send(ctx, tgt, "pwm", ch=[1], pwm=[50], dur=120)
    check_payload(ctx, body2["payload"], seq=body2["seq"], act="pwm", ch=[1], dur=120, pwm=[50])
    ctx.check(body2["seq"] > seq, "seq 전역 단조 증가")
    await wait_acks(ctx, [d], body2["seq"], "OK")
    tm = await tm_now(ctx, d)
    ctx.check_eq(tm["pw"][0], round(d.model.pwm[0] * 50 / 100), "pwm 50% → pw1 = 기준×0.5")

    body3 = await send(ctx, tgt, "auto")
    check_payload(ctx, body3["payload"], seq=body3["seq"], act="auto", ch=[1, 2])
    await wait_acks(ctx, [d], body3["seq"], "OK")
    ctx.check(d.slot_dump() == {} and d.md == 0, f"auto → 스케줄 복귀: {d.slot_dump()}")
    tm = await tm_now(ctx, d)
    ctx.check_eq(tm["md"], 0, "TM md=0")

    async def remote_off() -> dict | None:
        dv = await rest_device(ctx, d.uuid)
        return dv if dv.get("remote_active") is False else None
    dev = await ctx.wait_until(remote_off, timeout=SERVER_WAIT, what="REST remote_active=false")
    ctx.check(dev.get("override_until") is None and dev.get("override_act") is None,
              f"개별 auto → override 필드 NULL: {dev.get('override_act')} {dev.get('override_until')}")


# ── S5-02 그룹(말단) 명령 ────────────────────────────────────────────────

@scenario("S5-02", "그룹(말단) 명령 — 그룹 topic 1회, 10대 ACK, 무응답 2대만 개별 재시도(같은 seq·새 ts)", phase=5,
          requires="group_cmd", timeout=240)
async def s5_02(ctx: Ctx) -> None:
    """말단 A 10대 + 말단 B 3대. A 에 명령 → `group/<A>00/cmd` **정확히 1회**(서버 계정 `iotlight/#` 로 셈), 응답한 8대 OK,
    B 3대는 아무것도 못 받음. A 중 2대는 `silent_commands=1`(첫 명령 통째로 무시) → counts OK 8·pending 2·미종료 →
    그 단말이 Telemetry 를 보낸 직후 서버가 **개별 topic 으로 같은 seq·더 새 ts** 재발송(COMMAND_RETRY_MIN_SEC 뒤) → OK.
    그룹 재발행 없음, 응답한 8대에는 개별 재발송 없음, attempts 는 재시도 2대만 2, 최종 counts OK 10·result OK."""
    tree = await make_tree(ctx, [2])
    a, b = tree.leaves
    devs_a = await devices_in(ctx, 10, a, offset=0)
    devs_b = await devices_in(ctx, 3, b, offset=10)
    silent, answering = devs_a[:2], devs_a[2:]
    for d in silent:
        d.silent_commands = 1
    sn = await sniff(ctx, "a")
    tgt = target("node", a.id)

    pv = await preview(ctx, body_of(tgt, "off", dur=300))
    ctx.check_eq(pv.get("expected"), 10, "preview expected = 말단 A ACTIVE 10")
    ctx.check_eq(pv.get("topics"), [a.topic(_root(ctx))], "preview topics = 그룹 A 1개")

    body = await send(ctx, tgt, "off", dur=300)
    seq, first = body["seq"], body["payload"]
    check_payload(ctx, first, seq=seq, act="off", dur=300)
    ctx.check_eq(body.get("topics"), [a.topic(_root(ctx))], "응답 topics")
    ctx.check_eq(body.get("expected"), 10, "응답 expected")

    await wait_acks(ctx, answering, seq, "OK", timeout=ACK_WAIT * 2)
    ctx.check(all(d.stats.cmd_silenced == 1 and not d.acks_for(seq) for d in silent), "무응답 2대는 ACK 없음")
    ctx.check(all(d.effective_act == "off" and "group" in d.slot_dump() for d in answering), "8대 그룹 슬롯 off")
    await ctx.hold(1.0, "B 쪽 늦은 도착이 없는지")
    ctx.check(all(not d.commands_received(seq=seq) and not d.acks_for(seq) for d in devs_b), "말단 B 3대는 수신 없음")
    grp_pubs = await published(ctx, sn, seq, 1)
    ctx.check(len(grp_pubs) == 1 and grp_pubs[0][1] == a.topic(_root(ctx)),
              f"발행 = 그룹 A topic 정확히 1회: {[p[1] for p in grp_pubs]}")
    await wait_counts(ctx, seq, {"OK": 8, "pending": 2}, finished=False)

    await kick_until(ctx, silent, lambda: all(device_cmds(d, seq) for d in silent),
                     timeout=max(getattr(ctx.opt, "cmd_retry_timeout", 90), RETRY_MIN_SEC + 30),
                     what="무응답 2대에 개별 재시도(단말 TM 직후)")
    for d in silent:
        retry = device_cmds(d, seq)[0]
        ctx.check(retry["seq"] == seq and retry["ts"] > first["ts"],
                  f"{d.uuid[-4:]} 재시도 = 같은 seq {retry['seq']}·새 ts {retry['ts']} > {first['ts']}")
        ctx.check(retry.get("act") == "off" and retry.get("dur") == 300, "재시도 payload 는 원래 명령 그대로")
    await wait_acks(ctx, silent, seq, "OK")
    det = await wait_counts(ctx, seq, {"OK": 10, "pending": 0}, finished=True)
    ctx.check_eq(det.get("result"), "OK", "재시도 뒤 result OK")
    tb = targets_by_uuid(det)
    ctx.check(set(tb) == {d.uuid for d in devs_a}, "대상 스냅숏 = 말단 A 10대")
    ctx.check(all(tb[d.uuid]["attempts"] == 2 for d in silent),
              f"재시도 2대 attempts=2: {[tb[d.uuid]['attempts'] for d in silent]}")
    ctx.check(all(tb[d.uuid]["attempts"] == 1 for d in answering), "응답한 8대 attempts=1")

    pubs = sn.find(type_="COMMAND", seq=seq)
    groups = [p for p in pubs if "/group/" in p[1]]
    indiv = sorted({p[1] for p in pubs if "/device/" in p[1]})
    ctx.check(len(groups) == 1, f"그룹 재발행 없음(그룹 발행 {len(groups)}회)")
    ctx.check_eq(indiv, sorted(d.topic("cmd") for d in silent), "개별 재발송 = 무응답 2대 topic 만")
    ctx.check(all(not device_cmds(d, seq) for d in answering), "응답한 8대에는 개별 재발송 없음")


# ── S5-03 상위 노드 명령 ─────────────────────────────────────────────────

@scenario("S5-03", "상위 노드(시군구) 명령 — 말단마다 그룹 topic 1회, 같은 seq, 집계", phase=5, requires="group_cmd",
          timeout=180)
async def s5_03(ctx: Ctx) -> None:
    """시군구1(말단 3개: 3·3·2대) + 시군구2(말단 1개: 2대). GET /api/regions device_count/active_count 가 트리로 합산
    (말단 3·3·2, 시군구1 8, 시도 10) → 시군구1 에 pwm 명령 → 응답 topics = 말단 3개 그룹 topic, 브로커에 **말단마다 1회씩
    3회**·전부 같은 payload(같은 seq·ts), 8대 OK·pwm 적용, 시군구2 2대 수신 없음 → counts OK 8·expected 8·result OK·
    target_kind node, 이력 `?node_id=`(counts 포함)와 `?uuid=` 에 나옴."""
    tree = await make_tree(ctx, [3, 1])
    sg1, sg2 = tree.sigungus
    devs: list[SimDevice] = []
    offset = 0
    for leaf, n in zip(sg1.leaves, (3, 3, 2)):
        devs += await devices_in(ctx, n, leaf, offset=offset)
        offset += n
    others = await devices_in(ctx, 2, sg2.leaves[0], offset=offset)

    r = await ctx.s.rest.regions(_admin(ctx))
    rows = {int(x["id"]): x for x in r.json()}
    ctx.check(rows[sg1.id].get("device_count") == 8 and rows[sg1.id].get("active_count") == 8,
              f"시군구1 device_count/active_count 8: {rows[sg1.id]}")
    ctx.check_eq(rows[tree.sido_id].get("device_count"), 10, "시도 합산 device_count")
    ctx.check_eq([rows[x.id].get("device_count") for x in sg1.leaves], [3, 3, 2], "말단별 device_count")

    sn = await sniff(ctx, "a")
    tgt = target("node", sg1.id)
    want_topics = sorted(x.topic(_root(ctx)) for x in sg1.leaves)
    pv = await preview(ctx, body_of(tgt, "pwm", ch=[1, 2], pwm=[30, 60], dur=300))
    ctx.check_eq(pv.get("expected"), 8, "preview expected")
    ctx.check_eq(sorted(pv.get("topics") or []), want_topics, "preview topics = 말단 3개")

    body = await send(ctx, tgt, "pwm", ch=[1, 2], pwm=[30, 60], dur=300)
    seq = body["seq"]
    check_payload(ctx, body["payload"], seq=seq, act="pwm", ch=[1, 2], dur=300, pwm=[30, 60])
    ctx.check_eq(sorted(body.get("topics") or []), want_topics, "응답 topics = 말단 3개")
    await wait_acks(ctx, devs, seq, "OK", timeout=ACK_WAIT * 2)
    pubs = await published(ctx, sn, seq, len(want_topics))
    ctx.check_eq(sorted(p[1] for p in pubs), want_topics, "브로커 발행 = 말단마다 1회(3회)")
    ctx.check(len({json.dumps(p[2], sort_keys=True) for p in pubs}) == 1, "3회 모두 같은 payload(같은 seq·ts)")
    ctx.check(all(d.channel_output()[1] == 30 and d.channel_output()[2] == 60 for d in devs), "8대 pwm 30/60 적용")
    ctx.check(all(not d.commands_received(seq=seq) for d in others), "시군구2 단말은 수신 없음")

    det = await wait_counts(ctx, seq, {"OK": 8, "pending": 0}, finished=True)
    ctx.check_eq(det.get("expected_count"), 8, "expected_count")
    ctx.check_eq(det.get("result"), "OK", "result")
    ctx.check_eq(det.get("target_kind"), "node", "target_kind")
    lst = await ctx.s.rest.commands(_admin(ctx), node_id=sg1.id, limit=20)
    items = {c["seq"]: c for c in lst.json()} if lst.status_code == 200 else {}
    ctx.check(seq in items and int((items[seq].get("counts") or {}).get("OK", 0)) == 8,
              f"이력 ?node_id= 에 seq·counts OK 8: {items.get(seq)}")
    lst = await ctx.s.rest.commands(_admin(ctx), uuid=devs[0].uuid, limit=20)
    ctx.check(lst.status_code == 200 and any(c["seq"] == seq for c in lst.json()), "이력 ?uuid= 에 seq")


# ── S5-04 권한·전체 명령 ─────────────────────────────────────────────────

@scenario("S5-04", "권한 — 전체 명령은 최고관리자만, all/cmd 1회, PENDING/SUSPENDED 는 STATE·대상 제외", phase=5,
          requires="group_cmd", timeout=180)
async def s5_04(ctx: Ctx) -> None:
    """`GET /api/me` 역할(admin → super_admin, operator → admin) → 관리자는 전체 명령·지역 추가 403 `FORBIDDEN`(발행 없음),
    말단 명령은 201(`created_by=operator`) → 최고관리자 preview `topics=[all/cmd]`·`not_active ≥ 2` → 전체 명령 201,
    `all/cmd` 정확히 1회 → ACTIVE 2대 OK(전체 슬롯), PENDING·SUSPENDED 단말은 `all/cmd` 를 받지만 `STATE` 로 답하고
    대상 스냅숏에 없다 → /health ok. 정리로 전체 auto 를 보낸다."""
    tree = await make_tree(ctx, [1])
    leaf = tree.leaves[0]
    active = await devices_in(ctx, 2, leaf, offset=0)
    (pending,) = await devices_in(ctx, 1, None, offset=2)
    (suspended,) = await devices_in(ctx, 1, leaf, offset=3)
    all_topic = f"{_root(ctx)}/all/cmd"
    await ctx.wait_until(lambda: pending.state == "PENDING", timeout=15, what="PENDING 단말 REGISTER_ACK PENDING")
    r = await ctx.s.rest.patch_state(suspended.uuid, "SUSPENDED", reason="S5-04")
    ctx.check(r.status_code < 300, f"SUSPENDED: {_describe(r)}")
    await ctx.wait_until(lambda: suspended.state == "SUSPENDED" and suspended.group_topic is None, timeout=15,
                         what="SUSPENDED 수신·그룹 구독 해제")
    ctx.check(all(all_topic in d.subscriptions for d in (*active, pending, suspended)), "모든 단말 all/cmd 구독(§3.10.10)")

    r = await ctx.s.rest.me(_admin(ctx))
    _skip_if_missing(ctx, r, "GET /api/me")
    ctx.check(r.status_code == 200 and r.json().get("role") == "super_admin" and r.json().get("user") == _admin(ctx),
              f"/api/me admin → super_admin: {r.text[:120]}")
    r = await ctx.s.rest.me(_operator(ctx))
    ctx.check(r.status_code == 200 and r.json().get("role") == "admin", f"/api/me operator → admin: {r.text[:120]}")

    sn = await sniff(ctx, "a")
    t_forbidden = time.monotonic()
    r = await ctx.s.rest.post_command(body_of(target("all"), "off", dur=60), _operator(ctx))
    _skip_if_missing(ctx, r, "POST /api/commands")
    ctx.check(r.status_code == 403 and error_code(r) == "FORBIDDEN", f"관리자 전체 명령 → 403 FORBIDDEN: {_describe(r)}")
    p = _name_prefix(ctx)
    r = await ctx.s.rest.region_from_address({"sido": f"{p}도", "sigungu": f"{p}시9구", "dong": f"{p}-9-9동",
                                              "bjd_code": fake_bjd_code(ctx.scenario.id, 9, 9)}, _operator(ctx))
    ctx.check(r.status_code == 403 and error_code(r) == "FORBIDDEN", f"관리자 지역 추가 → 403: {_describe(r)}")

    op = await send(ctx, target("node", leaf.id), "auto", user=_operator(ctx))
    ctx.check_eq(op.get("created_by"), _operator(ctx), "관리자 말단 명령 201·created_by")
    await wait_acks(ctx, active, op["seq"], "OK")
    await ctx.hold(1.0, "403 요청이 발행되지 않았는지 확인 전")
    ctx.check(not sn.find(type_="COMMAND", topic=all_topic, since=t_forbidden), "관리자 전체 명령(403)은 발행 없음")

    pv = await preview(ctx, body_of(target("all"), "off", dur=60))
    ctx.check_eq(pv.get("topics"), [all_topic], "preview topics = all/cmd")
    ctx.check(int(pv.get("expected", 0)) >= 2 and int(pv.get("not_active", 0)) >= 2,
              f"preview expected ≥ 2·not_active ≥ 2(PENDING·SUSPENDED): {pv.get('expected')}/{pv.get('not_active')}")

    async def restore() -> None:
        await ctx.s.rest.post_command(body_of(target("all"), "auto"), _admin(ctx))
    ctx.defer(restore)
    body = await send(ctx, target("all"), "off", dur=60)
    seq = body["seq"]
    check_payload(ctx, body["payload"], seq=seq, act="off", dur=60)
    ctx.check_eq(body.get("topics"), [all_topic], "응답 topics = all/cmd")
    ctx.check_eq((body.get("target") or {}).get("kind"), "all", "target.kind")
    await wait_acks(ctx, active, seq, "OK")
    await wait_acks(ctx, [pending, suspended], seq, "STATE", what="PENDING·SUSPENDED 는 STATE")
    ctx.check(all("all" in d.slot_dump() for d in active) and not pending.slot_dump() and not suspended.slot_dump(),
              "ACTIVE 만 전체 슬롯 적용")
    pubs = await published(ctx, sn, seq, 1)
    ctx.check(len(pubs) == 1 and pubs[0][1] == all_topic, f"all/cmd 정확히 1회: {[x[1] for x in pubs]}")

    async def both_ok() -> bool:
        tb = targets_by_uuid(await detail(ctx, seq))
        return all(tb.get(d.uuid, {}).get("status") == "OK" for d in active)
    await ctx.wait_until(both_ok, timeout=SERVER_WAIT, what="ACTIVE 2대 target OK")
    tb = targets_by_uuid(await detail(ctx, seq))
    ctx.check(pending.uuid not in tb and suspended.uuid not in tb, "PENDING·SUSPENDED 는 대상 스냅숏에 없음")
    ctx.check(await ctx.s.rest.health_ok(), "/health ok (대상 밖 STATE ACK 수신 뒤)")


# ── S5-05 우선순위·만료 ─────────────────────────────────────────────────

@scenario("S5-05", "우선순위·만료 — 개별>그룹, 개별 만료 → 그룹 복귀, 개별 auto, 재부팅 소거", phase=5,
          requires="group_cmd", timeout=180)
async def s5_05(ctx: Ctx) -> None:
    """그룹 off(dur 90) → 개별 on(dur 20) 이 이긴다(TM pw1 = 기준, 서버 override_level device) → 20초 뒤 개별 만료 →
    그룹 off 로 복귀(md 2 유지, pw1 0) → 개별 on 다시 → 개별 auto → 그룹 off(개별 auto 는 그 계층만, 가정 B4) →
    그룹 off 새 명령(서버 override_level group) → `reboot()` → 슬롯 전부 소거·md 0, 재접속 retain 으로 그룹 재구독,
    서버는 sq 감소(REBOOT)로 override 필드 NULL·remote_active false·REBOOT 이벤트 1건."""
    tree = await make_tree(ctx, [1])
    leaf = tree.leaves[0]
    (d,) = await devices_in(ctx, 1, leaf, offset=0)
    base = d.model.pwm[0]
    g = await send(ctx, target("node", leaf.id), "off", dur=90)
    await wait_acks(ctx, [d], g["seq"], "OK")
    ctx.check_eq(d.effective_act, "off", "그룹 off 적용")
    tm = await tm_now(ctx, d)
    ctx.check(tm["md"] == 2 and tm["pw"][0] == 0, f"TM md 2·pw1 0: {tm['md']} {tm['pw']}")

    i = await send(ctx, target("device", d.uuid), "on", dur=20)
    await wait_acks(ctx, [d], i["seq"], "OK")
    ctx.check_eq(d.effective_act, "on", "개별 on 이 그룹 off 보다 우선(§3.10.8)")
    tm = await tm_now(ctx, d)
    ctx.check(tm["md"] == 2 and tm["pw"][0] == base, f"TM pw1 = 기준 {base}: {tm['pw']}")

    async def level_device() -> bool:
        dv = await rest_device(ctx, d.uuid)
        return dv.get("override_level") == "device" and dv.get("override_seq") == i["seq"]
    await ctx.wait_until(level_device, timeout=SERVER_WAIT, what="서버 override_level device")
    await ctx.wait_until(lambda: d.effective_act == "off", timeout=30, what="개별 dur 20 만료 → 그룹 off 복귀")
    ctx.check(set(d.slot_dump()) == {"group"}, f"남은 슬롯 = 그룹: {d.slot_dump()}")
    tm = await tm_now(ctx, d)
    ctx.check(tm["md"] == 2 and tm["pw"][0] == 0, f"복귀 뒤 TM md 2·pw1 0: {tm['md']} {tm['pw']}")

    i2 = await send(ctx, target("device", d.uuid), "on", dur=300)
    await wait_acks(ctx, [d], i2["seq"], "OK")
    ctx.check_eq(d.effective_act, "on", "개별 on 다시")
    au = await send(ctx, target("device", d.uuid), "auto")
    await wait_acks(ctx, [d], au["seq"], "OK")
    ctx.check(d.effective_act == "off" and set(d.slot_dump()) == {"group"}, f"개별 auto → 그룹 off 로: {d.slot_dump()}")

    g2 = await send(ctx, target("node", leaf.id), "off", dur=600)
    await wait_acks(ctx, [d], g2["seq"], "OK")
    await tm_now(ctx, d)

    async def override_group() -> bool:
        dv = await rest_device(ctx, d.uuid)
        return bool(dv.get("override_until")) and dv.get("override_level") == "group"
    await ctx.wait_until(override_group, timeout=SERVER_WAIT, what="서버 override_level group·until 기록")
    reboots0 = len(await ctx.s.db.events(d.uuid, "REBOOT"))
    await d.reboot(reconnect_after=0.5)
    await ctx.wait_until(lambda: d.state == "ACTIVE", timeout=20, what="재부팅 뒤 retain ACTIVE")
    await wait_group(ctx, [d], leaf)
    ctx.check(d.slot_dump() == {} and d.md == 0, f"재부팅 → 슬롯 전부 소거·md 0: {d.slot_dump()}")

    async def cleared() -> dict | None:
        dv = await rest_device(ctx, d.uuid)
        return dv if dv.get("override_until") is None and dv.get("remote_active") is False else None
    dv = await ctx.wait_until(cleared, timeout=SERVER_WAIT * 2, what="서버 REBOOT 감지 → override 필드 NULL")
    ctx.check(dv.get("override_act") is None and dv.get("override_level") is None, "override_act/level NULL")
    ctx.check_eq(len(await ctx.s.db.events(d.uuid, "REBOOT")), reboots0 + 1, "REBOOT 이벤트 수")


# ── S5-06 EXPIRED 흡수 ──────────────────────────────────────────────────

@scenario("S5-06", "EXPIRED 흡수 — 늦게 도착한 명령은 EXPIRED, 다음 송신 때 같은 seq·새 ts 로 재시도 → OK, exp 존중", phase=5,
          requires="group_cmd", timeout=300)
async def s5_06(ctx: Ctx) -> None:
    """X: RTC 가 120초 앞선 단말 → EXPIRED(아무것도 안 바뀜)·서버 target EXPIRED → 시계를 고친 뒤 Telemetry → 서버 자동 재시도
    (같은 seq, 더 새 ts) → OK·attempts 2·종료 OK. Y/Z: 모뎀 전달 지연 35초(`delay_commands_sec`) — Y 는 exp 30 → EXPIRED →
    지연을 풀고 Telemetry → 재시도 OK. Z 는 요청 본문 `exp 60` → payload exp 60 → 35초 늦어도 OK(exp 존중). exp 기본 30."""
    tree = await make_tree(ctx, [1])
    x, y, z = await devices_in(ctx, 3, tree.leaves[0], offset=0)
    retry_timeout = max(getattr(ctx.opt, "cmd_retry_timeout", 90), RETRY_MIN_SEC + 30)

    x.clock_skew_sec = 120
    y.delay_commands_sec = z.delay_commands_sec = 35
    bx = await send(ctx, target("device", x.uuid), "off", dur=600)
    by = await send(ctx, target("device", y.uuid), "off", dur=600)
    bz = await send(ctx, target("device", z.uuid), "off", dur=600, exp=60)
    ctx.check_eq(bx["payload"].get("exp"), 30, "exp 기본 30(COMMAND_EXP_SEC)")
    ctx.check_eq(bz["payload"].get("exp"), 60, "요청 exp 60 → payload exp")

    await wait_acks(ctx, [x], bx["seq"], "EXPIRED")
    ctx.check(x.slot_dump() == {}, "EXPIRED 는 적용하지 않음")
    await ctx.wait_until(lambda: _is(target_status(ctx, bx["seq"], x.uuid), "EXPIRED"), timeout=SERVER_WAIT,
                         what="서버 target X = EXPIRED")
    x.clock_skew_sec = 0
    await kick_until(ctx, [x], lambda: len(device_cmds(x, bx["seq"])) >= 2, timeout=retry_timeout,
                     what="X 자동 재시도(TM 직후)")
    retry = device_cmds(x, bx["seq"])[1]
    ctx.check(retry["ts"] > bx["payload"]["ts"], f"재시도 ts 더 새것: {retry['ts']} > {bx['payload']['ts']}")
    await wait_acks(ctx, [x], bx["seq"], "OK")
    ctx.check_eq(x.effective_act, "off", "재시도로 적용")
    tb = targets_by_uuid(await wait_counts(ctx, bx["seq"], {"OK": 1, "EXPIRED": 0}))
    ctx.check_eq(tb[x.uuid]["attempts"], 2, "X attempts")
    await wait_finished(ctx, bx["seq"], "OK")

    await wait_acks(ctx, [z], bz["seq"], "OK", timeout=60, what="Z(exp 60) 35초 지연에도 OK")
    await wait_acks(ctx, [y], by["seq"], "EXPIRED", timeout=15, what="Y(exp 30) 35초 지연 → EXPIRED")
    await ctx.wait_until(lambda: _is(target_status(ctx, by["seq"], y.uuid), "EXPIRED"), timeout=SERVER_WAIT,
                         what="서버 target Y = EXPIRED")
    y.delay_commands_sec = 0
    await kick_until(ctx, [y], lambda: _acked(y, by["seq"], "OK"), timeout=retry_timeout, what="Y 자동 재시도 → OK")
    await wait_counts(ctx, by["seq"], {"OK": 1})
    await wait_finished(ctx, by["seq"], "OK")
    await wait_counts(ctx, bz["seq"], {"OK": 1}, finished=True)


async def wait_finished(ctx: Ctx, seq: int, result: str) -> None:
    """EXPIRED → (재시도) OK 처럼 **첫 응답이 아닌** ACK 로 전부 종결된 명령은 서버가 ACK 때 바로 닫지 않고
    주기 판정(command_finisher, 30초)이 닫는다(2026-09-27 실행 확인, docs/06 §5 B12). 그래서 상한을 40초로 둔다."""
    t0 = time.monotonic()

    async def done() -> dict | None:
        dt = await detail(ctx, seq)
        return dt if dt.get("finished_at") else None
    dt = await ctx.wait_until(done, timeout=40, interval=1, what=f"seq {seq} 종료(주기 판정 포함)")
    ctx.check_eq(dt.get("result"), result, f"seq {seq} result")
    ctx.log(f"seq {seq} 종료까지 {time.monotonic() - t0:.1f}s (10s 넘으면 주기 판정으로 닫힌 것)")


async def _is(aw, value: Any) -> bool:
    return (await aw) == value


# ── S5-07 LOCAL ─────────────────────────────────────────────────────────

@scenario("S5-07", "현장 조작 중 — LOCAL 응답(종결)·버림, 현장 시작 = 원격 취소, 끝나면 스케줄(개정 2026-09-27)", phase=5,
          requires="group_cmd", timeout=150)
async def s5_07(ctx: Ctx) -> None:
    """§3.10.8 개정(현장 우선): 개별 off dur 600 → OK·md 2 → 단말 `start_local()`(DIP/엔코더/OLED) → 원격 슬롯 전부 취소 →
    TM md 1 → 서버 override 해제(remote_active false) → 현장 중 개별 on → `LOCAL`(버림, 적용 안 함) → 서버 target LOCAL·명령 종료
    (LOCAL 은 종결, ADR-005)·counts LOCAL 1·result PARTIAL(B7) → 종료된 명령 `POST /retry` 409 `COMMAND_FINISHED` →
    `end_local()` → **아무것도 적용하지 않고 스케줄**(md 0, 예전 원격·LOCAL 명령 되살리지 않음) → 새 명령(새 seq) on → OK·md 2."""
    tree = await make_tree(ctx, [1])
    (d,) = await devices_in(ctx, 1, tree.leaves[0], offset=0)
    b0 = await send(ctx, target("device", d.uuid), "off", dur=600)
    await wait_acks(ctx, [d], b0["seq"], "OK")
    ctx.check_eq(d.md, 2, "원격 적용")
    ctx.check_eq(d.start_local(), 1, "현장 시작 → 원격 슬롯 취소")
    tm = await tm_now(ctx, d)
    ctx.check_eq(tm["md"], 1, "현장 조작 중 TM md")

    async def remote_off() -> bool:
        dv = await rest_device(ctx, d.uuid)
        return dv.get("remote_active") is False and dv.get("override_until") is None
    await ctx.wait_until(remote_off, timeout=SERVER_WAIT, what="md 1 → 서버 override 해제")

    b = await send(ctx, target("device", d.uuid), "on", dur=300)
    await wait_acks(ctx, [d], b["seq"], "LOCAL")
    ctx.check(d.slot_dump() == {} and d.effective_act == "local", "LOCAL 은 적용하지 않음")
    det = await wait_counts(ctx, b["seq"], {"LOCAL": 1, "pending": 0}, finished=True)
    ctx.check(det.get("result") in ("PARTIAL", "OK"), f"LOCAL 만인 명령 result PARTIAL(가정 B7): {det.get('result')}")
    r = await ctx.s.rest.retry_command(b["seq"], None, _admin(ctx))
    ctx.check(r.status_code == 409 and error_code(r) == "COMMAND_FINISHED", f"종료된 명령 재시도 → 409: {_describe(r)}")

    d.end_local()
    ctx.check(d.slot_dump() == {} and d.md == 0 and d.effective_act == "schedule",
              "현장 종료 → 스케줄(LOCAL 명령·예전 원격 되살리지 않음)")
    tm = await tm_now(ctx, d)
    ctx.check_eq(tm["md"], 0, "TM md 0")
    b2 = await send(ctx, target("device", d.uuid), "on", dur=300)
    await wait_acks(ctx, [d], b2["seq"], "OK")
    tm = await tm_now(ctx, d)
    ctx.check(tm["md"] == 2 and tm["pw"][0] == d.model.pwm[0], f"새 명령 → TM md 2·pw1 기준: {tm['md']} {tm['pw']}")


# ── S5-08 BAD ───────────────────────────────────────────────────────────

@scenario("S5-08", "잘못된 명령 — 서버 422(발행·이력 없음), 서버를 우회한 원시 COMMAND 는 단말 BAD", phase=5,
          requires="group_cmd", timeout=120)
async def s5_08(ctx: Ctx) -> None:
    """서버 검증: pwm 길이≠ch, dur 0, dur 90000, ch [4], ch 중복, act blink, on 에 dur·dur_preset 없음, pwm 값 없음, pwm 101
    → 전부 422 `VALIDATION_FAILED`(preview 도), 명령 이력 증가·발행 없음. 서버 계정으로 원시 COMMAND(dur 누락 / act blink /
    pwm 길이 틀림 / dur 90000 / ts 모양 틀림)를 개별 topic 에 → 단말 `BAD`·슬롯 불변. 서버는 모르는 seq 의 ACK 에 죽지 않는다."""
    tree = await make_tree(ctx, [1])
    (d,) = await devices_in(ctx, 1, tree.leaves[0], offset=0)
    sn = await sniff(ctx, "a")
    tgt = target("device", d.uuid)
    before = await ctx.s.rest.commands(_admin(ctx), uuid=d.uuid, limit=50)
    _skip_if_missing(ctx, before, "GET /api/commands")
    n0 = len(before.json())
    t0 = time.monotonic()
    bad: list[tuple[str, str, dict]] = [
        ("pwm 길이 ≠ ch", "pwm", dict(ch=[1, 2], pwm=[50], dur=60)),
        ("dur 0", "off", dict(dur=0)),
        ("dur 90000", "off", dict(dur=90000)),
        ("ch [4]", "off", dict(ch=[4], dur=60)),
        ("ch 중복", "off", dict(ch=[1, 1], dur=60)),
        ("act blink", "blink", dict(dur=60)),
        ("dur·dur_preset 없음", "on", {}),
        ("pwm 값 없음", "pwm", dict(ch=[1], dur=60)),
        ("pwm 101", "pwm", dict(ch=[1], pwm=[101], dur=60)),
    ]
    for what, act, fields in bad:
        r = await ctx.s.rest.post_command(body_of(tgt, act, **fields), _admin(ctx))
        _skip_if_missing(ctx, r, "POST /api/commands")
        ctx.check(r.status_code == 422 and error_code(r) == "VALIDATION_FAILED",
                  f"{what} → 422 VALIDATION_FAILED: {_describe(r)}")
    r = await ctx.s.rest.command_preview(body_of(tgt, "off", dur=0), _admin(ctx))
    ctx.check(r.status_code == 422, f"preview dur 0 → 422: {_describe(r)}")
    await ctx.hold(1.0, "발행이 없었는지 확인 전")
    ctx.check(not sn.find(type_="COMMAND", since=t0), "422 뒤 COMMAND 발행 없음")
    after = await ctx.s.rest.commands(_admin(ctx), uuid=d.uuid, limit=50)
    ctx.check_eq(len(after.json()), n0, "명령 이력 행 수(증가 없음)")

    ts = kst_ts_sec()
    base = {"type": "COMMAND", "ts": ts, "exp": 30, "ch": [1, 2]}
    raws = [
        ("dur 누락", dict(base, act="on")),
        ("act blink", dict(base, act="blink", dur=60)),
        ("pwm 길이", dict(base, act="pwm", pwm=[50], dur=60)),
        ("dur 90000", dict(base, act="off", dur=90000)),
        ("ts 모양", dict(base, act="off", dur=60, ts=ts[:11])),
    ]
    for what, p in raws:
        p["seq"] = raw_seq()
        await ctx.s.broker.publish(d.topic("cmd"), p, qos=1)
        await wait_acks(ctx, [d], p["seq"], "BAD", what=f"원시 COMMAND {what} → 단말 BAD")
    ctx.check(d.slot_dump() == {} and d.md == 0, "BAD 는 슬롯 불변")
    await ctx.hold(1.0, "서버가 모르는 seq 의 ACK 처리")
    ctx.check(d.is_connected and await ctx.s.rest.health_ok(), "단말 연결·/health ok")


# ── S5-09 grp 변경 ──────────────────────────────────────────────────────

@scenario("S5-09", "grp 변경 — retain REGISTER_ACK 새 grp, 옛 그룹 해제·새 그룹 구독, 배정 해제·SUSPENDED 면 구독 없음", phase=5,
          requires="group_cmd", timeout=180)
async def s5_09(ctx: Ctx) -> None:
    """D1·D2 를 말단 A 에 → retain ACK `grp=A00`(문자열) → D1 을 `PATCH /config {node_id: B}` → `register_ack_republished`,
    retain `grp=B00`, DB grp/node_id/bjd_code·REST node_path 갱신 → D1 은 A 해제 후 B 구독(구독 4개, 그룹 1개) → A 명령은 D2 만
    (D1 수신 없음), B 명령은 D1 OK → `node_id: null` → retain 에 grp 없음·D1 구독 3개 → 빈 말단 B 명령 409 `NO_TARGETS` →
    D2 SUSPENDED → 그룹 해제, 다시 ACTIVE → 재구독(ACTIVE 가 아니면 grp 가 있어도 구독하지 않는다, §3.10.9)."""
    tree = await make_tree(ctx, [2])
    a, b = tree.leaves
    d1, d2 = await devices_in(ctx, 2, a, offset=0)
    ack = await retained_ack(ctx, d1)
    ctx.check(ack is not None and ack.get("grp") == a.grp and ack.get("state") == "ACTIVE", f"retain ACK grp A: {ack}")
    ctx.check(isinstance(ack.get("grp"), str), "grp 는 문자열 하나(배열 아님, §3.10.9)")
    ctx.check(len(d1.subscriptions) == 4, f"구독 4개(cmd·config·all·group): {d1.subscriptions}")

    r = await ctx.s.rest.patch_config(d1.uuid, node_id=b.id)
    _skip_if_missing(ctx, r, "PATCH /config node_id")
    ctx.check(r.status_code == 200, f"PATCH node_id B: {_describe(r)}")
    ctx.check(r.json().get("register_ack_republished") is True,
              f"register_ack_republished: {r.json().get('register_ack_republished')}")
    await wait_group(ctx, [d1], b)
    ctx.check(d1.group_log[-1][1:] == (a.topic(_root(ctx)), b.topic(_root(ctx))), f"A 해제 → B 구독: {d1.group_log[-1]}")
    ack = await retained_ack(ctx, d1)
    ctx.check(ack is not None and ack.get("grp") == b.grp, f"retain ACK grp B: {ack}")
    row = await ctx.s.db.device(d1.uuid)
    ctx.check(row.get("grp") == b.grp and row.get("node_id") == b.id and row.get("bjd_code") == b.bjd_code,
              f"DB grp/node_id/bjd_code: {row.get('grp')} {row.get('node_id')} {row.get('bjd_code')}")
    dev = await rest_device(ctx, d1.uuid)
    ctx.check(dev.get("node_id") == b.id and b.name in str(dev.get("node_path")), f"REST node_id/node_path: {dev.get('node_path')}")

    sn = await sniff(ctx, "a")
    ca = await send(ctx, target("node", a.id), "off", dur=60)
    await wait_acks(ctx, [d2], ca["seq"], "OK")
    await ctx.hold(1.5, "옛 그룹 명령이 D1 에 늦게 오지 않는지")
    ctx.check(not d1.commands_received(seq=ca["seq"]) and not d1.acks_for(ca["seq"]), "옛 그룹 A 명령은 D1 에 안 옴")
    pubs = await published(ctx, sn, ca["seq"], 1)
    ctx.check([x[1] for x in pubs] == [a.topic(_root(ctx))], f"A 발행은 A topic 1회: {[x[1] for x in pubs]}")
    cb = await send(ctx, target("node", b.id), "on", dur=60)
    await wait_acks(ctx, [d1], cb["seq"], "OK")
    ctx.check(not d2.commands_received(seq=cb["seq"]), "B 명령은 D2 에 안 옴")

    r = await ctx.s.rest.patch_config(d1.uuid, node_id=None)
    ctx.check(r.status_code == 200, f"node_id null: {_describe(r)}")
    await wait_group(ctx, [d1], None)
    ack = await retained_ack(ctx, d1)
    ctx.check(ack is not None and ack.get("state") == "ACTIVE" and not ack.get("grp"), f"배정 해제 → retain 에 grp 없음: {ack}")
    ctx.check(len(d1.subscriptions) == 3, f"구독 3개: {d1.subscriptions}")
    r = await ctx.s.rest.post_command(body_of(target("node", b.id), "off", dur=60), _admin(ctx))
    ctx.check(r.status_code == 409 and error_code(r) == "NO_TARGETS", f"빈 말단 명령 → 409 NO_TARGETS: {_describe(r)}")

    r = await ctx.s.rest.patch_state(d2.uuid, "SUSPENDED")
    ctx.check(r.status_code < 300, f"D2 SUSPENDED: {_describe(r)}")
    await ctx.wait_until(lambda: d2.state == "SUSPENDED" and d2.group_topic is None, timeout=15,
                         what="SUSPENDED → 그룹 구독 해제(retain 에 grp 가 있어도)")
    r = await ctx.s.rest.patch_state(d2.uuid, "ACTIVE")
    ctx.check(r.status_code < 300, f"D2 ACTIVE: {_describe(r)}")
    await wait_group(ctx, [d2], a)
    ctx.check(all(len(d.subscriptions) <= 6 for d in (d1, d2)), "구독 한도 6 이내(§3.10.10)")


# ── S5-10 중복 seq ──────────────────────────────────────────────────────

@scenario("S5-10", "중복 seq — 단말은 첫 결과로 ACK(재실행 없음), 서버는 ACK 이벤트 1건", phase=5, requires="group_cmd",
          timeout=120)
async def s5_10(ctx: Ctx) -> None:
    """개별 off → OK. 같은 payload 를 서버 계정으로 다시 발행 → 단말은 재실행 없이 첫 결과(OK·off)로 ACK. 같은 seq 에 act on·새 ts
    → 역시 첫 결과(off 유지). QoS1 중복(`duplicate_last_result`)까지 → DB `device_event COMMAND_ACK`(이 seq) 정확히 1건,
    command_target attempts 1·status OK 유지."""
    tree = await make_tree(ctx, [1])
    (d,) = await devices_in(ctx, 1, tree.leaves[0], offset=0)
    since = await ctx.s.db.now()
    b = await send(ctx, target("device", d.uuid), "off", dur=300)
    seq = b["seq"]
    await wait_acks(ctx, [d], seq, "OK")
    first = d.acks_for(seq)[0]
    await ctx.s.broker.publish(d.topic("cmd"), b["payload"], qos=1)
    await ctx.wait_until(lambda: d.stats.cmd_dup >= 1, timeout=ACK_WAIT, what="같은 payload 재수신 → 중복 판정")
    await ctx.s.broker.publish(d.topic("cmd"), dict(b["payload"], act="on", ts=kst_ts_sec()), qos=1)
    await ctx.wait_until(lambda: d.stats.cmd_dup >= 2, timeout=ACK_WAIT, what="같은 seq 다른 내용 → 중복 판정")
    ctx.check(len(d.acks_for(seq)) == 3 and all(x == first for x in d.acks_for(seq)), f"ACK 3회 전부 첫 결과: {d.acks_for(seq)}")
    ctx.check_eq(d.effective_act, "off", "재실행 없음")
    await d.duplicate_last_result()
    await ctx.hold(2.0, "서버 ACK 처리")

    async def events_for_seq() -> int:
        return len([e for e in await ctx.s.db.events(d.uuid, "COMMAND_ACK", since) if payload_of(e).get("seq") == seq])
    n = await events_for_seq()
    ctx.check_eq(n, 1, "COMMAND_ACK 이벤트(이 seq) 수 — 중복 흡수")
    t = targets_by_uuid(await detail(ctx, seq)).get(d.uuid, {})
    ctx.check(t.get("status") == "OK" and t.get("attempts") == 1, f"target OK·attempts 1: {t}")


# ── S5-11 명령 종료 ─────────────────────────────────────────────────────

@scenario("S5-11", "명령 종료 — 전부 OK 면 즉시 종료·재시도 409, 무응답은 시도 상한까지만 재시도, PARTIAL", phase=5,
          requires="group_cmd", timeout=300)
async def s5_11(ctx: Ctx) -> None:
    """A: 개별 명령 전부 OK → 곧바로 finished·result OK → `POST /retry` 409 `COMMAND_FINISHED`, 없는 seq 404 `COMMAND_NOT_FOUND`.
    B: 3대 말단 명령, 1대는 영영 무응답 → OK 2·pending 1·미종료 → OK 단말 지정 재시도 `resent 0` → 무응답 단말이 계속 Telemetry →
    자동 재시도는 `COMMAND_MAX_ATTEMPTS`(3)까지만(개별 topic 2회, 이후 RETRY_MIN+6초 동안 없음, attempts 3·pending) → 수동 재시도
    (참고) → `COMMAND_TIMEOUT_SEC` 이 `--cmd-finish-timeout` 안이면 PARTIAL·409, 아니면 참고 기록(개발 서버 기본 900초)."""
    tree = await make_tree(ctx, [1])
    leaf = tree.leaves[0]
    ok1, ok2, mute = await devices_in(ctx, 3, leaf, offset=0)

    a = await send(ctx, target("device", ok1.uuid), "off", dur=60)
    await wait_acks(ctx, [ok1], a["seq"], "OK")
    det = await wait_counts(ctx, a["seq"], {"OK": 1, "pending": 0}, finished=True)
    ctx.check_eq(det.get("result"), "OK", "전부 OK → result")
    r = await ctx.s.rest.retry_command(a["seq"], None, _admin(ctx))
    ctx.check(r.status_code == 409 and error_code(r) == "COMMAND_FINISHED", f"종료 뒤 재시도 409: {_describe(r)}")
    r = await ctx.s.rest.command(999_999_999_999, _admin(ctx))
    ctx.check(r.status_code == 404 and error_code(r) == "COMMAND_NOT_FOUND", f"없는 seq 404: {_describe(r)}")

    mute.silent_commands = 10**6
    b = await send(ctx, target("node", leaf.id), "off", dur=900)
    seq = b["seq"]
    await wait_acks(ctx, [ok1, ok2], seq, "OK")
    det = await wait_counts(ctx, seq, {"OK": 2, "pending": 1}, finished=False)
    ctx.check(not det.get("result"), f"미종료 result 없음: {det.get('result')}")
    r = await ctx.s.rest.retry_command(seq, [ok1.uuid], _admin(ctx))
    ctx.check(r.status_code == 200 and r.json().get("resent") == 0, f"OK 단말 재시도 → resent 0: {_describe(r)} {r.text[:80]}")

    want = MAX_ATTEMPTS - 1
    await kick_until(ctx, [mute], lambda: len(device_cmds(mute, seq)) >= want,
                     timeout=want * (RETRY_MIN_SEC + 10) + 20, what=f"자동 재시도 {want}회(시도 상한 {MAX_ATTEMPTS})")
    quiet = RETRY_MIN_SEC + 6
    t_cap = time.monotonic()
    while time.monotonic() - t_cap < quiet:
        await mute.send_tm_now()
        await asyncio.sleep(KICK_EVERY)
    ctx.check_eq(len(device_cmds(mute, seq)), want, f"상한 뒤 추가 재시도 없음({quiet:.0f}s 동안 TM) — 개별 재시도 수")
    t = targets_by_uuid(await detail(ctx, seq)).get(mute.uuid, {})
    ctx.check(t.get("attempts") == MAX_ATTEMPTS and t.get("status") == "pending", f"attempts {MAX_ATTEMPTS}·pending: {t}")

    r = await ctx.s.rest.retry_command(seq, [mute.uuid], _admin(ctx))
    ctx.check(r.status_code == 200, f"수동 재시도 200: {_describe(r)}")
    ctx.log(f"(참고) 시도 상한 뒤 수동 재시도 resent={r.json().get('resent')} — 가정 B8")
    if r.json().get("resent") == 1:
        await ctx.wait_until(lambda: len(device_cmds(mute, seq)) == want + 1, timeout=ACK_WAIT, what="수동 재시도 도착")

    finish = float(getattr(ctx.opt, "cmd_finish_timeout", 30))

    async def finished() -> dict | None:
        dt = await detail(ctx, seq)
        return dt if dt.get("finished_at") else None
    try:
        dt = await ctx.wait_until(finished, timeout=finish, interval=2, what="COMMAND_TIMEOUT_SEC 경과 → 종료")
    except Fail:
        ctx.log(f"(참고) {finish:.0f}s 안에 종료되지 않음 — 서버 COMMAND_TIMEOUT_SEC 가 더 길다(기본 900). PARTIAL 판정 생략")
        return
    ctx.check_eq(dt.get("result"), "PARTIAL", "응답 일부 → result")
    r = await ctx.s.rest.retry_command(seq, None, _admin(ctx))
    ctx.check(r.status_code == 409 and error_code(r) == "COMMAND_FINISHED", f"PARTIAL 종료 뒤 재시도 409: {_describe(r)}")


# ── S5-12 명령 폭주 ─────────────────────────────────────────────────────

@scenario("S5-12", "명령 폭주 — 200대·말단 5개, 시군구 명령 → 60초 안 전원 ACK, 발행률 상한, /health", phase=5,
          requires="group_cmd", timeout=480)
async def s5_12(ctx: Ctx) -> None:
    """`--storm5-count`(기본 200)대를 한 시군구의 말단 5개에 나눠 승인 → 시군구 명령 1회 → 그룹 topic 5회 발행(개별 재발송 0) →
    전원 COMMAND_ACK OK 60초 안, 서버 counts OK N·result OK 60초 안 → 서버 발행(cmd/config, retain 제외) 초당 최대가
    토큰 버킷 상한(`REGISTER_REPLY_RATE_PER_SEC`, 기본 200)×1.2 이하 → /health 실패 0회(2초 간격 감시)."""
    n = int(getattr(ctx.opt, "storm5_count", 200))
    leaves_n = 5
    tree = await make_tree(ctx, [leaves_n])
    sg = tree.sigungus[0]
    per = [n // leaves_n + (1 if i < n % leaves_n else 0) for i in range(leaves_n)]
    devs: list[SimDevice] = []
    offset = 0
    for leaf, k in zip(sg.leaves, per):
        devs += await devices_in(ctx, k, leaf, offset=offset)
        offset += k
    ctx.log(f"단말 {len(devs)}대 승인·그룹 구독 완료 {per}")

    health_fail = 0
    stop = asyncio.Event()

    async def watch() -> None:
        nonlocal health_fail
        while not stop.is_set():
            if not await ctx.s.rest.health_ok():
                health_fail += 1
            try:
                await asyncio.wait_for(stop.wait(), 2.0)
            except asyncio.TimeoutError:
                pass
    watcher = asyncio.create_task(watch())

    async def stop_watch() -> None:
        stop.set()
        await watcher
    ctx.defer(stop_watch)

    sn = await sniff(ctx, "a")
    t0 = time.monotonic()
    body = await send(ctx, target("node", sg.id), "off", dur=600)
    seq = body["seq"]
    ctx.check_eq(len(body.get("topics") or []), leaves_n, "topics 수")
    ctx.check_eq(body.get("expected"), n, "expected")
    await ctx.wait_until(lambda: all(_acked(d, seq, "OK") for d in devs), timeout=60, interval=0.5,
                         what=f"{n}대 전원 COMMAND_ACK OK")
    ctx.log(f"전원 ACK {time.monotonic() - t0:.1f}s")
    det = await wait_counts(ctx, seq, {"OK": n, "pending": 0}, timeout=60, finished=True)
    ctx.check_eq(det.get("result"), "OK", "result")
    pubs = await published(ctx, sn, seq, leaves_n)
    ctx.check_eq(len([p for p in pubs if "/group/" in p[1]]), leaves_n, "그룹 발행 수")
    ctx.check_eq(len([p for p in pubs if "/device/" in p[1]]), 0, "개별 재발송 수")

    per_sec: dict[int, int] = {}
    for (t, tp, _, ret) in sn.messages:
        if not ret and t >= t0 - 1 and (tp.endswith("/cmd") or tp.endswith("/config")):
            per_sec[int(t)] = per_sec.get(int(t), 0) + 1
    peak = max(per_sec.values(), default=0)
    limit = ctx.s.env.publish_rate_per_sec * 1.2
    ctx.check(peak <= limit, f"서버 발행 초당 최대 {peak} ≤ {limit:.0f}(토큰 버킷)")
    await stop_watch()
    ctx.check_eq(health_fail, 0, "/health 실패 횟수")
    ctx.check(await ctx.s.rest.health_ok(), "/health ok")
