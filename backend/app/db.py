"""DB 엔진 · 세션.

REST 핸들러는 get_db 의존성으로 세션을 받는다. 세션의 수명과 커밋은
미들웨어(app/main.py 의 db_session_middleware)가 관리한다.

왜 의존성이 아니라 미들웨어인가:
    FastAPI 의 `yield` 의존성은 정리(=커밋) 코드가 **응답을 보낸 뒤** 실행된다.
    그래서 쓰기 API 가 200 을 돌려준 직후 바로 읽으면 아직 커밋 전인 상태가
    보일 수 있다(aircast 에서 20회 중 1회 꼴로 재현). 미들웨어의 call_next 이후는
    응답 전송 전이라 여기서 커밋하면 순서가 뒤집히지 않는다.

MQTT 수신 경로와 스케줄러는 요청 컨텍스트가 없으므로 session_scope() 를 직접 연다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    # DB 가 멈추면 연결 시도가 20초 가까이 매달려 풀을 채운다(S2-12, 2026-09-29) — 그동안 /health 까지 풀을
    # 기다렸다. 연결은 5초, 풀 대기는 10초에서 포기해 빨리 실패하고 다음 판에 다시 한다.
    pool_timeout=10,
    connect_args={"timeout": 5},
)

SessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db(request: Request) -> AsyncSession:
    """요청에 붙은 세션을 꺼낸다. 만들지도, 닫지도 않는다."""
    return request.state.db


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """요청 밖(수신 버퍼·스케줄러)에서 쓰는 세션 컨텍스트."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
