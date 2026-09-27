# SolarLTE 단말 설정 — UI 항목 명세 (PC 설정 도구 · 서버 관제 공통)

기준 : 단말 F/W 1.4.0 (빌드 2026-09-27-7), PC 도구 1.3 (2026-09-27)

단말 설정 화면에 넣을 **항목, 범위, 변환, 검사 규칙, 동작 순서**를 모았다.
항목 정의는 같은 폴더의 **`ui_items.json`** 을 그대로 읽어 쓰면 된다(이 문서의 표와 같은 내용이며
selftest 가 펌웨어와 대조한다).

| 읽는 사람 | 볼 곳 |
|---|---|
| PC 설정 도구 (UART, 현장 설치자) | 1 ~ 7장 |
| **서버 관제 (LTE, 관리자)** | **1 ~ 2장(항목 정의 공통) + 8장**. 3 ~ 6장은 UART 방식이라 서버와 다르다 |

---

## 1. 원칙 세 가지

1. **범위는 단말이 알려 준다.** (PC 도구) 연결 후 `cfg get` 응답의 `min=` `max=` 를 입력 한계로
   쓴다. `ui_items.json` 의 min/max 는 연결 전 표시용이다. 펌웨어가 범위를 바꿔도
   UI 를 고치지 않아도 된다. 서버는 `ui_items.json` 범위를 쓰고, 단말이 범위 밖을 `RANGE` 로 거부한다(8장).
2. **값은 정수로 주고받는다.** 전압만 100배다(`2550` = 25.50V). 화면값 = 값 / `scale`.
   서버 DB 도 이 정수 그대로 둔다.
3. **쓰기와 저장은 다르다.** (PC 도구) `단말에 쓰기`는 RAM 에만 반영되어 **바로 운전에 적용**되고,
   `Flash 저장`을 눌러야 전원을 꺼도 남는다. 화면에 "저장 안 됨" 상태를 보여 준다.
   서버(LTE)는 이 구분이 없다. **보내면 적용 + 저장까지 한 번에** 한다(8장).

---

## 2. 설정 항목 (25개) — PC 도구·서버 공통

| 그룹 | key | 표시 이름 | 단위 | 배율 | 범위 | 기본값 |
|---|---|---|---|---|---|---|
| 스케줄 | `start_ofst` | 시작 Offset | 분 | 1 | -60 ~ 60 | 0 |
| | `stop_ofst` | 종료 Offset | 분 | 1 | -60 ~ 60 | 0 |
| | `start_pwm` | 시작 밝기 | % | 1 | 0 ~ 100 | 100 |
| 밝기 | `manual_40w` | PWM1 40W | % | 1 | 10 ~ 99 | 90 |
| | `manual_5w1` | PWM2 5W-1 | % | 1 | 10 ~ 99 | 90 |
| | `manual_5w2` | PWM3 5W-2 | % | 1 | 10 ~ 99 | 90 |
| | `fade` | Fade 시간 | x100ms | 1 | 1 ~ 20 | 10 (1.0초) |
| 다단계 | `stage1_h` `stage1_m` `stage1_pwm` | 1단계 시각·밝기 | 시/분/% | 1 | 0~23 / 0~59 / 0~100 | 20:00, 70% |
| | `stage2_*` | 2단계 | | | | 00:00, 40% |
| | `stage3_*` | 3단계 | | | | 02:00, 30% |
| | `stage4_*` | 4단계 | | | | 04:00, 60% |
| 배터리 | `cut12` | 12V 차단 전압 | V | **100** | 11.00 ~ 15.00 | 12.75 |
| | `rtn12` | 12V 복귀 전압 | V | **100** | 11.00 ~ 15.00 | 13.15 |
| | `cut24` | 24V 차단 전압 | V | **100** | 22.00 ~ 30.00 | 25.50 |
| | `rtn24` | 24V 복귀 전압 | V | **100** | 22.00 ~ 30.00 | 26.30 |
| | `cut_time` | 차단 유지 시간 | 분 | 1 | 1 ~ 60 | 10 |
| | `rtn_time` | 복귀 유지 시간 | 분 | 1 | 1 ~ 60 | 30 |

항목별 설명 문구(도움말)는 `ui_items.json` 의 `help` 에 있다. **항목 순서는 `ui_items.json` 의 순서가
기준이다**(8.6 설정 지문 계산에 쓴다).

### 2.1 화면에서 미리 막아야 하는 규칙

| 규칙 | 어기면 단말은 |
|---|---|
| `cut12 < rtn12` | (PC 도구) Flash 저장 때 두 값을 **기본값으로 조용히 바꾼다** (오류 없음) |
| `cut24 < rtn24` | 같음 |
| 1→2→3→4단계 시각이 저녁부터 밤 순서(자정 넘김 허용), 점등부터 4단계까지 24시간 미만 | Flash 저장이 `CFG ERR schedule flash write fail` 로 거부된다 |

첫 두 규칙은 단말이 오류를 내지 않으므로 **UI 가 막지 않으면 사용자가 모른다.**
서버도 같은 규칙으로 막는다(서버 경로에서는 단말이 `RULE` 로 거부한다, 8.4).

### 2.2 계산해서 보여 줄 값

실제 출력 밝기 = `round(기준 밝기 x 시작 밝기 / 100)`, 99 초과면 99, 0 초과 10 미만이면 10.
`manual_40w` `manual_5w1` `manual_5w2` 각각에 대해 표시한다(예 `x 100% -> PWM1 90 PWM2 90 PWM3 90`).
펌웨어 `schedule_pwm_calculate()` 와 같은 식이다. 원격 명령 `pwm`(서버 사양서 §3.10.7)도 같은 식이다.

---

## 3. 1년 스케줄 (372일 표) — PC 도구

> **서버는 이 장의 표 읽기·쓰기를 하지 않는다.** 표는 조건(지역·좌표·보정)만 있으면 단말이 직접
> 계산한다. 서버는 조건만 다루고, 필요하면 같은 계산식으로 화면 미리보기만 만든다(8.3).

### 3.1 입력

| 항목 | 형식 | 범위 | 비고 |
|---|---|---|---|
| 지역 | 글자 | UTF-8 **47바이트**(한글 15자) | 목록 36곳에서 고르거나 직접 입력. 고르면 위경도가 채워진다. 단말에 저장된다 |
| 위도 / 경도 | 소수 | ±90 / ±180 | 좌표 두 개나 지도 주소를 위도 칸에 붙여도 받는다. 단말에는 도 x 1,000,000 정수 |
| 점등 보정 | 정수 분 | -180 ~ 180 | 일몰 기준, 표에 들어간다(운전 중 Offset 과 별개) |
| 소등 보정 | 정수 분 | -180 ~ 180 | 일출 기준 |

**연도 입력은 없다.** 표는 해마다 같이 쓴다.

### 3.2 표시

- 미리보기 : 월별 1일·15일 24행 (날짜, 점등, 소등, 점등 시간)
- 표 근거 한 줄 : `군포 · 위도 37.3617 경도 126.9352 · 보정 +0 / +0분 · PC 도구`
  (만든 쪽 = 펌웨어 기본 표 / PC 도구 / 서버)
- 단말에서 읽은 표면 근거가 입력 칸에 되살아난다. 1.4.0 전 펌웨어는 "근거 정보 없음"

### 3.3 표 계산

`suntable.py` 의 `build_table(lat_e6, lon_e6, on_corr, off_corr)` 를 쓴다. **다른 계산식을
쓰면 안 된다.** 단말·서버와 비트 단위로 같은 정수 계산이며, 단말이 같은 표를 직접
계산해 CRC 로 확인한다. 서버용 사본은 `spec/server/ref/`.

---

## 4. 버튼과 동작 순서 — PC 도구 (UART)

| 버튼 | 순서 |
|---|---|
| 포트 검색 / 연결 | 115200 bps. 연결 직후 `lte trace off` 를 보내 모뎀 로그를 줄인다 |
| **단말에서 읽기** | 설정값 : `cfg get` → 25개 값과 범위 채움. 1년 스케줄(선택) : `sch get` → `sch info` |
| **단말에 쓰기** | 설정값 : 항목마다 `cfg set <key> <값>`. 1년 스케줄(선택) : 아래 4.1 |
| **Flash 저장** | `cfg save` (설정값과 스케줄을 한 번에 쓴다). 확인 창을 띄운다 |
| 되돌리기 (선택) | `cfg reload` : Flash 값을 다시 읽어 편집분 버림 |
| 파일로 저장 / 열기 | `.json` (6장) |
| 표 만들기 / CSV 저장 | PC 안에서만 동작 |

읽기·쓰기 대상은 **설정값 / 1년 스케줄**을 따로 고르게 한다. 스케줄은 수 초 걸린다.
연결 직후 자동 읽기는 설정값만 한다.

### 4.1 스케줄 쓰기 (자동 선택)

1. 계산으로 만든 표이고 손대지 않았으면 : `sch gen <lat_e6>,<lon_e6>,<on>,<off>,1,<지역 hex>` 한 줄
   → 응답 `SCH OK gen crc=XXXXXXXX` 의 CRC 를 도구 계산(`suntable.table_crc32`)과 대조
2. 단말이 모르거나(1.4.0 전) CRC 가 다르거나 손댄 표면 : `sch set <index> <hex>` 47줄(8일씩)
   → `sch info <lat_e6>,<lon_e6>,<on>,<off>,1,<지역 hex>`

지역 hex = 지역 이름 UTF-8 바이트를 대문자 hex 로(`군포` → `EAB5B0ED8FAC`). CLI 는 ASCII 만 받는다.

---

## 5. 단말 명령과 응답 — PC 도구 (UART CLI)

모든 설정 응답은 접두어로 시작한다. 같은 포트로 LTE/GPS 로그가 섞여 나오므로 **접두어로
골라낸다.** 한 줄 최대 191자.

| 명령 | 응답 | 기다리는 시간 |
|---|---|---|
| `cfg get` | `CFG BEGIN count=25` … `CFG <key>=<값> min=<a> max=<b>` … `CFG END` | 3초 |
| `cfg set <key> <값>` | `CFG OK <key>=<값>` / `CFG ERR <이유>` | 3초 |
| `cfg save` | `CFG SAVED seq=<n> crc=<hex>` / `CFG ERR ...` | 6초 |
| `cfg reload` | `CFG RELOADED` | 3초 |
| `sch get` | `SCH BEGIN days=372 chunk=8` … `SCH <index> <hex 64자>` … `SCH END` | 20초 |
| `sch set <index> <hex>` | `SCH OK <index> n=<일수>` / `SCH ERR ...` | 3초 |
| `sch info` | `SCH INFO src=<n> lat=<e6> lon=<e6> on=<n> off=<n> region=<hex>` (+ 사람용 한 줄) | 3초 |
| `sch info <...>` | `SCH OK info` / `SCH ERR ...` | 3초 |
| `sch gen <...>` | `SCH OK gen crc=<hex>` / `SCH ERR ...` (1.4.0 전 펌웨어 : `SCH ERR usage`) | 3초 (계산 약 0.6초) |

스케줄 한 칸 hex = `시 분 시 분` 4바이트(점등 시, 분, 소등 시, 분). 월마다 31칸, 372칸.

오류 줄(`CFG ERR`, `SCH ERR`)은 그대로 사용자에게 보여 준다. 응답이 없으면 연결 끊김으로 본다.

---

## 6. 설정 파일 (.json) — PC 도구

```json
{
  "tool": "SolarLTE 설정 도구", "version": "1.3",
  "values": {"start_ofst": 0, "cut24": 2550, "...": 0},
  "schedule": [[17, 24, 7, 47], "... 372칸"],
  "schedule_src": "군포 · 위도 ... · PC 도구",
  "schedule_info": {"region": "군포", "lat_e6": 37361700, "lon_e6": 126935200,
                    "on": 0, "off": 0, "src": 1}
}
```

`values` 는 단말 정수값(전압은 10mV 단위). `schedule` 과 `schedule_info` 는 표를 만든 경우에만.
서버가 이 파일을 가져오기(import)할 때는 `values` 와 `schedule_info` 만 쓰면 된다.

---

## 7. PC 도구 UI 에 넣지 않는 것

| 항목 | 이유 | 서버는 |
|---|---|---|
| Telemetry 주기 `ti`, keepalive `ka`, 설치 좌표 | 서버가 CONFIG 로 바꾼다(서버 사양서 §1.1.7) | **관리한다** (8.2) |
| MQTT 비밀번호 | UUID 로 계산한다. 넣을 것이 없다 | 저장하지 않는다(계산값) |
| 그룹(`grp`), 승인 상태, 시설명 | 서버가 접속마다 알려 준다 | **관리한다** (8.2) |
| RTC 시각 | GPS / LTE 망시각으로 자동 동기화 | 보여 주기만(Telemetry `ts`) |

읽기 전용으로 보여 주고 싶다면 CLI `status`(모델, F/W, UUID, 부팅 모드, RDP, 배터리, MPPT,
오류, LTE)와 `cfg nv`(저장 레코드 상태)를 쓸 수 있다. 형식은 사람용이라 바뀔 수 있다.

---

## 8. 서버 관제 화면 (LTE) — 서버 담당 참고

서버의 단말 설정 화면은 PC 도구와 **같은 항목(2장)** 을 같은 모양으로 보여 주면 된다.
다른 것은 통신 방식과, 서버가 단말 값을 **처음에는 모른다**는 점이다.

> **진행 상태 (2026-09-27)** : 단말 **구현됨** — F/W 빌드 2026-09-27-7 이상(`SETTINGS_ACK` 의 `ss` 는 -7 부터),
> `LTE_SETTINGS_INCLUDE`. 서버는 8.4 메시지대로 만들면 된다.
> 서버 없이 시험 : `python Tools\mqtt_test.py --device-uuid <UUID> --settings-get` (8.9).

### 8.1 PC 도구(UART)와 서버(LTE)의 차이

| | PC 도구 (UART) | 서버 (LTE, MQTT) |
|---|---|---|
| 연결 | 케이블, 115200 bps, 사람이 현장에서 | 상시 연결, 단말이 먼저 접속. 명령은 **단말이 보낸 직후** 잘 전달된다(서버 사양서 §1.1.10) |
| 명령 형식 | CLI 한 줄 (`cfg get`, `cfg set`) | JSON 메시지 (`cmd` topic → `result` topic) |
| 한 번에 | 항목 하나씩 | **전체 한 번에**(읽기 1건, 쓰기 1건). 가장 큰 응답 약 640B |
| 적용 | 쓰기(RAM) → 저장(Flash) 두 단계 | **보내면 적용 + Flash 저장 한 번에** |
| 범위 | 단말이 `cfg get` 으로 알려 줌 | `ui_items.json` 범위로 막고, 벗어나면 단말이 `RANGE` |
| 규칙 위반(2.1) | 배터리는 조용히 기본값으로 | 단말이 **`RULE` 로 거부**, 이전 값 유지 |
| 1년 스케줄 표 | 읽기(`sch get`) / 쓰기(`sch set` 47줄) | **표는 주고받지 않는다.** 조건만(8.3) |
| 승인 | 상관없음 | 읽기는 `PENDING` 부터, 쓰기는 `ACTIVE` 만 |

### 8.2 서버가 다루는 항목 전체

| 묶음 | 항목 | 서버 → 단말 | 단말 → 서버 | 비고 |
|---|---|---|---|---|
| 운전 설정 25개 (2장) | 스케줄·밝기·다단계·배터리 | `SETTINGS_SET` `v` | `SETTINGS` `v` | 서버가 처음엔 모른다 → **전체 읽기** |
| 스케줄 조건 | `region` `lat_e6` `lon_e6` `on` `off` | `SETTINGS_SET` `tbl` (선택) | `SETTINGS` `tbl` (+ `src` `ss` `crc`) | 372일 표 자체는 없음 |
| 현장 스위치 (읽기 전용) | DIP1~8, 배터리 계통 12/24V | — | `SETTINGS` `dev` | 어느 배터리 임계값 쌍이 쓰이는지, 다단계(DIP4) 사용 여부 |
| 통신 설정 | `cv` `ti` `ka` `lat` `lon`(설치 좌표) | `CONFIG_SET` (§1.1.7) | REGISTER `cv` `ti` `ka` | 서버가 기준(S-13) |
| 관리 정보 | `state`(승인), `site`(시설명), `grp` | retain `REGISTER_ACK` (§3.3) | — | 단말은 저장하지 않음 |
| 단말 정보 (읽기 전용) | `fw` `device_model` `imei` `iccid` `msisdn` | — | REGISTER | |
| 운전 상태 (읽기 전용) | `on` `md` `pw` `bv` `er` ... | — | Telemetry | |

`lat`/`lon`(CONFIG, 설치 위치)과 `lat_e6`/`lon_e6`(스케줄 표 계산 좌표)는 **다른 값**이다. 보통 같지만
표를 이웃 도시 좌표로 만들었을 수 있다. DB 에 따로 둔다.

### 8.3 1년 스케줄은 조건만 다룬다

- 서버 화면 입력 : 지역(한글 15자), 계산 좌표, 점등/소등 보정 — 3.1 과 같다. **연도 없음.**
- 미리보기가 필요하면 서버가 `spec/server/ref/suntable.py` 로 표를 계산해 **화면에만** 보여 준다.
- 단말에 보낼 때 : `SETTINGS_SET` 의 `tbl`(조건 + 서버가 계산한 `crc`). 단말이 같은 식으로 372일을 계산하고
  CRC 가 같을 때만 저장한다(약 0.6초). 다르면 `CRC` 로 거부하고 아무것도 바꾸지 않는다.
- 단말 표가 조건대로인지 : `SETTINGS` `tbl.crc` 와 서버가 같은 조건으로 계산한 CRC 를 비교.
  다르면 "현장에서 손댄 표"로 표시한다(표를 읽어 오지는 않는다).
- `tbl.src` : 0 펌웨어 기본 표(부산) / 1 PC 도구 / 2 서버. `tbl.ss` 는 스케줄 저장 번호(Telemetry `ss` 와 같다).

### 8.4 메시지 (구현됨)

topic 은 기존과 같다. 서버 → `iotlight/device/{uuid}/cmd`, 단말 → `iotlight/device/{uuid}/result` (QoS 1).
`seq` 는 COMMAND 와 같은 서버 전역 번호(§1.1.5, 32비트 부호 없음)다. 그룹·전체 topic 으로 보내면 무시한다.

**읽기**

```json
서버 → {"type":"SETTINGS_GET","seq":501}
단말 → {"type":"SETTINGS","uuid":"20363930594D50170004003A","seq":501,"sh":"38AF0DBD",
        "v":{"start_ofst":0,"stop_ofst":0,"start_pwm":100,"manual_40w":90,"manual_5w1":90,"manual_5w2":90,
             "fade":10,"stage1_h":20,"stage1_m":0,"stage1_pwm":70,"stage2_h":0,"stage2_m":0,"stage2_pwm":40,
             "stage3_h":2,"stage3_m":0,"stage3_pwm":30,"stage4_h":4,"stage4_m":0,"stage4_pwm":60,
             "cut12":1275,"rtn12":1315,"cut24":2550,"rtn24":2630,"cut_time":10,"rtn_time":30},
        "tbl":{"region":"서울","lat_e6":37566500,"lon_e6":126978000,"on":0,"off":0,"src":1,"ss":31,"crc":"69C1DF86"},
        "dev":{"dip":8,"bat":24}}
```

| key | 내용 |
|---|---|
| `v` | 25개. 이름·순서·단위는 2장 / `ui_items.json`. **정수 그대로**(전압 x100) |
| `sh` | 설정 지문(8.6) |
| `tbl` | 표 조건과 지금 표의 CRC. `region` 은 UTF-8 그대로(따옴표·역슬래시·제어문자는 `?` 로 바꿔 보낸다) |
| `dev.dip` | DIP1 = bit0 … DIP8 = bit7, **ON = 1** (예 8 = DIP4 만 ON) |
| `dev.bat` | 배터리 계통 12 / 24. MPPT 에서 아직 못 받았으면 0 |

승인 상태가 `PENDING`·`ACTIVE` 가 아니면 `SETTINGS` 대신 `SETTINGS_ACK` `result:"STATE"` 로 답한다.

**쓰기**

```json
서버 → {"type":"SETTINGS_SET","seq":502,
        "v":{ 25개 전부 },
        "tbl":{"region":"서울","lat_e6":37566500,"lon_e6":126978000,"on":0,"off":0,"crc":"69C1DF86"}}
단말 → {"type":"SETTINGS_ACK","uuid":"...","seq":502,"result":"OK","sh":"38AF0DBD","ss":32}
```

- **`v` 25개를 모두** 싣는다(CONFIG_SET 과 같은 이유 — 일부만 보내면 어긋남을 알 수 없다). 하나라도 없으면 `BAD`.
- `tbl` 은 표를 바꿀 때만 싣는다. 실으면 `crc` 가 있어야 한다(`suntable.table_crc32(build_table(...))`).
- 단말은 **전부 검사한 뒤 한꺼번에** 적용하고 Flash 에 저장한다. 하나라도 틀리면 아무것도 바꾸지 않는다.
- 지금 값과 같으면 Flash 를 쓰지 않고 `OK` 로 답한다.
- `OK` 면 2초 뒤 Telemetry 가 한 건 더 온다(밝기가 바뀌었을 수 있어서, 서버 사양서 §3.10.8 과 같은 방식).
- 서버는 ACK 의 `sh` 를 자기가 보낸 값으로 계산한 지문과 비교해 적용을 확인한다.
- ACK 의 `ss` 는 쓰기 뒤 스케줄 저장 번호다. 서버가 기록해 둔다(8.5 의 현장 변경 판단 기준).

| `result` | 의미 |
|---|---|
| `OK` | 적용하고 Flash 에 저장함(같은 값이면 저장 생략). `sh` 는 적용 후 지문 |
| `RANGE` | 범위 밖 항목이 있음 (2장, 표 조건 좌표 ±90/±180·보정 ±180) |
| `RULE` | 2.1 규칙 위반 (`cut < rtn`, 다단계 순서·24시간) |
| `CRC` | 표 조건으로 단말이 계산한 표의 CRC 가 `tbl.crc` 와 다름 |
| `BAD` | 형식 오류 (25개 중 누락, `tbl` 항목 누락, `crc` 없음) |
| `STATE` | 쓰기는 `ACTIVE` 만, 읽기는 `PENDING`·`ACTIVE` 만 |
| `FLASH` | Flash 기록 실패, 이전 값 유지 |

응답이 없으면(단말이 발행에 실패하면 다시 보내지 않는다) 서버가 30초 뒤 같은 요청을 **새 `seq`** 로 다시 보낸다.

**크기와 모뎀 한도**

| | 최대 크기 | 한도 | 근거 |
|---|---|---|---|
| 단말 → 서버 `SETTINGS` | payload 약 640B (AT 명령 약 700B) | 모뎀 발행 **1,500B** | AT Commands Guide v2.2 §7.1.5 `*WMQTPUB` msg 최대 1500 |
| 서버 → 단말 `SETTINGS_SET` | payload 약 560B (수신 줄 약 610B) | 모뎀 수신 문서에 없음 → **실측 950B 까지 정상**(2026-09-27, 단말 수신 줄 1,024B 가 한계) | `mqtt_test.py --rx-probe 300,600,800,950` 모두 PONG |

- **반드시 한 줄 JSON 으로 보낸다**(줄바꿈·들여쓰기 없이, 예 `json.dumps(obj, separators=(",", ":"), ensure_ascii=False)`).
  모뎀은 payload 안의 줄바꿈을 그대로 여러 줄로 넘겨서(AT 문서 예시) 단말이 한 메시지로 읽지 못한다.
- 한글(`region`)은 UTF-8 그대로 보낸다. 서버 → 단말 한글은 시설명(`site`)으로 실기 확인했다.

### 8.5 첫 연결과 동기화 흐름

**서버는 새 단말의 운전 설정을 모른다.** 단말은 출하 기본값(2장) 또는 현장 PC 도구로 넣은 값으로 이미
운전 중이다. 서버가 기본값을 가정해서 쓰면 현장 설정을 덮어쓴다. 그래서 **읽고 나서 쓴다.**

```text
1. 새 단말 접속 → REGISTER             서버 : device 행 생성(PENDING), 운전 설정 = "읽지 않음"
2. (선택) 승인 화면에서 "단말에서 읽기"   서버 : SETTINGS_GET → SETTINGS → 현장 설정을 보고 승인 판단
3. 관리자 승인(ACTIVE)                   서버 : REGISTER_ACK(state·site·grp), CONFIG_SET(cv·ti·ka·좌표)
4. 관리자 "단말에서 전체 읽기" (수동)      서버 : SETTINGS_GET → SETTINGS 한 건 → DB 에 '단말 보고값'으로 저장
5. 관리자가 화면에서 고침 → "단말에 쓰기"   서버 : SETTINGS_SET(25개 전부 + 필요하면 tbl) → SETTINGS_ACK OK 면 DB 반영
6. 운영 중                               Telemetry `ss` 가 서버가 기록한 ss 와 다르면 "현장에서 저장함" 표시
   → 관리자 "다시 읽기" → SETTINGS 의 sh 를 DB 값으로 계산한 지문과 비교 (8.6)
   - 같음 → 동기 (저장만 했고 값은 그대로)
   - 다름 → "단말 값이 바뀜" 표시. 서버가 자동으로 덮어쓰지 않는다.
            관리자가 받아들이기(DB 를 단말 값으로) 또는 되돌리기(서버 값 쓰기)를 고른다
```

- 4번 전에는 화면에 기본값을 채우지 말고 "읽지 않음"으로 둔다. 필요하면 옆에 기본값을 흐리게 참고로만.
- **`ss` 가 현장 저장 신호다.** 현장 PC 도구 `cfg save`, OLED 메뉴 저장, 엔코더 SAVE 는 모두 스케줄을 다시 저장해
  `ss` 가 1 오른다(값이 같아도). Telemetry 에 이미 있으므로 따로 묻지 않아도 다음 Telemetry(기본 10분)에 안다.
  서버 자신의 `SETTINGS_SET` 도 `ss` 를 올리므로 ACK 의 `ss` 로 기준을 갱신한다. `SETTINGS` 의 `tbl.ss` 로도 갱신.
  (예외 : 개발용 CLI `pwm smooth` 는 fade 만 저장하고 `ss` 를 올리지 않는다)
- CONFIG(`ti` `ka` 좌표)는 지금처럼 **서버가 기준**이다(S-13). 운전 설정 25개는 현장 PC 도구로도 바꾸므로
  **읽고 확인 후 쓰기**로 다르게 다룬다.

### 8.6 설정 지문 `sh` — 무엇이고 왜 있나

**25개 설정값을 8글자로 줄인 요약값**이다. 값이 하나라도 바뀌면 `sh` 가 완전히 달라진다.
서버가 25개를 하나씩 비교하지 않고 **8글자 하나만 비교해** "단말 설정이 DB 와 같은가"를 안다.

- 계산 : 25개 값을 **`ui_items.json` 순서대로** 각각 32비트 정수(little-endian)로 이어 붙여 CRC-32(zlib/IEEE),
  8자리 대문자 hex. 서버는 DB 값으로 같은 계산을 한다.

  ```python
  import json, struct, zlib
  items = [i for g in json.load(open("ui_items.json", encoding="utf-8"))["groups"] for i in g["items"]]
  sh = "%08X" % zlib.crc32(b"".join(struct.pack("<i", values[i["key"]]) for i in items))
  ```

- 기본값 25개의 `sh` = **`38AF0DBD`** (서버 구현 확인용)
- 싣는 곳 : `SETTINGS`, `SETTINGS_ACK`. **Telemetry 에는 싣지 않는다**(2026-09-27 결정 — 항목이 많지 않아
  읽어서 비교하면 된다. 현장 저장 여부는 Telemetry `ss` 로 안다, 8.5).
- 스케줄 표는 `sh` 에 넣지 않는다. 표는 `tbl.crc` 로 본다.

### 8.7 DB 제안

서버 사양서 §4.2 `device` 에 더해 단말당 한 행.

`device_settings`

| 컬럼 | 비고 |
|---|---|
| `uuid` PK | |
| 25개 항목 (`start_ofst` ~ `rtn_time`) | **단말 정수값 그대로**(전압 x100). 컬럼 이름 = key |
| `tbl_region`, `tbl_lat_e6`, `tbl_lon_e6`, `tbl_on`, `tbl_off`, `tbl_src`, `tbl_crc`, `ss` | 스케줄 조건(`tbl`) |
| `dip`, `bat` | 현장 스위치(`dev`), 읽기 전용 |
| `sh_device` | 마지막 SETTINGS / ACK 의 `sh` |
| `ss_known` | 서버가 마지막으로 확인한 스케줄 저장 번호(ACK `ss`, SETTINGS `tbl.ss`). Telemetry `ss` 가 다르면 현장 저장 |
| `read_at` | 마지막 전체 읽기 시각. 비어 있으면 "읽지 않음" |
| `sync` | `unknown`(읽지 않음) / `synced` / `writing`(SET 보냄, ACK 대기) / `local_saved`(Telemetry `ss` 바뀜, 다시 읽기 필요) / `device_changed`(읽어 보니 `sh` 다름) |

`device_settings_history` — `uuid`, `changed_at`, `by`(`device_read` / `server_write` / 관리자 id), 바뀐 key·이전값·새값.
현장 PC 도구로 바뀐 것도 다시 읽을 때 이력으로 남는다.

화면 표시값 = 정수 / `scale` (`ui_items.json`). 범위·도움말·기본값도 `ui_items.json` 을 그대로 읽는다.

### 8.8 서버 화면에 넣지 않는 것

| 항목 | 이유 |
|---|---|
| 372일 표 읽기·쓰기, 표 CSV | 조건만 다룬다(8.3). 미리보기는 서버 계산 |
| 포트·연결 버튼, "Flash 저장" 버튼 | LTE 는 상시 연결, 쓰기가 곧 저장 |
| `cfg reload`(되돌리기) | 서버 DB 가 되돌릴 값을 갖고 있다. 다시 쓰면 된다 |
| MQTT 비밀번호 | UUID 로 계산, 저장하지 않는다 |

### 8.9 서버 없이 시험 (PC)

```powershell
cd Tools
python mqtt_test.py --device-uuid <UUID> --settings-get
python mqtt_test.py --device-uuid <UUID> --settings-set "{\"fade\":5}"
python mqtt_test.py --device-uuid <UUID> --settings-set "{}" --tbl 서울,37.5665,126.978,0,0
```

`--settings-set` 은 먼저 읽어서 25개를 채우고 바꿀 것만 덮어 한 번에 보낸다. 서버가 할 일과 같은 순서다.
단말 로그 : `[LTE] <- SETTINGS_GET seq=..` → `[LTE] -> SETTINGS seq=.. sh=..`,
`[LTE] <- SETTINGS_SET seq=.. +tbl` → `[LTE] SETTINGS apply OK sh=..` → `[LTE] -> SETTINGS_ACK seq=.. OK`.

---

관련 : MCU 사양서 §4.4 ~ §4.6(스케줄), §8.1(설정 저장), §24(PC 도구 연동).
서버 사양서 §1.1.7(CONFIG), §3.3(REGISTER_ACK), §4.2(DB), §9.1(스케줄 조건), §16.4(STATUS_GET)
