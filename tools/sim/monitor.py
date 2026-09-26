"""브로커 감시 · 사양 검증기 — `iotlight/#` 를 구독해서 흐르는 메시지를 보여주고 종류별로 센다.

    python -m tools.sim.monitor
    python -m tools.sim.monitor --seconds 120 --raw

단말이 보낸 것은 사양서와 대조해서 어긋나면 그 자리에서 표시한다:
  · topic UUID 가 24자리 대문자 16진수인가, payload uuid 와 같은가 (§1.1.3, §1.1.4)
  · `type` 값이 사양에 있는가, 1차 펌웨어의 `"t":"TM"` 이 보이는가 (§1.1.6)
  · `ts` 가 `YYMMDDThhmm` 인가, `sq` 가 건너뛰거나 되돌아갔는가
  · config topic 의 retain 이 REGISTER_ACK 뿐인가 (ADR-002)
Ctrl+C 로 끝내면 요약을 낸다. 실물 단말을 붙일 때 이 창을 띄워 둔다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

import aiomqtt

from tools.sim.env import ENV

DEVICE_TYPES = {"REGISTER", "TELEMETRY", "TM", "PONG", "CONFIG_ACK", "CMD_ACK", "LWT", "EV"}
SERVER_TYPES = {"PING", "CMD", "CONFIG_SET", "REGISTER_ACK", "SCH", "OTA", "STATUS_GET"}
_UUID_RE = re.compile(r"^[0-9A-F]{24}$")
_TS_RE = re.compile(r"^\d{6}T\d{4}$")

_C = {"dim": "\033[90m", "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
      "blue": "\033[94m", "cyan": "\033[96m", "off": "\033[0m"}


def paint(text: str, color: str, *, enabled: bool) -> str:
    return f"{_C[color]}{text}{_C['off']}" if enabled else text


class Monitor:
    def __init__(self, *, color: bool, raw: bool) -> None:
        self.color, self.raw = color, raw
        self.uuids: set[str] = set()
        self.types: Counter[str] = Counter()
        self.retained: Counter[str] = Counter()
        self.per_uuid: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.last_sq: dict[str, int] = {}
        self.lost = Counter()
        self.reboots = Counter()
        self.problems: list[str] = []

    def flag(self, uuid: str, message: str) -> None:
        self.problems.append(f"{uuid}: {message}")
        print("      " + paint(f"! {message}", "red", enabled=self.color))

    def check_device_message(self, uuid: str, leaf: str, data: dict) -> None:
        kind = data.get("type")
        if kind is None and data.get("t") == "TM":
            kind = "TM"
            print("      " + paint('1차 펌웨어 "t":"TM" (1.1.0 부터는 type:"TELEMETRY")', "yellow",
                                   enabled=self.color))
        if kind == "TELEMETRY":
            kind = "TM"
        if kind not in DEVICE_TYPES:
            self.flag(uuid, f"사양에 없는 type {kind!r}")
            return
        reported = data.get("uuid")
        if reported is not None and reported != uuid:
            self.flag(uuid, f"payload uuid({reported}) 가 topic 과 다르다")
        if kind == "TM":
            if leaf != "status":
                self.flag(uuid, f"TM 이 {leaf} 로 왔다(status 여야 함)")
            ts = data.get("ts")
            if not isinstance(ts, str) or not _TS_RE.match(ts):
                self.flag(uuid, f"ts 형식 오류 {ts!r} (YYMMDDThhmm)")
            sq = data.get("sq")
            if isinstance(sq, int):
                prev = self.last_sq.get(uuid)
                if prev is not None:
                    if sq < prev:
                        self.reboots[uuid] += 1
                        print("      " + paint(f"sq {prev} → {sq}: 재부팅", "cyan", enabled=self.color))
                    elif sq > prev + 1:
                        self.lost[uuid] += sq - prev - 1
                        print("      " + paint(f"sq {prev} → {sq}: {sq - prev - 1}건 유실", "yellow",
                                               enabled=self.color))
                self.last_sq[uuid] = sq
            missing = {"sq", "ts", "fw", "ss", "cv", "er", "on", "md", "pw", "bv", "bi", "sc", "pp", "li", "cs"} - set(data)
            if missing:
                self.flag(uuid, f"TM 필드 누락 {sorted(missing)}")
        elif kind == "REGISTER":
            missing = {"uuid", "fw", "device_model", "modem_model", "msisdn", "imei", "iccid"} - set(data)
            if missing:
                self.flag(uuid, f"REGISTER 필드 누락 {sorted(missing)}")
            if not {"cv", "ss", "ti"} <= set(data):
                print("      " + paint("REGISTER 에 cv/ss/ti 없음 (1차 펌웨어)", "yellow", enabled=self.color))
        elif kind in {"PONG", "CONFIG_ACK", "CMD_ACK"} and leaf != "result":
            self.flag(uuid, f"{kind} 가 {leaf} 로 왔다(result 여야 함)")
        elif kind == "LWT" and leaf != "event":
            self.flag(uuid, "LWT 가 event 가 아닌 topic 으로 왔다")

    def show(self, topic: str, raw: bytes, *, retained: bool) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        parts = topic.split("/")
        uuid = parts[2] if len(parts) == 4 and parts[1] == "device" else ""
        leaf = parts[-1]
        to_device = leaf in {"cmd", "config"}
        arrow = "S→D" if to_device else "D→S"
        tag = paint(" [보관]", "dim", enabled=self.color) if retained else ""

        if not raw:
            print(f"{paint(now, 'dim', enabled=self.color)} {arrow} {topic} "
                  f"{paint('(빈 payload = retain 삭제)', 'dim', enabled=self.color)}")
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"{paint(now, 'dim', enabled=self.color)} {arrow} {topic} "
                  f"{paint('JSON 아님', 'red', enabled=self.color)}: {raw[:120]!r}")
            if uuid:
                self.flag(uuid, "JSON 으로 파싱되지 않는 payload")
            return
        if not isinstance(data, dict):
            print(f"{paint(now, 'dim', enabled=self.color)} {arrow} {topic} JSON 객체 아님")
            return

        kind = str(data.get("type") or data.get("t") or "?")
        (self.retained if retained else self.types)[kind] += 1
        if uuid:
            if not _UUID_RE.match(uuid):
                self.problems.append(f"{uuid}: topic UUID 가 24자리 대문자 16진수가 아니다")
            if not to_device:
                self.uuids.add(uuid)
                self.per_uuid[uuid][kind] += 1
        if retained and kind != "REGISTER_ACK":
            self.flag(uuid or topic, f"retain 된 {kind} — retain 은 REGISTER_ACK 뿐이어야 한다(ADR-002)")
        if leaf == "cmd" and retained:
            self.flag(uuid or topic, "cmd 에 retain 메시지가 있다(금지)")

        color = "blue" if to_device else "green"
        body = json.dumps(data, ensure_ascii=False) if self.raw else _brief(data)
        print(f"{paint(now, 'dim', enabled=self.color)} {paint(arrow, color, enabled=self.color)} "
              f"{paint(kind, color, enabled=self.color)}{tag}  {topic}\n      {body}")
        if uuid and not to_device:
            self.check_device_message(uuid, leaf, data)
        elif to_device and kind not in SERVER_TYPES:
            self.flag(uuid or topic, f"서버 → 단말에 사양에 없는 type {kind!r}")

    def summary(self) -> int:
        print("\n" + "=" * 62 + "\n  요약\n" + "=" * 62)
        if not self.uuids:
            print(paint("  단말에서 온 메시지가 하나도 없다.", "red", enabled=self.color))
            print("  확인: MQTT_HOST/PORT, 계정, 방화벽 1883, 단말이 붙은 브로커가 이것인가")
            return 1
        print(f"  단말 {len(self.uuids)}대")
        for uuid in sorted(self.uuids):
            kinds = ", ".join(f"{k}×{v}" for k, v in self.per_uuid[uuid].most_common())
            extra = ""
            if self.lost[uuid] or self.reboots[uuid]:
                extra = f"  유실 {self.lost[uuid]} 재부팅 {self.reboots[uuid]}"
            print(f"    {uuid}  sq={self.last_sq.get(uuid, '?')}{extra}\n      {kinds}")
        live = ", ".join(f"{k}×{v}" for k, v in self.types.most_common()) or "없음"
        print(f"\n  실시간 발행: {live}")
        if self.retained:
            kept = ", ".join(f"{k}×{v}" for k, v in self.retained.most_common())
            print(f"  보관 메시지(구독 직후 1회): {kept}")
        if self.problems:
            print(paint(f"\n  사양 위반 {len(self.problems)}건:", "red", enabled=self.color))
            for line in dict.fromkeys(self.problems):
                print(f"    - {line}")
            return 1
        print(paint("\n  사양 위반 없음.", "green", enabled=self.color))
        return 0


def _brief(data: dict) -> str:
    out = []
    for k, v in data.items():
        if k in ("type", "t"):
            continue
        text = str(v)
        if len(text) > 46:
            text = text[:43] + "…"
        out.append(f"{k}={text}")
    return "  ".join(out)


async def watch(mon: Monitor, host: str, port: int, user: str, password: str,
                seconds: float | None, topic: str) -> None:
    print(f"브로커 {host}:{port} · 구독 {topic} · 계정 {user}")
    print("Ctrl+C 로 종료하면 요약이 나온다.\n")
    async with aiomqtt.Client(host, port, username=user, password=password,
                              identifier=f"monitor-{datetime.now():%H%M%S}") as client:
        await client.subscribe(topic, qos=1)

        async def loop() -> None:
            async for message in client.messages:
                mon.show(str(message.topic), bytes(message.payload or b""), retained=bool(message.retain))

        if seconds is None:
            await loop()
        else:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(loop(), timeout=seconds)


def _utf8_console() -> None:
    """Windows 콘솔(cp949)에서 한글·기호가 깨지지 않게 stdout/stderr 를 UTF-8 로 맞춘다."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main() -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(description="브로커 감시 · 사양 검증기")
    parser.add_argument("--host", default=ENV.mqtt_host)
    parser.add_argument("--port", type=int, default=ENV.mqtt_port)
    parser.add_argument("--user", default=ENV.mqtt_server_user)
    parser.add_argument("--password", default=ENV.mqtt_server_password)
    parser.add_argument("--topic", default=f"{ENV.topic_root}/#")
    parser.add_argument("--raw", action="store_true", help="payload 를 줄이지 않고 전부")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--seconds", type=float, default=None, help="이 시간만 보고 요약")
    args = parser.parse_args()

    mon = Monitor(color=not args.no_color, raw=args.raw)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(watch(mon, args.host, args.port, args.user, args.password, args.seconds, args.topic))
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"\n브로커 접속 실패: {exc}")
        return 1
    return mon.summary()


if __name__ == "__main__":
    raise SystemExit(main())
