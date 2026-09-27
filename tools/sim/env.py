"""환경 변수 한 곳. backend/app/config 를 import 하지 않는다(영역 분리)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Env:
    """시뮬레이터·시나리오가 읽는 환경 변수. 값이 없으면 로컬 docker compose 기본값."""

    mqtt_host: str
    mqtt_port: int
    #: 1차 공용 시험 계정(사양서 §1.1.2.1). 2차 모드 단말은 uuid/단말별 비밀번호를 쓴다.
    mqtt_test_user: str
    mqtt_test_password: str
    #: 시나리오 러너가 "서버 역할"로 브로커에 붙을 때 쓰는 계정(§1.1.2.2 `server`).
    mqtt_server_user: str
    mqtt_server_password: str
    #: 브로커에 공용 시험 계정이 아직 살아 있는가(2차 이행 스위치). S2-06 이 본다.
    mqtt_test_account_enabled: bool
    #: 단말 비밀번호 HMAC 키(hex 64자, §1.1.2.2). 비우면 사양서 공개 시험 키. 백엔드 MQTT_HMAC_KEYS 와 같아야 한다.
    mqtt_hmac_key: str
    topic_root: str
    database_url: str
    backend_url: str
    #: docker compose 를 부를 때 쓸 파일. 비우면 저장소 루트의 기본 파일.
    compose_file: str
    #: 5차 — REST 를 부르는 사용자(`X-Remote-User`, ADR-005). 최고관리자 / 일반 관리자.
    admin_user: str = "admin"
    operator_user: str = "operator"
    #: 5차 — 백엔드 토큰 버킷 상한(초당 발행). S5-12 판정용. 백엔드 `REGISTER_REPLY_RATE_PER_SEC` 와 같게.
    publish_rate_per_sec: float = 200.0

    @classmethod
    def load(cls) -> "Env":
        return cls(
            mqtt_host=os.environ.get("MQTT_HOST", "localhost"),
            mqtt_port=_int("MQTT_PORT", 1883),
            mqtt_test_user=os.environ.get("MQTT_TEST_USER", "solarlte-test"),
            mqtt_test_password=os.environ.get("MQTT_TEST_PASSWORD", "solarlte-test-2026"),
            mqtt_server_user=os.environ.get("MQTT_SERVER_USER", os.environ.get("MQTT_USERNAME", "server")),
            mqtt_server_password=os.environ.get(
                "MQTT_SERVER_PASSWORD", os.environ.get("MQTT_PASSWORD", "server")
            ),
            mqtt_test_account_enabled=_bool("MQTT_TEST_ACCOUNT_ENABLED", True),
            mqtt_hmac_key=os.environ.get("MQTT_HMAC_KEY", "").strip(),
            topic_root=os.environ.get("MQTT_TOPIC_ROOT", "iotlight"),
            # backend 의 .env 는 SQLAlchemy 방언(postgresql+asyncpg://)을 쓴다. asyncpg 는 그걸 모른다.
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql://iotlight:iotlight-dev-pw@localhost:5432/iotlight"
            ).replace("postgresql+asyncpg://", "postgresql://", 1),
            backend_url=os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/"),
            compose_file=os.environ.get("COMPOSE_FILE", ""),
            admin_user=os.environ.get("SCENARIO_ADMIN_USER", "admin"),
            operator_user=os.environ.get("SCENARIO_OPERATOR_USER", "operator"),
            publish_rate_per_sec=float(os.environ.get("REGISTER_REPLY_RATE_PER_SEC") or 200),
        )


ENV = Env.load()
