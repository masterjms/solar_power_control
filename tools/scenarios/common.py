"""시나리오들이 같이 쓰는 도우미 — 단말 띄우기·승인·metrics 읽기·UUID 네임스페이스."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from tools.scenarios.framework import Ctx, Fail
from tools.scenarios.services import error_code
from tools.sim.device import SimDevice, hmac_key_from_hex, uuid_from_index

OUT_DIR = Path(__file__).resolve().parent / "out"

#: TM 이 DB 에 보이기까지의 여유(백엔드 1초 배치 + 왕복). wait_until 의 상한으로만 쓴다.
FLUSH_WAIT = 10.0


def namespace_of(scenario_id: str) -> int:
    """S2-03 → 0x0203. 시나리오마다 UUID 대역을 나눠 서로 레코드를 건드리지 않게 한다."""
    m = re.match(r"^S(\d)-(\d+)$", scenario_id)
    if not m:
        raise ValueError(scenario_id)
    return (int(m.group(1)) << 8) | int(m.group(2))


def uuid_prefix(scenario_id: str) -> str:
    return f"51A0{namespace_of(scenario_id):04X}"


def sim_mode(ctx: Ctx) -> str:
    """--sim-mode auto 는 2cha(1.4.0 — HMAC 계정, 서버 준비 불필요). 1cha 는 공용 계정이 살아 있을 때만 뜻이 있다."""
    mode = getattr(ctx.opt, "sim_mode", "auto")
    return "2cha" if mode == "auto" else mode


def hmac_key(ctx: Ctx) -> bytes:
    return hmac_key_from_hex(ctx.s.env.mqtt_hmac_key)


def payload_of(event: dict[str, Any]) -> dict[str, Any]:
    """device_event.payload (jsonb → dict 또는 str)."""
    p = event.get("payload")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            return {}
    return p if isinstance(p, dict) else {}


def _describe(r) -> str:
    return f"{r.status_code} {error_code(r) or r.text[:120]}"


async def wait_rows(ctx: Ctx, uuids: list[str], *, timeout: float = 60.0, what: str = "") -> None:
    """REGISTER 가 처리되어 device 행이 생길 때까지."""
    async def all_present() -> bool:
        n = await ctx.s.db.fetchval("SELECT count(*) FROM device WHERE uuid = ANY($1::text[])", uuids)
        return n == len(uuids)
    await ctx.wait_until(all_present, timeout=timeout, interval=0.5,
                         what=what or f"device 행 {len(uuids)}개 생성(REGISTER 처리)")


async def _override_config(ctx: Ctx, uuid: str, ti: int, ka: int) -> None:
    """승인 전에 단말 시험값(ti/ka)을 서버 override 로 걸어 둔다.

    승인 뒤 서버는 프로필 기본값(600/300)을 CONFIG_SET 으로 내려 시뮬레이터 주기를 바꿔 버린다(서버가 맞다).
    PENDING 중 PATCH 는 `published=false` 로 저장만 되고 ACTIVE 뒤 첫 TM 때 이 값으로 CONFIG_SET 이 나간다.
    백엔드 하한은 `CONFIG_TI_MIN_SEC`/`CONFIG_KA_MIN_SEC`(로컬 compose 1) — 시뮬레이터 `ti_min`/`ka_min` 과 짝.
    """
    r = await ctx.s.rest.patch_config(uuid, ti_override=ti, ka_override=ka)
    if r.status_code >= 400:
        raise Fail(f"override 실패 {uuid} (ti={ti}, ka={ka}): {_describe(r)} — 백엔드 CONFIG_TI_MIN_SEC/CONFIG_KA_MIN_SEC 확인")


async def approve(ctx: Ctx, devices: list[SimDevice], *, site: str | None = None, timeout: float = 30.0,
                  concurrency: int = 20, override: bool = True, node_id: int | None = None) -> None:
    """2cha 단말을 ACTIVE 로 승인한다 — `PATCH /api/devices/{uuid}/state {"state":"ACTIVE"}` (docs/05).

    `node_id`(5차)를 주면 같은 요청에 `node_id` 를 실어 말단 법정동에 배정한다 → REGISTER_ACK retain 에 `grp`.

    REGISTER 로 행이 생긴 뒤에만 승인할 수 있다(PATCH 는 없는 uuid 에 404). `override=True`(기본)면 그 전에
    `PATCH /config {ti_override: d.ti, ka_override: d.ka}` 로 시험값을 걸어 승인 뒤 CONFIG_SET 이 시뮬레이터 주기를
    프로필 기본값으로 바꾸지 않게 한다. 승인 뒤 단말이 retain/실시간 REGISTER_ACK ACTIVE 를 받을 때까지 기다린다.
    게이트가 꺼진 단말(1cha)은 건너뛴다.
    """
    gated = [d for d in devices if d.gate.enabled]
    if not gated:
        return
    await wait_rows(ctx, [d.uuid for d in gated], timeout=timeout)
    sem = asyncio.Semaphore(concurrency)

    async def one(d: SimDevice) -> None:
        async with sem:
            if override:
                await _override_config(ctx, d.uuid, d.ti, d.ka)
            extra = {} if node_id is None else {"node_id": node_id}
            r = await ctx.s.rest.patch_state(d.uuid, "ACTIVE", site=site, **extra)
            if r.status_code == 409 and error_code(r) != "NODE_REQUIRED":
                # 이전 실행의 잔여 행이 이미 ACTIVE 면 같은 상태 전이라 409 — 그대로 진행한다.
                cur = await ctx.s.db.device(d.uuid)
                if cur and cur.get("state") == "ACTIVE":
                    if node_id is not None:
                        await ctx.s.rest.patch_config(d.uuid, node_id=node_id)
                    await ctx.s.rest.republish_register_ack(d.uuid)
                    return
            if r.status_code >= 400:
                raise Fail(f"승인 실패 {d.uuid}: {_describe(r)}")
    await asyncio.gather(*(one(d) for d in gated))
    not_active = [d for d in gated if not await d.wait_state("ACTIVE", timeout=timeout)]
    if not_active:
        raise Fail(f"승인 뒤 ACTIVE 를 못 받은 단말 {len(not_active)}/{len(gated)}: "
                   f"{[(d.uuid[-4:], d.state) for d in not_active[:5]]}")
    ctx.log(f"승인(ACTIVE) {len(gated)}대" + (f" site={site}" if site else "") + (f" node={node_id}" if node_id else ""))


async def approve_uuids(ctx: Ctx, uuids: list[str], *, site: str | None = None, timeout: float = 120.0,
                        concurrency: int = 20, ti: int | None = None, ka: int | None = None) -> int:
    """fleet 이 별도 프로세스라 SimDevice 핸들이 없을 때(S2-10). 행이 생긴 것부터 승인하고 성공 수를 돌려준다.
    `ti`/`ka` 를 주면 승인 전에 override 를 건다(`approve` 와 같은 이유)."""
    await wait_rows(ctx, uuids, timeout=timeout)
    sem = asyncio.Semaphore(concurrency)
    ok = 0

    async def one(u: str) -> None:
        nonlocal ok
        async with sem:
            if ti is not None and ka is not None:
                await _override_config(ctx, u, ti, ka)
            r = await ctx.s.rest.patch_state(u, "ACTIVE", site=site)
            if r.status_code < 400:
                ok += 1
            else:
                ctx.log(f"승인 실패 {u}: {_describe(r)}")
    await asyncio.gather(*(one(u) for u in uuids))
    ctx.log(f"승인(ACTIVE) {ok}/{len(uuids)}대")
    return ok


async def make_devices(ctx: Ctx, count: int, *, mode: str | None = None, start: bool = True,
                       approve_now: bool = False, site: str | None = None,
                       connect_timeout: float = 30.0, offset: int = 0, **flags: Any) -> list[SimDevice]:
    """시나리오 네임스페이스로 단말 count 대. 종료 시 stop + DB 정리를 자동 등록한다.

    2cha(기본): username=UUID, password=HMAC(MQTT_HMAC_KEY, UUID). 서버 쪽 준비는 필요 없다.
    `approve_now=True` 면 REGISTER 가 처리된 뒤 REST 로 ACTIVE 승인까지 하고 돌아온다(Telemetry 가 필요한 시나리오).
    CONFIG 검증 하한은 `ti_min=1, ka_min=1`(시험값 ti=5 를 RANGE 로 거부하지 않게) — 사양 하한을 보려면 flags 로 60 을 준다.
    """
    mode = mode or sim_mode(ctx)
    ns = namespace_of(ctx.scenario.id)
    uuids = [uuid_from_index(offset + i, ns) for i in range(count)]
    env = ctx.s.env
    key = hmac_key(ctx) if mode == "2cha" else None
    devices = []
    for u in uuids:
        kwargs: dict[str, Any] = dict(host=env.mqtt_host, port=env.mqtt_port, topic_root=env.topic_root,
                                      mode=mode, ti_min=1, ka_min=1)
        if mode == "2cha":
            kwargs["hmac_key"] = key
        else:
            kwargs["username"], kwargs["password"] = env.mqtt_test_user, env.mqtt_test_password
        kwargs.update(flags)
        devices.append(SimDevice(u, **kwargs))

    async def cleanup() -> None:
        await asyncio.gather(*(d.stop(graceful=True) for d in devices), return_exceptions=True)
        if not getattr(ctx.opt, "keep_rows", False):
            for u in uuids:
                try:
                    await ctx.s.rest.delete_device(u)
                except Exception:  # noqa: BLE001
                    pass
                await ctx.s.db.delete_device_rows(u)

    ctx.defer(cleanup)
    if start:
        await asyncio.gather(*(d.start() for d in devices))
        results = await asyncio.gather(*(d.wait_connected(connect_timeout) for d in devices))
        n = sum(1 for ok in results if ok)
        if n != count:
            errs = {d.stats.last_error for d in devices if not d.is_connected}
            raise Fail(f"단말 접속 {n}/{count} — {errs}")
        ctx.log(f"단말 {count}대 접속 (mode={mode}, {uuids[0]}…)")
        if approve_now:
            await approve(ctx, devices, site=site)
    return devices


async def tm_in_db(ctx: Ctx, uuid: str, sq: int, since) -> bool:
    return bool(await ctx.s.db.fetchval(
        "SELECT 1 FROM telemetry WHERE uuid=$1 AND sq=$2 AND received_at >= $3 LIMIT 1", uuid, sq, since))


async def send_tm_and_wait(ctx: Ctx, dev: SimDevice, since) -> dict[str, Any]:
    """TM 1건을 보내고 그 sq 가 telemetry 에 적재될 때까지 기다린다."""
    payload = await dev.send_tm_now()
    if payload is None:
        raise Fail(f"{dev.uuid}: 승인 전(state={dev.state})이라 TM 을 보내지 않았다")
    await ctx.wait_until(lambda: tm_in_db(ctx, dev.uuid, payload["sq"], since), timeout=FLUSH_WAIT,
                         what=f"TM sq={payload['sq']} 적재")
    return payload


async def wait_config_set(ctx: Ctx, dev: SimDevice, *, count: int, timeout: float = 15.0,
                          what: str = "") -> dict[str, Any]:
    """단말의 CONFIG_SET 수신 횟수가 count 에 이를 때까지 기다리고 마지막 payload 를 돌려준다."""
    await ctx.wait_until(lambda: dev.stats.config_set_rx >= count, timeout=timeout,
                         what=what or f"CONFIG_SET 수신 {count}회")
    assert dev.last_config_set is not None
    return dev.last_config_set


def check_config_set_shape(ctx: Ctx, payload: dict[str, Any], *, ti: int | None = None, ka: int | None = None,
                           cv: int | None = None, lat_lon: bool | None = None) -> None:
    """§1.1.7 S-13 — 매번 전체값: cv/ti/ka 항상, cv ≥ 1, lat/lon 은 값이 있을 때만."""
    ctx.check(payload.get("type") == "CONFIG_SET", f"type CONFIG_SET: {payload}")
    ctx.check(all(k in payload for k in ("cv", "ti", "ka")), f"CONFIG_SET 에 cv/ti/ka 전부 있음: {payload}")
    ctx.check(isinstance(payload["cv"], int) and payload["cv"] >= 1, f"cv ≥ 1 (0 금지): {payload['cv']}")
    if cv is not None:
        ctx.check_eq(payload["cv"], cv, "CONFIG_SET cv")
    if ti is not None:
        ctx.check_eq(payload["ti"], ti, "CONFIG_SET ti")
    if ka is not None:
        ctx.check_eq(payload["ka"], ka, "CONFIG_SET ka")
    if lat_lon is False:
        ctx.check("lat" not in payload and "lon" not in payload, "좌표 없으면 lat/lon 키 자체가 없음")
    elif lat_lon is True:
        ctx.check("lat" in payload and "lon" in payload, "좌표 있으면 lat/lon 실림")
    extra = set(payload) - {"type", "cv", "ti", "ka", "lat", "lon"}
    ctx.check(not extra, f"CONFIG_SET 에 사양 밖 키 없음: {extra}")


def rx_delay(dev: SimDevice, kind: str, sent_at: float | None) -> float | None:
    """단말이 `sent_at`(monotonic) 에 보낸 뒤 `kind` 를 받기까지 걸린 초(§1.1.10 즉시성). 못 받았으면 None."""
    got = dev.last_rx_by_type.get(kind)
    if got is None or sent_at is None or got < sent_at:
        return None
    return got - sent_at


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """중첩 dict 를 'a.b.c' 키 한 단계로 편다."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    else:
        out[prefix.rstrip(".")] = obj
    return out


def metric_sum(metrics: dict[str, Any], pattern: str) -> int:
    """중첩 dict 를 펼쳐 key 가 정규식에 맞는 숫자 값을 더한다. 백엔드 counter 이름이 확정되지
    않아서 이름 패턴으로 찾는다(docs/06 §가정)."""
    rx = re.compile(pattern, re.IGNORECASE)
    total = 0
    for k, v in flatten(metrics).items():
        if rx.search(k) and isinstance(v, (int, float)) and not isinstance(v, bool):
            total += int(v)
    return total


def metric_keys(metrics: dict[str, Any], pattern: str) -> list[str]:
    rx = re.compile(pattern, re.IGNORECASE)
    return [k for k in flatten(metrics) if rx.search(k)]


def last_tm_of(rest_device: dict[str, Any] | None) -> dict[str, Any] | None:
    """REST 단말 응답에서 마지막 TM 원본을 찾는다(컬럼명 last_telemetry, docs/03)."""
    if not rest_device:
        return None
    for key in ("last_telemetry", "telemetry", "last_tm"):
        v = rest_device.get(key)
        if isinstance(v, dict):
            return v
    return None


def monotonic() -> float:
    return time.monotonic()
