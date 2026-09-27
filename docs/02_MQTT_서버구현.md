# 02. MQTT 서버 구현 (2·3·5차)

- 갱신: 2026-09-27 (5차 — 법정동 트리·COMMAND 발행·응답·재시도·종료·override·권한, §16)
- 2026-09-26 (사양서 2026-09-26 개정 — 승인 게이트, HMAC 인증, 브로커 로그 presence, CONFIG 전체값)
- 코드: `backend/app/mqtt/`, `backend/app/core/`, `backend/app/tasks/broker_log.py`, `backend/app/modules/mqtt_auth/`
- 기준: 사양서 §1.1.2.2, §1.1.4~§1.1.7, §1.1.10, §3, §4, §16.1, ADR-001~004. 프로토콜은 단말측 사양서가
  정본이고 여기서는 **서버가 그것을 어떻게 처리하는가**만 적는다.

## 1. 연결

- aiomqtt 클라이언트 **1개**, Client ID `iotlight-backend`(`MQTT_CLIENT_ID`), 계정 `server`.
- 끊기면 1초부터 2배씩 늘려 최대 30초 간격으로 재접속. 재접속마다 아래 구독을 다시 건다
  (Clean Session 이라 브로커가 기억하지 않는다).
- 끊긴 동안의 발행은 `MqttUnavailable` 예외다. 조용히 삼키지 않는다 — REST 는 `published=false`
  (state/config) 또는 503(ping)을 받고, 응답 큐는 로그를 남기고 그 건을 버린다(다음 단말 송신이 다시 잡는다).
- 4차 TLS 전환은 `MQTT_TLS=true`, `MQTT_PORT=8883` 뿐이다. `connection.py` 밖은 손대지 않는다.

## 2. topic 표

### 구독 (단말 → 서버)

| topic | QoS(구독) | payload.type | 처리 |
|---|---|---|---|
| `iotlight/device/+/register` | 1 | `REGISTER` | device upsert(보강) + device_event → **REGISTER_ACK(항상)** → ACTIVE 면 cv 비교 |
| `iotlight/device/+/status` | 1 | `TELEMETRY` (`TM`, 1.0.0 의 `t:TM` 도 허용) | TelemetryBuffer |
| `iotlight/device/+/result` | 1 | `PONG` `CONFIG_ACK` `COMMAND_ACK`(5차, 옛 이름 `CMD_ACK` 도 받음) | device_event + command_ack / command_target(§16) |
| `iotlight/device/+/event` | 1 | `LWT` `EV` | offline 처리 / device_event |

단말은 status 를 QoS0 으로 보낸다. 구독 QoS 는 상한일 뿐이라 1 로 걸어도 QoS0 메시지는
QoS0 으로 온다. 네 패턴을 하나(`iotlight/device/+/#`)로 합치지 않는 이유는 cmd/config 를
서버 자신이 되받지 않기 위해서다.

### 발행 (서버 → 단말) — `publisher.py` 단일 창구

| topic | payload.type | QoS | retain | 언제 |
|---|---|---|---|---|
| `iotlight/device/<uuid>/config` | `REGISTER_ACK` | 1 | **1** (유일) | REGISTER 마다(큐), 관리자 상태 변경·site 변경·재발행(즉시) |
| `iotlight/device/<uuid>/config` | (빈 payload) | 1 | 1 | RETIRED 정리·단말 삭제 — retain 보관본 삭제 |
| `iotlight/device/<uuid>/config` | `CONFIG_SET` | 1 | **0** | ACTIVE 단말의 REGISTER/TELEMETRY 직후 cv 다를 때(큐), PATCH config(즉시) |
| `iotlight/device/<uuid>/cmd` | `PING` | 1 | 0 | REST 로만 |
| `iotlight/device/<uuid>/cmd` | `COMMAND` (6·7차 `SCH` `OTA`) | 1 | 0 | 개별 명령(즉시) · 개별 재시도(응답 큐) |
| `iotlight/group/<grp>/cmd` · `iotlight/all/cmd` | `COMMAND` | 1 | 0 | 노드·전체 명령(즉시). 재시도는 그룹으로 안 한다 |
| `iotlight/device/<uuid>/cmd` | `SETTINGS_GET` `SETTINGS_SET` (S-23) | 1 | 0 | 관리자 읽기·쓰기(즉시) · 새 seq 재발송(응답 큐), §17 |

발행 규칙(publisher `_send`):
- compact **한 줄** JSON(구분자 뒤 공백 없음, `ensure_ascii=False`). 결과에 줄바꿈 바이트가 있으면 발행 전에 막는다
  (모뎀은 payload 안의 줄바꿈을 여러 줄로 넘겨 단말이 한 메시지로 못 읽는다, UI_항목_명세 8.4).
- **900B 초과면 발행하지 않고 `PayloadTooLarge`**(단말 수신 줄 1,024B 한계·실측 950B 정상, ADR-007). 800B 초과는 경고.
  (2026-09-27 전에는 384B 였다 — 그것은 단말 → 서버 AT 발행 버퍼 이야기였다. SETTINGS_SET 최대 약 560B.)
- `cmd` 는 절대 retain 하지 않는다. 재접속 단말에 옛 명령이 되살아난다(5차 소등이면 사고).
- `REGISTER_ACK` = `{"type":"REGISTER_ACK","uuid","state"}` + `site`(있을 때) + `reason`(**REJECTED 일 때만**).
  `cv`/`ti`/`ka` 는 절대 싣지 않는다(§3.3). 5차: `grp`(말단 배정돼 있으면 상태와 무관하게 항상) —
  `{"type","uuid","state"[,"site"][,"reason"][,"grp"]}`. 모양은 `config_sync.register_ack_job_for()` 하나가 정하고
  수신 경로(큐)와 관리자 즉시 경로가 같이 쓴다.
- `CONFIG_SET` = `{"type":"CONFIG_SET","cv","ti","ka"}` + `lat`/`lon`(둘 다 있을 때). **항상 전체값**(S-13).
  `cv` 0 은 빌더가 `ValueError` 로 막는다.

## 3. 수신 공통 검증 (`handlers.Dispatcher`)

순서대로. 걸리면 카운터를 올리고 **무시**한다(예외 없음, 수신 루프가 죽지 않는다).

1. topic 정규식 `^iotlight/device/([0-9A-F]{24})/(register|status|result|event)$`
   불일치 → `malformed_topic`. 소문자 uuid 도 위반이다(DB CHECK·인증 API 전제).
2. JSON 이 아니거나 객체가 아님 → `malformed_payload`.
3. `"t"` 만 있으면 `"type"` 으로 복사(1.0.0 펌웨어). uuid 당 1회 로그, `legacy_t_devices` 갱신.
4. payload 에 `uuid` 가 있고 topic uuid 와 다르면 → `uuid_mismatch` (사양서 §1.1.4).
5. kind/type 조합이 표에 없으면 → `unknown_type`.

## 4. 단말 상태 기계 (사양서 §2, §3.4, docs/05)

```
              REGISTER(새 UUID)
                    │
                    ▼
  ┌──────────── PENDING ◄────────────┐◄──────────┐
  │  승인         │  거부              │ 재검토      │ 재설치(REGISTER 다시 옴)
  ▼               ▼                  │            │
ACTIVE ──중지──► SUSPENDED      REJECTED        RETIRED
  ▲               │                  │            ▲
  └────해제───────┘                  └──폐기──────┘   (ACTIVE/PENDING/SUSPENDED 에서도 폐기 가능)
  │
  └──승인 취소──► PENDING
```

- **새 UUID 는 PENDING** 으로 생긴다(DB 기본값). REGISTER 를 놓치고 TELEMETRY/result 로 먼저 생겨도 PENDING.
- 전이 표는 `constants.STATE_TRANSITIONS`. 표에 없는 전이는 409 `INVALID_STATE_TRANSITION`.
  같은 상태로의 "전이"도 409 다 — site 만 바꾸려면 PATCH config.
- **RETIRED → PENDING 은 서버가 자동으로도 한다**: 빈 retain 상태에서 REGISTER 가 다시 오면
  (같은 보드를 다른 곳에 재설치) 핸들러가 PENDING 으로 되돌리고 `device_event(STATE_CHANGE, by=register)` 를 남긴 뒤 ACK 한다.
- 관리자 전이는 `modules/device/service.set_state()`: DB 저장 + `STATE_CHANGE` 이벤트 → REGISTER_ACK retain
  **즉시** 발행(RETIRED 는 발행 뒤 빈 retain) → ACTIVE 로 갈 때 `cv_server = next_cv_server(...)`(1 이상, 단말보다 크게).
  **CONFIG_SET 은 여기서 보내지 않는다** — 단말이 ACTIVE 를 받고 보내는 첫 TELEMETRY 때 §6 판정으로 나간다(S-10, §1.1.10).

## 5. 메시지별 처리

### REGISTER
1. `device` upsert(topic uuid 기준). 있는 키만 갱신: `fw device_model modem_model imei iccid msisdn`,
   `cv→cv_device ss→ss_device ti→ti_device ka→ka_device`. `last_register_at`, `last_seen_at`(GREATEST).
2. `device_event(REGISTER)`. dedup 없음 — 재연결마다 같은 내용으로 오는 것이 정상.
3. `online=true` (`presence.apply_presence`, source=register). REGISTER 는 살아 있는 연결로만 오므로
   브로커 로그 tail 이 꺼져 있거나(개발 PC) 줄을 놓쳤어도 여기서 맞춰진다. 플래그가 이미 true 면 no-op(이력 없음).
4. state 가 RETIRED 면 PENDING 으로 되돌린다(§4).
5. **REGISTER_ACK 를 상태와 무관하게 항상** 응답 큐(§7)에 넣는다. retain=1.
6. **ACTIVE 일 때만** §6 판정 → CONFIG_SET 큐. FIFO 라 같은 단말의 ACK 뒤에 나간다(사양서 §3.1 순서).

### TELEMETRY (status)
DB 를 만지지 않는다. `TelemetryBuffer.offer()` 로 끝. §8 참고. type 은 `TELEMETRY`/`TM`/`t:TM` 셋 다.

### result
공통: `last_seen_at` GREATEST 갱신(미등록 uuid 면 PENDING 행 생성). 이력 1행 = 트랜잭션 1개.

| type | device_event | dedup_key | 그 외 |
|---|---|---|---|
| `PONG` | `PONG` | `uuid:PONG:<seq>:sha1` | `command` 조회 → target 이 이 uuid 인지 확인(§1.1.5) → `command_ack` 1행(PK 충돌 무시) → `acked_count+1`, 다 모이면 `finished_at`, `result=OK`. seq 를 모르거나 단말이 다르면 `pong_mismatch` |
| `CONFIG_ACK` | `CONFIG_ACK` | `uuid:CONFIG_ACK:<cv>:sha1` | 아래 표 |
| `COMMAND_ACK` (`CMD_ACK`) | `COMMAND_ACK` | `uuid:COMMAND_ACK:<seq>:sha1` | §16.4 |
| `SETTINGS` | `SETTINGS` | `uuid:SETTINGS:<seq>:sha1` | §17.3 |
| `SETTINGS_ACK` | `SETTINGS_ACK` | `uuid:SETTINGS_ACK:<seq>:sha1` | §17.4 |

CONFIG_ACK `result` 별:

| result | 처리 | 카운터 |
|---|---|---|
| `OK` | `cv == cv_server` 일 때만 `cv_device=cv`, `ti_device`/`ka_device` = 서버 적용값(`override ?? profile`). ti/ka 는 TELEMETRY 에 안 실려 여기서만 알 수 있다. 옛 ack 로 덮지 않도록 cv 일치 조건. **동기화 완료 판정은 여전히 다음 TELEMETRY 의 cv echo** | `config_ack_ok` |
| `RANGE` | 경고. 서버가 범위 밖 값을 보낸 것 = PATCH/프로필 검증 버그 | `config_ack_range` |
| `STATE` | 단말이 "승인 전"이라 함. DB 가 ACTIVE 면 단말이 REGISTER_ACK 를 못 받은 것(retain 유실) → **REGISTER_ACK 를 다시 retain**(큐). DB 가 ACTIVE 가 아니면 서버가 ACTIVE 전에 CONFIG 를 보낸 것이라 error 로그 | `config_ack_state` |
| `FLASH` | 단말 Flash 기록 실패, 이전 값 유지. **그 단말의 60초 쿨다운을 풀어** 다음 송신 때 바로 재전송. 반복되면 단말 점검 | `config_ack_flash` |

dedup_key 에 payload 해시가 들어가는 이유: 같은 seq/cv 의 두 번째 결과(내용이 다름)는 남기고,
QoS1 재전송(바이트 동일)만 걸러진다. `ON CONFLICT DO NOTHING`.

### event
| type | 처리 |
|---|---|
| `LWT` | `presence.apply_presence({uuid: False})` — 브로커 로그 경로(§9)와 **같은 함수**. 플래그가 바뀔 때만 `online_changed_at`, `offline_at`, `device_event(OFFLINE)`. 별도로 `device_event(LWT)` 도 남긴다. **`last_seen_at` 은 건드리지 않는다** — 브로커가 대신 보내는 사망 통지를 "방금 통신"으로 적으면 죽은 단말이 온라인이 된다. 버퍼의 TM 은 버리지 않는다(TM flush 는 `online` 을 안 건드린다). 모뎀은 Will 을 못 넣지만 시뮬레이터·후속 모뎀용으로 남긴다 |
| `EV` | `last_seen_at` 갱신 → `device_event(kind=ERR, payload={er,ep,bv,ts})`, dedup `uuid:EV:<ts>:sha1` |

## 6. CONFIG 판정과 cv 규칙 (`core/config_rules.py`, `mqtt/config_decide.py` — 사양서 §1.1.7 S-13, §4.2)

적용값: `ti = ti_override ?? profile.ti`, `ka = ka_override ?? profile.ka` (`config_profile` 시드 1~3, docs/03).

**단말 송신 직후**(REGISTER 핸들러, TELEMETRY flush) 판정 — `decide_and_enqueue()`:

```
state != ACTIVE          → 안 보냄 (단말이 STATE 로 거부, S-10)
cv_device 없음           → 안 보냄 (1차 펌웨어)
cv_device == cv_server   → 안 보냄 (동기화됨)
그 외:
    cv_server = next_cv_server(cv_server, cv_device)
                 = 1                if cv_server == 0        (규칙 2: 0 은 보내지 않는다)
                 = cv_device + 1    if cv_device > cv_server (규칙 3: PC 도구·DB 복구)
                 = cv_server        otherwise
    → DB 에 먼저 쓰고 → 큐(§7)  (60초 쿨다운은 큐가 본다)
```

cv_server 를 큐에 넣기 **전**에 쓰는 이유: 발행이 늦거나 실패해도 서버 의도값이 1 이상·단말보다 크게
굳어 있어야 다음 TELEMETRY 의 비교가 같은 답을 낸다. 65535 를 넘으면 1 로 감는다(0 은 건너뛴다).

**관리자 경로**:

| 경로 | cv 규칙 | 발행 |
|---|---|---|
| `PATCH /state → ACTIVE` | `next_cv_server` (0 → 1, 단말이 크면 +1) | REGISTER_ACK 만. CONFIG 는 첫 TELEMETRY 때 |
| `PATCH /config` 적용값(ti/ka/lat/lon) 변경 | `bump_cv_server = max(cv_server, cv_device or 0) + 1` | ACTIVE 면 **즉시 1회**(+쿨다운), 아니면 `published=false, reason=NOT_ACTIVE` |
| `PATCH /config` site/address/bjd 만 | 그대로 | site 바뀌면 REGISTER_ACK 재발행(ACTIVE/PENDING/SUSPENDED/REJECTED) |
| `PATCH /profiles/{id}` ti/ka 변경 | 그 프로필의 **`cv_server > 0` 단말 전부 +1** (0 은 그대로) | 안 보냄 — 각 단말의 다음 송신 때 |

발행 시 `device.config_sent_at` + `device_event(CONFIG_SET, payload)`.

## 7. 응답 큐 (`mqtt/config_sync.py`) — 토큰 버킷 + CONFIG 쿨다운

REGISTER_ACK 와 CONFIG_SET 이 같은 FIFO 로 나간다. 사양서 §1.1.10 "단말이 보낸 직후에 보낸다" 를
지키되, 브로커 재시작 뒤 1만 대 REGISTER 폭주에서 발행량을 누른다.

- 토큰 버킷 `REGISTER_REPLY_RATE_PER_SEC`(기본 200/s). 1만 대면 50초 안에 다 나간다 — 단말 REGISTER
  재전송(5분)보다 훨씬 짧아 "즉시"로 본다.
- 대기열 상한 `REGISTER_REPLY_QUEUE_MAX`(50,000). 넘으면 가장 오래된 것을 버리고 `register_reply_dropped`.
  REGISTER_ACK 를 못 받은 단말은 5분 뒤 REGISTER 를 다시 보내고, CONFIG 를 못 받은 단말은 다음 TELEMETRY 때 다시 잡힌다.
- **REGISTER_ACK 는 쿨다운·중복 검사 없음** — REGISTER 마다 반드시 답한다(§3.3).
- **CONFIG_SET 은 같은 단말 60초 쿨다운**(`CONFIG_RESEND_COOLDOWN_SEC`). REGISTER 직후 첫 TELEMETRY 가 아직
  옛 cv 를 싣고 오는 것이 정상 흐름이라 쿨다운이 없으면 같은 CONFIG_SET 이 연달아 두 번 나간다. 이미 줄 서 있는
  uuid 도 다시 넣지 않는다. CONFIG_ACK `FLASH` 는 쿨다운을 푼다. PATCH config 즉시 발행은 `mark_sent()` 로 쿨다운을 건다.
- 발행 성공 시 DB 기록: CONFIG_SET → `config_sent_at` + `device_event(CONFIG_SET)`, REGISTER_ACK → `register_ack_at`
  (RETIRED 정리는 NULL) + `device_event(REGISTER_ACK)`. 실패(브로커 끊김)는 로그 후 폐기.

관리자 REST 경로(state/config/register-ack/delete)는 큐를 타지 않고 publisher 로 즉시 발행한다.

## 8. TelemetryBuffer — 1초 배치

```
offer() ──▶ _rows[(uuid, payload, received_at)…]  (도착 순서, DB 접근 없음)
                    │  1초마다 / 20,000행 넘으면 즉시 / 종료 시
                    ▼
        _judge_batch()  프로세스 내 last_sq 캐시로 sq 판정, uuid 별 최신 1건 선별
                    ▼
        ┌── 트랜잭션 1개 ─────────────────────────────────────────────┐
        │ INSERT telemetry  (모든 행, 1,000행씩 multi-row, 충돌 무시)    │
        │ SELECT config_profile (프로필 캐시)                            │
        │ INSERT device … ON CONFLICT (uuid) DO UPDATE  (uuid 별 1행)   │
        │    last_telemetry, last_sq, cv_device, ss_device,             │
        │    fw = coalesce(new, old),                                   │
        │    last_telemetry_at / last_seen_at = GREATEST(old, new),     │
        │    lost_count += Δ, reboot_count += Δ                         │
        │    RETURNING uuid, state, cv_server, cv_device, overrides,     │
        │              profile_id, lat, lon                             │
        │ decide_and_enqueue(§6)  → cv_server UPDATE (필요 시)          │
        │ INSERT device_event (REBOOT / LOST)                            │
        └───────────────────────────────────────────────────────────────┘
                    ▼
        ACTIVE 이고 cv 다른 uuid → ConfigSyncQueue.offer(reason=telemetry)
        state != ACTIVE 인 uuid  → telemetry_not_active += n (저장은 한다)
```

- **GREATEST 인 이유**: flush 가 늦어진 사이 result/event 가 더 최근 `last_seen_at` 을 썼을 수 있다.
- **실패 정책**: 트랜잭션이 실패하면 그 묶음을 **버리고** `telemetry_dropped += 행 수`, `flush_failures += 1`.
  되돌려 넣지 않는다 — DB 가 아픈 동안 대기열이 무한정 자라는 것이 재기동보다 나쁘다(docs/00 §5).
- **처음 보는 uuid** 는 여기서 `device` 행이 생긴다(PENDING).
- 기동 시 `warm()` 으로 DB 의 `last_sq` 를 캐시에 적재한다.
- sq 판정은 2차 그대로(`mqtt/sq.py`): 건너뜀=유실(상한 10만), 감소=재부팅, uint32 wrap 은 유실.

## 9. 접속 상태 (`tasks/broker_log.py`, `core/broker_log.py`, `core/presence.py` — ADR-004, 사양서 §16.1)

모뎀이 LWT 를 못 넣으므로 **A(Mosquitto 로그) + C(수신 시각)** 를 쓴다.

### A. 브로커 로그 tail
- `MOSQUITTO_LOG_PATH`(공유 볼륨, 기본 `/var/lib/iotlight/mqtt/mosquitto.log`) 를 0.5초마다 poll.
  비어 있으면 태스크를 띄우지 않는다(개발 PC).
- 파서(`core/broker_log.parse_line`, 단위 시험 있음) — Mosquitto 2.0.15, `log_timestamp_format %Y-%m-%dT%H:%M:%S`:

  | 줄 | 결과 |
  |---|---|
  | `New client connected from <addr> as <UUID> (p2, c1, k300, u'<UUID>').` | ONLINE (keepalive 파싱만, 저장 안 함) |
  | `Client <UUID> disconnected.` / `closed its connection.` / `has exceeded timeout, disconnecting.` | OFFLINE |
  | `Socket error on client <UUID>, disconnecting.` | OFFLINE |
  | `Client <UUID> disconnected, not authorised.` | **무시** (접속된 적 없음). `broker_log_not_authorised` 로만 센다 |
  | client id 가 `^[0-9A-F]{24}$` 가 아닌 줄 (`iotlight-backend`, healthcheck, 시험 도구) | 무시 |

- 한 poll 안에서 같은 UUID 가 여러 번 나오면 **마지막** 전이만 반영. 묶음을 트랜잭션 1개로.
- `apply_presence()`: `online` 이 실제로 바뀌는 행만 `online_changed_at`(+`offline_at`) 갱신하고
  `device_event(ONLINE|OFFLINE, payload={source})`. 같은 줄이 두 번 와도 이력이 두 번 남지 않는다.
  **처음 보는 UUID 는 행을 만들지 않는다**(REGISTER 가 먼저다, §3.2). `last_seen_at` 은 건드리지 않는다.
- 기동 시 **파일 끝부터**. 파일 없음 → 5초마다 재시도(`/health.broker_log_tail=false`). 크기가 마지막
  오프셋보다 작아지면(truncate/회전) 처음부터. 개행 없는 꼬리는 다음 poll 로.
- LWT(§5 event)와 REGISTER 수신(online=true)도 같은 `apply_presence()` 를 쓴다.

### C. 수신 시각 보조 — `is_online` (파이썬·SQL 같은 규칙)

```
is_online = device.online                                       (A)
        AND last_seen_at >= now - window                        (C)
window    = ACTIVE → (ti_override ?? profile.ti) × DEVICE_ONLINE_FACTOR(3)
            그 외  → PENDING_OFFLINE_SEC (4200 = 70분: REGISTER 재전송 최대 30분 × 2 + 여유)
```

둘 다 만족해야 true(docs/05). C 가 있어야 백엔드 재시작·로그 회전으로 플래그가 굳어도 오프라인이
잡힌다. 목록 필터 `?online=`, `counts.online`, `is_online` 이 전부 `online_clause()` 하나를 쓴다.
화면은 "MQTT 상태(online/online_changed_at)" 와 "마지막 수신(last_seen_at)" 을 나눠 보여 준다.

## 10. 단말 인증 (`modules/mqtt_auth/`, `core/device_password.py`, `core/mqtt_acl.py` — ADR-003, 사양서 §1.1.2.2)

브로커 이미지 `iegomez/mosquitto-go-auth`: `files`(server, solarlte-test) + `http`(단말 → 백엔드).

| API | 규칙 |
|---|---|
| `POST /internal/mqtt/auth` `{username,password,clientid}` | username 이 `^[0-9A-F]{24}$` 아니면 403(server/시험 계정은 files 몫 — 틀린 비번의 `server` 도 여기로 넘어오는데 UUID 가 아니라 403). `clientid` 가 있고 username 과 다르면 403. `password == HMAC-SHA256(K_i, UUID)[:16].hex()` 를 **활성 키 전부**에 `compare_digest`(첫 일치에서 끊지 않는다 — 어느 키인지 타이밍으로 새지 않게). **승인 상태·DB 를 보지 않는다** — PENDING/REJECTED/처음 보는 UUID 도 200 |
| `POST /internal/mqtt/acl` `{username,clientid,topic,acc}` | `core/mqtt_acl.device_acl()` 순수 함수. acc 1 read / 2 write / 4 subscribe(3 readwrite 는 항상 거부). write: `device/<u>/{register,status,result,event}`. read/subscribe: `device/<u>/{cmd,config}`, `group/#`(하위 어떤 것이든, `group/#` 구독도 허용), `all/cmd`. 그 외·와일드카드(`iotlight/#`, `device/+/status`)·남의 UUID 는 403 |
| `POST /internal/mqtt/superuser` | 항상 403 |

- 키: `MQTT_HMAC_KEYS="K1:<hex64>[,K2:<hex64>]"`. 기동 때 파싱해 틀리면 죽는다. 기본값은 사양서 공개 시험 키
  `TEST:000102…1e1f`(prod 에서 TEST 가 있으면 경고). 값은 로그·DB·`/health` 에 안 나간다(`hmac_keys` 는 ID 만).
- `MQTT_AUTH_SHARED_SECRET` 이 비어 있지 않으면 `X-Auth-Secret` 헤더 검사. 인증 라우터는 DB 세션을 쓰지 않는다
  (1만 대 재접속 폭주에서 DB 왕복이 인증 경로에 끼면 안 된다).
- 시험값(단위 시험 `tests/test_device_password.py`): `20363930594D50170004003A → 70e8a87fba4a997f7c24d1f98300753a`,
  `00112233445566778899AABB → 13f271bab5a9de23c3577ced778a46b9`.
- 카운터 `mqtt_auth_ok` / `mqtt_auth_fail` / `mqtt_acl_deny`.

## 11. 계정 파일 (`core/mqtt_accounts.py`) — server 계정만

```
render_passwd()  = server:PBKDF2$sha512$100000$<salt b64>$<hash b64>
                 + solarlte-test:… (MQTT_TEST_ACCOUNT_ENABLED 일 때만)
render_acl()     = user server / topic readwrite iotlight/#  (+ 시험 계정)   ← pattern 줄 없음
        │  같은 디렉터리 임시 파일 → os.replace (원자적), pg_advisory_xact_lock 으로 직렬화
        ▼
  MOSQUITTO_PASSWD_EXPORT / MOSQUITTO_ACL_EXPORT (공유 볼륨)
        │
        ▼
  mosquitto entrypoint 감시 루프(infra/)가 md5 가 바뀐 경우만 설치 + 브로커 재시작
  → passwd.applied / aclfile.applied 에 md5 기록 → wait_applied() 가 본다
  mqtt_account_export(id=1) 에 passwd_md5 / acl_md5 / acl_applied_md5 기록
```

- 해시 salt 는 **결정적**(`HMAC-SHA256(key=username, msg=password)[:16]`) — go-auth 는 HUP 으로 passwd 를 다시 읽지
  못해 감시 루프가 md5 변화로 재시작을 결정하는데, salt 가 매번 다르면 5분마다 불필요한 재시작이 난다.
- 시점: 기동 시 + 5분 재조정. 단말 행은 없다(HMAC). CSV import 와 `POST /api/devices/import-accounts` 는 폐기.
- 경로가 비어 있으면 no-op(개발 PC, anonymous 브로커).
- 전환 순서(사양서 §1.1.2.2): 플러그인 설치(공용 계정 유지) → 시험 키 확인 → `MQTT_HMAC_KEYS=K1:…` →
  단말 HMAC 펌웨어 → `MQTT_TEST_ACCOUNT_ENABLED=false`.

## 12. 카운터 (`GET /api/metrics`)

| 이름 | 뜻 |
|---|---|
| `received.{register,status,result,event}` | kind 별 수신 |
| `received_type.{REGISTER,TELEMETRY,TM,PONG,CONFIG_ACK,…}` | type 별 수신 |
| `malformed_topic` `malformed_payload` `uuid_mismatch` `unknown_type` | §3 검증 탈락 |
| `telemetry_flushed` `telemetry_dropped` `flush_failures` `telemetry_not_active` | 배치 적재 / 폐기 / ACTIVE 아닌 단말의 TM |
| `flush_ms_last` `flush_ms_max` `buffer_pending` | flush 소요 / 대기 행 수 |
| `register_queue` `register_reply_dropped` | 응답 큐 길이 / 상한 초과 폐기 |
| `register_ack_sent` `config_set_sent` | 발행 수 |
| `config_ack_ok` `config_ack_range` `config_ack_state` `config_ack_flash` | CONFIG_ACK result 별 |
| `pong_mismatch` | seq 모름 또는 단말 불일치 응답 |
| `command_published` `command_retry_sent` `command_ack` `command_ack_mismatch` | 5차 COMMAND 발행(topic 단위) / 개별 재시도 / ACK / 모르는 seq·스냅숏 밖 단말 |
| `settings_sent` `settings_resent` `settings_timeout` `settings_report` `settings_ack` `settings_mismatch` `settings_local_saved` | S-23 설정 첫 발송 / 새 seq 재발송 / 3회 무응답 / SETTINGS·SETTINGS_ACK 수신 / 모르는 seq / ss 변화로 local_saved (§17) |
| `mqtt_connected` `mqtt_reconnects` `mqtt_publish_failures` | 연결 |
| `mqtt_auth_ok` `mqtt_auth_fail` `mqtt_acl_deny` | 브로커 인증 API |
| `broker_log_tail` `broker_log_lines` `broker_log_online` `broker_log_offline` `broker_log_not_authorised` `broker_log_errors` | 로그 tail |
| `legacy_t_devices` | `t` 키로 보고 중인 단말 수 |
| `cmd_seq` `cmd_seq_alert` | 시퀀스 현재값 / uint32 99% 초과 경보 |

`GET /health`: `ok = mqtt_connected AND db_ok`, `buffer_pending`, `register_queue`, `telemetry_dropped`,
`flush_failures`, `broker_log_tail`(태스크 살아 있고 파일 찾음), `hmac_keys`(ID 목록), `test_account_enabled`, `env`.
DB 가 죽어도 HTTP 200(healthcheck 로 컨테이너를 재시작하면 버퍼와 MQTT 세션까지 잃는다).

## 13. 주기 작업

| 작업 | 시각(KST) | 내용 |
|---|---|---|
| `tasks/partitions.run` | 기동 시 + 매일 01:00 | `telemetry_YYYYMM` 이번 달·다음 달 생성, 13개월 초과 DROP, `device_event` 365일 초과 DELETE |
| `tasks/daily_rollup.run` | 매일 00:30 (+`POST /api/admin/rollup`) | 전날(KST) `telemetry_daily` |
| `_reconcile_accounts` | 5분 | server 계정 passwd/aclfile 재내보내기(내용 같으면 감시 루프가 무시) |
| `tasks/broker_log.BrokerLogTail` | 상시(0.5초) | §9 |
| `tasks/command_finisher.run` | 30초 | 5차 COMMAND 종료 판정 + 재시도 후보 집합 재조정(§16.5) |

## 14. 기동·종료 순서 (`main.py`)

HMAC 키 파싱(틀리면 죽음) → DB 대기 → 파티션 → 계정 파일 내보내기 → last_sq warm → 재시도 후보 warm(5차) → 응답 큐 → 버퍼 →
MQTT 연결 → 브로커 로그 tail → 스케줄러. 종료는 역순(로그 tail → 연결 → 버퍼 flush → 큐).

## 15. 시나리오와 연결 (docs/06)

- S3-01 신규 접속 → PENDING, REGISTER_ACK retain, TELEMETRY 없음, PING/PONG 됨(§5 result 는 상태 무관).
- S3-02 승인 → ACTIVE retain → 단말 첫 TELEMETRY → cv 비교 → CONFIG_SET(전체값, cv≥1) → CONFIG_ACK OK → 다음 TELEMETRY cv 일치.
- S3-03 재부팅 → retain ACK 로 복귀. 같은 topic 의 CONFIG_SET(retain 0)이 retain 보관본을 지우지 않는다.
- S3-04 서버 재시작 → 단말 REGISTER 재전송 → 큐가 ACK. 브로커 로그는 EOF 부터라 online 은 REGISTER 가 맞춘다.
- S3-05 SUSPENDED → TELEMETRY 중지·연결 유지·CONFIG_ACK STATE 없음(안 보내니까) → ACTIVE 로 해제 → 재개.
- 인증: 시험 키로 `mosquitto_pub -u <UUID> -P <계산값>` 성공, 한 글자 틀리면 거부, 남의 UUID topic 발행 거부, `iotlight/#` 구독 거부.


## 연결 세션 (2026-09-26 S2-11 반영)

- 백엔드 연결은 **영속 세션**(`clean_session=False`, client id `iotlight-backend` 고정). 브로커 재기동·백엔드
  재시작 직후 단말이 보낸 QoS1 REGISTER/status 를 브로커가 큐(`max_queued_messages 10000`)에 쌓았다가
  넘겨준다. 전제: 브로커 `persistence true`.
- 재접속 백오프 1→2→4→**5초 상한**. 30초 상한이면 브로커 재시작 뒤 단말(7~30초)보다 늦게 돌아와
  REGISTER 를 놓쳤다(S2-11 실측: 1+2+4+8 = 15초 공백).


## 16. 5차 — 원격 명령 (ADR-005, 사양서 §3.9.3 · §3.10 · §17)

코드: `core/command_rules.py`(순수 규칙), `modules/command/`(REST), `mqtt/command_retry.py`(자동 재시도),
`tasks/command_finisher.py`(종료), `mqtt/handlers.py`(COMMAND_ACK), `core/auth.py`(권한),
`core/region_tree.py`·`modules/region/`·`core/kakao_geo.py`(법정동 트리).

### 16.1 트리와 grp
- `region`: 시도 > 시군구 > 법정동(말단). 말단만 `bjd_code`(카카오 `address.b_code` 앞 10자리). group_id =
  `bjd_code + "00"` 은 저장하지 않고 계산한다.
- 말단 추가는 카카오 주소 검색 → 시도(약칭 "경기" 는 정식 "경기도" 로)·시군구(세종은 시군구가 없어 시도 이름을
  다시 쓴다)·법정동(`region_3depth_name`)·좌표(x→lon, y→lat)로 상위까지 find-or-create. 말단은 **코드로** 찾는다.
  `APP_ENV=dev` 만 직접 입력 허용. 키는 헤더(`Authorization: KakaoAK …`)로만 나가고 로그·응답에 안 남는다. 시간 초과 5초.
- 단말 배정(`PATCH /config`·`/state` 의 `node_id`) → `node_id`, `bjd_code`, `grp` 를 같이 쓰고 grp 가 바뀌면
  REGISTER_ACK retain 재발행(RETIRED 제외). 승인(ACTIVE)은 말단이 필요하다 — `APPROVE_REQUIRES_NODE`,
  **안 적으면 운영 true / `APP_ENV=dev` 는 false**(3차 시나리오 호환). 명시하면 그 값.

### 16.2 발행 (`POST /api/commands`)
```
대상 해석 ─ device: device/<uuid>/cmd
          ─ node  : 말단 = group/<grp>/cmd 1회, 상위 = 하위 말단마다 1회(bjd 순)   (seq 는 하나)
          ─ all   : all/cmd 1회 — 최고관리자만(403 FORBIDDEN)
검증     ─ ch 1~3 중복 없음(기본 [1,2]) · pwm 은 act=pwm 만·ch 와 같은 길이·0~100
           · act≠auto: dur(1~86400) 또는 dur_preset(30m/1h/3h/tonight) 중 하나 · auto: 둘 다 없음 · exp 1~3600(기본 30)
tonight  ─ suntable(app/vendor, 원본 그대로) 표의 **소등(아침, 일출 쪽)** 시각. 지금(KST)이 오늘 소등 전이면
           오늘 것, 지났으면 내일 것까지 남은 초(올림, 1~86400). 좌표: 노드 → 그 노드 좌표, 없으면 첫 하위 말단 /
           단말 → 단말 lat/lon, 없으면 그 말단 / 전체·없음 → DEFAULT_LAT/LON(서울)
대상 수  ─ 범위 안 ACTIVE 단말. 0 이면 409 NO_TARGETS(seq 발번 전)
seq      ─ nextval('cmd_seq') → payload {"type":"COMMAND","seq","ts","exp","act","ch"[,"pwm"][,"dur"]}
           (ts = 보낸 시각 KST YYMMDDThhmmss, 키 순서 고정, 384B 검사)
DB       ─ command(type=COMMAND, target_kind, target_id, created_by, topics, exp, expected_count)
           + command_target 스냅숏(INSERT … SELECT, status=pending, attempts=1, last_sent_at=now)
커밋     ─ **발행 전에 커밋**. 시뮬레이터는 수 ms 안에 ACK 를 돌려준다 — 미들웨어 커밋(응답 뒤)을 기다리면
           ACK 핸들러가 대상 행을 못 찾는다
발행     ─ topic 마다 publisher.publish_command (QoS1, retain 0). 실패 → command 행 삭제(대상 CASCADE)
           + 503 MQTT_UNAVAILABLE. 여러 topic 중 일부만 나갔으면 그 단말들의 ACK 는 "모르는 seq" 로 남는다
이력     ─ 개별(device)만 device_event(COMMAND_SENT). 대상 uuid 를 재시도 후보 집합에 넣는다
```

### 16.3 자동 재시도 — 단말이 뭔가 보낸 직후 (S-17/S-18)
- 걸리는 곳: REGISTER 핸들러(트랜잭션 커밋 뒤), TELEMETRY flush(커밋 뒤, 묶음의 uuid 전부).
- **평소 비용 0**: 메모리 집합(재시도 후보가 있는 uuid)에 없으면 쿼리를 안 한다. 기동 시 DB 로 채우고, 발송 때
  넣고, 30초 종료 타이머가 DB 기준으로 다시 맞춘다(ACK 로 끝난 uuid 가 빠진다).
- 후보면 `UPDATE command_target … FROM command … RETURNING` 한 문장으로 **선점**(attempts+1, last_sent_at=now).
  조건: status ∈ {pending, EXPIRED} · 명령 미종료 · now < sent_at + dur(auto 는 + COMMAND_TIMEOUT_SEC)
  · attempts < COMMAND_MAX_ATTEMPTS(3) · last_sent_at < now − COMMAND_RETRY_MIN_SEC(20)
  · **그 단말에 더 새 명령(seq 큰 대상 행)이 없다** — 옛 소등이 새 점등 뒤에 도착하면 옛 것이 적용된다.
  인덱스 `(uuid, status)`. 파이썬 판 `command_rules.retry_eligible` 과 같은 규칙.
- 선점한 것만 응답 큐(§7, 토큰 버킷·FIFO)에 `CommandRetryJob` → **개별 topic, 같은 seq, 새 ts**(발행 순간 시각 —
  큐 대기가 exp 를 잡아먹지 않게) → device_event(COMMAND_SENT, by=register/telemetry). 그룹 재발행은 하지 않는다.
- 수동 재시도(`POST /api/commands/{seq}/retry`)는 같은 선점 쿼리에서 시도 상한·RETRY_MIN 만 뺀다(사람이 누른 것).
  나머지 조건(미종료·유효·pending/EXPIRED·더 새 명령 없음)은 같다. 발행은 같은 큐. 브로커 끊김이면 503.

### 16.4 COMMAND_ACK (result: OK / LOCAL / EXPIRED / BAD / STATE)
1. device_event(COMMAND_ACK), dedup `uuid:COMMAND_ACK:<seq>:sha1(payload)` — QoS1 재전송은 여기서 끝.
2. seq 가 COMMAND 가 아니거나(모름·PING) 이 단말이 스냅숏에 없으면 경고 + `command_ack_mismatch`.
3. `command_target.status = result`(단, **OK·LOCAL 은 바뀌지 않는다** — 늦은 EXPIRED 가 OK 를 덮지 않게.
   LOCAL 은 "현장 조작 중이라 버림"이고 현장이 끝나도 단말이 적용하지 않는다(§3.10.8·§3.10.11 2026-09-27 개정) — 종결),
   `acked_at`, `ack`(원본). 첫 응답(pending 에서)이면 `command.acked_count + 1`. 모르는 result 값은 원본만 남긴다.
4. **OK**(그리고 target 이 OK) → `device.override_*` (§16.6). LOCAL 은 override 를 남기지 않는다.
5. 대상이 다 응답했으면 종료 판정을 그 seq 하나에 바로 돌린다(개별 명령이 30초 동안 "진행 중"으로 보이지 않게).

### 16.5 종료 (30초 타이머, `tasks/command_finisher`)
미종료 COMMAND 마다 대상 상태를 센다. 종결 = OK/LOCAL/BAD/STATE + 시도를 다 쓴 EXPIRED.
- 전부 종결 → `OK`(전부 OK) / `PARTIAL`
- `COMMAND_TIMEOUT_SEC`(900) 경과 → 응답 0(전부 pending)이면 `TIMEOUT`, 아니면 `PARTIAL`
끝난 명령은 재시도 대상에서 빠진다. 파이썬 판 `command_rules.finish_result`.

### 16.6 override 표시 (S-19, §3.10.8)
- 계층: device 명령 → `device`, node → `group`, all → `all`.
- OK(on/off/pwm): `override_until = 그 단말에 마지막으로 보낸 시각(last_sent_at = 그 발송의 ts) + dur`.
  기록된 것이 **더 높은 계층이고 아직 유효**하면 덮지 않는다(단말은 여전히 그 계층 값을 쓴다).
- OK(auto): 개별 auto 는 전부 NULL. 그룹/전체 auto 는 기록된 계층이 같을 때만 NULL.
- 재부팅(TM flush 의 sq 감소 판정, REBOOT 이벤트)이면 그 단말 override 필드 NULL — 단말이 잃는다.
- **Telemetry `md == 1`(현장 조작)** 이 오면 override 필드 NULL — 현장 조작이 시작되면 단말이 원격을 전부 취소한다
  (§3.10.8 2026-09-27 개정). 둘 다 그 TM 을 받은 **뒤에** 보낸 명령의 override 는 남긴다(flush 1초 지연 경합, B11).
  대기 중인 command_target 은 건드리지 않는다(단말이 LOCAL 로 답하거나 무응답으로 끝난다).
- 원격 OK·유지시간 끝·현장 시작 뒤 2초에 오는 추가 Telemetry 는 주기 TM 과 같다(`sq` 이어짐) — 유실·재부팅이 아니고,
  주기보다 이르다고 따로 판정하는 곳도 없다.
- 화면: `remote_active = last_telemetry.md == 2 AND override_until > now`, `remote_remaining_sec` = until 까지
  남은 초(미래일 때만. md 와 무관 — OK 직후 md 를 실은 TM 전에도 보인다). 목록 `?remote=true` 는 SQL 판.

### 16.7 권한 (§3.9.3 #9)
- 사용자 = `X-Remote-User`(nginx Basic auth 사용자명). `SUPER_ADMIN_USERS`(쉼표, 기본 `admin`) = super_admin.
- 헤더 없음: `APP_ENV=dev` 면 super_admin `local`, 아니면 admin `anonymous`.
- super_admin 전용: 전체(all) 명령, 트리 편집(from-address·PATCH·DELETE). 그 외 403 `FORBIDDEN`.
- `command.created_by` = 사용자명. `GET /api/me` → `{"user","role"}`.

### 16.8 설정 (`.env`)
`KAKAO_REST_API_KEY`(""), `SUPER_ADMIN_USERS`(admin), `APPROVE_REQUIRES_NODE`(미설정 = prod true / dev false),
`COMMAND_EXP_SEC`(30), `COMMAND_TIMEOUT_SEC`(900), `COMMAND_MAX_ATTEMPTS`(3), `COMMAND_RETRY_MIN_SEC`(20),
`DEFAULT_LAT`/`DEFAULT_LON`(37.5665/126.9780).

## 17. S-23 단말 운전 설정 (ADR-007, `UI_항목_명세.md` 8장)

### 17.1 항목 · 지문 · 표 CRC (`core/settings_rules.py`, 순수)
- 항목 정의 = `app/vendor/ui_items.json`(docs/spec/settings 사본, 고치지 않는다). 키·**순서**·범위·기본·배율.
- `sh` = 25개를 ui_items 순서대로 `<i` 로 이어 CRC-32, 대문자 8자리. 기본값 = `38AF0DBD`(단위 시험 고정).
- 표 CRC = `suntable.table_crc32(build_table(lat_e6, lon_e6, on, off))`. 서울 = `69C1DF86`.
- 검사: 25개 전부(없으면 INCOMPLETE, 모르는 키도 거부) → 범위(ui_items min/max) → 규칙
  `cut12<rtn12`, `cut24<rtn24`, **다단계 순서** — 1→4단계 시각(분)을 1단계부터 펼쳐 앞 단계보다 같거나 이르면 +1440,
  펼친 값이 엄격히 증가하고 1단계→4단계 간격 < 1440분. 일몰(점등) 시각은 보지 않는다(1단계가 저녁 첫 단계).
  단말도 `RULE` 로 거부하므로 서버 검사는 미리 막기다.
- `region`: UTF-8 47바이트 이하, 비어 있지 않음, `"` `\` 제어문자(C0·DEL·C1) 불가. 좌표 ±90/±180(→ `round(x*1e6)`), 보정 -180~180.

### 17.2 발행 (`SETTINGS_GET` / `SETTINGS_SET`, 개별 cmd topic, QoS1, retain 없음)
- `{"type":"SETTINGS_GET","seq"}`, `{"type":"SETTINGS_SET","seq","v":{25개 ui_items 순서},"tbl"?:{"region","lat_e6","lon_e6","on","off","crc"}}`.
- seq 는 `cmd_seq`(COMMAND 와 공유). 발송마다 `command` 행(type SETTINGS_GET|SET, target device, created_by). 요청 상태는
  `device_settings.pending_*`(단말당 하나). SET 이면 `sync=writing`. **커밋 뒤 발행**, 실패하면 pending 해제·원래 sync·command FAILED·503.
- 재발송(`mqtt/settings_sync.py`): 30초 무응답 → **새 seq**·새 command 행(옛 행 result `RESENT`), 최대 3회, 소진하면
  `last_result=TIMEOUT`·pending 해제·writing 이면 원래 sync. 단말이 REGISTER/TELEMETRY 를 보낸 직후(§1.1.10)는 마지막 발송에서
  5초 이상 지났으면 바로 재발송. 메모리 후보 집합(`SettingsSync._pending`)에 없는 uuid 는 쿼리 없음, 5초 타이머가 판정,
  1분마다 DB 로 재조정. 재발송은 응답 큐(§7 토큰 버킷)의 `SettingsJob`.
- device_event(SETTINGS_SENT) = `{"topic","payload","attempt","by"}`(by = 관리자 / timer / register / telemetry).

### 17.3 SETTINGS (읽기 응답)
1. device_event(SETTINGS) dedup. 2. 대기 중인 GET 과 맞는가(첫 seq ≤ seq ≤ pending_seq). 안 맞아도 새 보고로 받되(로그),
   SET 대기 중(writing)이면 `last_report` 에만 두고, 그 뒤 **다른** 요청이 이미 나갔으면 옛 보고로 보고 버린다.
3. `v` 25개가 정수가 아니면 반영 안 함. 4. DB 에 25개가 없으면 **첫 읽기** → 값·tbl 저장, 이력 `device_read`(키마다) → `synced`.
   있으면 `fingerprint(DB 값) == sh` → `synced`(tbl 갱신, 바뀌었으면 이력 `tbl`), 다르면 **`device_changed`**(DB 는 그대로).
5. 공통: `dip`/`bat`, `sh_device`, `ss_known = tbl.ss`, `read_at`, `last_report = 원본`. 맞는 요청이면 pending 해제·`last_result=OK`.

### 17.4 SETTINGS_ACK (쓰기 응답, 읽기의 STATE 거부 포함)
1. device_event(SETTINGS_ACK) dedup. seq 가 SETTINGS 명령이 아니거나 다른 단말이면 `settings_mismatch`. command 행 result = ACK result.
2. 다른 요청이 대기 중이면 반영 안 함. 끝난 요청의 늦은 응답은 그 뒤 다른 요청이 없을 때만 반영.
3. `OK` + `sh == fingerprint(보낸 v)` → DB ← 보낸 값(+tbl 이면 `tbl_src=2`), 이력 by = 요청한 관리자(없으면 `server_write`), `synced`.
   `OK` + sh 다름 → `device_changed`(last_report 비움 — 다시 읽기). 그 밖(RANGE/RULE/CRC/BAD/STATE/FLASH) → DB 그대로,
   writing 이면 보내기 전 sync 로. OK 면 `sh_device`·`ss_known = ack.ss`. `last_result(_at)`, pending 해제.

### 17.5 현장 저장 감지 (TM flush, §8)
flush 의 device upsert 뒤 한 문장: `device_settings ⋈ device` 에서 이 묶음 uuid 중 `sync='synced'`, `ss_known` 있음,
`device.ss_device <> ss_known`, **`device.last_telemetry_at > device_settings.updated_at`**(ACK·SETTINGS 가 기준을 바꾼 뒤 받은 TM 만)
→ `local_saved`. 자동으로 다시 읽지 않는다(표시만). 카운터 `settings_local_saved`.
