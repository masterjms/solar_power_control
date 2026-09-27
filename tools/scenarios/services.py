"""시나리오가 쓰는 외부 서비스 핸들 — DB(asyncpg), REST(httpx), 브로커(aiomqtt), docker.

백엔드 코드를 import 하지 않는다. 테이블 이름·컬럼은 docs/03_DB_스키마.md 를 따른다.
서비스가 내려가 있으면 `probe()` 가 이유를 돌려주고, 러너는 시나리오를 SKIP/FAIL 로 처리한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import aiomqtt
import httpx

from tools.sim.env import ENV, Env

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[2]


class Db:
    """docs/03 스키마 직접 조회. 시나리오 단언은 REST 보다 이쪽을 우선한다(정본)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.pool: Any = None

    async def open(self) -> None:
        if asyncpg is None:
            raise RuntimeError("asyncpg 미설치")
        self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=4, command_timeout=30)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        async with self.pool.acquire() as con:
            return await con.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        async with self.pool.acquire() as con:
            return await con.fetch(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.pool.acquire() as con:
            return await con.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.pool.acquire() as con:
            return await con.execute(sql, *args)

    # ── 자주 쓰는 조회 ───────────────────────────────────────────────────
    async def device(self, uuid: str) -> dict[str, Any] | None:
        row = await self.fetchrow("SELECT * FROM device WHERE uuid = $1", uuid)
        return dict(row) if row else None

    async def telemetry_count(self, uuid: str, since: datetime) -> int:
        return await self.fetchval(
            "SELECT count(*) FROM telemetry WHERE uuid = $1 AND received_at >= $2", uuid, since)

    async def telemetry_rows(self, uuid: str, since: datetime, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.fetch(
            "SELECT * FROM telemetry WHERE uuid = $1 AND received_at >= $2 ORDER BY received_at DESC LIMIT $3",
            uuid, since, limit)
        return [dict(r) for r in rows]

    async def events(self, uuid: str, kind: str | None = None, since: datetime | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM device_event WHERE uuid = $1"
        args: list[Any] = [uuid]
        if kind:
            sql += f" AND kind = ${len(args) + 1}"
            args.append(kind)
        if since:
            sql += f" AND received_at >= ${len(args) + 1}"
            args.append(since)
        sql += " ORDER BY id"
        return [dict(r) for r in await self.fetch(sql, *args)]

    async def command(self, seq: int) -> dict[str, Any] | None:
        row = await self.fetchrow("SELECT * FROM command WHERE seq = $1", seq)
        return dict(row) if row else None

    async def command_acks(self, seq: int) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.fetch("SELECT * FROM command_ack WHERE seq = $1 ORDER BY uuid", seq)]

    async def command_targets(self, seq: int) -> dict[str, dict[str, Any]]:
        """5차 `command_target` (docs/03 5차 추가) → {uuid: row}."""
        rows = await self.fetch("SELECT * FROM command_target WHERE seq = $1", seq)
        return {r["uuid"]: dict(r) for r in rows}

    async def now(self) -> datetime:
        """서버(DB) 시각. received_at 비교는 이 시각 기준으로 한다(PC 시계와 어긋날 수 있다)."""
        return await self.fetchval("SELECT now()")

    async def devices_like(self, prefix: str) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.fetch("SELECT * FROM device WHERE uuid LIKE $1", prefix + "%")]

    async def delete_device_rows(self, uuid: str) -> None:
        """REST DELETE 가 없거나 실패했을 때의 정리. telemetry/event 도 지운다."""
        for sql in ("DELETE FROM telemetry WHERE uuid = $1", "DELETE FROM device_event WHERE uuid = $1",
                    "DELETE FROM command_ack WHERE uuid = $1", "DELETE FROM command_target WHERE uuid = $1",
                    "DELETE FROM device WHERE uuid = $1"):
            with contextlib.suppress(Exception):
                await self.execute(sql, uuid)


_UNSET: Any = object()


class _RetryClient(httpx.AsyncClient):
    """전송 오류(연결 리셋·ReadError)를 3회까지 재시도한다.

    브로커 재시작·폭주 직후 백엔드가 잠깐 연결을 끊는 일이 있어(S2-11 에서 1회) 시나리오 판정과
    무관한 전송 오류로 ERROR 가 나지 않게 한다. 4xx/5xx 응답은 재시도하지 않는다.
    """

    async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        last: Exception | None = None
        for attempt in range(3):
            try:
                return await super().request(*args, **kwargs)
            except httpx.TransportError as e:
                last = e
                await asyncio.sleep(1.0 * (attempt + 1))
        assert last is not None
        raise last


class Rest:
    """백엔드 REST — docs/05_API.md 그대로. 응답 검증은 시나리오가 한다(여기서는 상태코드를 올리지 않는다)."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.client = _RetryClient(base_url=base, timeout=20.0)

    async def close(self) -> None:
        await self.client.aclose()

    # ── 시스템 ─────────────────────────────────────────────────────────
    async def health(self) -> dict[str, Any] | None:
        try:
            r = await self.client.get("/health")
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    async def health_ok(self) -> bool:
        h = await self.health()
        if h is None or not isinstance(h, dict):
            return False
        if "ok" in h:
            return bool(h["ok"])
        return str(h.get("status", "ok")).lower() in {"ok", "healthy", "up"}

    async def test_account_enabled(self) -> bool | None:
        """`/health.test_account_enabled`. 키가 없으면 None(호출자가 env 로 대신한다)."""
        h = await self.health() or {}
        for key in ("test_account_enabled", "mqtt_test_account_enabled"):
            if key in h:
                return bool(h[key])
        return None

    async def metrics(self) -> dict[str, Any]:
        r = await self.client.get("/api/metrics")
        r.raise_for_status()
        return r.json()

    # ── 단말 ───────────────────────────────────────────────────────────
    async def device(self, uuid: str) -> dict[str, Any] | None:
        """`GET /api/devices/{uuid}` → DeviceOut. 404 면 None."""
        r = await self.client.get(f"/api/devices/{uuid}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def devices(self, *, q: str | None = None, state: str | None = None, online: bool | None = None,
                      page: int | None = None, size: int | None = None) -> dict[str, Any]:
        """`GET /api/devices` → {items, total, page, size, counts}. counts 는 state 별 + online."""
        params: dict[str, Any] = {"q": q, "state": state, "page": page, "size": size}
        if online is not None:
            params["online"] = "true" if online else "false"
        r = await self.client.get("/api/devices", params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    async def telemetry(self, uuid: str, **params: Any) -> Any:
        r = await self.client.get(f"/api/devices/{uuid}/telemetry", params=params)
        r.raise_for_status()
        return r.json()

    async def events(self, uuid: str, kind: str | None = None, limit: int = 100) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if kind:
            params["kind"] = kind
        r = await self.client.get(f"/api/devices/{uuid}/events", params=params)
        r.raise_for_status()
        return r.json()

    async def patch_state(self, uuid: str, state: str, *, site: str | None = None,
                          reason: str | None = None, node_id: Any = _UNSET) -> httpx.Response:
        """`PATCH /api/devices/{uuid}/state {state, site?, reason?, node_id?}` — 승인·거부·중지·해제·폐기(§3.9.2).
        `node_id`(5차)는 넘겼을 때만 싣는다(None 도 그대로 = 배정 해제)."""
        body: dict[str, Any] = {"state": state}
        if site is not None:
            body["site"] = site
        if reason is not None:
            body["reason"] = reason
        if node_id is not _UNSET:
            body["node_id"] = node_id
        return await self.client.patch(f"/api/devices/{uuid}/state", json=body)

    async def republish_register_ack(self, uuid: str) -> httpx.Response:
        """`POST /api/devices/{uuid}/register-ack` — DB 상태로 REGISTER_ACK retain 재발행."""
        return await self.client.post(f"/api/devices/{uuid}/register-ack")

    async def patch_config(self, uuid: str, **body: Any) -> httpx.Response:
        """`PATCH /api/devices/{uuid}/config {profile_id?, ti_override?, ka_override?, lat?, lon?, site?, address?, bjd_code?}`.

        None 값도 그대로 보낸다(override 해제 = null)."""
        return await self.client.patch(f"/api/devices/{uuid}/config", json=body)

    async def ping(self, uuid: str) -> httpx.Response:
        return await self.client.post(f"/api/devices/{uuid}/ping")

    async def delete_device(self, uuid: str) -> httpx.Response:
        return await self.client.delete(f"/api/devices/{uuid}")

    # ── 프로필 ─────────────────────────────────────────────────────────
    async def profiles(self) -> list[dict[str, Any]]:
        r = await self.client.get("/api/profiles")
        r.raise_for_status()
        return r.json()

    async def create_profile(self, name: str, ti: int, ka: int) -> httpx.Response:
        return await self.client.post("/api/profiles", json={"name": name, "ti": ti, "ka": ka})

    async def patch_profile(self, profile_id: int, **body: Any) -> httpx.Response:
        return await self.client.patch(f"/api/profiles/{profile_id}", json=body)

    async def delete_profile(self, profile_id: int) -> httpx.Response:
        return await self.client.delete(f"/api/profiles/{profile_id}")

    # ── 5차 (docs/05 "5차 API") ─────────────────────────────────────────
    #: 5차 호출은 `X-Remote-User` 를 싣는다(nginx Basic auth 사용자명, ADR-005). user=None 이면 헤더 없음.
    @staticmethod
    def _h(user: str | None) -> dict[str, str]:
        return {"X-Remote-User": user} if user else {}

    async def me(self, user: str | None) -> httpx.Response:
        return await self.client.get("/api/me", headers=self._h(user))

    async def regions(self, user: str | None = None) -> httpx.Response:
        return await self.client.get("/api/regions", headers=self._h(user))

    async def region_from_address(self, body: dict[str, Any], user: str | None) -> httpx.Response:
        """`POST /api/regions/from-address` — 개발 환경 직접 입력 `{sido,sigungu,dong,bjd_code,lat?,lon?}`."""
        return await self.client.post("/api/regions/from-address", json=body, headers=self._h(user))

    async def delete_region(self, region_id: int, user: str | None) -> httpx.Response:
        return await self.client.delete(f"/api/regions/{region_id}", headers=self._h(user))

    async def command_preview(self, body: dict[str, Any], user: str | None) -> httpx.Response:
        return await self.client.post("/api/commands/preview", json=body, headers=self._h(user))

    async def post_command(self, body: dict[str, Any], user: str | None = None) -> httpx.Response:
        return await self.client.post("/api/commands", json=body, headers=self._h(user))

    async def commands(self, user: str | None = None, **params: Any) -> httpx.Response:
        return await self.client.get("/api/commands", params={k: v for k, v in params.items() if v is not None},
                                     headers=self._h(user))

    async def command(self, seq: int, user: str | None = None) -> httpx.Response:
        return await self.client.get(f"/api/commands/{seq}", headers=self._h(user))

    async def retry_command(self, seq: int, uuids: list[str] | None, user: str | None = None) -> httpx.Response:
        return await self.client.post(f"/api/commands/{seq}/retry", json={"uuids": uuids}, headers=self._h(user))


def error_code(response: httpx.Response) -> str:
    """docs/05 오류 봉투 `{"error": {"code": ...}}` 의 code. 모양이 다르면 빈 문자열."""
    try:
        body = response.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    return str(err.get("code", "")) if isinstance(err, dict) else ""


class Broker:
    """러너가 "서버 역할"로 브로커에 붙어 원시 메시지를 쏘거나 엿본다."""

    def __init__(self, env: Env) -> None:
        self.env = env

    def client(self, *, identifier: str = "scenario-runner", user: str | None = None,
               password: str | None = None) -> aiomqtt.Client:
        return aiomqtt.Client(
            self.env.mqtt_host, self.env.mqtt_port,
            username=user or self.env.mqtt_server_user,
            password=password or self.env.mqtt_server_password,
            identifier=identifier, protocol=aiomqtt.ProtocolVersion.V311, timeout=15,
        )

    async def publish(self, topic: str, payload: dict[str, Any] | bytes | str, *, qos: int = 1,
                      retain: bool = False) -> None:
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":"))
        async with self.client(identifier=f"runner-pub-{os.getpid()}") as c:
            await c.publish(topic, payload, qos=qos, retain=retain)

    async def probe(self) -> str | None:
        try:
            async with self.client(identifier=f"runner-probe-{os.getpid()}"):
                return None
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    async def can_connect(self, user: str, password: str, identifier: str) -> tuple[bool, str]:
        try:
            async with self.client(identifier=identifier, user=user, password=password):
                return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    async def retained(self, topic: str, *, timeout: float = 3.0) -> bytes | None:
        """topic 의 보관 메시지. 없으면 None (빈 payload 는 b'')."""
        async with self.client(identifier=f"runner-ret-{os.getpid()}") as c:
            await c.subscribe(topic, qos=1)

            async def first() -> bytes | None:
                async for m in c.messages:
                    return bytes(m.payload or b"") if m.retain else None
                return None

            try:
                return await asyncio.wait_for(first(), timeout)
            except asyncio.TimeoutError:
                return None

    async def retained_many(self, topic_filter: str, *, seconds: float = 3.0) -> dict[str, bytes]:
        """필터에 걸리는 보관 메시지 전부 {topic: payload}. 폭주 시나리오에서 1,000개 topic 을 한 번에 본다."""
        got: dict[str, bytes] = {}
        async with self.client(identifier=f"runner-retm-{os.getpid()}") as c:
            await c.subscribe(topic_filter, qos=1)

            async def loop() -> None:
                async for m in c.messages:
                    if m.retain:
                        got[str(m.topic)] = bytes(m.payload or b"")

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(loop(), seconds)
        return got

    async def sniffer(self, topic_filter: str | None = None, *, name: str = "sniff") -> Sniffer:
        """백그라운드 구독을 시작해 돌려준다. 호출자가 `stop()` 한다(ctx.defer)."""
        sn = Sniffer(self, topic_filter or f"{self.env.topic_root}/#", f"runner-{name}-{os.getpid()}")
        await sn.start()
        return sn

    async def collect(self, topic: str, *, seconds: float, identifier: str = "runner-collect") -> list[tuple[str, bytes, bool]]:
        """seconds 동안 topic 에 흐르는 메시지를 모은다 (topic, payload, retained)."""
        got: list[tuple[str, bytes, bool]] = []
        async with self.client(identifier=f"{identifier}-{os.getpid()}") as c:
            await c.subscribe(topic, qos=1)

            async def loop() -> None:
                async for m in c.messages:
                    got.append((str(m.topic), bytes(m.payload or b""), bool(m.retain)))

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(loop(), seconds)
        return got


class Sniffer:
    """서버 계정으로 topic 필터(기본 `iotlight/#`)를 백그라운드 구독해 흐르는 메시지를 전부 모은다.

    "그룹 topic 에 정확히 1회 발행", "개별 재시도만 나감" 같은 발행 횟수 판정용. `start()` 는 SUBACK 까지 기다린다.
    """

    def __init__(self, broker: "Broker", topic_filter: str, identifier: str) -> None:
        self.broker, self.filter, self.identifier = broker, topic_filter, identifier
        #: (monotonic, topic, payload dict|bytes|None, retained)
        self.messages: list[tuple[float, str, Any, bool]] = []
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self.error: str = ""

    async def start(self, timeout: float = 15.0) -> None:
        self._task = asyncio.create_task(self._run())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout)
        if not self._ready.is_set():
            await self.stop()
            raise RuntimeError(f"sniffer 구독 실패 {self.filter}: {self.error or 'timeout'}")

    async def _run(self) -> None:
        try:
            async with self.broker.client(identifier=self.identifier) as c:
                await c.subscribe(self.filter, qos=1)
                self._ready.set()
                async for m in c.messages:
                    raw = bytes(m.payload or b"")
                    try:
                        data: Any = json.loads(raw) if raw else None
                    except (ValueError, UnicodeDecodeError):
                        data = raw
                    self.messages.append((time.monotonic(), str(m.topic), data, bool(m.retain)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def find(self, *, type_: str | None = None, seq: int | None = None, topic: str | None = None,
             topic_prefix: str | None = None, since: float | None = None,
             retained: bool | None = False) -> list[tuple[float, str, Any, bool]]:
        """조건에 맞는 메시지. retained 기본 False(구독 순간 받은 보관 메시지 제외)."""
        out = []
        for (t, tp, data, ret) in self.messages:
            if since is not None and t < since:
                continue
            if retained is not None and ret != retained:
                continue
            if topic is not None and tp != topic:
                continue
            if topic_prefix is not None and not tp.startswith(topic_prefix):
                continue
            if type_ is not None and not (isinstance(data, dict) and data.get("type") == type_):
                continue
            if seq is not None and not (isinstance(data, dict) and data.get("seq") == seq):
                continue
            out.append((t, tp, data, ret))
        return out


class Docker:
    """docker compose 조작. `--allow-docker` 없으면 절대 부르지 않는다."""

    def __init__(self, env: Env) -> None:
        self.compose_file = env.compose_file or self._find_compose()

    @staticmethod
    def _find_compose() -> str:
        for cand in ("docker-compose.yml", "compose.yml", "infra/docker-compose.yml", "infra/compose.yml"):
            if (REPO_ROOT / cand).exists():
                return str(REPO_ROOT / cand)
        return ""

    def _base(self) -> list[str]:
        cmd = ["docker", "compose"]
        # COMPOSE_FILE 은 docker 규약대로 여러 파일을 os.pathsep(리눅스 ':', Windows ';')로 잇는다.
        # 하나의 -f 로 넘기면 "파일 없음"이 된다.
        for f in re.split(r"[;:](?![\\/])", self.compose_file or ""):
            if f:
                cmd += ["-f", f]
        return cmd

    async def run(self, *args: str, timeout: float = 120) -> tuple[int, str]:
        # asyncio 서브프로세스는 Windows 셀렉터 루프에서 NotImplementedError 다(시뮬레이터가
        # 셀렉터 루프를 요구한다). 스레드에서 동기 subprocess 로 돌린다.
        def _run() -> tuple[int, str]:
            try:
                cp = subprocess.run(
                    [*self._base(), *args], cwd=str(REPO_ROOT), timeout=timeout,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except subprocess.TimeoutExpired:
                return 124, "timeout"
            return cp.returncode or 0, cp.stdout.decode(errors="replace")

        return await asyncio.to_thread(_run)

    async def available(self) -> bool:
        code, _ = await self.run("version", timeout=20)
        return code == 0


class Services:
    def __init__(self, env: Env = ENV) -> None:
        self.env = env
        self.db = Db(env.database_url)
        self.rest = Rest(env.backend_url)
        self.broker = Broker(env)
        self.docker = Docker(env)
        self.status: dict[str, str] = {}

    async def probe(self) -> dict[str, str]:
        """세 서비스에 붙어 본다. 값이 'ok' 가 아니면 이유 문자열."""
        try:
            await self.db.open()
            await self.db.fetchval("SELECT 1")
            self.status["db"] = "ok"
        except Exception as exc:  # noqa: BLE001
            self.status["db"] = f"{type(exc).__name__}: {exc}"
        self.status["backend"] = "ok" if await self.rest.health_ok() else "health 실패/응답 없음"
        err = await self.broker.probe()
        self.status["mqtt"] = "ok" if err is None else err
        return self.status

    def all_ok(self) -> bool:
        return all(v == "ok" for v in self.status.values())

    async def close(self) -> None:
        await self.db.close()
        await self.rest.close()


def selector_loop_policy() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
