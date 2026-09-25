"""시나리오가 쓰는 외부 서비스 핸들 — DB(asyncpg), REST(httpx), 브로커(aiomqtt), docker.

백엔드 코드를 import 하지 않는다. 테이블 이름·컬럼은 docs/03_DB_스키마.md 를 따른다.
서비스가 내려가 있으면 `probe()` 가 이유를 돌려주고, 러너는 시나리오를 SKIP/FAIL 로 처리한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
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

    async def now(self) -> datetime:
        """서버(DB) 시각. received_at 비교는 이 시각 기준으로 한다(PC 시계와 어긋날 수 있다)."""
        return await self.fetchval("SELECT now()")

    async def delete_device_rows(self, uuid: str) -> None:
        """REST DELETE 가 없거나 실패했을 때의 정리. telemetry/event 도 지운다."""
        for sql in ("DELETE FROM telemetry WHERE uuid = $1", "DELETE FROM device_event WHERE uuid = $1",
                    "DELETE FROM command_ack WHERE uuid = $1", "DELETE FROM device WHERE uuid = $1"):
            with contextlib.suppress(Exception):
                await self.execute(sql, uuid)


class Rest:
    """백엔드 REST. 엔드포인트 목록은 docs/06 §환경 참고."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.client = httpx.AsyncClient(base_url=base, timeout=20.0)

    async def close(self) -> None:
        await self.client.aclose()

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

    async def metrics(self) -> dict[str, Any]:
        r = await self.client.get("/api/metrics")
        r.raise_for_status()
        return r.json()

    async def device(self, uuid: str) -> dict[str, Any] | None:
        r = await self.client.get(f"/api/devices/{uuid}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def devices(self, **params: Any) -> Any:
        r = await self.client.get("/api/devices", params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    async def telemetry(self, uuid: str, **params: Any) -> Any:
        r = await self.client.get(f"/api/devices/{uuid}/telemetry", params=params)
        r.raise_for_status()
        return r.json()

    async def patch_config(self, uuid: str, **body: Any) -> httpx.Response:
        return await self.client.patch(f"/api/devices/{uuid}/config", json=body)

    async def ping(self, uuid: str) -> httpx.Response:
        return await self.client.post(f"/api/devices/{uuid}/ping")

    async def import_accounts(self, csv_path: Path) -> httpx.Response:
        with csv_path.open("rb") as f:
            return await self.client.post("/api/devices/import-accounts",
                                          files={"file": (csv_path.name, f, "text/csv")})

    async def delete_device(self, uuid: str) -> httpx.Response:
        return await self.client.delete(f"/api/devices/{uuid}")

    # 3차·5차 — 아직 확정되지 않은 엔드포인트(가정). 404/405 면 시나리오가 SKIP 한다.
    async def set_state(self, uuid: str, state: str, **extra: Any) -> httpx.Response:
        return await self.client.patch(f"/api/devices/{uuid}/state", json={"state": state, **extra})

    async def post_command(self, body: dict[str, Any]) -> httpx.Response:
        return await self.client.post("/api/commands", json=body)


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
        if self.compose_file:
            cmd += ["-f", self.compose_file]
        return cmd

    async def run(self, *args: str, timeout: float = 120) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *self._base(), *args, cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "timeout"
        return proc.returncode or 0, out.decode(errors="replace")

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
