"""fleet 계정 CSV, 시나리오 프레임워크(등록·판정·보고서), metrics 도우미."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from tools.scenarios import framework
from tools.scenarios.common import flatten, metric_keys, metric_sum, namespace_of
from tools.scenarios.framework import Ctx, Fail, Result, Scenario, Skip, run_one, write_report
from tools.sim.fleet import Fleet, FleetOptions, generate_password, read_accounts, write_accounts


def test_write_and_read_accounts(tmp_path: Path):
    path = tmp_path / "acc.csv"
    rows = write_accounts(path, 5, namespace=0x0206, offset=10)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("uuid,password\n") and "\r" not in text
    back = read_accounts(path)
    assert dict(rows) == back and len(back) == 5
    assert all(re.match(r"^51A00206[0-9A-F]{16}$", u) for u in back)
    assert all(re.match(r"^[A-Za-z0-9]{24}$", p) for p in back.values())
    assert generate_password() != generate_password()


def test_fleet_2cha_requires_accounts(tmp_path: Path):
    with pytest.raises(ValueError):
        Fleet(FleetOptions(count=2, mode="2cha", accounts=None))
    path = tmp_path / "acc.csv"
    write_accounts(path, 2)
    fleet = Fleet(FleetOptions(count=2, mode="2cha", accounts=read_accounts(path)))
    assert [d.username for d in fleet.devices] == [d.uuid for d in fleet.devices]
    fleet1 = Fleet(FleetOptions(count=2, mode="1cha"))
    assert {d.username for d in fleet1.devices} == {"solarlte-test"}
    assert fleet1.totals()["connected"] == 0


def test_namespace_of():
    assert namespace_of("S2-03") == 0x0203 and namespace_of("S5-01") == 0x0501
    with pytest.raises(ValueError):
        namespace_of("X")


def test_metric_helpers():
    m = {"mqtt": {"payload_invalid": 3, "uuid_mismatch": 1}, "telemetry_dropped": 0, "register": {"queue": 4}, "ok": True}
    assert flatten(m)["mqtt.payload_invalid"] == 3
    assert metric_sum(m, r"(invalid|mismatch)") == 4
    assert metric_sum(m, r"telemetry_dropped") == 0
    assert metric_keys(m, r"register.*queue") == ["register.queue"]
    assert metric_sum(m, r"^ok$") == 0  # bool 은 세지 않는다


class _Opt:
    flags = {"docker"}
    quiet = True


class _Services:
    pass


def _scn(fn, **kw) -> Scenario:
    return Scenario(id="T-1", title="t", phase=2, func=fn, timeout=kw.pop("timeout", 5), **kw)


async def test_run_one_pass_fail_error_skip_and_teardown_order():
    order = []

    async def ok(ctx: Ctx):
        ctx.defer(lambda: order.append("a"))
        ctx.defer(lambda: order.append("b"))
        ctx.check(True, "fine")
        ctx.check_eq(1, 1, "eq")
    r = await run_one(_scn(ok), _Services(), _Opt())
    assert r.status == "PASS" and order == ["b", "a"]  # LIFO

    async def fail(ctx: Ctx):
        ctx.defer(lambda: order.append("cleanup"))
        ctx.check(False, "nope")
    r = await run_one(_scn(fail), _Services(), _Opt())
    assert r.status == "FAIL" and r.message == "nope" and order[-1] == "cleanup"

    async def boom(ctx: Ctx):
        raise RuntimeError("x")
    r = await run_one(_scn(boom), _Services(), _Opt())
    assert r.status == "ERROR" and "RuntimeError" in r.message

    async def skip(ctx: Ctx):
        ctx.skip("no service")
    r = await run_one(_scn(skip), _Services(), _Opt())
    assert r.status == "SKIP" and r.message == "no service"

    async def needs_flag(ctx: Ctx):
        raise AssertionError("must not run")
    r = await run_one(_scn(needs_flag, requires=("approval_gate",)), _Services(), _Opt())
    assert r.status == "SKIP" and "approval_gate" in r.message
    r = await run_one(_scn(needs_flag, docker=True), _Services(), _Opt())  # docker 플래그는 있음 → 실행됨
    assert r.status == "ERROR"

    async def slow(ctx: Ctx):
        await asyncio.sleep(5)
    r = await run_one(_scn(slow, timeout=0.2), _Services(), _Opt())
    assert r.status == "FAIL" and "timeout" in r.message


async def test_wait_until_and_hold_limits():
    ctx = Ctx(_scn(lambda c: None), _Services(), _Opt())
    n = [0]

    async def pred():
        n[0] += 1
        return n[0] >= 3
    assert await ctx.wait_until(pred, timeout=2, interval=0.01)
    with pytest.raises(Fail):
        await ctx.wait_until(lambda: False, timeout=0.05, interval=0.01, what="never")
    with pytest.raises(ValueError):
        await ctx.hold(3, "too long")


def test_report_markdown(tmp_path: Path):
    scn = _scn(lambda c: None)
    results = [Result(scn, "PASS", 1.0), Result(scn, "FAIL", 2.0, "bad | pipe", ["  0.1s  line"])]
    path = tmp_path / "out" / "report.md"
    write_report(results, path, {"mqtt": "x"})
    text = path.read_text(encoding="utf-8")
    assert "PASS 1 / FAIL 1" in text and "bad \\| pipe" in text and "```" in text
    assert "\r" not in text


def test_registry_has_all_required_scenarios():
    from tools.scenarios import phase2, phase3, phase5  # noqa: F401
    ids = set(framework.REGISTRY)
    assert {f"S2-{i:02d}" for i in range(1, 15)} <= ids
    assert {f"S3-{i:02d}" for i in range(1, 8)} <= ids
    assert "S5-01" in ids
    for s in framework.REGISTRY.values():
        assert s.doc, f"{s.id} 에 docstring(목적·합격 기준) 이 없다"
        if s.phase >= 3:
            assert s.requires, f"{s.id} 는 requires 로 잠겨 있어야 한다"
