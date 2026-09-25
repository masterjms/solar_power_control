# 05. REST API (2차)

- 작성일: 2026-09-25
- 코드: `backend/app/modules/`. OpenAPI: `GET /docs`.
- **2차는 인증이 없다.** `APP_HOST` 기본 `127.0.0.1`. 컨테이너는 compose 에서 포트를 호스트에
  노출하지 않는다(nginx 또는 SSH 터널로만). 3차에서 `admin_user` 인증이 붙는다.
- 오류 응답은 항상 `{"error": {"code": "...", "message": "...", "detail": {...}}}`.
  `code` 로 분기하고 `message` 는 매칭하지 않는다.
- 시각은 전부 ISO-8601 timestamptz(UTC). uuid 경로는 소문자로 줘도 대문자로 접는다.

## 시스템

### `GET /health`
컨테이너 healthcheck. DB 장애여도 200(재시작 방지).
```json
{"ok": true, "mqtt_connected": true, "db_ok": true, "buffer_pending": 12,
 "register_queue": 0, "telemetry_dropped": 0, "flush_failures": 0, "env": "dev"}
```

### `GET /api/metrics`
프로세스 카운터 JSON. 목록은 `docs/02` §10. 재시작하면 0 이 된다.

### `POST /api/admin/rollup?day=YYYY-MM-DD`
일 집계 수동 실행(KST 날짜, 생략 시 어제). 같은 날은 덮어쓴다.
→ `{"day": "2026-09-24", "devices": 9832}`

### `POST /api/admin/partitions`
파티션 점검 수동 실행. → `{"ok": true}`

## 단말

### `GET /api/devices?page=1&size=50&state=ACTIVE&online=true`
- `state`: PENDING/ACTIVE/SUSPENDED/REJECTED/RETIRED (2차는 전부 ACTIVE)
- `online`: presence 규칙(docs/02 §9)으로 필터. 생략하면 전체
- 정렬: `last_telemetry_at` 내림차순(NULL 마지막), uuid
```json
{"items": [ {DeviceOut} ], "total": 9832, "page": 1, "size": 50}
```

`DeviceOut` — `device` 컬럼 전부(단, `mqtt_password_hash` 는 제외하고 `has_mqtt_account`
bool 로) + 계산 필드:
- `is_online`: presence 판정값. `online` 컬럼(LWT, 3차)과 별개
- `config_pending`: `cv_device IS NULL OR cv_device != cv_server`

### `GET /api/devices/{uuid}`
`DeviceOut` + `last_telemetry`(마지막 TM 원본). 404 `DEVICE_NOT_FOUND`.

### `GET /api/devices/{uuid}/telemetry?from=&to=&limit=200`
이력, 최신순. `from` 포함, `to` 미포함. `limit` 1~5000.
```json
[{"received_at": "...", "ts_device": "260924T2103", "sq": 41, "fw": "1.0.0", "ss": 15, "cv": 3,
  "er": 0, "on": 1, "md": 0, "pw": [70, 64, 64], "bv": 2612, "bi": -150, "sc": 87,
  "pp": 3400, "li": 230, "cs": 3073}]
```
단위 변환 없음(x100 그대로). `cs` 비트 해석은 클라이언트 몫.

### `GET /api/devices/{uuid}/events?kind=&limit=100`
`device_event` 최신순. `kind`: REGISTER/LWT/REBOOT/LOST/ERR/CONFIG_ACK/PONG/CMD_ACK/STATE_CHANGE.
```json
[{"id": 1, "kind": "REBOOT", "payload": {"sq": 0, "last_sq": 41}, "received_at": "..."}]
```
단말이 삭제돼도 이력은 남으므로 404 를 내지 않는다(빈 배열).

### `PATCH /api/devices/{uuid}/config`
```json
{"ti": 300, "lat": 37.3617, "lon": 126.9352, "site": "A-12"}
```
- 전부 선택. `ti` 60~3600 (422 `VALIDATION_FAILED`), lat -90~90, lon -180~180.
- ti/lat/lon 중 하나라도 **바뀌면** `cv_server += 1` 후 CONFIG_SET **즉시 1회** 발행.
  `site` 만 바꾸면 cv 를 올리지 않는다(단말에 안 내려가는 값).
- 값이 같아도 `cv_device != cv_server` 면 다시 발행한다(재전송 강제용).
- 브로커가 끊겨 있으면 DB 는 커밋하고 `published=false` — 다음 TM 이 재전송한다.
```json
{"uuid": "...", "cv_server": 4, "ti_server": 300, "lat": 37.3617, "lon": 126.9352,
 "site": "A-12", "published": true,
 "payload": {"type": "CONFIG_SET", "cv": 4, "ti": 300, "lat": 37.3617, "lon": 126.9352}}
```

### `POST /api/devices/{uuid}/ping`
`cmd_seq` 발번 → `command` 행 → `{"type":"PING","seq":n}` 을 `device/<uuid>/cmd` 로 QoS1.
→ `{"uuid": "...", "seq": 17}`. PONG 은 `GET …/events?kind=PONG` 과 `command.acked_count` 로 확인.
브로커 끊김이면 503 `MQTT_UNAVAILABLE`(command 행은 롤백, seq 는 소모).

### `POST /api/devices/import-accounts`  (multipart, `file`=CSV)
```
uuid,password
00112233445566778899AABB,k3Jd9$xQ
```
- 헤더 행 선택. uuid 는 대문자로 접음. 같은 uuid 중복 시 뒤의 것.
- 비밀번호에 `:` 나 공백 불가(passwd 형식). 오류 행은 `errors` 에 담고 나머지는 처리.
- 해시만 저장 → passwd/aclfile 내보내기 → `aclfile.applied` 를 최대 `ACL_APPLY_TIMEOUT_SEC`
  기다림. `acl_applied=false` 면 브로커가 아직 옛 ACL 이다(감시 루프 확인).
```json
{"imported": 120, "created": 118, "updated": 2, "errors": ["7행: UUID 형식 아님 ('BAD')"],
 "export_enabled": true, "passwd_md5": "…", "acl_md5": "…", "acl_applied": true}
```

### `DELETE /api/devices/{uuid}`
`device` 행 삭제(계정 포함) + 재내보내기. `telemetry`/`device_event` 이력은 남긴다.
→ `{"uuid": "...", "deleted": true, "export_enabled": true, "acl_applied": true}`

## 에러 코드

| code | HTTP | 뜻 |
|---|---|---|
| `VALIDATION_FAILED` | 422 | 본문/쿼리 검증 실패. `detail.errors` 에 pydantic 목록 |
| `DEVICE_NOT_FOUND` | 404 | |
| `MQTT_UNAVAILABLE` | 503 | 브로커 미연결 |
| `MQTT_PAYLOAD_TOO_LARGE` | 500 | 384B 초과 — 서버 버그 |

## 3차 예정
`POST /api/devices/{uuid}/state` (승인/중지/거부/폐기 → REGISTER_ACK retain 재발행),
`POST /api/auth/login`, 모든 `/api/*` 에 인증.
