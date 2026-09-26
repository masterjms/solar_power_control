"""fleet(HMAC 비밀번호 목록·옵션), 시나리오 프레임워크(등록·판정·보고서), metrics 도우미, 시나리오 도우미."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from tools.scenarios import framework
from tools.scenarios.common import (check_config_set_shape, flatten, metric_keys, metric_sum, namespace_of,
                                    rx_delay, uuid_prefix)
from tools.scenarios.framework import Ctx, Fail, Result, Scenario, Skip, run_one, write_report
from tools.sim.device import TEST_HMAC_KEY_HEX, SimDevice, device_password, uuid_from_index
from tools.sim.fleet import Fleet, FleetOptions, passwords_for

TEST_KEY = bytes.fromhex(TEST_HMAC_KEY_HEX)


def test_passwords_for_is_deterministic_hmac():
    rows = passwords_for(5, namespace=0x0206, offset=10, key=TEST_KEY)
    assert len(rows) == 5
    assert all(re.match(r"^51A00206[0-9A-F]{16}$", u) for u, _ in rows)
    assert all(p == device_password(TEST_KEY, u) for u, p in rows)
    assert rows == passwords_for(5, namespace=0x0206, offset=10, key=TEST_KEY)


def test_fleet_2cha_uses_hmac_without_server_side_prep():
    fleet = Fleet(FleetOptions(count=2, mode="2cha", hmac_key=TEST_KEY, ka=120))
    assert [d.username for d in fleet.devices] == [d.uuid for d in fleet.devices]
    assert all(d.password == device_password(TEST_KEY, d.uuid) for d in fleet.devices)
    assert all(d.gate.enabled and d.ka == 120 and d.lwt is False for d in fleet.devices)
    fleet1 = Fleet(FleetOptions(count=2, mode="1cha"))
    assert {d.username for d in fleet1.devices} == {"solarlte-test"}
    assert all(not d.gate.enabled for d in fleet1.devices)
    fleet_nohmac = Fleet(FleetOptions(count=1, mode="2cha", hmac=False))
    assert fleet_nohmac.devices[0].username == "solarlte-test" and fleet_nohmac.devices[0].gate.enabled
    assert fleet1.totals()["connected"] == 0 and fleet1.totals()["active"] == 0
    assert fleet.state_counts() == {"NONE": 2}


def test_fleet_cli_parser_accepts_new_flags():
    from tools.sim.fleet import build_parser
    args = build_parser().parse_args(["--count", "3", "--ka", "10", "--hmac-key", "ab" * 32, "--no-approval-gate"])
    assert args.ka == 10 and args.hmac_key == "ab" * 32 and args.no_approval_gate
    gen = build_parser().parse_args(["gen-passwords", "--count", "2"])
    assert gen.command == "gen-passwords" and gen.count == 2


def test_namespace_of():
    assert namespace_of("S2-03") == 0x0203 and namespace_of("S5-01") == 0x0501
    assert uuid_prefix("S2-10") == "51A0020A"
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


def test_check_config_set_shape_rules():
    ctx = Ctx(_scn(lambda c: None), _Services(), _Opt())
    check_config_set_shape(ctx, {"type": "CONFIG_SET", "cv": 1, "ti": 600, "ka": 300}, cv=1, ti=600, ka=300, lat_lon=False)
    check_config_set_shape(ctx, {"type": "CONFIG_SET", "cv": 2, "ti": 600, "ka": 300, "lat": 37.0, "lon": 127.0}, lat_lon=True)
    with pytest.raises(Fail):  # cv 0 금지
        check_config_set_shape(ctx, {"type": "CONFIG_SET", "cv": 0, "ti": 600, "ka": 300})
    with pytest.raises(Fail):  # ka 누락 = 부분 전송
        check_config_set_shape(ctx, {"type": "CONFIG_SET", "cv": 1, "ti": 600})
    with pytest.raises(Fail):  # 좌표 없는데 키가 있음
        check_config_set_shape(ctx, {"type": "CONFIG_SET", "cv": 1, "ti": 600, "ka": 300, "lat": None}, lat_lon=False)
    with pytest.raises(Fail):  # 사양 밖 키
        check_config_set_shape(ctx, {"type": "CONFIG_SET", "cv": 1, "ti": 600, "ka": 300, "site": "x"})


def test_rx_delay_uses_last_rx_by_type():
    d = SimDevice(uuid_from_index(1, 0x0301), password="pw")
    assert rx_delay(d, "REGISTER_ACK", None) is None
    d.last_rx_by_type["REGISTER_ACK"] = 100.5
    assert rx_delay(d, "REGISTER_ACK", 100.0) == pytest.approx(0.5)
    assert rx_delay(d, "REGISTER_ACK", 101.0) is None  # 송신보다 먼저 받은 건 이번 것이 아니다
    assert rx_delay(d, "CONFIG_SET", 100.0) is None


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
    r = await run_one(_scn(needs_flag, requires=("group_cmd",)), _Services(), _Opt())
    assert r.status == "SKIP" and "group_cmd" in r.message
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
    assert {f"S3-{i:02d}" for i in range(1, 12)} <= ids
    assert "S5-01" in ids
    for s in framework.REGISTRY.values():
        assert s.doc, f"{s.id} 에 docstring(목적·합격 기준) 이 없다"
        if s.phase >= 5:
            assert s.requires, f"{s.id} 는 requires 로 잠겨 있어야 한다"
        if s.phase <= 3:
            assert not s.requires, f"{s.id}: 2·3차는 플래그 없이 돌아야 한다(단말 1.4.0 은 게이트 항상 ON)"
    assert {s.id for s in framework.REGISTRY.values() if s.docker} == {"S2-11", "S2-12", "S3-04"}


def test_run_list_does_not_need_services(capsys):
    from tools.scenarios.run import main
    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "S2-01" in out and "S3-11" in out and "S5-01" in out
