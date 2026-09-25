# backend — iotlight 관제 서버 (2차)

태양광 가로등 단말(STM32 + WD-N522S)이 보내는 `register/status/result/event` 를 MQTT 로 받아
PostgreSQL 에 쌓고, 단말별 MQTT 계정과 CONFIG_SET 을 관리한다. 설계는 `docs/02_MQTT_서버구현.md`,
DDL 은 `docs/03_DB_스키마.md`, REST 는 `docs/05_API.md`.

## 실행 (로컬)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy ..\.env.example ..\.env        # DATABASE_URL, MQTT_* 를 맞춘다
.venv\Scripts\alembic upgrade head   # 테이블 + 이번 달/다음 달 telemetry 파티션
.venv\Scripts\python run.py          # Windows 는 반드시 run.py (uvicorn 직접 실행 금지 — 셀렉터 루프)
```

Linux/컨테이너는 `uvicorn app.main:app --host 0.0.0.0 --port 8000` 그대로.

- `.env` 는 리포지토리 루트에서 읽는다. `IOTLIGHT_PROFILE=local` 이면 `.env.local` 을 덮어쓴다.
- 2차 REST 는 **인증이 없다**. 기본 `APP_HOST=127.0.0.1`. 컨테이너는 compose 에서 포트를
  호스트에 노출하지 않는다.
- MQTT 계정 내보내기는 `MOSQUITTO_PASSWD_EXPORT`/`MOSQUITTO_ACL_EXPORT` 가 있을 때만 켜진다.
  비우면 anonymous 브로커로 개발한다.

## 마이그레이션

```powershell
.venv\Scripts\alembic upgrade head
.venv\Scripts\alembic revision -m "설명"    # 새 리비전. 손으로 쓴다(autogenerate 는 파티션을 모른다)
```

`telemetry` 는 월 파티션이다. 기동 시와 매일 01:00 KST 에 `app/tasks/partitions.py` 가
이번 달·다음 달 파티션을 만들고 `TELEMETRY_RETENTION_MONTHS`(13) 를 넘긴 파티션을 DROP 한다.

## 시험

```powershell
.venv\Scripts\pytest -q          # 순수 단위 시험 — DB/브로커 없이 돈다
.venv\Scripts\ruff check .
```

시나리오 시험(브로커+DB 필요)은 `tools/scenarios/` (docs/06).

## 모듈 지도

| 경로 | 역할 |
|---|---|
| `app/config.py` | 환경 변수 → `settings`. 환경 의존성의 유일한 입구 |
| `app/constants.py` | 프로토콜 상수 (UUID 형식, 384B 한계, ti 범위, 메시지 type) |
| `app/errors.py` | API 에러 규약 `{"error":{code,message,detail}}` |
| `app/db.py` | 엔진·세션. REST 는 미들웨어가 트랜잭션을 연다 |
| `app/main.py` | 앱 조립. 기동 순서: 파티션 → 계정 내보내기 → last_sq warm → MQTT |
| `app/models/` | device · telemetry(+daily) · device_event · command(+ack) · system |
| `app/mqtt/topics.py` | 토픽 문자열·구독 목록·수신 토픽 파서 |
| `app/mqtt/connection.py` | aiomqtt 연결 1개, 1→30초 백오프 재연결, 재구독 |
| `app/mqtt/publisher.py` | **발행 단일 창구.** compact JSON, 384B 검사, retain 정책 |
| `app/mqtt/handlers.py` | 수신 디스패치 (REGISTER / TM / PONG / CONFIG_ACK / LWT / EV) |
| `app/mqtt/telemetry_buffer.py` | TM 1초 배치: 이력 INSERT + 최신값 upsert + sq 판정 + CONFIG 재전송 판정 |
| `app/mqtt/sq.py` | sq 판정 순수 함수 (lost / reboot / wrap) |
| `app/mqtt/config_sync.py` | CONFIG_SET 발행 큐 — 토큰 버킷 + 단말별 60초 쿨다운 |
| `app/core/ids.py` | `cmd_seq` 시퀀스 발번 |
| `app/core/presence.py` | 온라인 판정 단일 정의 (파이썬 판 + SQL 판) |
| `app/core/mqtt_accounts.py` | mosquitto 해시, passwd/aclfile 렌더, 원자적 내보내기, ACL 적용 대기 |
| `app/core/ratelimit.py` | 토큰 버킷 |
| `app/core/metrics.py` | 프로세스 카운터 (`/api/metrics`) |
| `app/modules/system/` | `/health`, `/api/metrics`, `/api/admin/*` |
| `app/modules/device/` | `/api/devices*` 라우터·서비스·스키마 |
| `app/tasks/partitions.py` | 월 파티션 생성/DROP, device_event 보존 삭제 |
| `app/tasks/daily_rollup.py` | KST 일 집계 → telemetry_daily (SQL 사다리꼴 적분) |
| `alembic/` | 마이그레이션 (정본 DDL) |
| `tests/` | 순수 단위 시험 |
