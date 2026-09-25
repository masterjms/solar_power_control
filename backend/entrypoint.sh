#!/bin/sh
# 컨테이너 기동 순서: DB 대기 → 마이그레이션 → 서버.
#
# compose 의 depends_on 은 postgres 가 프로파일(local-db) 뒤에 있어 조건을 걸 수 없고,
# 운영(RDS)에서는 애초에 compose 밖이다. 그래서 DB 준비는 여기서 직접 기다린다.
# 첫 기동에 마이그레이션이 안 돼 있으면 첫 flush 부터 실패하므로 서버보다 먼저 올린다.
set -eu

DB_WAIT_SEC="${DB_WAIT_SEC:-120}"

python - <<'PY'
import asyncio, os, sys, time
import asyncpg

url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
deadline = time.monotonic() + float(os.environ.get("DB_WAIT_SEC", "120"))

async def main():
    while True:
        try:
            conn = await asyncpg.connect(url, timeout=5)
            await conn.close()
            print("DB 준비됨", flush=True)
            return 0
        except Exception as e:  # noqa: BLE001
            if time.monotonic() > deadline:
                print(f"DB 대기 시간 초과: {e}", flush=True)
                return 1
            print(f"DB 대기 중... ({e.__class__.__name__})", flush=True)
            await asyncio.sleep(2)

sys.exit(asyncio.run(main()))
PY

alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
