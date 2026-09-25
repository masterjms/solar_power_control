"""시나리오들이 같이 쓰는 도우미 — 단말 띄우기·계정 import·metrics 읽기·UUID 네임스페이스."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from tools.scenarios.framework import Ctx, Fail
from tools.sim.device import SimDevice, uuid_from_index
from tools.sim.fleet import write_accounts

OUT_DIR = Path(__file__).resolve().parent / "out"


def namespace_of(scenario_id: str) -> int:
    """S2-03 → 0x0203. 시나리오마다 UUID 대역을 나눠 서로 레코드를 건드리지 않게 한다."""
    m = re.match(r"^S(\d)-(\d+)$", scenario_id)
    if not m:
        raise ValueError(scenario_id)
    return (int(m.group(1)) << 8) | int(m.group(2))


def sim_mode(ctx: Ctx) -> str:
    """--sim-mode auto 면 공용 계정이 살아 있을 때 1cha, 아니면 2cha(계정 import 필요)."""
    mode = getattr(ctx.opt, "sim_mode", "auto")
    if mode != "auto":
        return mode
    return "1cha" if ctx.s.env.mqtt_test_account_enabled else "2cha"


async def import_accounts(ctx: Ctx, uuids: list[str], *, offset: int = 0, wait_acl: bool = True) -> dict[str, str]:
    """uuid,password CSV 를 만들어 REST 로 import 하고 ACL 적용을 기다린다. {uuid: password}."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"accounts_{ctx.scenario.id}_{offset}.csv"
    ns = namespace_of(ctx.scenario.id)
    # write_accounts 는 순번으로 만든다 — uuids 가 uuid_from_index(offset+i, ns) 순서라고 가정.
    rows = write_accounts(path, len(uuids), ns, offset)
    accounts = dict(rows)
    if set(accounts) != set(uuids):
        raise ValueError("uuids 가 uuid_from_index(i, namespace) 순서가 아니다")
    r = await ctx.s.rest.import_accounts(path)
    if r.status_code >= 400:
        raise Fail(f"계정 import 실패 {r.status_code}: {r.text[:200]}")
    body = r.json()
    ctx.log(f"계정 import: {body}")
    imported = body.get("imported")
    if imported is not None and int(imported) != len(uuids):
        raise Fail(f"imported={imported}, 기대 {len(uuids)}")
    if wait_acl and not body.get("acl_applied"):
        # 응답에 acl_applied=false 면 entrypoint 감시 루프가 HUP 할 때까지 기다린다.
        # 상태 조회 API 가 없으므로 실제 접속으로 확인한다.
        probe_uuid = uuids[0]

        async def try_connect() -> bool:
            ok, _ = await ctx.s.broker.can_connect(probe_uuid, accounts[probe_uuid], f"{probe_uuid}")
            return ok

        await ctx.wait_until(try_connect, timeout=60, interval=2, what="ACL 적용(단말 계정으로 접속 가능)")
    return accounts


async def make_devices(ctx: Ctx, count: int, *, mode: str | None = None, start: bool = True,
                       connect_timeout: float = 30.0, offset: int = 0, **flags: Any) -> list[SimDevice]:
    """시나리오 네임스페이스로 단말 count 대. 종료 시 stop + DB 정리를 자동 등록한다."""
    mode = mode or sim_mode(ctx)
    ns = namespace_of(ctx.scenario.id)
    uuids = [uuid_from_index(offset + i, ns) for i in range(count)]
    env = ctx.s.env
    accounts: dict[str, str] = {}
    if mode == "2cha":
        accounts = await import_accounts(ctx, uuids, offset=offset)
    devices = []
    for u in uuids:
        kwargs: dict[str, Any] = dict(host=env.mqtt_host, port=env.mqtt_port, topic_root=env.topic_root,
                                      mode=mode)
        if mode == "2cha":
            kwargs["password"] = accounts[u]
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
    return devices


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
