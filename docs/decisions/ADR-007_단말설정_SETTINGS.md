# ADR-007 단말 운전 설정(S-23) — 읽고 나서 쓴다, 지문 `sh`, 조건만 주고받는 스케줄

- 날짜: 2026-09-27
- 상태: 확정 (사양서 09-27 "다음 전달 범위" 6-2 S-23, `docs/spec/settings/UI_항목_명세.md` 8장, `ui_items.json`)

## 결정

### 항목 정의는 `ui_items.json` 하나
- `docs/spec/settings/ui_items.json` 을 백엔드에 그대로 복사(`backend/app/vendor/ui_items.json`)해 키·순서·범위·기본값·배율의
  **유일한 기준**으로 쓴다. 화면도 `GET /api/settings/schema` 로 같은 파일을 받는다(프론트에 중복 정의하지 않는다).
- 25개 값은 **단말 정수 그대로** 저장·전송(전압 x100). 화면값 = 값 / scale.

### 지문 `sh`, 표 CRC
- `sh` = 25개 값을 `ui_items.json` 순서대로 `<i`(32비트 LE) 로 이어 붙여 CRC-32, 대문자 8자리. 기본값 = `38AF0DBD`.
- 표 CRC = `suntable.table_crc32(suntable.build_table(lat_e6, lon_e6, on, off))`. 서울(37566500, 126978000, 0, 0) = `69C1DF86`.
  두 값을 단위 시험으로 고정한다.

### 흐름 — 서버는 처음에 모른다, 읽고 나서 쓴다
- 새 단말 `device_settings.sync = unknown`. **기본값을 가정해 쓰지 않는다**(현장 설정을 덮어쓴다).
- 관리자 "단말에서 읽기" → `SETTINGS_GET`(PENDING·ACTIVE 허용) → `SETTINGS` → 첫 읽기면 DB 에 저장(`synced`).
  이미 DB 값이 있는데 단말 `sh` 가 DB 로 계산한 `sh` 와 다르면 **덮어쓰지 않고** `device_changed` + 단말 값은 `last_report` 에 보관.
  관리자가 **받아들이기**(DB ← 단말 값) 또는 **되돌리기**(DB 값을 SETTINGS_SET) 를 고른다.
- 관리자 "단말에 쓰기" → 서버 검사(범위, `cut<rtn`, 다단계 순서·24시간) → `SETTINGS_SET`(**25개 전부** + 표를 바꿀 때만 `tbl`+`crc`)
  (ACTIVE 만) → `writing` → `SETTINGS_ACK OK` 이고 ACK `sh` == 보낸 값의 `sh` 이면 DB 반영·`synced`·`ss_known = ack.ss`.
  OK 인데 `sh` 가 다르면 `device_changed`(다시 읽기 권장). RANGE/RULE/CRC/BAD/STATE/FLASH 는 DB 를 바꾸지 않고 결과만 남긴다.
- **현장 저장 감지**: Telemetry `ss` ≠ `ss_known` (그리고 `sync` 가 `synced`) → `local_saved`. 자동으로 다시 읽지 않는다(표시만).
  서버 자신의 SET 도 `ss` 를 올리므로 ACK `ss`, SETTINGS `tbl.ss` 로 기준을 갱신한다.
- 응답 없음: 30초 뒤 **새 seq** 로 같은 요청 재발송(8.4), 최대 3회. 단말이 뭔가 보낸 직후라면(§1.1.10) 30초를 기다리지 않고 바로 재발송한다
  (5차 자동 재시도와 같은 원리).
- 요청은 `command` 테이블에 `type=SETTINGS_GET|SETTINGS_SET` 으로 남긴다(seq 는 `cmd_seq` 공유). 이력 = `device_settings_history`.

### 메시지 크기·형식 (서버 → 단말)
- **한 줄 JSON**, `separators=(",",":")`, `ensure_ascii=False`(UTF-8 그대로). 줄바꿈 금지.
- 상한 **900B**(단말 수신 줄 1,024B 한계, 실측 950B 정상). 기존 384B 제한은 단말 → 서버 AT 버퍼 이야기였으므로 발행 상한을 900 으로 올린다.
  SETTINGS_SET 최대 약 560B. `region` 은 UTF-8 47바이트까지, 따옴표·역슬래시·제어문자 금지(서버에서 거부).

### 5차 개정 반영(같은 전달분)
- `LOCAL` = 현장 조작 중이라 **버림**. 나중에 적용되지 않는다(종결).
- **Telemetry `md == 1`(현장) 이 오면 서버가 기록한 override 를 지운다**(단말이 원격을 전부 취소했다).
- 원격 OK·만료·현장 시작 뒤 2초에 오는 추가 Telemetry 는 정상(주기 이상으로 보지 않는다, `sq` 는 이어진다).
