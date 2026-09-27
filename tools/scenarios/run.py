"""시나리오 러너.

    python -m tools.scenarios.run --list
    python -m tools.scenarios.run --all
    python -m tools.scenarios.run --only S2-01,S2-03 --report tools/scenarios/out/report.md
    python -m tools.scenarios.run --all --phase-only 2                  # 2차만
    python -m tools.scenarios.run --all --phase-only 3 --allow-docker   # 3차(승인 게이트)만, docker 시나리오 포함
    python -m tools.scenarios.run --all --phase 5                       # 5차(트리·COMMAND·재시도·권한)까지 연다
    python -m tools.scenarios.run --all --phase-only 5 --phase 5        # 5차만

`--list` 는 서비스 없이 동작한다. 서비스가 내려가 있으면 시나리오는 SKIP(이유 표시)로 끝나고
러너는 종료 코드 1 을 돌려준다(FAIL/ERROR 가 있어도 1).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 시나리오 모듈을 import 해야 REGISTRY 가 채워진다.
from tools.scenarios import phase2, phase3, phase5  # noqa: F401
from tools.scenarios.framework import REGISTRY, Result, Scenario, paint, run_one, write_report
from tools.scenarios.services import Services, selector_loop_policy
from tools.sim.env import ENV


@dataclass
class Options:
    flags: set[str] = field(default_factory=set)
    sim_mode: str = "auto"
    quiet: bool = False
    keep_rows: bool = False
    storm_count: int = 1000
    cmd_retry_timeout: float = 90.0
    cmd_finish_timeout: float = 30.0
    storm5_count: int = 200


def select(args: argparse.Namespace) -> list[Scenario]:
    items = sorted(REGISTRY.values(), key=lambda s: (s.phase, s.id))
    if args.only:
        wanted = [x.strip().upper() for x in args.only.split(",") if x.strip()]
        unknown = [w for w in wanted if w not in REGISTRY]
        if unknown:
            raise SystemExit(f"모르는 시나리오: {unknown}. --list 로 확인")
        items = [REGISTRY[w] for w in wanted]
    elif args.phase_only:
        items = [s for s in items if s.phase == args.phase_only]
    return items


def print_list() -> None:
    print(f"{'ID':7} {'단계':4} {'필요':20} {'제목'}")
    for s in sorted(REGISTRY.values(), key=lambda s: (s.phase, s.id)):
        need = ",".join([*s.requires, *(["docker"] if s.docker else [])])
        print(f"{s.id:7} {s.phase}차   {need:20} {s.title}")
    print(f"\n총 {len(REGISTRY)}개")


async def run_all(scenarios: list[Scenario], opt: Options, report: Path | None) -> int:
    services = Services(ENV)
    status = await services.probe()
    print("서비스:", ", ".join(f"{k}={paint(v, 'green' if v == 'ok' else 'red')}" for k, v in status.items()))
    print(f"브로커 {ENV.mqtt_host}:{ENV.mqtt_port}  백엔드 {ENV.backend_url}  DB {ENV.database_url.split('@')[-1]}")
    results: list[Result] = []
    try:
        for scn in scenarios:
            if not services.all_ok():
                down = ", ".join(f"{k}: {v}" for k, v in status.items() if v != "ok")
                results.append(Result(scn, "SKIP", 0.0, f"서비스 없음 — {down}"))
                print(paint(f"  SKIP  {scn.id}  — 서비스 없음", "yellow"))
                continue
            results.append(await run_one(scn, services, opt))
    finally:
        await services.close()

    counts = {k: sum(1 for r in results if r.status == k) for k in ("PASS", "FAIL", "ERROR", "SKIP")}
    print("\n" + "=" * 62)
    print(f"  PASS {counts['PASS']}  FAIL {counts['FAIL']}  ERROR {counts['ERROR']}  SKIP {counts['SKIP']}")
    for r in results:
        if r.status != "PASS":
            print(f"    {r.status:5} {r.scenario.id}  {r.message}")
    print("=" * 62)
    if report:
        write_report(results, report, {"mqtt": f"{ENV.mqtt_host}:{ENV.mqtt_port}", "backend": ENV.backend_url,
                                       "sim_mode": opt.sim_mode, "flags": ",".join(sorted(opt.flags)) or "-",
                                       **{f"svc_{k}": v for k, v in status.items()}})
        print(f"보고서: {report}")
    return 0 if counts["FAIL"] == 0 and counts["ERROR"] == 0 and counts["PASS"] > 0 else 1


def _utf8_console() -> None:
    """Windows 콘솔(cp949)에서 한글·기호가 깨지지 않게 stdout/stderr 를 UTF-8 로 맞춘다."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    p = argparse.ArgumentParser(description="시나리오 시험 러너")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="등록된 전부(단계 플래그로 열린 것만 실제 실행)")
    g.add_argument("--only", help="쉼표로 구분한 ID 목록, 예: S2-01,S2-03")
    g.add_argument("--list", action="store_true", help="목록만")
    p.add_argument("--phase", type=int, action="append", default=[],
                   help="이 단계 기능을 연다(5 → group_cmd). 2·3차는 항상 열려 있다. 여러 번 가능. "
                        "--phase-only 5 만 주면 5 도 연다")
    p.add_argument("--phase-only", type=int, default=None, help="--all 에서 이 단계 시나리오만")
    p.add_argument("--allow-docker", action="store_true", help="docker compose 를 조작하는 시나리오 허용")
    p.add_argument("--sim-mode", choices=["auto", "1cha", "2cha"], default="auto",
                   help="시뮬레이터 모드. auto = 2cha(1.4.0: HMAC 계정·승인 게이트). 1cha 는 공용 계정·1차 펌웨어")
    p.add_argument("--report", type=Path, default=None, help="마크다운 보고서 경로")
    p.add_argument("--keep-rows", action="store_true", help="시험 뒤 DB 행을 지우지 않는다(디버깅)")
    p.add_argument("--storm-count", type=int, default=1000, help="S2-10 단말 수")
    p.add_argument("--cmd-retry-timeout", type=float, default=90.0,
                   help="5차 자동 재시도(단말 TM 직후 개별 재발송) 대기 상한(초). COMMAND_RETRY_MIN_SEC 보다 커야 한다")
    p.add_argument("--cmd-finish-timeout", type=float, default=30.0,
                   help="S5-11 PARTIAL 종료 대기(초). 서버 COMMAND_TIMEOUT_SEC 가 이보다 길면 참고로만 기록")
    p.add_argument("--storm5-count", type=int, default=200, help="S5-12 단말 수(말단 5개로 나눈다)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if args.list or not (args.all or args.only):
        print_list()
        return 0

    flags: set[str] = set()
    if args.phase_only and args.phase_only >= 5:
        args.phase.append(args.phase_only)  # --phase-only 5 는 5차를 연다는 뜻이기도 하다
    for ph in args.phase:
        if ph >= 5:
            flags.add("group_cmd")
    if args.allow_docker:
        flags.add("docker")
    opt = Options(flags=flags, sim_mode=args.sim_mode, quiet=args.quiet, keep_rows=args.keep_rows,
                  storm_count=args.storm_count, cmd_retry_timeout=args.cmd_retry_timeout,
                  cmd_finish_timeout=args.cmd_finish_timeout, storm5_count=args.storm5_count)
    scenarios = select(args)
    selector_loop_policy()
    try:
        return asyncio.run(run_all(scenarios, opt, args.report))
    except KeyboardInterrupt:
        print("\n중단")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
