# 사양서·ADR-001 기준 Python 3.12. 로컬 개발은 3.10 이어도 코드가 호환된다.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# curl: HEALTHCHECK 용. 그 외 시스템 패키지는 없다(ffmpeg 같은 미디어 처리 없음).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 의존성 레이어를 소스와 분리해서 코드만 바뀔 때 재설치하지 않게 한다.
COPY pyproject.toml ./
# ⚠ 이 목록은 pyproject.toml 의 dependencies 와 같아야 한다. 어긋나면 운영에서만 ImportError.
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" "pydantic-settings>=2.6" \
      "sqlalchemy[asyncio]>=2.0.36" "asyncpg>=0.30" "alembic>=1.14" "aiomqtt>=2.3" \
      "apscheduler>=3.10,<4" "python-multipart>=0.0.17" "httpx>=0.28"

COPY alembic.ini entrypoint.sh ./
COPY alembic ./alembic
COPY app ./app

# MQTT 계정 내보내기 마운트 지점(passwd.generated / aclfile.generated / aclfile.applied).
# compose 가 mosquitto 컨테이너와 공유하는 볼륨을 여기 붙인다.
RUN mkdir -p /var/lib/iotlight/mqtt

# root 로 돌리지 않는다.
RUN useradd --system --uid 10001 iotlight \
    && chown -R iotlight:iotlight /srv /var/lib/iotlight
USER iotlight

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# entrypoint.sh: DB 대기 → alembic upgrade head → uvicorn(0.0.0.0 — 호스트 노출은 compose ports 가 정한다).
CMD ["/bin/sh", "/srv/entrypoint.sh"]
