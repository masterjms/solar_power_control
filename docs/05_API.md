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
- `q`: **부분 일치, 한 글자부터**(문제점 #8) — uuid · site · address · 지역 이름(말단 법정동과 그 시군구·시도 이름). 대소문자 무시, `%` `_` `\` 는 글자 그대로.
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

### `GET /api/devices/map?state=` → 지도 핀 (2026-09-28, 문제점 #3)
좌표(lat·lon)가 있는 단말 전부. 필드 최소: `[{"uuid","site","lat","lon","state","is_online","on","lit","bv","node_name"}]`. `on` = 마지막 Telemetry on(없으면 null).
`lit`(2026-09-29, 문제점 13번) = 점등 중인가 — 마지막 Telemetry `pw` 앞 두 채널(주등·입간판) 중 하나라도 > 0, `pw` 가 없으면 `on == 1`, Telemetry 없으면 null. `bv` = 마지막 배터리 전압(×100 V). 오프라인이어도 둘 다 마지막 값.
`/{uuid}` 보다 먼저 선언(경로 충돌 방지).

### `GET /api/devices/energy?uuid=A&uuid=B…` (최대 500) → 오늘 발전량·사용량 (문제점 #11, #23)
`{uuid: {"samples","gen_wh","use_wh","co2_g","no_value","source"}}`.
**2026-10-01(문제점 23번): 단말이 보낸 값을 쓴다** — `gen_wh` = 최신 Telemetry `eg`×10, `use_wh` = `eu`×10(`eg`·`eu` 는 kWh×100), `source:"device"`.
MPPT 무응답(er 0x0010)이면 `no_value:true`·값 null(화면 "값 없음"), 마지막 Telemetry 가 오늘 것이 아니면 값 null(공백).
`eg` 가 없는 옛 펌웨어만 아래 서버 계산(`source:"server"`, 오늘 Telemetry 가 없으면 빠진다): 발전 = ∫pp, 사용 = ∫li·bv (사다리꼴, 간격 2시간 넘으면 빼기 — daily_rollup 과 같은 규칙), 감축 = 발전 kWh × `GHG_KG_PER_KWH`(기본 0.4781 kgCO2eq/kWh, .env 로 바꿈). `core/energy.py`.

### `GET /api/devices/{uuid}` → `DeviceOut`. 404 `DEVICE_NOT_FOUND`.
### `GET /api/devices/{uuid}/telemetry?from=&to=&limit=200` — 2026-10-01 `eg`·`eu`·`yg`·`yu` 추가(없으면 null)
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
| `MQTT_PAYLOAD_TOO_LARGE` | 500 | 900B 초과(단말 수신 한계, ADR-007. 2026-09-27 전 384B) — 서버 버그 |

## 상태 전이

```
PENDING  → ACTIVE | REJECTED | RETIRED
ACTIVE   → SUSPENDED | RETIRED | PENDING(승인 취소)
SUSPENDED→ ACTIVE(해제) | RETIRED
REJECTED → PENDING(재검토) | RETIRED
RETIRED  → PENDING(같은 보드 재설치. 빈 retain 상태에서 REGISTER 가 다시 오면 서버가 PENDING 으로 되돌리고 ACK)
```

## 로그인 (2026-09-30, 문제점 16번, ADR-011)
| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/auth/login` | `{"user","password"}` → 200 `{"user"}` + 쿠키 `iotl_session`(HttpOnly, 기본 7일). 실패 401 `LOGIN_FAILED`(1초 지연), nginx 제한 초과 429 |
| POST | `/api/auth/logout` | 쿠키 지움 |
| GET | `/api/auth/check` | nginx `auth_request` 전용. 쿠키 또는 `Authorization: Basic` 이 맞으면 200 + `X-Auth-User`, 아니면 401 |

그 밖의 모든 `/api/*`·`/health` 는 nginx 에서 로그인 판정을 통과해야 한다(없으면 401 → 화면이 로그인 창으로).

---

# 5차 API (2026-09-27, ADR-005)

모든 `/api/*` 요청의 사용자 = 헤더 `X-Remote-User`(nginx 가 로그인 판정 결과로 채움, ADR-011). 역할은 `SUPER_ADMIN_USERS` 로 판정.
최고관리자 전용 API 는 관리자에게 403 `FORBIDDEN`. 헤더가 없으면 `APP_ENV=dev` 만 최고관리자 `local`, 그 외는 관리자 `anonymous`.
구현: `backend/app/modules/{region,command}/`, 규칙 `app/core/command_rules.py`, 처리 흐름 docs/02 §16.

### `GET /api/me` → `{"user":"admin","role":"super_admin"|"admin"}`
### `GET /api/ui-config` → `{"kakao_js_key": "..."|null, "ghg_kg_per_kwh": 0.4781}` (2026-09-28)
카카오 **JavaScript** 키(지도)는 브라우저가 쓰는 공개 키라 준다. REST 키는 절대 싣지 않는다.

## 법정동 트리

### `GET /api/regions`
평면 목록(화면이 트리로 조립). `device_count` = 그 노드 아래(자신 포함) 단말 수, `active_count` = 그중 ACTIVE.
```json
[{"id":1,"parent_id":null,"level":"sido","name":"경기도","bjd_code":null,"grp":null,"lat":null,"lon":null,"device_count":4296,"active_count":4200},
 {"id":3,"parent_id":2,"level":"dong","name":"안양동","bjd_code":"4117110100","grp":"411711010000","lat":37.40,"lon":126.92,"device_count":12,"active_count":12}]
```

### `GET /api/geo/search?query=경기도 군포시 금산로 91`
카카오 로컬 주소 검색 프록시(`KAKAO_REST_API_KEY`). 키가 없으면 503 `GEO_UNAVAILABLE`(카카오 HTTP 오류·5초 시간 초과도 같은 503,
`detail.reason` = `NO_KEY`/`HTTP`/`TIMEOUT`/`CONNECT`). 최대 10건, 같은 법정동은 한 번만. 법정동코드(`address.b_code`)가 없는 행은 뺀다.
`sido` 는 정식 이름("경기" → "경기도"), 세종처럼 시군구가 없으면 `sigungu` 에 시도 이름을 다시 넣는다. `dong` = `region_3depth_name`(법정동).
```json
[{"address_name":"경기 군포시 금정동 ...","bjd_code":"4141010400","sido":"경기도","sigungu":"군포시","dong":"금정동","lat":37.36,"lon":126.93}]
```

### `GET /api/geo/reverse?lat=&lon=` → `GeoResult | null` (2026-09-28, 승인 창 지도 위치 보정)
카카오 `coord2regioncode`(region_type **B** = 법정동 code 앞 10자리) + `coord2address`(도로명 있으면 도로명, 없으면 지번). 법정동이 없는 자리(바다 등)면 `null`. 오류는 search 와 같은 503.

### `POST /api/regions/from-address` (최고관리자) `{"query":"..."}` 또는 `{"pick":{GeoResult}}`
검색 첫 결과(또는 고른 결과)로 시도·시군구·법정동을 find-or-create. 말단 반환(201, 이미 있으면 200).
개발 환경(`APP_ENV=dev`)에서는 `{"sido","sigungu","dong","bjd_code","lat"?,"lon"?}` 직접 입력도 허용(카카오 키 없이 시험).
응답 = 위 목록의 한 항목(카운트 포함). 운영에서 직접 입력 → 422(`detail.reason="MANUAL_NOT_ALLOWED"`), 검색 결과 없음 → 422(`NO_RESULT`).
말단은 법정동코드로 찾는다(이미 있으면 이름·위치가 달라도 그 말단, 200).

### `PATCH /api/regions/{id}` (최고관리자) `{"name"}` · `DELETE /api/regions/{id}` (최고관리자)
자식 또는 배정된 단말이 있으면 409 `REGION_IN_USE`(`detail.children`, `detail.devices`). 이름 중복(같은 부모) 422.
PATCH → 목록 항목 모양, DELETE → `{"id","deleted":true}`. 없는 id 404 `REGION_NOT_FOUND`.

## 단말 배정

- `PATCH /api/devices/{uuid}/config` 에 `node_id` 추가(말단만, 아니면 422 `NODE_NOT_LEAF`, 없는 id 404 `REGION_NOT_FOUND`).
  `null` 이면 배정 해제(`grp`·`bjd_code` 도 null). 바뀌면 `grp`·`bjd_code` 갱신 → REGISTER_ACK retain 재발행(`register_ack_republished`).
  `node_id` 와 `bjd_code` 를 같이 보내면 말단의 코드가 이긴다. 응답에 `node_id`, `grp` 추가.
- `PATCH /api/devices/{uuid}/state` 에 `node_id` 추가. ACTIVE 로 갈 때 단말에 말단이 없고 `APPROVE_REQUIRES_NODE=true` 면 409 `NODE_REQUIRED`.
  `APPROVE_REQUIRES_NODE` 를 **안 적으면** 운영 true, `APP_ENV=dev` 는 false. 응답 `StateOut` 에 `node_id`, `grp` 추가.
- REGISTER_ACK: `{"type":"REGISTER_ACK","uuid","state"[,"site"][,"grp"][,"reason"]}` — `grp` 는 배정돼 있으면 항상 싣는다.
- `DeviceOut` 추가: `node_id`, `node_name`, `node_path`(예 "경기도 > 안양시 만안구 > 안양동"), `override_act`, `override_level`, `override_seq`, `override_until`,
  `remote_active`(= `last_telemetry.md == 2 AND override_until > now`), `remote_remaining_sec`(until 이 미래면 남은 초 — md 와 무관, 지났으면 null).
- 목록 필터 추가: `node_id`(그 노드 아래 전체, 없는 id 404), `remote=true|false`.

## 명령

공통 요청 본문:
```json
{"target":{"kind":"device"|"node"|"all","id":"<uuid>"|"<node id>"|null},
 "act":"on"|"off"|"pwm"|"auto", "ch":[1,2], "pwm":[70,40],
 "dur":3600 | null, "dur_preset":"30m"|"1h"|"3h"|"tonight"|null, "exp":30}
```
- `ch` 기본 `[1,2]`, 값 1~3 중복 없음(빈 배열 422). `pwm` 은 `act=pwm` 일 때만(그 외에 주면 422), `ch` 와 같은 길이, 0~100.
- `act != auto` 면 `dur`(1~86400) 또는 `dur_preset` 중 **정확히 하나**. `act=auto` 면 둘 다 없어야 한다(422).
  `30m`=1800, `1h`=3600, `3h`=10800. `tonight` = 대상 좌표 기준 **다음 소등(아침) 시각**까지 남은 초(suntable, 올림, 1~86400) —
  소등 전 새벽이면 오늘 아침, 그 뒤면 내일 아침. 좌표: 노드 좌표(없으면 첫 하위 말단) / 단말 lat·lon(없으면 그 말단) / 없으면 서울.
- `exp` 기본 `COMMAND_EXP_SEC`(30), 1~3600. `target.id`: device = uuid(대소문자 무관), node = region id(숫자·문자열).
- `kind=all` 은 최고관리자만. 없는 노드 404 `REGION_NOT_FOUND`, 없는 단말 404 `DEVICE_NOT_FOUND`. 검증 실패는 422 `VALIDATION_FAILED`(`detail.field`).

### `POST /api/commands/preview` → 보내기 전 확인
```json
{"expected":120,"online":112,"offline":8,"low_battery":3,"not_active":5,
 "topics":["iotlight/group/411711010000/cmd"],"dur":3600,"payload":{...seq 제외...}}
```
`low_battery` = 대상 중 `last_telemetry.er & 1`(BATT_LOW) — 점등 명령이어도 안 켜질 수. `not_active` = 대상 범위 안이지만 ACTIVE 가 아니라 제외된 수.
`online`/`offline` 은 ACTIVE 대상 중 `is_online` 기준. `payload` 의 `ts` 는 미리보기 시각(보낼 때 다시 찍는다). 대상 0 이어도 200(`expected:0`).

### `POST /api/commands` → 201
```json
{"seq":57,"target":{"kind":"node","id":"3","label":"경기도 > 안양시 만안구"},"topics":[...],
 "payload":{"type":"COMMAND","seq":57,"ts":"260927T013512","exp":30,"act":"off","ch":[1,2],"dur":3600},
 "expected":118,"offline":2,"sent_at":"...","created_by":"admin"}
```
브로커 끊김이면 503 `MQTT_UNAVAILABLE`(명령 행 삭제 — 서버는 **발행 전에 커밋**하고 실패하면 보상 삭제한다. seq 는 되감지 않는다).
`expected` = 실제로 보내고 응답을 기다리는 온라인 대수, `offline` = 오프라인이라 보내지 않은 대수(대상에 `OFFLINE` 으로 남는다, 문제점 14번).
대상 ACTIVE 0 → 409 `NO_TARGETS`, ACTIVE 가 있지만 전부 오프라인 → 409 `NO_ONLINE_TARGETS`(명령 행 없음). `label`: device = `"<site> (<uuid>)"`(site 없으면 uuid), node = 경로, all = `"전체"`.
개별(device) 명령만 `device_event(COMMAND_SENT)` 를 남긴다.

### `GET /api/commands?limit=50&uuid=&node_id=`
이력(최신순, `type=COMMAND` 만 — PING 제외): `{"seq","created_by","target_kind","target_id","target_label","act","ch","pwm","dur","sent_at","finished_at","result",
"expected_count","counts":{"OK":n,"LOCAL":n,"EXPIRED":n,"BAD":n,"STATE":n,"pending":n}}`. `limit` 1~500.
`uuid` = 그 단말이 대상 스냅숏에 든 명령(그룹·전체 포함). `node_id` = 그 노드 또는 하위 노드를 대상으로 한 node 명령.
`result` = `OK`(전부 OK) / `PARTIAL` / `TIMEOUT`(응답 0), 진행 중이면 null(종료 판정은 30초마다 + 마지막 응답 직후).

### `GET /api/commands/{seq}` → 위 + `targets:[{"uuid","site","node_name","status","attempts","last_sent_at","acked_at","is_online"}]`

### `POST /api/commands/{seq}/retry` `{"uuids":[...]|null}`
무응답(pending)·EXPIRED 대상만 **개별 topic** 으로 같은 seq·새 ts 재발송. → `{"resent":n}`. 종료된 명령이면 409 `COMMAND_FINISHED`.
본문 생략 가능(= 전부). 시도 상한·최소 간격은 보지 않지만(사람이 누른 것) **그 단말에 더 새 명령이 있으면 건너뛴다**
(옛 명령이 새 명령을 덮지 않게). 발행은 응답 큐(토큰 버킷)로 수 초 안에 나간다. 브로커 끊김 503. 자동 재시도는 docs/02 §16.3.

### 에러 코드 추가
`FORBIDDEN` 403 · `GEO_UNAVAILABLE` 503 · `REGION_IN_USE` 409 · `NODE_NOT_LEAF` 422 · `NODE_REQUIRED` 409 · `REGION_NOT_FOUND` 404 · `COMMAND_NOT_FOUND` 404 · `COMMAND_FINISHED` 409 · `NO_TARGETS` 409(대상 ACTIVE 단말 0) · `NO_ONLINE_TARGETS` 409(대상 ACTIVE 전부 오프라인)

---

# 단말 설정 API (S-23, 2026-09-27, ADR-007)

### `GET /api/settings/schema`
`ui_items.json` 그대로(groups/items/rules/calc/year_table). 화면은 이것으로 폼을 그린다.

### `GET /api/devices/{uuid}/settings`
```json
{"uuid":"…","sync":"unknown|synced|writing|local_saved|device_changed","read_at":null,
 "values":{"start_ofst":0,…25개…}|null, "sh_db":"38AF0DBD"|null, "sh_device":"…"|null,
 "tbl":{"region":"서울","lat_e6":37566500,"lon_e6":126978000,"on":0,"off":0,"src":1,"ss":31,"crc":"69C1DF86","crc_expected":"69C1DF86","matches":true}|null,
 "dev":{"dip":8,"bat":24}|null, "ss_known":31, "ss_telemetry":31,
 "report":{…device_changed 일 때 단말 값 25개…}|null, "diff":[{"key","db","device"}],
 "pending":{"kind":"SETTINGS_GET|SETTINGS_SET","seq":501,"sent_at":"…","attempts":1}|null,
 "last_result":"OK","last_result_at":"…"}
```
`tbl.matches` = 단말 `crc` 가 같은 조건으로 서버가 계산한 CRC 와 같은가(다르면 "현장에서 손댄 표").

### `POST /api/devices/{uuid}/settings/read` → 202 `{"seq","sent_at"}`
`SETTINGS_GET`. state 가 PENDING·ACTIVE 가 아니면 409 `INVALID_STATE`. 이미 대기 중이면 409 `SETTINGS_PENDING`.

### `PUT /api/devices/{uuid}/settings` (ACTIVE 만)
```json
{"values":{…25개 전부…}, "tbl":{"region":"서울","lat":37.5665,"lon":126.978,"on":0,"off":0} | null}
```
- 25개 중 하나라도 없으면 422 `SETTINGS_INCOMPLETE`. 범위 밖 422 `SETTINGS_RANGE`(detail.key). 규칙 위반 422 `SETTINGS_RULE`(detail.rule).
- `tbl` 은 표를 바꿀 때만. `lat/lon` 은 실수 → `round(x*1e6)`. `region` UTF-8 47바이트 이하, `"` `\` 제어문자 불가. `on/off` -180~180.
- 한 번도 읽지 않은 단말(`sync=unknown`)은 409 `SETTINGS_NOT_READ`(읽고 나서 쓴다 — 8.5). `force=true` 쿼리로만 허용.
→ 202 `{"seq","sent_at","sh_expected","payload_bytes"}`. 결과는 GET 으로(`writing` → `synced` 등).

### `POST /api/devices/{uuid}/settings/accept` — `device_changed` 일 때 DB ← 단말 보고값. 이력 `by` = 사용자.
### `POST /api/devices/{uuid}/settings/revert` — `device_changed`/`local_saved` 일 때 DB 값으로 SETTINGS_SET(= PUT 과 같은 경로).
### `GET /api/devices/{uuid}/settings/history?limit=100` → `[{"changed_at","by","key","old","new","note"}]`
최신순, `limit` 1~1000. 읽기·쓰기·받아들이기·되돌리기는 대기 중인 요청이 있으면 409 `SETTINGS_PENDING`.
`tbl.ss` = `ss_known`. `report`/`diff` 는 `device_changed` 이고 마지막 SETTINGS 원본이 있을 때만(쓰기 OK 인데 `sh` 가 다르면 원본이
없어 비어 있다 — 다시 읽기). revert 는 DB 에 표 조건이 있으면 `tbl` 도 싣는다.

### `GET /api/schedule/preview?lat=&lon=&on=0&off=0`
suntable 로 표 계산 → `{"crc":"69C1DF86","lat_e6","lon_e6","on","off","rows":[{"month","day","on":"18:02","off":"06:31","hours":12.5}]}` — 매달 1일·15일 24행.
`hours` = 점등 → 다음 날 소등까지(소수 1자리). 범위 밖 lat/lon/on/off 는 422 `VALIDATION_FAILED`.

### DeviceOut 추가
`settings_sync`(device_settings.sync, 없으면 `unknown`).

### 에러 코드 추가
`INVALID_STATE` 409 · `SETTINGS_PENDING` 409 · `SETTINGS_NOT_READ` 409 · `SETTINGS_INCOMPLETE` 422 · `SETTINGS_RANGE` 422 · `SETTINGS_RULE` 422 · `SETTINGS_NOT_CHANGED`(accept/revert 할 게 없음) 409

### DeviceOut 추가 (2026-09-28, 0005)
- `override_ch`: `{"1": {"act","seq","level","remaining_sec"}, "2": {...}}` — 채널별 원격(끝난 채널은 빠진다). `override_*` 는 그 요약.

## 알람 API (S-24, ADR-009)

### `GET /api/alarms?status=open|closed&tab=&kind=&q=&page=&size=`
열린 알람(기본) 또는 이력. 관찰 중(지속 기준 전)은 안 나온다. 정렬: 열림 = 등급(경고→주의→정보) → 발생 최신, 이력 = 해제 최신.
`tab` = fault / comm / pending / config / local. `q` = 시설명·UUID·주소 부분 일치.
```json
{"items":[{"id":7,"uuid":"…","kind":"LED_FAULT","label":"LED FAULT","tab":"fault","severity":"warn",
  "first_seen_at":"…","opened_at":"…","last_seen_at":"…","closed_at":null,"duration_sec":320,
  "value":{"er":4},"site":"…","address":"…","node_path":"경기도 > 군포시 > 산본동","state":"ACTIVE"}],
 "total":1,"page":1,"size":50,"counts":{"all":3,"fault":1,"comm":1,"pending":1,"config":0,"local":0}}
```
`counts` 는 필터와 무관한 열린 알람 탭별 수(대시보드·사이드바 배지).

### `GET /api/devices/{uuid}/alarms?limit=30` → 그 단말의 열린 알람 전부 + 최근 이력 limit 건.

## 스케줄 배포 API (S-25, ADR-010)

| 메서드 · 경로 | 내용 |
|---|---|
| `GET /api/schedule/profile-keys` | `{keys:[15개], defaults:{…}}` |
| `GET /api/schedule/profiles` | 프로필 + `assigned_nodes assigned_devices targets applied` |
| `POST /api/schedule/profiles` | `{name, region, lat, lon, on, off, values(15), address?}` → 201. 409 `SCHEDULE_PROFILE_NAME_TAKEN`, 422 `SETTINGS_RANGE/RULE/INCOMPLETE`·`VALIDATION_FAILED`(region) |
| `PATCH /api/schedule/profiles/{id}` | 보낸 것만. 조건·15개가 바뀌면 version +1 |
| `DELETE /api/schedule/profiles/{id}` | 배정이 있으면 409 `SCHEDULE_PROFILE_IN_USE` |
| `GET /api/schedule/assign` | `[{id, node_id, uuid, profile_id, profile_name, label, assigned_at, assigned_by}]` |
| `PUT /api/schedule/assign` | `{node_id | uuid, profile_id | null}` — null = 해제. 배포는 따로 |
| `GET /api/schedule/devices?node_id=&profile_id=&q=&page=&size=` | ACTIVE 단말별 `DeviceScheduleOut`(아래) |
| `GET /api/devices/{uuid}/schedule` | 한 단말(단말 상세 스케줄 탭) |
| `POST /api/schedule/deploy` | `{profile_id, scope: profile|node|device, scope_id?}` → 201 작업. 대상 = 범위 안 ACTIVE 이면서 지금 배정이 그 프로필. 없으면 409 `DEPLOY_NO_TARGETS` |
| `GET /api/schedule/deploy?limit=&profile_id=&uuid=` | 작업 목록 + `counts`(상태별) |
| `GET /api/schedule/deploy/{id}` | + `items:[{uuid, site, is_online, status, rounds, sent_at, acked_at, detail}]` |
| `POST /api/schedule/deploy/{id}/retry` | `{uuids?}` — 실패 항목(NO_RESPONSE READ_FAILED FLASH CRC STATE RULE RANGE BAD)을 waiting 으로, 항목당 `DEPLOY_MAX_ROUNDS` 번까지 → `{retried, skipped}` |
| `POST /api/schedule/deploy/{id}/cancel` | 열린 항목 CANCELLED |

`DeviceScheduleOut`: `uuid site state is_online node_path profile_id profile_name profile_version profile_crc source(device|node:<id>)
applied_profile_id applied_version applied_crc applied_at device_crc device_region device_src applied_ok deploy_status deploy_job_id dip4 today_on today_off`.

## 서버 설정 API (2026-10-01, 문제점 14번, ADR-012) — 최고관리자 전용
| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/server-settings` | `{"groups":[{"id","title","items":[{"key","label","help","unit","min","max","default","value","updated_by","updated_at"}]}]}`. 관리자는 403 |
| PUT | `/api/server-settings` | `{"values":{"command_wait_sec":30,"command_attempts":2}}` — 보낸 항목만. 하나라도 범위 밖·모르는 key 면 아무것도 안 바꾸고 422 `VALIDATION_FAILED`(`detail.fields`). 저장 즉시 적용 |

`GET /api/ui-config` 에 `command_wait_sec`·`command_attempts` 추가(모든 사용자 — 명령 창 안내 문구용).
2026-10-03: 항목에 `kind`(int|date)·`scale` 추가, 묶음 "기록 보관 기간"·"발전·사용 통계"(docs/03 server_setting).

## 발전·사용 통계 API (2026-10-03, 문제점 27·29번)
| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/energy/summary?period=7d\|30d\|12m&node_id=` | `{period, since, from, today, days:[{day, gen_wh, use_wh, est_gen_wh, devices, today}], now:{gen_wh, use_wh, reported, no_report, mppt_offline, total_devices}, total:{gen_wh, use_wh, co2_kg}, ghg_kg_per_kwh}`. 날짜별은 `telemetry_daily`(단말 값; 옛 펌웨어는 `est_gen_wh` 로 서버 추정), 오늘은 지금 `eg`·`eu` 합, 누적은 `device.energy_*_wh_total` + 오늘. 통계 시작일 전은 없음, 값 없는 날은 0. `12m` 은 월 합계. `node_id` 는 그 지역 아래 단말만 |
| POST | `/api/energy/reset` | 최고관리자. 통계 시작일 = 오늘(KST), 모든 단말 누적 0, 오늘 전 `telemetry_daily` 삭제 → `{since, daily_rows_deleted, devices_zeroed}` |

## 계정 API (2026-10-04, 문제점 21번, ADR-013)
접근 검사는 `core/access.access_guard` 한 곳: 지역관리자·게스트는 목록이 맡은 시·도로 걸러지고, 범위 밖 `{uuid}` 는 404, 게스트는 대시보드 GET 만(403 `GUEST_READ_ONLY`), 최고관리자 전용은 403 `FORBIDDEN`. 본문의 노드가 범위 밖이면 403 `OUT_OF_REGION`.

| 메서드 | 경로 | 누가 | 설명 |
|---|---|---|---|
| GET | `/api/me` | 누구나 | `{user, role, source(env/db/dev), region_ids, regions, expires_at, can_change_password}` |
| GET | `/api/accounts` | 최고관리자 | `{items:[{id, username, role, role_label, region_ids, regions, expires_at, expired, expiring, disabled, created_by, created_at, last_login_at, source}], regions:[{id,name}](시·도), max_super, super_count, expiry_choices}`. `.env` 계정은 id null·source env |
| POST | `/api/accounts` | 최고관리자 | `{username, password, role(super_admin/region_admin/guest), region_ids, expires(7d…365d/never)}` → 201. 잘못되면 422 `ACCOUNT_INVALID` `detail.fields` |
| PATCH | `/api/accounts/{id}` | 최고관리자 | `{role?, region_ids?, expires?(지금부터), disabled?}` |
| POST | `/api/accounts/{id}/password` | 최고관리자 | `{password}` 재설정 — 그 계정의 기존 로그인 끊김 |
| DELETE | `/api/accounts/{id}` | 최고관리자 | 자기 자신은 못 지움 |
| GET | `/api/accounts/logins?limit=` | 최고관리자 | 로그인 기록 `[{at, username, ok, reason(ok/bad_password/expired/disabled), ip}]` |
| POST | `/api/accounts/me/password` | 화면 계정 | `{old, new}` — 바꾸면 다시 로그인 |
| GET | `/api/devices/pending-search?suffix=` | 관리자 | UUID 뒤 6자리 이상 → `{count, uuid}`(1대일 때만 uuid) |

로그인(`POST /api/auth/login`)은 `.env` 계정 다음 DB 계정. 만료면 401 `ACCOUNT_EXPIRED`, 중지면 401 `ACCOUNT_DISABLED`. 쓰는 중 만료·중지되면 다음 요청이 401 `SESSION_ENDED`.

## 서버 상태 API (2026-10-04, 문제점 31번)
`GET /api/system/status`(최고관리자) → `{at, broker:{backend_connected, port_1883, port_8883, devices_online, devices_broker_connected, devices_active, log_tail}, db:{ok, size_bytes, tables, disk_total_bytes, disk_free_bytes, disk_used_pct, disk_warn, last_rollup_at, last_rollup_day, last_purge_at, telemetry_months}, processing:{buffer_pending, register_queue, telemetry_dropped, flush_failures, commands_open, deploy_open, mqtt_reconnects}, security:{hmac_keys, test_account_enabled, login_failures_24h, accounts_expiring_7d}, server:{version(GIT_SHA), started_at, uptime_sec, env}}`.

## 최근 활동 API (2026-10-05, 문제점 30·34번)
`GET /api/activity?page=&size=&cat=&q=` — 대시보드 "최근 활동". 새 표 없이 기존 기록을 합쳐 최신순.
- `size` 1~100(화면은 20·50·100, 기본 20 — 문제점 35번), `page×size ≤ 2000`(넘으면 422 — 그 전 기록은 검색·분류로).
- `cat`: `alarm`(알람 발생·해제) · `device`(상태 바뀜·재등록·재부팅) · `control`(원격 명령 1건 1줄, 그룹 스케줄 보내기) · `admin`(로그인 실패·서버 설정 변경·계정 만듦 — 최고관리자만).
- 빼는 것: 10분 보고, CONFIG/SETTINGS 응답, 재발송, 브로커 순간 끊김(통신 두절은 알람으로만), 로그인 성공.
- `q`: 시설명·주소·UUID·지역(동·시군구·시도 이름) 부분 일치. 단말에 묶인 줄만(명령·보내기는 대상 단말로), 검색하면 관리 분류는 안 나온다.
- 범위: 지역관리자·게스트는 맡은 시·도 단말 것만(명령·보내기는 대상에 범위 안 단말이 하나라도 있으면), 관리 분류 없음. 게스트도 GET 가능.
- 응답 `{items:[{at, cat, kind(ALARM_OPEN/ALARM_CLOSE/STATE_CHANGE/REBOOT/COMMAND/DEPLOY/LOGIN_FAIL/SETTING/ACCOUNT), uuid, site, region, text, by?, severity?, seq?, job_id?}], total, page, size, max_rows, cats}`. `total` = 원천별 건수 합.

### 로그인 실패 문구 (문제점 33번)
API 는 그대로 401 `{code: LOGIN_FAILED, message}`. 화면은 `api.userMessage` 로 **문구만** 보인다("사용자 이름 또는 비밀번호가 맞지 않습니다.") — 상태 번호·코드는 로그인·비밀번호 바꾸기 창에서 빼고, 다른 화면은 지금처럼 `errorText`.
