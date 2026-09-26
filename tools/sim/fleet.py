"""가짜 단말 N 대를 한꺼번에 띄운다.

    python -m tools.sim.fleet --count 100 --ti 60                       # 2cha(1.4.0): HMAC 계정, 승인 게이트 ON
    python -m tools.sim.fleet --count 1000 --storm --storm-window 10 --workers 3
    python -m tools.sim.fleet --count 100 --mode 1cha                   # 1차 펌웨어(공용 계정)
    python -m tools.sim.fleet gen-passwords --count 3                   # uuid,password (mosquitto_pub 확인용)

매초 접속 수 / TM 발행 수 / ACK 수를 찍는다. Ctrl+C 로 내리면 정상 DISCONNECT 로 끊고,
`--cut-after` 로 "N초 뒤 전부 강제 절단 → `--storm-window` 안에 재접속" 폭주를 흉내낼 수 있다.

승인은 서버가 한다(`PATCH /api/devices/{uuid}/state`). fleet 에는 승인 기능이 없다 — 2cha 단말은
REGISTER_ACK ACTIVE 를 받기 전까지 Telemetry 를 보내지 않는다(§3.4).

Windows 의 selector 이벤트 루프는 소켓 512개가 한계라 500대 이상은 `--workers` 로
프로세스를 나눈다(리눅스는 한 프로세스로 1만 대까지 되지만 ulimit -n 을 올려야 한다).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from tools.sim.device import (KA_DEFAULT, SimDevice, device_password, hmac_key_from_hex, uuid_from_index)
from tools.sim.env import ENV

log = logging.getLogger("sim.fleet")

#: Windows select() 한계 512 에서 여유를 둔 프로세스당 최대 단말 수.
WINDOWS_MAX_PER_PROCESS = 400


def passwords_for(count: int, *, namespace: int = 0, offset: int = 0,
                  key: bytes | None = None) -> list[tuple[str, str]]:
    """(uuid, HMAC 비밀번호) 목록. 사양서 §1.1.2.2 전환 순서 2 "mosquitto_pub -u <UUID> -P <계산값>" 확인용."""
    key = key or hmac_key_from_hex(ENV.mqtt_hmac_key)
    return [(u, device_password(key, u)) for u in (uuid_from_index(offset + i, namespace) for i in range(count))]


@dataclass
class FleetOptions:
    count: int
    offset: int = 0
    namespace: int = 0
    ti: int = 600
    ka: int = KA_DEFAULT
    mode: str = "2cha"
    #: 2cha 에서 False 면 공용 계정(HMAC 펌웨어 이전 단말). None 이면 모드 기본값.
    hmac: bool | None = None
    hmac_key: bytes | None = None
    jitter: float = 0.0
    time_scale: float = 1.0
    storm: bool = False
    storm_window: float = 10.0
    cut_after: float | None = None
    duration: float | None = None
    approval_gate: bool | None = None
    legacy_register: bool = False
    legacy_t_key: bool = False
    lwt: bool | None = None
    fw: str | None = None
    host: str = ENV.mqtt_host
    port: int = ENV.mqtt_port
    topic_root: str = ENV.topic_root
    quiet: bool = False


class Fleet:
    """SimDevice 묶음. 시나리오에서도 이 클래스를 직접 쓴다."""

    def __init__(self, opts: FleetOptions) -> None:
        self.opts = opts
        self.devices: list[SimDevice] = []
        key = opts.hmac_key
        if key is None and opts.mode == "2cha" and opts.hmac is not False:
            key = hmac_key_from_hex(ENV.mqtt_hmac_key)
        for i in range(opts.count):
            uuid = uuid_from_index(opts.offset + i, opts.namespace)
            kwargs: dict = dict(
                host=opts.host, port=opts.port, topic_root=opts.topic_root,
                mode=opts.mode, hmac=opts.hmac, hmac_key=key,
                ti=opts.ti, ka=opts.ka, time_scale=opts.time_scale,
                approval_gate=opts.approval_gate, fw=opts.fw,
                legacy_register=opts.legacy_register, legacy_t_key=opts.legacy_t_key,
                lwt=opts.lwt, reconnect_jitter=opts.jitter,
            )
            if opts.mode == "1cha" or opts.hmac is False:
                kwargs["username"], kwargs["password"] = ENV.mqtt_test_user, ENV.mqtt_test_password
            self.devices.append(SimDevice(uuid, **kwargs))
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
        """전부 강제 절단(DISCONNECT 없음) 뒤 window 초 안에 재접속시킨다 — 재접속 폭주."""
        delays = self._spread(window)
        for dev, delay in zip(self.devices, delays):
            await dev.disconnect(hard=True, reconnect=True, reconnect_after=delay)

    def connected_count(self) -> int:
        return sum(1 for d in self.devices if d.is_connected)

    def state_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.devices:
            k = d.state or "NONE"
            out[k] = out.get(k, 0) + 1
        return out

    def totals(self) -> dict[str, int]:
        keys = ("connects", "connect_failures", "disconnects", "register_sent", "tm_sent", "tm_suppressed",
                "result_sent", "config_ack_ok", "config_ack_range", "config_ack_state", "config_ack_flash",
                "register_ack_rx", "ping_rx")
        out = {k: sum(getattr(d.stats, k) for d in self.devices) for k in keys}
        out["connected"] = self.connected_count()
        out["active"] = sum(1 for d in self.devices if d.state == "ACTIVE")
        return out


async def run_fleet(opts: FleetOptions) -> int:
    fleet = Fleet(opts)
    if not opts.quiet:
        print(f"{opts.host}:{opts.port} 로 단말 {opts.count}대 "
              f"(uuid {fleet.devices[0].uuid} ~ {fleet.devices[-1].uuid}, mode={opts.mode}, "
              f"hmac={fleet.devices[0].hmac}, ti={opts.ti}, ka={opts.ka}, gate={fleet.devices[0].gate.enabled})")
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
                print(f"[{elapsed:6.0f}s] 접속 {now['connected']}/{opts.count}  ACTIVE {now['active']}  "
                      f"REGISTER {now['register_sent']} (+{now['register_sent'] - prev['register_sent']})  "
                      f"TM {now['tm_sent']} (+{now['tm_sent'] - prev['tm_sent']})  "
                      f"ACK ok/range/state/flash {now['config_ack_ok']}/{now['config_ack_range']}/"
                      f"{now['config_ack_state']}/{now['config_ack_flash']}  "
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
    parser = argparse.ArgumentParser(description="가짜 단말 N 대 (펌웨어 1.4.0 모델)")
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("gen-passwords", help="uuid,password(HMAC 계산값) 출력 — mosquitto_pub 확인용")
    gen.add_argument("--count", type=int, default=3)
    gen.add_argument("--namespace", type=int, default=0)
    gen.add_argument("--offset", type=int, default=0)
    gen.add_argument("--hmac-key", default=None, help="hex 64자. 없으면 MQTT_HMAC_KEY → 사양서 시험 키")

    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--offset", type=int, default=0, help="uuid 순번 시작")
    parser.add_argument("--namespace", type=int, default=0, help="uuid 네임스페이스(0~65535)")
    parser.add_argument("--ti", type=int, default=600, help="Telemetry 주기(초)")
    parser.add_argument("--ka", type=int, default=KA_DEFAULT, help="CONNECT keepalive(초). 브로커 끊김 판정은 ×1.5")
    parser.add_argument("--mode", choices=["1cha", "2cha"], default="2cha",
                        help="2cha = 1.4.0(HMAC 계정·승인 게이트·TELEMETRY), 1cha = 1차 펌웨어(공용 계정)")
    parser.add_argument("--hmac-key", default=None, help="hex 64자. 없으면 MQTT_HMAC_KEY → 사양서 시험 키")
    parser.add_argument("--no-hmac", action="store_true", help="2cha 인데 공용 계정으로 접속(HMAC 펌웨어 이전)")
    parser.add_argument("--jitter", type=float, default=0.0, help="접속·재접속 지터 최대(초)")
    parser.add_argument("--time-scale", type=float, default=1.0, help="재전송·재접속 대기 압축 배수")
    parser.add_argument("--storm", action="store_true", help="전부 --storm-window 초 안에 접속")
    parser.add_argument("--storm-window", type=float, default=10.0)
    parser.add_argument("--cut-after", type=float, default=None, help="N초 뒤 전부 강제 절단 후 폭주 재접속")
    parser.add_argument("--duration", type=float, default=None, help="N초 뒤 정상 종료")
    parser.add_argument("--no-approval-gate", action="store_true", help="승인 게이트 끔(승인 없이 TM)")
    parser.add_argument("--fw", default=None, help="REGISTER/TM 의 fw 문자열(기본 1.4.0 / 1cha 1.0.0)")
    parser.add_argument("--legacy-register", action="store_true", help="REGISTER 에 cv/ss/ti/ka 없음(1차 펌웨어)")
    parser.add_argument("--legacy-t-key", action="store_true", help='Telemetry 를 "t":"TM" 으로')
    parser.add_argument("--lwt", action="store_true", help="LWT 등록(모뎀은 못 넣는다 — 시험용)")
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

    try:
        key = hmac_key_from_hex(getattr(args, "hmac_key", None) or ENV.mqtt_hmac_key)
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    if args.command == "gen-passwords":
        for u, pw in passwords_for(args.count, namespace=args.namespace, offset=args.offset, key=key):
            print(f"{u},{pw}")
        return 0

    workers = args.workers
    if workers <= 1 and sys.platform == "win32" and args.count > WINDOWS_MAX_PER_PROCESS:
        workers = -(-args.count // WINDOWS_MAX_PER_PROCESS)
        print(f"Windows 소켓 한계로 {workers}개 프로세스로 나눈다", file=sys.stderr)
    if workers > 1:
        return _spawn_workers(argv, args.count, args.offset, workers)

    opts = FleetOptions(
        count=args.count, offset=args.offset, namespace=args.namespace, ti=args.ti, ka=args.ka,
        mode=args.mode, hmac=(False if args.no_hmac else None), hmac_key=key,
        jitter=args.jitter, time_scale=args.time_scale,
        storm=args.storm, storm_window=args.storm_window, cut_after=args.cut_after,
        duration=args.duration, approval_gate=(False if args.no_approval_gate else None),
        legacy_register=args.legacy_register, legacy_t_key=args.legacy_t_key,
        lwt=(True if args.lwt else None), fw=args.fw, host=args.host, port=args.port, quiet=args.quiet,
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
