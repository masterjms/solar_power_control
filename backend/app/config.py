"""환경 변수 → 설정 객체.

여기가 환경 의존성의 유일한 입구다. 다른 모듈은 os.environ 을 직접 읽지 않는다.
RDS 전환·운영 Broker 분리(4차) 때 바꾸는 값도 전부 여기 모여 있다
(DATABASE_URL, MQTT_HOST/PORT/TLS). 서버 로직은 전송 방식에 의존하지 않는다.

`.env` 를 리포지토리 루트에서 읽는다. IOTLIGHT_PROFILE 이 있으면 `.env.<프로파일>` 을
덮어쓴다 — 목 단말 시험용과 실물 단말용 주소를 한 파일에서 번갈아 고치다 보면
반드시 엉뚱한 쪽을 가리키게 되므로 파일을 나눠 둔다.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py → backend/ → 리포지토리 루트
REPO_ROOT = Path(__file__).resolve().parents[2]


def _profile_env_files() -> tuple[Path, ...]:
    """읽을 .env 목록. 뒤에 오는 파일이 앞을 덮어쓴다."""
    files = [REPO_ROOT / ".env"]
    profile = os.getenv("IOTLIGHT_PROFILE", "").strip()
    if profile:
        files.append(REPO_ROOT / f".env.{profile}")
    return tuple(files)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_profile_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 앱 ──────────────────────────────────────────────
    app_env: str = "dev"
    log_level: str = "INFO"
    #: 바인딩 주소. 2차는 REST 에 인증이 없다 — 기본을 127.0.0.1 로 두어 같은 호스트
    #: (nginx·시나리오 도구)만 닿게 한다. 컨테이너에서는 compose 가 0.0.0.0 을 넣고
    #: 포트를 호스트에 노출하지 않는 것으로 같은 효과를 낸다. 3차에서 admin_user
    #: 인증이 붙기 전까지 공인 IP 로 여는 일은 없어야 한다.
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    #: 쉼표 구분. pydantic-settings 는 list 환경변수를 JSON 으로 파싱하려 들어 원문을 받는다.
    cors_origins_raw: str = Field(default="", validation_alias="CORS_ORIGINS")

    # ── DB ──────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://iotlight:iotlight-dev-pw@localhost:5432/iotlight"

    # ── MQTT ────────────────────────────────────────────
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    #: 서버 계정. 사양서 §1.1.2.2 — `server` 하나, iotlight/# readwrite.
    mqtt_username: str = "server"
    mqtt_password: str | None = None
    mqtt_tls: bool = False
    #: 백엔드의 MQTT Client ID. 같은 ID 로 두 프로세스가 붙으면 브로커가 앞 연결을
    #: 끊는다 — 단일 프로세스 전제(ADR-001)라 고정값이어도 되지만 바꿀 수 있게 둔다.
    mqtt_client_id: str = "iotlight-backend"
    mqtt_topic_root: str = "iotlight"
    #: 1차 공용 계정(`solarlte-test`)을 passwd/acl 에 유지할지. 2차 합격 기준 1번이
    #: "공용 계정 접속 거부" 라서 기본은 끔. 1차 시험 단말이 아직 남아 있을 때만 켠다.
    mqtt_test_account_enabled: bool = False
    mqtt_test_username: str = "solarlte-test"
    mqtt_test_password: str = "solarlte-test-2026"
    #: 서버(+공용 시험) 계정 passwd/aclfile 을 내보낼 경로(공유 볼륨). 비우면 내보내기 꺼짐
    #: (개발 PC 에서 anonymous 브로커로 시험할 때). 운영 compose 가
    #: /var/lib/iotlight/mqtt/passwd.generated, aclfile.generated 로 지정한다.
    #: entrypoint 감시 루프는 설치한 뒤 같은 디렉터리의 `passwd.applied`/`aclfile.applied` 에
    #: 적용본 md5 를 적는다 — wait_applied 가 그 파일을 본다.
    #: 2026-09-26 부터 단말 행은 여기 들어가지 않는다(ADR-003, HMAC 은 접속 순간 검증).
    mosquitto_passwd_export: str | None = None
    mosquitto_acl_export: str | None = None
    #: 내보낸 파일이 브로커에 적용될 때까지 기다리는 상한(초).
    acl_apply_timeout_sec: float = 5.0

    # ── 단말 인증 (ADR-003, 사양서 §1.1.2.2) ────────────
    #: 활성 HMAC 키 목록 "K1:<hex64>,K2:<hex64>". 평소 하나, 교체 중 둘. 기본값은 사양서의
    #: **공개 시험 키**(0x00~0x1F) — 운영 .env 는 반드시 K1 을 넣는다. 로그·DB 에 남기지 않는다.
    mqtt_hmac_keys: str = (
        "TEST:000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    )
    #: go-auth http 백엔드가 보내는 `X-Auth-Secret` 헤더 값. 비우면 검사하지 않는다
    #: (/internal/* 는 어차피 컨테이너 네트워크 안에서만 닿아야 한다).
    mqtt_auth_shared_secret: str = ""

    # ── 접속 상태 (ADR-004, 사양서 §16.1) ───────────────
    #: Mosquitto 로그 파일(공유 볼륨). 비우면 tail 을 돌리지 않는다(개발 PC).
    mosquitto_log_path: str = "/var/lib/iotlight/mqtt/mosquitto.log"
    #: ACTIVE 가 아닌 단말(REGISTER 만 5~30분 주기)의 수신 시각 보조 판정 창(초). 70분.
    pending_offline_sec: int = 4200

    # ── 수신 처리 ───────────────────────────────────────
    #: Telemetry 를 모아 쓰는 간격(초). 1만 대 × 10분 = 17 msg/s 라 처리량이 문제는
    #: 아니지만, 건마다 트랜잭션을 열면 재접속 폭주 때 DB 왕복이 상한이 된다.
    #: 모아 쓰면 대수와 무관하게 초당 트랜잭션 1개다. 0 이하면 1.0 으로 본다.
    telemetry_flush_interval_sec: float = 1.0
    #: 대기 행이 이 수를 넘으면 주기를 기다리지 않고 바로 flush 한다.
    telemetry_flush_max_pending: int = 20_000
    #: REGISTER 응답(CONFIG_SET, 3차부터 REGISTER_ACK) 발행 상한(초당). 브로커 재시작
    #: 뒤 1만 대가 30초 안에 REGISTER 를 보내면 응답을 그대로 쏘는 게 폭주다.
    register_reply_rate_per_sec: float = 200.0
    #: 응답 대기열 상한. 넘으면 가장 오래된 것을 버리고 센다 — 단말은 어차피
    #: 다음 Telemetry 때 cv 불일치로 다시 잡힌다.
    register_reply_queue_max: int = 50_000
    #: 같은 단말에 CONFIG_SET 을 다시 보내기까지의 최소 간격(초). ADR-002.
    config_resend_cooldown_sec: float = 60.0

    # ── 온라인 판정 ────────────────────────────────────
    #: 2차 대체 규칙: 마지막 Telemetry 가 `ti × 이 배수` 안이면 온라인 (사양서 §16.1).
    device_online_factor: int = 3
    # CONFIG ti/ka 하한. 사양은 60 이지만 시뮬레이터로 주기 5초 시험을 하려면 개발 환경에서만 낮춘다
    # (docker-compose.dev.yml). 운영은 기본값 60 을 그대로 둔다. 상한(3600/1800)은 상수.
    config_ti_min_sec: int = 60
    config_ka_min_sec: int = 60

    # ── 보존·집계 ───────────────────────────────────────
    #: telemetry 월 파티션 보존 개월 수. 13 = 1년 + 여유 1달.
    telemetry_retention_months: int = 13
    #: device_event 보존 일수. 파티션 없이 일 배치 DELETE.
    device_event_retention_days: int = 365
    #: 일 집계 실행 시각(KST).
    rollup_hour_kst: int = 0
    rollup_minute_kst: int = 30

    # ── 5차: 법정동 트리 · 원격 명령 · 권한 (ADR-005) ───────────
    #: 카카오 로컬 REST 키. 비우면 주소 검색이 503 GEO_UNAVAILABLE(개발은 직접 입력으로 시험).
    #: 서버 전용 비밀값 — 로그·응답에 절대 내보내지 않는다.
    kakao_rest_api_key: str = ""
    #: 최고관리자 사용자명(쉼표). nginx Basic auth 사용자명이 X-Remote-User 로 들어온다.
    super_admin_users_raw: str = Field(default="admin", validation_alias="SUPER_ADMIN_USERS")
    #: 승인(ACTIVE)에 말단 법정동 배정을 요구할지. **안 적으면** 운영 true, APP_ENV=dev 는 false
    #: — 개발 compose 로 도는 3차 시나리오는 노드 없이 승인한다(approve_requires_node 참고).
    approve_requires_node_raw: bool | None = Field(
        default=None, validation_alias="APPROVE_REQUIRES_NODE"
    )
    #: COMMAND.exp 기본(초). 단말 RTC - ts 가 이보다 크면 EXPIRED(사양서 §3.10.7, 30 권장).
    command_exp_sec: int = 30
    #: 명령 종료 판정 상한(초). 이 뒤로는 자동 재시도도 멈춘다(ADR-005).
    command_timeout_sec: int = 900
    #: 대상 단말 1대당 최대 발송 횟수(첫 발송 포함). 자동 재시도 상한.
    command_max_attempts: int = 3
    #: 같은 대상에 다시 보내기까지 최소 간격(초). 단말 송신 직후 재시도의 연타 방지.
    command_retry_min_sec: int = 20
    #: "오늘 밤" 유지시간 계산에 쓸 좌표가 없을 때의 기본값(서울시청).
    default_lat: float = 37.5665
    default_lon: float = 126.9780

    @property
    def super_admin_users(self) -> frozenset[str]:
        return frozenset(u.strip() for u in self.super_admin_users_raw.split(",") if u.strip())

    @property
    def approve_requires_node(self) -> bool:
        """명시값이 있으면 그것, 없으면 dev 만 false(3차 시나리오 호환). 운영 기본 true."""
        if self.approve_requires_node_raw is not None:
            return self.approve_requires_node_raw
        return self.app_env != "dev"

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins_raw.split(",") if item.strip()]

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def flush_interval(self) -> float:
        return self.telemetry_flush_interval_sec if self.telemetry_flush_interval_sec > 0 else 1.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
