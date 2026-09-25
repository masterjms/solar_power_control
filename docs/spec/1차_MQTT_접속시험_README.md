# SolarLTE 서버 1차 MQTT 접속 시험

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

## 현재 진행 상태 (2026-09-24)

단말은 준비되었다. **남은 것은 서버 Broker 기동뿐이다.**

| 항목 | 상태 |
|---|---|
| LTE 망 등록 | 완료 |
| IP 할당 | 완료 (IPv6 단독, 정상) |
| MQTT 설정 및 접속 시도 | 정상 동작 |
| Broker 접속 결과 | `*WMQTCON:0` — 상대 응답 없음 |
| 재시도 | 30초 주기로 정상 반복 |

`*WMQTCON:0`은 접속을 시도했지만 상대가 응답하지 않았다는 뜻이다. Mosquitto가
아직 없기 때문이며 단말 쪽 문제는 아니다.

단말 IP가 IPv6 단독으로 잡히는 것은 국내 Cat.M1 망의 정상 동작이다. MQTT는
WD-N522S 모뎀 내장 스택이 호스트명을 직접 해석해 접속하므로 **서버는 IPv4
전용으로 구성해도 된다.**

아래 세 가지를 마치면 곧바로 1차-A 판정이 가능하다.

1. Mosquitto 설치 및 TCP 1883 listener 기동
2. AWS Security Group과 OS 방화벽에서 TCP 1883 허용
3. `solarlte-test` / `solarlte-test-2026` 계정 등록

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
```

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
{"t":"TM","sq":41,"ts":"260924T2103","fw":"1.0.0","ss":15,"cv":3,
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
