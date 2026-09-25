"""작은 시나리오 프레임워크.

시나리오 = `@scenario(...)` 로 등록한 async 함수 하나. 함수는 `Ctx` 를 받아
setup → run → assert 를 자기 안에서 하고, 정리는 `ctx.defer()` 로 등록한다(teardown 은
실패해도 반드시 돈다). 판정은 PASS / FAIL(단언 실패) / ERROR(예외) / SKIP(전제 미충족).
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

_C = {"dim": "\033[90m", "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
      "cyan": "\033[96m", "off": "\033[0m"}
COLOR = sys.stdout.isatty()


def paint(text: str, color: str) -> str:
    return f"{_C[color]}{text}{_C['off']}" if COLOR else text


class Skip(Exception):
    """전제가 안 맞아 판정을 내릴 수 없다(서비스 없음, 플래그 없음 …)."""


class Fail(AssertionError):
    """합격 기준 미달."""


@dataclass
class Scenario:
    id: str
    title: str
    phase: int
    func: Callable[["Ctx"], Awaitable[None]]
    timeout: float = 180.0
    #: "approval_gate" 처럼 특정 기능/플래그가 있어야 하는 시나리오. `--phase` 로 열어 준다.
    requires: tuple[str, ...] = ()
    docker: bool = False
    tags: tuple[str, ...] = ()
    doc: str = ""


REGISTRY: dict[str, Scenario] = {}


def scenario(id: str, title: str, *, phase: int, timeout: float = 180.0,
             requires: tuple[str, ...] | str = (), docker: bool = False,
             tags: tuple[str, ...] = ()) -> Callable:
    if isinstance(requires, str):
        requires = (requires,)

    def deco(func: Callable[["Ctx"], Awaitable[None]]) -> Callable:
        if id in REGISTRY:
            raise ValueError(f"시나리오 id 중복: {id}")
        REGISTRY[id] = Scenario(id=id, title=title, phase=phase, func=func, timeout=timeout,
                                requires=tuple(requires), docker=docker, tags=tags,
                                doc=inspect.getdoc(func) or "")
        return func
    return deco


@dataclass
class Result:
    scenario: Scenario
    status: str  # PASS / FAIL / ERROR / SKIP
    seconds: float
    message: str = ""
    log: list[str] = field(default_factory=list)


class Ctx:
    """시나리오 하나가 쓰는 도구 상자. 서비스 핸들은 `services` 에 있다."""

    def __init__(self, scenario: Scenario, services: Any, options: Any) -> None:
        self.scenario = scenario
        self.s = services
        self.opt = options
        self.log_lines: list[str] = []
        self._deferred: list[Callable[[], Awaitable[None] | None]] = []
        self.started = time.monotonic()

    # ── 기록 ────────────────────────────────────────────────────────────
    def log(self, message: str) -> None:
        stamp = f"{time.monotonic() - self.started:6.1f}s"
        line = f"  {stamp}  {message}"
        self.log_lines.append(line)
        if not getattr(self.opt, "quiet", False):
            print(paint(line, "dim"), flush=True)

    # ── 판정 ────────────────────────────────────────────────────────────
    def check(self, condition: bool, message: str) -> None:
        if not condition:
            raise Fail(message)
        self.log(paint("ok ", "green") + message)

    def check_eq(self, actual: Any, expected: Any, what: str) -> None:
        self.check(actual == expected, f"{what}: {actual!r} == {expected!r}")

    def skip(self, reason: str) -> None:
        raise Skip(reason)

    def require_flag(self, name: str) -> None:
        """`--phase N` 또는 `--allow-*` 로 열리는 기능 플래그가 없으면 건너뛴다."""
        if name not in getattr(self.opt, "flags", ()):
            raise Skip(f"'{name}' 필요 — --phase/--allow-docker 로 연다")

    # ── 대기 ────────────────────────────────────────────────────────────
    async def wait_until(self, predicate: Callable[[], Awaitable[Any] | Any], *, timeout: float,
                         interval: float = 0.5, what: str = "") -> Any:
        """predicate 가 참(truthy)이 될 때까지 interval 마다 확인. 시간 초과면 Fail.

        고정 sleep 대신 이것을 쓴다 — 상한은 두되 조건이 되면 바로 넘어간다.
        """
        deadline = time.monotonic() + timeout
        last: Any = None
        while True:
            last = predicate()
            if inspect.isawaitable(last):
                last = await last
            if last:
                if what:
                    self.log(f"{what} ({timeout:.0f}s 안에 충족)")
                return last
            if time.monotonic() >= deadline:
                raise Fail(f"{what or '조건'}: {timeout:.0f}s 안에 충족되지 않음 (마지막 값 {last!r})")
            await asyncio.sleep(interval)

    async def hold(self, seconds: float, why: str) -> None:
        """조건으로 표현할 수 없는 짧은 대기(배치 flush 등). 2초를 넘기지 않는다."""
        if seconds > 2.0:
            raise ValueError("고정 대기는 2초 이하로 — 그 이상은 wait_until 로 조건을 걸 것")
        self.log(f"{seconds:.1f}s 대기: {why}")
        await asyncio.sleep(seconds)

    # ── 정리 ────────────────────────────────────────────────────────────
    def defer(self, fn: Callable[[], Awaitable[None] | None]) -> None:
        self._deferred.append(fn)

    async def teardown(self) -> None:
        while self._deferred:
            fn = self._deferred.pop()
            try:
                result = fn()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=30)
            except Exception as exc:  # noqa: BLE001 — 정리 실패는 판정에 영향 없음, 기록만
                self.log(f"정리 중 오류(무시): {type(exc).__name__}: {exc}")


async def run_one(scn: Scenario, services: Any, options: Any) -> Result:
    ctx = Ctx(scn, services, options)
    started = time.monotonic()
    print(paint(f"\n▶ {scn.id} {scn.title}", "cyan") + paint(f"  (phase {scn.phase}, timeout {scn.timeout:.0f}s)", "dim"))
    status, message = "PASS", ""
    try:
        for flag in scn.requires:
            ctx.require_flag(flag)
        if scn.docker:
            ctx.require_flag("docker")
        await asyncio.wait_for(scn.func(ctx), timeout=scn.timeout)
    except Skip as exc:
        status, message = "SKIP", str(exc)
    except Fail as exc:
        status, message = "FAIL", str(exc)
    except asyncio.TimeoutError:
        status, message = "FAIL", f"시나리오 timeout {scn.timeout:.0f}s 초과"
    except Exception as exc:  # noqa: BLE001
        status = "ERROR"
        message = f"{type(exc).__name__}: {exc}"
        ctx.log_lines.append(traceback.format_exc())
    finally:
        await ctx.teardown()
    seconds = time.monotonic() - started
    color = {"PASS": "green", "FAIL": "red", "ERROR": "red", "SKIP": "yellow"}[status]
    print(paint(f"  {status}", color) + f"  {scn.id}  {seconds:.1f}s" + (f"  — {message}" if message else ""))
    return Result(scn, status, seconds, message, ctx.log_lines)


def write_report(results: list[Result], path: Path, env_summary: dict[str, str]) -> None:
    """junit 비슷한 마크다운 보고서."""
    counts = {k: sum(1 for r in results if r.status == k) for k in ("PASS", "FAIL", "ERROR", "SKIP")}
    lines = [
        "# 시나리오 시험 보고서",
        "",
        f"- 실행: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 결과: PASS {counts['PASS']} / FAIL {counts['FAIL']} / ERROR {counts['ERROR']} / SKIP {counts['SKIP']}",
        "- 환경: " + ", ".join(f"{k}={v}" for k, v in env_summary.items()),
        "",
        "| ID | 제목 | 단계 | 판정 | 시간 | 메시지 |",
        "|---|---|---|---|---:|---|",
    ]
    for r in results:
        msg = r.message.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r.scenario.id} | {r.scenario.title} | {r.scenario.phase}차 | {r.status} | {r.seconds:.1f}s | {msg} |")
    lines.append("")
    for r in results:
        if r.status in ("FAIL", "ERROR") or getattr(r, "verbose", False):
            lines += [f"## {r.scenario.id} {r.scenario.title} — {r.status}", "", "```"]
            lines += [ln.replace("\033[90m", "").replace("\033[92m", "").replace("\033[0m", "") for ln in r.log]
            lines += ["```", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
