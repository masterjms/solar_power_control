"""가짜 단말 N 대를 한꺼번에 띄운다.

    python -m tools.sim.fleet --count 100 --ti 60 --mode 2cha --accounts accounts.csv
    python -m tools.sim.fleet --count 1000 --storm --storm-window 10 --workers 3
    python -m tools.sim.fleet gen-accounts --count 100 --out accounts.csv

매초 접속 수 / TM 발행 수 / ACK 수를 찍는다. Ctrl+C 로 내리면 정상 DISCONNECT 로 끊고,
`--cut-after` 로 "N초 뒤 전부 강제 절단 → `--storm-window` 안에 재접속" 폭주를 흉내낼 수 있다.

Windows 의 selector 이벤트 루프는 소켓 512개가 한계라 500대 이상은 `--workers` 로
프로세스를 나눈다(리눅스는 한 프로세스로 1만 대까지 되지만 ulimit -n 을 올려야 한다).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import logging
import os
import random
import secrets
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from tools.sim.device import SimDevice, uuid_from_index
from tools.sim.env import ENV

log = logging.getLogger("sim.fleet")

#: Windows select() 한계 512 에서 여유를 둔 프로세스당 최대 단말 수.
WINDOWS_MAX_PER_PROCESS = 400


def generate_password(length: int = 24) -> str:
    """단말별 무작위 비밀번호(§1.1.2.2). 모뎀 AT 명령에 안전한 영숫자만."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def write_accounts(path: Path, count: int, namespace: int = 0, offset: int = 0) -> list[tuple[str, str]]:
    """`uuid,password` CSV 를 만든다. PC 설정 도구가 만드는 파일과 같은 모양."""
    rows = [(uuid_from_index(offset + i, namespace), generate_password()) for i in range(count)]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["uuid", "password"])
        writer.writerows(rows)
    return rows


def read_accounts(path: Path) -> dict[str, str]:
    accounts: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            uuid = (row.get("uuid") or "").strip().upper()
            if uuid:
                accounts[uuid] = (row.get("password") or "").strip()
    return accounts


@dataclass
class FleetOptions:
    count: int
    offset: int = 0
    namespace: int = 0
    ti: int = 600
    mode: str = "2cha"
    accounts: dict[str, str] | None = None
    jitter: float = 0.0
    time_scale: float = 1.0
    storm: bool = False
    storm_window: float = 10.0
    cut_after: float | None = None
    duration: float | None = None
    approval_gate: bool = False
    legacy_register: bool = False
    legacy_t_key: bool = False
    lwt: bool = True
    host: str = ENV.mqtt_host
    port: int = ENV.mqtt_port
    topic_root: str = ENV.topic_root
    quiet: bool = False


class Fleet:
    """SimDevice 묶음. 시나리오에서도 이 클래스를 직접 쓴다."""

    def __init__(self, opts: FleetOptions) -> None:
        self.opts = opts
        self.devices: list[SimDevice] = []
        for i in range(opts.count):
            uuid = uuid_from_index(opts.offset + i, opts.namespace)
            password = None
            username = None
            if opts.mode == "2cha":
                if opts.accounts is None or uuid not in opts.accounts:
                    raise ValueError(f"{uuid} 의 비밀번호가 accounts CSV 에 없다 (2차 모드)")
                password = opts.accounts[uuid]
            else:
                username, password = ENV.mqtt_test_user, ENV.mqtt_test_password
            self.devices.append(SimDevice(
                uuid,
                host=opts.host, port=opts.port, topic_root=opts.topic_root,
                mode=opts.mode, username=username, password=password,
                ti=opts.ti, time_scale=opts.time_scale,
                approval_gate=opts.approval_gate,
                legacy_register=opts.legacy_register, legacy_t_key=opts.legacy_t_key,
                lwt=opts.lwt, reconnect_jitter=opts.jitter,
            ))
        self._rng = random.Random(opts.namespace * 1000 + opts.offset)

    def _spread(self, window: float) -> list[float]:
        """window 초 안에 고르게(무작위) 흩어진 시작 지연."""
        if window <= 0:
            return [0.0] * len(self.devices)
        return [self._rng.uniform(0, window) for _ in self.devices]

    async def start(self, *, window: float | None = None) -> None:
        """전부 띄운다. window(초) 안에 흩어 붙는다(폭주 흉내는 window 를 작게)."""
        if window is None:
            window = self.opts.storm_window if self.opts.storm else self.opts.jitter
        delays = self._spread(window)

        async def launch(dev: SimDevice, delay: float) -> None:
            await asyncio.sleep(delay)
            await dev.start()

        await asyncio.gather(*(launch(d, t) for d, t in zip(self.devices, delays)))

    async def wait_all_connected(self, timeout: float = 60.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            n = self.connected_count()
            if n == len(self.devices):
                return n
            await asyncio.sleep(0.2)
        return self.connected_count()

    async def stop(self, *, graceful: bool = True) -> None:
        await asyncio.gather(*(d.stop(graceful=graceful) for d in self.devices), return_exceptions=True)

    async def cut_all(self, *, window: float = 10.0) -> None:
        """전부 강제 절단(LWT 발생) 뒤 window 초 안에 재접속시킨다 — 재접속 폭주."""
        delays = self._spread(window)
        for dev, delay in zip(self.devices, delays):
            await dev.disconnect(hard=True, reconnect=True, reconnect_after=delay)

    def connected_count(self) -> int:
        return sum(1 for d in self.devices if d.is_connected)

    def totals(self) -> dict[str, int]:
        keys = ("connects", "connect_failures", "disconnects", "register_sent", "tm_sent",
                "result_sent", "config_ack_ok", "config_ack_range", "register_ack_rx", "ping_rx")
        out = {k: sum(getattr(d.stats, k) for d in self.devices) for k in keys}
        out["connected"] = self.connected_count()
        return out


async def run_fleet(opts: FleetOptions) -> int:
    fleet = Fleet(opts)
    if not opts.quiet:
        print(f"{opts.host}:{opts.port} 로 단말 {opts.count}대 "
              f"(uuid {fleet.devices[0].uuid} ~ {fleet.devices[-1].uuid}, mode={opts.mode}, ti={opts.ti})")
    stop_event = asyncio.Event()

    def _stop(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, _stop)

    await fleet.start()
    started = time.monotonic()
    cut_done = False
    prev = fleet.totals()
    try:
        while not stop_event.is_set():
            await asyncio.sleep(1.0)
            elapsed = time.monotonic() - started
            now = fleet.totals()
            if not opts.quiet:
                print(f"[{elapsed:6.0f}s] 접속 {now['connected']}/{opts.count}  "
                      f"REGISTER {now['register_sent']} (+{now['register_sent'] - prev['register_sent']})  "
                      f"TM {now['tm_sent']} (+{now['tm_sent'] - prev['tm_sent']})  "
                      f"ACK ok/range {now['config_ack_ok']}/{now['config_ack_range']}  "
                      f"REG_ACK {now['register_ack_rx']}  실패 {now['connect_failures']}", flush=True)
            prev = now
            if opts.cut_after is not None and not cut_done and elapsed >= opts.cut_after:
                cut_done = True
                if not opts.quiet:
                    print(f"--- 전부 강제 절단, {opts.storm_window}초 안에 재접속 ---", flush=True)
                await fleet.cut_all(window=opts.storm_window)
            if opts.duration is not None and elapsed >= opts.duration:
                break
    finally:
        await fleet.stop(graceful=True)
        if not opts.quiet:
            print("종료:", fleet.totals())
    return 0


def _spawn_workers(argv: list[str], count: int, offset: int, workers: int) -> int:
    """단말 범위를 나눠 같은 명령을 자식 프로세스로 돌린다(Windows 소켓 한계 회피)."""
    import subprocess

    # 자식마다 다시 정할 옵션(--workers/--count/--offset)은 `--k v`, `--k=v` 두 형태 모두 뗀다.
    strip = {"--workers", "--count", "--offset"}
    common: list[str] = []
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in strip:
            skip = True
            continue
        if a.split("=", 1)[0] in strip:
            continue
        common.append(a)

    per = -(-count // workers)
    procs = []
    for w in range(workers):
        start = offset + w * per
        n = min(per, count - w * per)
        if n <= 0:
            break
        cmd = [sys.executable, "-m", "tools.sim.fleet", *common, "--count", str(n), "--offset", str(start)]
        procs.append(subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parents[2])))
    try:
        return max(p.wait() for p in procs)
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
        for p in procs:
            with contextlib.suppress(Exception):
                p.wait(timeout=10)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="가짜 단말 N 대")
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("gen-accounts", help="uuid,password CSV 생성")
    gen.add_argument("--count", type=int, required=True)
    gen.add_argument("--out", type=Path, default=Path("accounts.csv"))
    gen.add_argument("--namespace", type=int, default=0)
    gen.add_argument("--offset", type=int, default=0)

    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--offset", type=int, default=0, help="uuid 순번 시작")
    parser.add_argument("--namespace", type=int, default=0, help="uuid 네임스페이스(0~65535)")
    parser.add_argument("--ti", type=int, default=600, help="Telemetry 주기(초)")
    parser.add_argument("--mode", choices=["1cha", "2cha"], default="2cha")
    parser.add_argument("--accounts", type=Path, default=None, help="2차 모드 uuid,password CSV")
    parser.add_argument("--jitter", type=float, default=0.0, help="접속·재접속 지터 최대(초)")
    parser.add_argument("--time-scale", type=float, default=1.0, help="재전송·재접속 대기 압축 배수")
    parser.add_argument("--storm", action="store_true", help="전부 --storm-window 초 안에 접속")
    parser.add_argument("--storm-window", type=float, default=10.0)
    parser.add_argument("--cut-after", type=float, default=None, help="N초 뒤 전부 강제 절단 후 폭주 재접속")
    parser.add_argument("--duration", type=float, default=None, help="N초 뒤 정상 종료")
    parser.add_argument("--approval-gate", action="store_true", help="3차 승인 게이트 동작")
    parser.add_argument("--legacy-register", action="store_true", help="REGISTER 에 cv/ss/ti 없음(1차 펌웨어)")
    parser.add_argument("--legacy-t-key", action="store_true", help='Telemetry 를 "t":"TM" 으로')
    parser.add_argument("--no-lwt", action="store_true", help="LWT 미등록(P-2 모뎀)")
    parser.add_argument("--workers", type=int, default=1, help="프로세스 수(Windows 는 400대/프로세스)")
    parser.add_argument("--host", default=ENV.mqtt_host)
    parser.add_argument("--port", type=int, default=ENV.mqtt_port)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


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
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "gen-accounts":
        rows = write_accounts(args.out, args.count, args.namespace, args.offset)
        print(f"{args.out}: {len(rows)}건 ({rows[0][0]} ~ {rows[-1][0]})")
        return 0

    workers = args.workers
    if workers <= 1 and sys.platform == "win32" and args.count > WINDOWS_MAX_PER_PROCESS:
        workers = -(-args.count // WINDOWS_MAX_PER_PROCESS)
        print(f"Windows 소켓 한계로 {workers}개 프로세스로 나눈다", file=sys.stderr)
    if workers > 1:
        return _spawn_workers(argv, args.count, args.offset, workers)

    accounts = read_accounts(args.accounts) if args.accounts else None
    opts = FleetOptions(
        count=args.count, offset=args.offset, namespace=args.namespace, ti=args.ti,
        mode=args.mode, accounts=accounts, jitter=args.jitter, time_scale=args.time_scale,
        storm=args.storm, storm_window=args.storm_window, cut_after=args.cut_after,
        duration=args.duration, approval_gate=args.approval_gate,
        legacy_register=args.legacy_register, legacy_t_key=args.legacy_t_key,
        lwt=not args.no_lwt, host=args.host, port=args.port, quiet=args.quiet,
    )
    if sys.platform == "win32":
        # aiomqtt(paho add_reader) 는 selector 루프가 필요하다.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(run_fleet(opts))
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
