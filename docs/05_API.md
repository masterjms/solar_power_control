# 05. REST API (2·3차)

- 갱신: 2026-09-26 (사양서 2026-09-26 개정 반영 — 승인, 프로필, HMAC 인증)
- 코드: `backend/app/modules/`. OpenAPI: `GET /docs`.
- **아직 관리자 인증이 없다.** `APP_HOST` 기본 `127.0.0.1`. 운영 compose 는 backend 포트를 호스트에
  노출하지 않는다(nginx 또는 SSH 터널로만). 로그인은 4차 전에 붙인다.
- 오류 응답은 항상 `{"error": {"code": "...", "message": "...", "detail": {...}}}`.
- 시각은 전부 ISO-8601 timestamptz(UTC). uuid 경로는 소문자로 줘도 대문자로 접는다.

## 시스템

### `GET /health`
컨테이너 healthcheck. DB 장애여도 200(재시작 방지).
```json
{"ok": true, "mqtt_connected": true, "db_ok": true, "buffer_pending": 12,
 "register_queue": 0, "telemetry_dropped": 0, "flush_failures": 0,
 "broker_log_tail": true, "hmac_keys": ["K1"], "test_account_enabled": false, "env": "dev"}
```

### `GET /api/metrics`
프로세스 카운터 JSON. 목록은 `docs/02` §10.

### `POST /api/admin/rollup?day=YYYY-MM-DD` / `POST /api/admin/partitions`
일 집계·파티션 점검 수동 실행.

## 브로커 인증 (내부 — go-auth `http` 백엔드만 호출)

컨테이너 네트워크 안에서만 닿는다(운영 compose 는 backend 포트를 호스트에 열지 않는다 — 이것이 보호 장치다).
`MQTT_AUTH_SHARED_SECRET` 가 비어 있지 않을 때만 헤더 `X-Auth-Secret` 를 검사한다. go-auth 의 http 백엔드는
사용자 정의 헤더를 못 보내므로 기본은 비워 둔다(리버스 프록시를 끼울 때 쓸 수 있게만 남김).
go-auth 설정: `params_mode json`, `response_mode status` (200 = 허용, 그 외 = 거부).

### `POST /internal/mqtt/auth` `{"username","password","clientid"}`
- `username` 이 `^[0-9A-F]{24}$` 가 아니면 403 (`server`/`solarlte-test` 는 go-auth `files` 백엔드가 본다. files 에서 실패한 `server` 도 여기로 넘어오지만 UUID 가 아니라 403). `clientid` 는 없어도 된다(있으면 username 과 같아야 한다). DB 를 보지 않는다.
- `password == hmac_sha256(K, username)[:16].hex()` 를 **활성 키 전부**(`MQTT_HMAC_KEYS`)에 대해 `compare_digest`. 하나라도 맞으면 200.
- clientid 가 username 과 다르면 403(사양서: Client ID = UUID).
- **승인 상태로 막지 않는다.** PENDING/REJECTED/RETIRED 도 200. 처음 보는 UUID 도 200(DB 에 아직 없어도 된다 — REGISTER 가 만든다).
- 카운터 `mqtt_auth_ok` / `mqtt_auth_fail`.

### `POST /internal/mqtt/acl` `{"username","clientid","topic","acc"}`
`acc`: 1 read, 2 write, 3 readwrite, 4 subscribe. 사양서 §1.1.2.2 ACL 표를 코드로:
- write 허용: `iotlight/device/<u>/{register,status,result,event}`
- read/subscribe 허용: `iotlight/device/<u>/{cmd,config}`, `iotlight/group/#`(하위 어떤 것이든), `iotlight/all/cmd`
- 그 외 403. `<u>` 는 username. 와일드카드 구독(`iotlight/#`, `device/+/status`)은 403.

### `POST /internal/mqtt/superuser` `{"username"}` → 항상 403.

## 프로필

### `GET /api/profiles` → `[{"id":1,"name":"1,100원 시험","ti":600,"ka":300,"device_count":12}]`
### `POST /api/profiles` `{"name","ti","ka"}` → 201 `{ProfileOut}`
### `PATCH /api/profiles/{id}` `{"name"?,"ti"?,"ka"?}`
`ti`/`ka` 가 바뀌면 그 프로필의 **모든 단말 `cv_server += 1`** (0 인 단말은 그대로 0 — 아직 승인 전).
ACTIVE 단말에는 즉시 CONFIG_SET 을 보내지 않는다 — 다음 송신 때 나간다(§1.1.10). 응답은 `{ProfileOut}` + `bumped_devices`.
`ProfileOut` = `{"id","name","ti","ka","device_count","created_at","updated_at"}`. 이름 중복은 422.
### `DELETE /api/profiles/{id}` — 단말이 하나라도 쓰면 409 `PROFILE_IN_USE`. id 1 은 삭제 불가(같은 409, `detail.reason="default"`). → `{"id","deleted":true}`

## 단말

### `GET /api/devices?page=1&size=50&state=&online=&q=`
- 정렬: **PENDING 먼저**, 그다음 `last_seen_at` 내림차순(NULL 마지막), uuid.
- `q`: uuid 부분 일치 또는 site 부분 일치(대소문자 무시).
- `online`: `is_online` 판정값으로 필터.
```json
{"items": [ {DeviceOut} ], "total": 9832, "page": 1, "size": 50,
 "counts": {"PENDING": 3, "ACTIVE": 9800, "SUSPENDED": 2, "REJECTED": 1, "RETIRED": 26, "online": 9750}}
```
`counts` 는 **필터와 무관한 전체** 집계(상단 탭용). `total` 은 필터 적용 건수.

`DeviceOut` — `device` 컬럼 전부(`uuid state state_reason state_changed_at register_ack_at fw device_model modem_model imei iccid msisdn cv_device ss_device ti_device ka_device cv_server profile_id ti_override ka_override lat lon site address bjd_code grp last_register_at last_telemetry_at last_seen_at last_sq last_telemetry online online_changed_at offline_at lost_count reboot_count config_sent_at created_at updated_at`) + 계산 필드:
- `ti_effective`, `ka_effective`: `override ?? profile` 값. `profile_name`.
- `config_pending`: `cv_server > 0 AND cv_device != cv_server` (0 이면 아직 보낸 적 없음 → false)
- `config_mismatch`: `ti_device != ti_effective OR ka_device != ka_effective` (단말 보고값 vs 서버 의도값, 화면 1 "다르면 표시")
- `is_online`: `online` 컬럼(브로커 로그) AND 수신 시각 보조 규칙(docs/02 §9). 둘 다 만족해야 true.
- `last_telemetry` 는 **목록에도 포함**(bv/sc 열 표시용. 2026-09-26 프론트 요청).

### `GET /api/devices/{uuid}` → `DeviceOut`. 404 `DEVICE_NOT_FOUND`.
### `GET /api/devices/{uuid}/telemetry?from=&to=&limit=200` (변경 없음)
### `GET /api/devices/{uuid}/events?kind=&limit=100` (kind 목록은 docs/03 device_event)

### `PATCH /api/devices/{uuid}/state`
```json
{"state": "ACTIVE", "site": "A-12", "reason": null}
```
- `state`: ACTIVE / SUSPENDED / REJECTED / RETIRED / PENDING(해제·되돌리기). `site` 는 ACTIVE 로 갈 때 선택(없으면 기존 값), 24자 이내.
  같은 상태로의 요청도 409(전이 표에 없다) — site 만 바꾸려면 `PATCH …/config`. `reason` 은 REJECTED 일 때만 REGISTER_ACK 에 실린다.
- 처리 순서(사양서 §3.9.2):
  1. DB `state`, `site`, `state_reason`, `state_changed_at` 저장 + `device_event(STATE_CHANGE)`
  2. `REGISTER_ACK {"type":"REGISTER_ACK","uuid","state","site"?,"reason"?}` 를 `device/<uuid>/config` 에 **retain 1** 로 발행. `register_ack_at` 기록.
  3. **ACTIVE 로 갈 때** `cv_server == 0` 이면 `1` 로(`cv_device` 가 더 크면 `cv_device+1`). CONFIG_SET 은 **여기서 보내지 않는다** — 단말이 ACTIVE 를 받고 보내는 첫 TELEMETRY 때 `cv` 비교로 나간다(§1.1.10).
  4. **RETIRED**: RETIRED 를 retain 으로 발행한 뒤 **빈 payload retain** 으로 지운다(`register_ack_at = null`).
- 브로커 끊김이면 DB 는 커밋하고 `published=false` → `POST …/register-ack` 로 재발행하거나 다음 REGISTER 때 자동.
→ `{"uuid","state","site","published":true,"register_ack":{...}}`

### `POST /api/devices/{uuid}/register-ack`
DB 상태 그대로 REGISTER_ACK retain 을 다시 발행(재조정용). RETIRED 면 빈 retain.
→ `{"uuid","state","published","cleared":false,"register_ack":{...}|null}` (RETIRED 는 `cleared=true`, `register_ack=null`)

### `PATCH /api/devices/{uuid}/config`
```json
{"profile_id": 2, "ti_override": null, "ka_override": 600, "lat": 37.36, "lon": 126.93, "site": "A-12", "address": "…", "bjd_code": "4141011000"}
```
- 전부 선택. `ti` 60~3600, `ka` 60~1800, lat/lon 범위. 422 `VALIDATION_FAILED`.
- `profile_id`/`ti_override`/`ka_override`/`lat`/`lon` 중 **적용값이 바뀌면** `cv_server` 를 올린다: `max(cv_server, cv_device or 0) + 1`.
  `site`/`address`/`bjd_code` 만 바꾸면 cv 는 그대로(단말에 CONFIG 로 안 내려감). `site` 가 바뀌고 state 가 ACTIVE/PENDING/SUSPENDED/REJECTED 면 REGISTER_ACK retain 을 다시 발행(site 가 거기 실린다. RETIRED 는 retain 이 비어 있어야 하므로 제외).
  `ti_override: null` 을 **보내면** override 해제(프로필 값으로). 키를 안 보내면 그대로(보낸 키만 바꾼다).
- ACTIVE 단말이면 CONFIG_SET **즉시 1회** 발행(`published`). 그 외 상태면 `published=false, reason="NOT_ACTIVE"` — 승인 뒤 첫 TELEMETRY 때 나간다.
- CONFIG_SET payload 는 **항상 전체값**: `{"type":"CONFIG_SET","cv","ti","ka"}` + `lat`/`lon` 은 값이 있을 때. retain 0.
→ `{"uuid","state","cv_server","profile_id","ti_override","ka_override","ti_effective","ka_effective","lat","lon","site","address","bjd_code","cv_bumped","published","reason","payload","register_ack_republished"}`
  `reason`: `null`(발행함) / `NOT_ACTIVE` / `NOT_NEEDED`(ACTIVE 인데 바뀐 것도 cv 불일치도 없음) / `PUBLISH_FAILED`(브로커 끊김 — 다음 TELEMETRY 때 재전송). `payload` 는 발행했거나 다음 송신 때 나갈 CONFIG_SET 전체값(`cv_server` 가 0 이면 null).

### `POST /api/devices/{uuid}/ping` (변경 없음. PONG 대기는 화면에서 60초 — 서버→단말 지연은 수십 초가 정상, §1.1.10)

### `DELETE /api/devices/{uuid}`
행 삭제 + REGISTER_ACK retain 삭제(빈 retain). 이력은 남긴다. 계정 파일은 손대지 않는다(단말 계정은 파일에 없다).
→ `{"uuid","deleted":true,"retain_cleared":true}` (브로커 끊김이면 `retain_cleared=false`, 행은 지워졌다)

### 삭제된 API
`POST /api/devices/import-accounts` — 비밀번호 CSV 방식 폐기(ADR-003).

## 에러 코드

| code | HTTP | 뜻 |
|---|---|---|
| `VALIDATION_FAILED` | 422 | 본문/쿼리 검증 실패 |
| `DEVICE_NOT_FOUND` | 404 | |
| `PROFILE_NOT_FOUND` | 404 | |
| `PROFILE_IN_USE` | 409 | 단말이 쓰는 프로필 삭제 |
| `INVALID_STATE_TRANSITION` | 409 | 예: RETIRED → ACTIVE 는 PENDING 을 거쳐야 함 |
| `MQTT_UNAVAILABLE` | 503 | 브로커 미연결 (ping 만. state/config 는 DB 커밋 + published=false) |
| `MQTT_PAYLOAD_TOO_LARGE` | 500 | 384B 초과 — 서버 버그 |

## 상태 전이

```
PENDING  → ACTIVE | REJECTED | RETIRED
ACTIVE   → SUSPENDED | RETIRED | PENDING(승인 취소)
SUSPENDED→ ACTIVE(해제) | RETIRED
REJECTED → PENDING(재검토) | RETIRED
RETIRED  → PENDING(같은 보드 재설치. 빈 retain 상태에서 REGISTER 가 다시 오면 서버가 PENDING 으로 되돌리고 ACK)
```

## 다음(4차 전)
`POST /api/auth/login`, 모든 `/api/*` 인증. 주소 검색(법정동코드 자동) API 연동.
