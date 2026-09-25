"""개발 서버 실행 (Windows 포함).

`python -m uvicorn app.main:app` 는 Windows 에서 ProactorEventLoop 를 잡아 paho 의
add_reader 가 실패한다. 여기서 셀렉터 루프를 강제한 뒤 uvicorn 을 띄운다.

    python run.py            # APP_HOST:APP_PORT (.env)
    python run.py --reload
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.config import settings

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload="--reload" in sys.argv,
        loop="asyncio",
    )
