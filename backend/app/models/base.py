"""SQLAlchemy 선언적 베이스.

모델은 테이블 모양만 정의한다. 정본 DDL 은 alembic/versions/ 이고 문서는 docs/03.
비즈니스 로직은 각 모듈의 service.py 로 간다.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
