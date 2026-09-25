# 02. MQTT 서버 구현 (2차)

- 작성일: 2026-09-25
- 코드: `backend/app/mqtt/`, `backend/app/core/`
- 기준: 사양서 §1.1.3~§1.1.7, §4, §16.1, ADR-001, ADR-002. 프로토콜은 단말측 사양서가 정본이고
  여기서는 **서버가 그것을 어떻게 처리하는가**만 적는다.

## 1. 연결

- aiomqtt 클라이언트 **1개**, Client ID `iotlight-backend`(`MQTT_CLIENT_ID`), 계정 `server`.
- 끊기면 1초부터 2배씩 늘려 최대 30초 간격으로 재접속. 재접속마다 아래 구독을 다시 건다
  (Clean Session 이라 브로커가 기억하지 않는다).
- 끊긴 동안의 발행은 `MqttUnavailable` 예외다. 조용히 삼키지 않는다 — REST 는 503 을 받고,
  CONFIG 큐는 로그를 남기고 그 건을 버린다(다음 Telemetry 가 다시 잡는다).
- 4차 TLS 전환은 `MQTT_TLS=true`, `MQTT_PORT=8883` 뿐이다. `connection.py` 밖은 손대지 않는다.

## 2. topic 표

### 구독 (단말 → 서버)

| topic | QoS(구독) | payload.type | 처리 |
|---|---|---|---|
| `iotlight/device/+/register` | 1 | `REGISTER` | device upsert(보강) + device_event + cv 비교 |
| `iotlight/device/+/status` | 1 | `TM` (`t` 도 허용) | TelemetryBuffer |
| `iotlight/device/+/result` | 1 | `PONG` `CONFIG_ACK` `CMD_ACK`(5차) | device_event + command_ack |
| `iotlight/device/+/event` | 1 | `LWT` `EV` | offline 처리 / device_event |

단말은 status 를 QoS0 으로 보낸다. 구독 QoS 는 상한일 뿐이라 1 로 걸어도 QoS0 메시지는
QoS0 으로 온다. 네 패턴을 하나(`iotlight/device/+/#`)로 합치지 않는 이유는 cmd/config 를
서버 자신이 되받지 않기 위해서다.

### 발행 (서버 → 단말) — `publisher.py` 단일 창구

| topic | payload.type | QoS | retain | 단계 |
|---|---|---|---|---|
| `iotlight/device/<uuid>/config` | `CONFIG_SET` | 1 | **0** | 2차 |
| `iotlight/device/<uuid>/config` | `REGISTER_ACK` | 1 | **1** (유일) | 3차 (메서드만 있음) |
| `iotlight/device/<uuid>/cmd` | `PING` | 1 | 0 | 2차 (REST 로만) |
| `iotlight/device/<uuid>/cmd` | `CMD` `SCH` `OTA` | 1 | 0 | 5~7차 |
| `iotlight/group/<grp>/cmd` · `iotlight/all/cmd` | `CMD` | 1 | 0 | 5차 |

발행 규칙(publisher `_send`):
- compact JSON(구분자 뒤 공백 없음, `ensure_ascii=False`).
- **384B 초과면 발행하지 않고 `PayloadTooLarge`**(AT 버퍼, 사양서 §1.1.6). 300B 초과는 경고.
- `cmd` 는 절대 retain 하지 않는다. 재접속 단말에 옛 명령이 되살아난다(5차 소등이면 사고).
- CONFIG_SET 은 `{"type":"CONFIG_SET","cv":N,"ti":N,"lat":..,"lon":..}`. lat/lon 이 NULL 이면
  키를 뺀다(둘 중 하나만 있어도 뺀다).

## 3. 수신 공통 검증 (`handlers.Dispatcher`)

순서대로. 걸리면 카운터를 올리고 **무시**한다(예외 없음, 수신 루프가 죽지 않는다).

1. topic 정규식 `^iotlight/device/([0-9A-F]{24})/(register|status|result|event)$`
   불일치 → `malformed_topic`. 소문자 uuid 도 위반이다(DB CHECK·ACL `%u` 전제).
2. JSON 이 아니거나 객체가 아님 → `malformed_payload`.
3. `"t"` 만 있으면 `"type"` 으로 복사(1차 펌웨어). uuid 당 1회 로그, `legacy_t_devices` 갱신.
   2차 펌웨어 확정 후 `normalize_type()` 을 지운다.
4. payload 에 `uuid` 가 있고 topic uuid 와 다르면 → `uuid_mismatch` (사양서 §1.1.4).
5. kind/type 조합이 표에 없으면 → `unknown_type`.

## 4. 메시지별 처리

### REGISTER
1. `device` upsert(topic uuid 기준). 있는 키만 갱신: `fw device_model modem_model imei iccid
   msisdn`, `cv→cv_device ss→ss_device ti→ti_device`. `last_register_at`, `last_seen_at`
   (GREATEST). 처음 보는 uuid 는 `state=ACTIVE` 로 생긴다(2차, 사양서 §2).
2. `device_event(kind=REGISTER)`. dedup 없음 — 재연결마다 같은 내용으로 오는 것이 정상.
3. `cv` 가 실려 왔고 `cv != cv_server` → CONFIG 큐(§6)에 `reason=register` 로 넣는다.
   `cv` 가 없으면(1차 펌웨어) 아무것도 보내지 않는다 — 다음 TM 의 cv 로 판정한다.
4. PING 은 보내지 않는다. 1차 시험 도구의 몫이고, 서버에서는 `POST /api/devices/{uuid}/ping`.
5. **3차 변경점**: 여기서 REGISTER_ACK(state) 를 **항상**(retain) 보내고, ACTIVE 일 때만 3번.

### TM (status)
DB 를 만지지 않는다. `TelemetryBuffer.offer()` 로 끝. §5 참고.

### result
공통: `last_seen_at` GREATEST 갱신(미등록 uuid 면 행 생성). 이력 1행 = 트랜잭션 1개.

| type | device_event | dedup_key | 그 외 |
|---|---|---|---|
| `PONG` | `PONG` | `uuid:PONG:<seq>:sha1` | `command` 조회 → target 이 이 uuid 인지 확인(§1.1.5) → `command_ack` 1행(PK 충돌 무시) → `acked_count+1`, 다 모이면 `finished_at`, `result=OK`. seq 를 모르거나 단말이 다르면 `pong_mismatch` |
| `CONFIG_ACK` | `CONFIG_ACK` | `uuid:CONFIG_ACK:<cv>:sha1` | `OK` 이고 `cv == cv_server` 이면 `cv_device=cv, ti_device=ti_server` (ti 는 TM 에 안 실려 여기서만 알 수 있다. 옛 ack 로 덮지 않도록 cv 일치 조건). **동기화 완료 판정은 여전히 다음 TM 의 cv echo** 다. `RANGE` → 경고 + `config_ack_range` (서버가 범위 밖 값을 보낸 것이므로 PATCH 검증 버그) |
| `CMD_ACK` | `CMD_ACK` | `uuid:CMD_ACK:<seq>:sha1` | PONG 과 같은 command 집계 (5차) |

dedup_key 에 payload 해시가 들어가는 이유: 같은 seq 의 두 번째 결과(내용이 다름)는 남기고,
QoS1 재전송(바이트 동일)만 걸러진다. `ON CONFLICT DO NOTHING`.

### event
| type | 처리 |
|---|---|
| `LWT` | `online=false, offline_at=now` → `device_event(LWT)`. **`last_seen_at` 은 건드리지 않는다** — 브로커가 대신 보내는 사망 통지를 "방금 통신"으로 적으면 죽은 단말이 온라인이 된다. 버퍼에 남은 그 단말의 TM 은 **버리지 않는다**(이력이라 한 건도 아깝다). TM flush 는 `online` 을 건드리지 않고 presence 가 `offline_at > last_telemetry_at` 로 판정하므로 늦게 적재된 TM 이 단말을 되살리지 않는다(S2-13). |
| `EV` | `last_seen_at` 갱신 → `device_event(kind=ERR, payload={er,ep,bv,ts})`, dedup `uuid:EV:<ts>:sha1` |

## 5. TelemetryBuffer — 1초 배치

```
offer() ──▶ _rows[(uuid, payload, received_at)…]  (도착 순서, DB 접근 없음)
                    │  1초마다 / 20,000행 넘으면 즉시 / 종료 시
                    ▼
        _judge_batch()  프로세스 내 last_sq 캐시로 sq 판정, uuid 별 최신 1건 선별
                    ▼
        ┌── 트랜잭션 1개 ─────────────────────────────────────────────┐
        │ INSERT telemetry  (모든 행, 1,000행씩 multi-row, 충돌 무시)    │
        │ INSERT device … ON CONFLICT (uuid) DO UPDATE  (uuid 별 1행)   │
        │    last_telemetry, last_sq, cv_device, ss_device,             │
        │    fw = coalesce(new, old),                                   │
        │    last_telemetry_at / last_seen_at = GREATEST(old, new),     │
        │    lost_count += Δ, reboot_count += Δ                         │
        │    RETURNING uuid, cv_server, ti_server, lat, lon              │
        │ INSERT device_event (REBOOT / LOST)                            │
        └───────────────────────────────────────────────────────────────┘
                    ▼
        cv(payload) != cv_server 인 uuid → ConfigSyncQueue.offer(reason=telemetry)
```

- **GREATEST 인 이유**: flush 가 늦어진 사이 result/event 가 더 최근 `last_seen_at` 을 썼을
  수 있다. 시각이 뒤로 가면 온라인 판정이 흔들린다.
- **실패 정책**: 트랜잭션이 실패하면 그 묶음을 **버리고** `telemetry_dropped += 행 수`,
  `flush_failures += 1`. 되돌려 넣지 않는다 — DB 가 아픈 동안 대기열이 무한정 자라는 것이
  재기동보다 나쁘다(docs/00 §5). last_sq 캐시는 이미 올라가 있으므로 다음 묶음이 "유실"로
  보이지 않는다(의도). 버린 사실은 카운터가 남긴다.
- **처음 보는 uuid** 는 여기서 `device` 행이 생긴다(사양서 §4.1 "REGISTER 를 놓쳐도").
- 1,000행 단위인 이유: asyncpg 문장당 바인드 32,767개 한계, 컬럼 21개.
- 기동 시 `warm()` 으로 DB 의 `last_sq` 를 캐시에 적재한다. 안 하면 재기동 직후 첫 TM 이
  전부 "처음 보는 단말"이 되어 그 사이 유실·재부팅을 놓친다.

## 6. sq 판정 (`mqtt/sq.py`, 사양서 §1.1.6)

| 관측 | 판정 | 기록 |
|---|---|---|
| `sq == last + 1` | 정상 | — |
| `sq > last + 1` | 유실 `sq - last - 1` | `lost_count += min(n, 100000)`, event `LOST{sq,last_sq,lost,jump}` — `jump` 가 원값. 상한을 두는 이유: 10만 이상 점프는 유실이 아니라 카운터 이상이고, int4 `lost_count` 가 넘치면 배치 flush 전체가 실패한다 |
| `sq == last` | 중복 | 무시 |
| `sq < last` | 재부팅 | `reboot_count += 1`, event `REBOOT{sq,last_sq}` |
| `last > 2³²-1-1000` 이고 `sq < 1000` | uint32 wrap → 유실로 계산, 재부팅 아님 | 단말 확인 대기(docs/00 §7) |
| `last` 없음(첫 관측) | 아무것도 세지 않음 | — |

## 7. CONFIG_SET 재전송 규칙 (ADR-002, `mqtt/config_sync.py`)

트리거는 **타이머가 아니라 수신 시점**이다.

| 경로 | 조건 | 방식 |
|---|---|---|
| REGISTER | payload `cv` ≠ `cv_server` | 큐 |
| TM flush | payload `cv` ≠ `cv_server` | 큐 |
| `PATCH /api/devices/{uuid}/config` | ti/lat/lon 중 하나라도 변경 → `cv_server = (cv_server+1) % 65536` | **즉시 1회** 발행 + 쿨다운 기록 |

큐(`ConfigSyncQueue`):
- 토큰 버킷 `REGISTER_REPLY_RATE_PER_SEC`(기본 200/s). 브로커 재시작 뒤 1만 대 REGISTER
  폭주에서 응답 발행량을 누른다. 1만 대면 최대 50초에 걸쳐 나간다.
- 대기열 상한 `REGISTER_REPLY_QUEUE_MAX`(50,000). 넘으면 가장 오래된 것을 버리고
  `register_reply_dropped` 로 센다. 버려진 단말은 다음 TM 에서 다시 잡힌다.
- **같은 단말 60초 쿨다운**(`CONFIG_RESEND_COOLDOWN_SEC`). REGISTER 직후 첫 TM 이 아직 옛 cv
  를 싣고 오는 것이 정상 흐름이라(CONFIG_ACK 전에 TM 이 나갈 수 있다) 쿨다운이 없으면
  같은 CONFIG_SET 이 연달아 두 번 나간다. 이미 줄 서 있는 uuid 도 다시 넣지 않는다.
- 발행 성공 시 `device.config_sent_at` 갱신. 실패(브로커 끊김)는 로그 후 폐기.
- CONFIG_ACK `OK` 는 `ti_device`(와 cv_device)만 적는다. **동기화 완료 = 다음 TM 의 `cv == cv_server`**.
  `RANGE` 는 서버 버그다(PATCH 가 60~3600 을 검증한다).

## 8. 계정 내보내기 순서 (`core/mqtt_accounts.py`, 사양서 §1.1.2.2)

```
CSV(uuid,password) ──▶ PBKDF2 $7$101$ 해시 ──▶ device.mqtt_password_hash (평문 저장 안 함)
        │
        ▼  export_all()  (pg_advisory_xact_lock 으로 직렬화)
  passwd.generated  = server 계정 + [solarlte-test, MQTT_TEST_ACCOUNT_ENABLED 일 때만] + 단말 전부
  aclfile.generated = 사양서 §1.1.2.2 그대로 (user server readwrite iotlight/#, pattern %u …)
        │  같은 디렉터리 임시 파일 → os.replace (원자적)
        ▼
  mosquitto entrypoint 감시 루프(infra/)가 설치 + SIGHUP → aclfile.applied 에 md5 기록
        │
        ▼  wait_applied(passwd_md5, acl_md5, ACL_APPLY_TIMEOUT_SEC=5)
           — passwd.applied / aclfile.applied 둘 다 일치해야 "적용". ACL 이 안 바뀐 import 는
             aclfile.applied 가 즉시 일치하므로 passwd 보고를 안 보면 접속 거절 구간이 생긴다
  mqtt_account_export(id=1) 에 passwd_md5 / acl_md5 / acl_applied_md5 기록
        │
        ▼
  그 뒤에야 CONFIG/명령 발행 — mosquitto 는 구독은 받아 두고 메시지를 넘길 때 ACL 을 보므로
  권한 설치 전에 나간 메시지는 그 단말에 영영 안 간다.
```

- 내보내기 시점: **기동 시**, CSV import 후, 단말 삭제 후. 항상 통째로 재생성(부분 병합 없음 —
  파일과 DB 가 어긋난 채 굳는 것이 최악).
- 경로가 비어 있으면 no-op(개발 PC, anonymous 브로커). 응답의 `export_enabled=false`.
- import 직후에는 CONFIG 를 보내지 않는다 — 그 단말은 아직 접속 전이고 CONFIG_SET 은
  비retain 이라 받을 수 없다. 접속하면 REGISTER/TM 의 cv 불일치가 알아서 보낸다.

## 9. 온라인 판정 (`core/presence.py`)

```
online = device.online                                  (LWT/CONNECT, 3차)
      OR ( last_telemetry_at >= now - coalesce(ti_device, ti_server) × DEVICE_ONLINE_FACTOR
           AND (offline_at IS NULL OR offline_at < last_telemetry_at) )
```
파이썬 판과 SQL 판이 같은 파일에 있다. 목록 필터 `?online=`, 상세의 `is_online` 이 둘을 쓴다.
`ti_device` 우선인 이유: 서버가 300 으로 바꿨는데 단말이 아직 600 으로 보내면 600 기준이어야
오프라인 오판이 없다.

## 10. 카운터 (`GET /api/metrics`)

| 이름 | 뜻 |
|---|---|
| `received.{register,status,result,event}` | kind 별 수신 |
| `received_type.{REGISTER,TM,PONG,…}` | type 별 수신 |
| `malformed_topic` `malformed_payload` `uuid_mismatch` `unknown_type` | §3 검증 탈락 |
| `telemetry_flushed` `telemetry_dropped` `flush_failures` | 배치 적재 / 폐기 |
| `flush_ms_last` `flush_ms_max` | flush 소요 |
| `buffer_pending` | 대기 TM 행 수 |
| `register_queue` `register_reply_dropped` | CONFIG 큐 길이 / 상한 초과 폐기 |
| `config_set_sent` `config_ack_range` | CONFIG 발행 / 단말 거부 |
| `pong_mismatch` | seq 모름 또는 단말 불일치 응답 |
| `mqtt_connected` `mqtt_reconnects` `mqtt_publish_failures` | 연결 |
| `legacy_t_devices` | `t` 키로 보고 중인 단말 수 |
| `cmd_seq` `cmd_seq_alert` | 시퀀스 현재값 / uint32 99% 초과 경보 |

`GET /health` 는 `ok = mqtt_connected AND db_ok` 와 버퍼·큐 크기만 준다. DB 가 죽어도 HTTP 200
(healthcheck 로 컨테이너를 재시작하면 버퍼와 MQTT 세션까지 잃는다).

## 11. 주기 작업

| 작업 | 시각(KST) | 내용 |
|---|---|---|
| `tasks/partitions.run` | 기동 시 + 매일 01:00 | `telemetry_YYYYMM` 이번 달·다음 달 생성, 13개월 초과 DROP, `device_event` 365일 초과 DELETE |
| `tasks/daily_rollup.run` | 매일 00:30 (+`POST /api/admin/rollup`) | 전날(KST) `telemetry_daily` — SQL 윈도 함수 사다리꼴 적분, 표본 간격 2시간 초과 구간 제외 |

파티션 경계는 UTC 월이다(이름과 DDL 경계가 일치하도록). 일 집계 범위는 KST 날짜다.

## 12. 3차에서 바뀌는 것

| 항목 | 2차 | 3차 |
|---|---|---|
| 신규 단말 `state` | ACTIVE | PENDING (DB 기본값 변경) |
| REGISTER 응답 | cv 다를 때 CONFIG_SET 만 | REGISTER_ACK(retain) **항상** + ACTIVE 면 CONFIG_SET |
| 상태 변경 | — | `device_event(STATE_CHANGE)`, REGISTER_ACK 재발행, RETIRED 는 `clear_register_ack()` |
| 온라인 | ti×3 대체 규칙 | LWT 로 `online=false`, CONNECT 감지(브로커 `$SYS` 또는 REGISTER)로 `online=true`. P-2 결과가 미지원이면 2차 규칙 유지 |
| PENDING 단말의 TM | 받아서 저장 | 받으면 경고(사양서 §3.8 — 보내면 안 되는 상태) |
| REST | 무인증, 127.0.0.1 | `admin_user` 인증 |

`publisher.publish_register_ack()` / `clear_register_ack()` 는 2차에 이미 있다(미사용).
