# SolarLTE 서버 MQTT 안내 — 1·2·3차 합격, 다음 4·5차

> **2026-09-27 : 2·3차 실기 합격.** 다음은 S-5(인증 플러그인) + 5차(그룹·원격 제어) + 4차(TLS)를
> 한 번에 진행한다. 할 일과 순서는 사양서 첫머리 **"다음 전달 범위"** (S-5, S-15 ~ S-22).

> **2026-09-26 : 1차 합격. 단말 1.2.0 부터 2·3차를 모두 지원한다(현재 1.4.0).**
> 서버는 2차와 3차를 **한 번에 진행해도 된다.** 전체 현황은 사양서 §0.5.
>
> **주의:** 1.2.0 이상은 승인 게이트가 켜져 있어 `REGISTER_ACK state=ACTIVE` 전에는
> Telemetry 가 오지 않는다. 시험 단말은 한 줄로 승인해 둔다(retain, 한 번이면 됨).
>
> ```powershell
> python Tools\mqtt_test.py --device-uuid <UUID> --approve ACTIVE --site TEST
> ```

## 최근 변경 (2026-09-27, 단말 빌드 2026-09-27-5)

| 항목 | 내용 | 사양서 |
|---|---|---|
| **현장 우선 (개정)** | 현장 조작(DIP·엔코더·OLED 메뉴) 중 받은 원격 명령은 `LOCAL` 로 응답하고 **버린다**. 현장이 끝나도 적용하지 않는다. **현장 조작이 시작되면 살아 있던 원격도 모두 취소** → 끝나면 스케줄. 다시 걸려면 서버가 새 명령(새 `seq`) | §3.10.8, §3.10.11 |
| **원격 뒤 Telemetry** | 원격 `OK` 뒤(ACK 발행 후 **2초**), 원격 유지시간 끝, 현장 취소 때 단말이 Telemetry 를 **추가로** 보낸다. 점등 상태(`on` `md` `pw`)는 ACK 가 아니라 이것으로 갱신. 간격이 `ti` 보다 짧아도 정상 | §1.1.6, §3.10.8, S-19 |
| COMMAND `seq` | 0 ~ 4294967295 전 범위. 없거나 문자열·음수면 단말은 응답 없이 버림 | §3.10.7 |
| COMMAND 작성 | key 순서·공백 자유, 숫자는 따옴표 없이, `pwm`·`ch` 는 배열, 문자열에 `\"` 쓰지 않음 | §3.10.7 |
| 5차 확정 | 법정동 트리, `grp` = 법정동코드 + `00`, 카카오 `b_code`, COMMAND `ts`·`ch`·`pwm` 배열 | §3.9.3, §3.10.4 ~ §3.10.7 |
| **단말 설정 화면·DB (참고)** | PC 설정 도구와 같은 25개 항목을 서버 화면·DB 에. 첫 연결 뒤 **수동 전체 읽기** → 확인 후 쓰기, Telemetry `ss` 가 바뀌면 현장 저장 → 다시 읽어 지문 `sh` 비교(Telemetry 에 `sh` 없음), 372일 표는 조건만. 메시지(`SETTINGS_GET/SET`) **단말 구현됨**(2026-09-27-6), 한 번에 주고받음. 시험 `mqtt_test.py --settings-get` (S-23) | `Tools/config_tool/UI_항목_명세.md` 8장, `ui_items.json` |

### 이전 변경 (2026-09-26)

| 항목 | 내용 | 사양서 |
|---|---|---|
| 단말 계정 | password = `HMAC-SHA256(K1, UUID)` 계산값. 브로커 **인증 플러그인** 필요. 키는 별도 전달 | §1.1.2.2 |
| CONFIG 규칙 | 매번 전체 값, `cv` 1 부터(0 금지), `ka_device` 저장. CONFIG_ACK `FLASH` 추가 | §1.1.7 |
| 브로커 | `persistence true` (승인 retain 보존) | 아래 §4 |
| 전달 시점 | 단말 송신 직후에 보낸다(안전 규칙). 실측 580초 유휴까지 전달 | §1.1.10 |
| keepalive | **300초 확정**, 운영 최대 600초 | §1.1.10 |
| 그룹 (5차) | `grp` 는 retain REGISTER_ACK 로. 단말 저장 안 함 | §3.10.9 |
| 스케줄 (6차) | 표 근거(지역·좌표·보정) 저장, **조건 전송 1번**, 계산은 `ref/suntable.py` 그대로 | §9.1, `ref/README.md` |
| Telemetry | **`fw` 삭제(단말 1.4.0)**. 버전은 REGISTER `fw` 로. 서버는 있든 없든 읽게 | §1.1.6 |
| 접속 순서 | 구독 직후 retain REGISTER_ACK → 단말 REGISTER → **서버가 REGISTER_ACK 한 번 더**(S-7) | §3.3 |

## 서버 할 일 (2·3차)

| # | 할 일 | 사양서 |
|---|---|---|
| S-1 | 수집 서비스: `iotlight/device/+/register`, `status`, `result` 구독, **UUID 로 upsert**, Telemetry raw 저장 | §4.1, §4.2 |
| S-2 | Telemetry 파서: `"type":"TELEMETRY"` (1.0.0 은 `"t":"TM"`, `fw` 로 구분) | §1.1.6 |
| S-3 | `sq` 로 유실(건너뜀)·재부팅(감소) | §1.1.6 |
| S-4 | CONFIG_SET: `config` topic, **retain 0**. `CONFIG_ACK` 확인 | §1.1.7 |
| S-5 | 단말별 계정: username = UUID, password = **HMAC-SHA256(K1, UUID)** 계산값. **브로커 인증 플러그인** + ACL. 키는 별도 전달 | **§1.1.2.2** |
| S-6 | Online/Offline — **LWT 대체**: Mosquitto 로그의 접속/끊김으로 판정 | §16.1 |
| S-7 | REGISTER 마다 `REGISTER_ACK`: `config` topic, **retain 1**, `state` / `site` | §3.3 |
| S-8 | 상태 DB: 새 단말은 `PENDING` | §2, §3.4 |
| S-9 | **최소 관리자 화면**: 미승인 목록, 승인 버튼, 장소·주소·요금제 | **§3.9.2** |
| S-10 | 승인 후 초기 동기화: REGISTER `cv` 비교 → CONFIG_SET. ACTIVE 전 CONFIG 금지 | §3.9 |
| S-11 | 폐기 시 빈 payload retain 으로 승인 정보 삭제 | §3.3 |
| **S-12** | **서버 → 단말은 단말이 보낸 직후에 보낸다.** 첫 측정에서 2분 뒤 막힌 적이 있어 안전 규칙으로 둔다(이후 580초까지 정상) | **§1.1.10** |
| **S-13** | **CONFIG_SET 은 매번 전체 값**(`cv` `ti` `ka` + 좌표). `cv` 는 1 부터, 0 금지. 단말 `cv` 가 더 크면 그 +1. `ka_device` 저장 | **§1.1.7** |
| S-14 | Mosquitto `persistence true` (아래 §4) | §3.3 |

**이번 범위는 2차 + 3차 전부다.** S-5 는 서버가 먼저 한다: 인증 플러그인을 넣고
공용 계정을 **유지한 채** 시험 키로 확인 → 운영 키 K1 로 전환(§1.1.2.2 전환 순서).
그 뒤 단말 HMAC 펌웨어가 들어간다. 나머지는 지금 할 수 있다. 4차(TLS)는 이번 범위가 아니다.

`grp` 를 REGISTER_ACK 에 싣는 것은 5차다(§3.10.9). 지금은 `state` / `site` 만 싣는다.

**UI 사양이 없어도 된다.** 2·3차에 필요한 화면은 §3.9.2 의 두 화면(목록, 승인)
뿐이다. 대시보드·지도는 7차, 그룹 제어 화면은 5차다.

### 단말이 보내는 것 (1.1.0 실기 로그. 이후 판도 형식은 같고 `fw` 값만 다르다)

```text
register  {"type":"REGISTER","uuid":"20363930594D50170004003A","fw":"1.1.0",
           "cv":0,"ss":29,"ti":600,"ka":300,"device_model":"RMCB-1100M",
           "modem_model":"WD-N522S","msisdn":"01248324427",
           "imei":"358777078476869","iccid":"8982051902411583376"}
status    {"type":"TELEMETRY","sq":0,"ts":"260926T1209","ss":29,"cv":0,
           "er":16,"on":0,"md":0,"pw":[0,0,0],"bv":0,"bi":0,"sc":0,"pp":0,"li":0,"cs":0}
          (1.3.x 까지는 ts 뒤에 "fw":"x.y.z" 가 있었다. 1.4.0 부터 없음, 버전은 REGISTER 로)
result    {"type":"CONFIG_ACK","uuid":"20363930594D50170004003A","cv":1,"result":"OK"}
```

### 단말 동작에서 서버가 알아야 할 것

| 항목 | 값 | 서버 영향 |
|---|---|---|
| keepalive | **300초 확정** (CONFIG `ka` 60~1800, 운영은 600 이하) | 브로커가 끊김을 알기까지 7.5분 (600초면 15분) |
| 재접속 간격 | 30초 x5 → 5분 x5 → 30분 | 서버가 오래 죽었다 살아나면 단말이 붙기까지 최대 30분 |
| LWT | 모뎀이 Will 을 못 넣는다 | **서버가 Mosquitto 로그로 대체** (7.5분 감지, §16.1) |
| CONFIG 적용 | `ti` 즉시, `ka` 다음 접속부터 | |
| 요금제 | SKT 1,100원 = 월 5MB | `ti` 10분 / `ka` 300초 = 월 약 2.4MB (MCU 사양서 §14.6) |
| 원격 뒤 Telemetry (5차) | 원격으로 조명이 바뀌면 2초 뒤 1건 추가, 그때부터 주기 다시 셈 | 주기 어긋남으로 보지 않는다. `sq` 는 이어진다 |
| 현장 조작 (5차) | 시작하면 원격 전부 취소, 중에 온 명령은 `LOCAL` 로 버림 | Telemetry `md:1` 이 오면 서버가 기록한 원격은 끝난 것으로 표시 |

### 승인 / CONFIG 시험 (서버 없이 PC 도구로)

```powershell
python Tools\mqtt_test.py --device-uuid <UUID> --approve PENDING
python Tools\mqtt_test.py --device-uuid <UUID> --approve ACTIVE --site A-12
python Tools\mqtt_test.py --device-uuid <UUID> --approve CLEAR
```

단말 로그 `[LTE] APPROVAL ACTIVE site=A-12` 뒤 Telemetry 가 바로 1건 온다.
PENDING 이면 Telemetry 대신 REGISTER 가 5분마다 다시 온다.

#### CONFIG_SET

```powershell
python Tools\mqtt_test.py --device-uuid <UUID> --config "{\"cv\":1,\"ti\":300}" --config-only
```

`CONFIG_ACK result=OK` 가 오고 다음 Telemetry 의 `cv` 가 1 이면 된다.
범위 밖(`ti` 10 등)은 `RANGE` 로 답하고 아무것도 바꾸지 않는다.

---

# 부록 : 1차 접속 시험 기록

## 이번 목표

WD-N522S LTE 모뎀이 `infontech.co.kr:1883`의 Mosquitto Broker에 접속하는지 확인한다.
시험할 단말은 DIP8(PD13)을 ON으로 설정한 뒤 재부팅하여 LTE 모드로 시작한다.

이번 단계에는 웹 서버, DB, 관제 화면, Schedule, 원격 제어 기능이 필요하지 않다.
**Mosquitto Broker 접속 성공이 1차-A의 전부이며, 성공 즉시 메시지 왕복 시험으로 진행한다.**

## 1차 구현 범위

| 단계 | 내용 | 서버가 만들 것 |
|---|---|---|
| 1차-A | 모뎀 Broker 접속 | Mosquitto만 |
| 1차-B | REGISTER / PING / PONG | 구독 + PING 발행 |
| 1차-C | Telemetry 수신 확인 | `mosquitto_sub` 로 확인만 |

**1차 합격 = 1차-A.** B, C 는 같은 시험 세션에서 확인만 하고, 실패해도 1차-A
판정을 되돌리지 않는다. CONFIG 전달은 **2차로 이관 확정**이다(아래 "주기 바꾸기").

**1차는 여기까지다.** 목적은 LTE 망 등록부터 메시지 왕복까지 통신 경로가
뚫리는지 확인하는 것이다.

### 1차에는 단말 승인 절차가 없다

> **시험 전용 Broker와 시험 계정에서만 진행한다.**
> 단말이 REGISTER 직후 바로 Telemetry를 보낸다. 운영 Broker에 붙이면 승인하지
> 않은 단말의 데이터가 그대로 쌓인다.

승인 절차(PENDING / ACTIVE, 설치 위치 배정)는 **2차에서 MQTTS 전환과 함께**
넣는다. 평문 구간에서 승인을 구현하면 UUID만 알면 승인된 단말인 척할 수 있어
의미가 없다.

2차 규격은 사양서 §3에 확정해 두었으니 **서버 DB와 화면을 설계할 때 미리
반영**해 두면 좋다. 요점만 옮기면 다음과 같다.

```text
단말 → REGISTER              등록 요청
서버 → REGISTER_ACK          state = PENDING
        ↓  관리자 승인 + 위치 배정
서버 → REGISTER_ACK          state = ACTIVE
서버 → CONFIG_SET → 단말 CONFIG_ACK
        ↓
단말 Telemetry 시작
```

- `REGISTER`가 승인보다 **먼저다.** 그게 없으면 서버가 단말의 존재를 모른다.
- 단말은 승인 상태를 저장하지 않는다. **연결할 때마다 서버가 알려줘야 한다.**
- 서버는 REGISTER를 받을 때마다 **반드시 응답한다.** 상태가 그대로여도 답한다.
- 승인을 못 받아도 **조명은 자체 스케줄로 정상 점등한다.**

## 고정 접속값

| 항목 | 값 |
|---|---|
| Protocol | MQTT v3.1.1 |
| Host | `infontech.co.kr` |
| Port | `1883` |
| Username | `solarlte-test` |
| Password | `solarlte-test-2026` |
| Client ID | 단말 STM32 UUID 전체 24자리 |
| TLS | 1차 미사용 |

ID/PW는 평문 MQTT 간이 시험용 공용 계정이다. 운영 서버에서는 재사용하지 않는다.

## 서버 준비

### 1. 네트워크

- `infontech.co.kr`이 Mosquitto 서버 공인 IP를 가리켜야 한다.
- AWS Security Group과 서버 방화벽에서 TCP 1883을 허용한다.
- 가능하면 시험 기간에는 허용할 접속원을 제한한다.

### 2. Mosquitto 설치

Ubuntu 기준:

```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients
```

### 3. 시험 계정 생성

최초 생성 시 다음 명령을 실행하고 Password 입력 요청에 `solarlte-test-2026`을 입력한다.

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd solarlte-test
sudo chown root:mosquitto /etc/mosquitto/passwd
sudo chmod 640 /etc/mosquitto/passwd
```

`-c`는 기존 password file을 덮어쓰므로 최초 생성할 때만 사용한다.

### 4. Broker 설정

`/etc/mosquitto/conf.d/solarlte-test.conf`:

```conf
listener 1883
protocol mqtt
allow_anonymous false
password_file /etc/mosquitto/passwd
connection_messages true
log_type notice
persistence true
persistence_location /var/lib/mosquitto/
```

`persistence true` 는 승인 retain(REGISTER_ACK)을 브로커 재시작 뒤에도 남긴다(S-14).
없으면 재시작 때 모든 단말이 미승인으로 돌아간다.

설정 적용:

```bash
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
sudo systemctl status mosquitto
```

## 서버 자체 확인

첫 번째 터미널에서 구독한다.

```bash
mosquitto_sub -h infontech.co.kr -p 1883 \
  -u solarlte-test -P solarlte-test-2026 \
  -t 'iotlight/#' -v
```

두 번째 터미널에서 시험 메시지를 발행한다.

```bash
mosquitto_pub -h infontech.co.kr -p 1883 \
  -u solarlte-test -P solarlte-test-2026 \
  -t 'iotlight/server/test' -m 'server-ready' -q 1
```

첫 번째 터미널에서 `iotlight/server/test server-ready`가 보이면 Broker 기본 준비가 완료된 것이다.

## 1차-A 모뎀 접속 확인

서버 로그를 연다.

```bash
sudo journalctl -u mosquitto -f
```

단말 전원을 켠 뒤 다음 조건을 확인한다.

1. 단말 로그에 `*WMQTCON:2`가 표시된다.
2. 단말 로그에 `MQTT CONNECTED infontech.co.kr:1883`이 표시된다.
3. Mosquitto 로그에 24자리 UUID를 Client ID로 사용하는 새 연결이 표시된다.

세 항목이 확인되면 **1차-A MQTT 접속 시험 성공**이다. 이 시점부터 다음 메시지 시험으로
바로 진행하며, REGISTER나 PING/PONG 결과 때문에 접속 성공 판정을 되돌리지 않는다.

## 1차-C 주기 상태 보고 확인

단말은 REGISTER 직후 한 건을 보내고 이후 설정 주기로 반복한다.

```bash
mosquitto_sub -h infontech.co.kr -p 1883 \
  -u solarlte-test -P solarlte-test-2026 \
  -t 'iotlight/device/+/status' -v
```

받는 모양이다. 한 줄 약 160바이트다.

```json
{"type":"TELEMETRY","sq":41,"ts":"260924T2103","ss":15,"cv":3,
 "er":0,"on":1,"md":0,"pw":[70,64,64],
 "bv":2612,"bi":-150,"sc":87,"pp":3400,"li":230,"cs":3073}
```

전압·전류·전력은 **100으로 나누면 실제값**이다. `2612`는 26.12V다.
항목 전체 설명은 사양서 §1.1.6에 있다.

### 서버가 꼭 알아야 하는 두 가지

**1. `cs`(충전기 상태)를 같이 봐야 한다.**
MPPT는 값을 못 읽는 항목을 0으로 응답한다. `pp`가 0인 것만으로는 밤인지,
패널 미연결인지, 고장인지 구분할 수 없다. `cs` 비트로 갈린다.

**2. `sq`가 되돌아가면 재부팅이다.**
전원 인가 시 0부터 시작한다. 번호가 건너뛰면 유실, 되돌아가면 재부팅이다.

### 주기 바꾸기 — 2차

> **CONFIG_SET 은 2차로 이관을 확정했다.** 1차 펌웨어는 `config` topic 을 구독하지
> 않으므로 1차 시험에서는 발행하지 않는다. 2차 규격은 사양서 §1.1.7 이다.

2차의 모습은 다음과 같다.

```bash
mosquitto_pub -h infontech.co.kr -p 1883 \
  -u solarlte-test -P solarlte-test-2026 \
  -t 'iotlight/device/<UUID>/config' -q 1 \
  -m '{"type":"CONFIG_SET","cv":1,"ti":1800,"lat":37.3617,"lon":126.9352}'
```

단말이 `iotlight/device/<UUID>/result`로 `CONFIG_ACK`를 보내고, 다음 Telemetry의
`cv`가 보낸 값과 같아지면 적용된 것이다.

기본 주기는 **600초(10분)**이고 범위는 60~3600초다. 시험 중에는 10분을 쓰고
운영 전환 시 30~60분으로 늘린다. **지금은 PC 설정 도구로만 바꿀 수 있다.**

## 1차-B 메시지 왕복 확인

**도구를 먼저 실행한 뒤 단말을 재부팅한다.** REGISTER 는 (재)연결 시에만 오므로
순서를 바꾸면 120초 타임아웃으로 실패한다.

PC에서 SolarLTE 프로젝트의 간이 서버를 실행한다. `<UUID>`에는 단말 로그의 24자리 UUID를 넣는다.

```powershell
python -m pip install -r Tools\requirements.txt
python Tools\mqtt_test.py --device-uuid <UUID> --timeout 120
```

도구가 REGISTER를 수신하고 PING을 보낸 뒤 같은 UUID와 sequence의 PONG을 받으면
`DEVICE MQTT TEST PASS`를 출력한다.

## 실패 판단

| 현상 | 우선 확인 |
|---|---|
| TCP 연결 불가 | DNS, AWS Security Group, OS 방화벽, Mosquitto 실행 상태 |
| `MQTT FAIL REASON=3` | Broker 주소, 포트, 서비스 상태 |
| `MQTT FAIL REASON=4` | Username/Password 및 password file |
| `*WMQTCON:2` 후 REGISTER 실패 | Broker 접속은 성공이며 1차-B topic/payload 단계 점검 |

상세 topic과 JSON 규격은 `태양광_조명_서버_관제_사양_v1.0.md`의
`1.1 1차 MQTT 간이 검증`을 따른다.
