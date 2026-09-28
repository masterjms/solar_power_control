# 4차 TLS(MQTTS 8883) 인증서 준비 — 서버 작업 절차

기준 : 2026-09-27, 단말 F/W 빌드 2026-09-27-10 이상. 서버 사양서 §1.2, S-21 / S-22.

## 1. 한눈에

```text
단말(WD-N522S 모뎀)  ── ssl://infontech.co.kr:8883, TLS 1.2 ──▶  Mosquitto 8883
  모뎀 /data 에 루트 인증서                                      서버 인증서 (Let's Encrypt, RSA 2048)
  ISRG Root X1 만 둔다                                           + 중간 인증서 (fullchain)
  → 서버가 보낸 인증서 사슬을 이 루트로 검증                      계정 : username = UUID, password (S-5)
```

| 항목 | 정한 것 | 이유 |
|---|---|---|
| 서버 인증서 | **Let's Encrypt, `infontech.co.kr`, RSA 2048** | 무료, 자동 갱신. 모뎀의 ECDSA 지원은 확인 안 됨 → RSA |
| 단말이 가진 것 | **루트 ISRG Root X1 하나**(2035-06-04 까지) | 서버 인증서가 90일마다 바뀌어도 단말은 그대로 (P-5 답) |
| TLS 버전 | **1.2 반드시 허용** | 모뎀은 TLS 1.3 을 지원하지 않는다(AT 문서 §7) |
| 클라이언트 인증서 | 쓰지 않는다 | 단말 신원은 계정(§1.1.2.2). 인증서 관리 부담 |
| 서버 검증 | 단말이 한다(`servercertauth 1`) | 가짜 서버에 붙지 않게 (§1.2.4 합격 기준 2) |
| 1883 | 시험용으로 남긴다. **운영 계정은 8883 에만** | S-21 |

## 2. 서버가 할 일

### 2.1 방화벽

| 포트 | 용도 |
|---|---|
| 8883/tcp | MQTTS (단말) — 연다 |
| 80/tcp | Let's Encrypt 발급·갱신(HTTP-01) 확인용. 80 을 열 수 없으면 DNS-01 방식으로 발급 |
| 1883/tcp | 시험 Broker. 지금처럼 접속원 IP 제한 유지 |

2026-09-27 PC 에서 확인 : `infontech.co.kr` 443·8883 모두 닫혀 있음(인증서 아직 없음).

### 2.2 인증서 발급 (certbot)

```bash
sudo apt install certbot
# 80 포트를 쓰는 다른 프로그램이 없을 때 (standalone)
sudo certbot certonly --standalone -d infontech.co.kr --key-type rsa --rsa-key-size 2048
# 결과 : /etc/letsencrypt/live/infontech.co.kr/fullchain.pem, privkey.pem
```

- **`--key-type rsa` 를 꼭 준다.** 요즘 certbot 기본은 ECDSA 다.
- 서버 이름은 **`infontech.co.kr`** 이어야 한다. 단말은 이 이름으로 접속하고 인증서 이름을 확인한다(IP 로 접속하면 안 맞는다).

### 2.3 Mosquitto 설정

Mosquitto 가 `/etc/letsencrypt` 를 직접 읽지 못하므로 복사해서 쓴다(2.4 의 훅이 갱신 때마다 복사).

```conf
# /etc/mosquitto/conf.d/solarlte.conf
per_listener_settings true
persistence true                      # S-14

# 시험 Broker (운영 계정 없음)
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd_test

# 운영 Broker
listener 8883
certfile /etc/mosquitto/certs/fullchain.pem    # 서버 + 중간 인증서 (중간이 없으면 단말 검증 실패)
keyfile  /etc/mosquitto/certs/privkey.pem
tls_version tlsv1.2                            # 1.2 이상 허용. 1.3 만 두면 단말이 못 붙는다
require_certificate false                      # 클라이언트 인증서 안 씀
allow_anonymous false
# S-5 이후 : 인증 플러그인(HMAC 계정)은 이 listener 에 건다. ACL 도 여기에
```

### 2.4 갱신 자동화

certbot 은 설치 때 자동 갱신 타이머를 만든다(하루 2번 확인, 만료 30일 전에 실제 갱신 → 약 60일마다).
실제로 갱신될 때만 실행되는 훅을 둔다.

```bash
# /etc/letsencrypt/renewal-hooks/deploy/mosquitto.sh  (chmod +x)
#!/bin/sh
install -m 0644 -o mosquitto /etc/letsencrypt/live/infontech.co.kr/fullchain.pem /etc/mosquitto/certs/fullchain.pem
install -m 0600 -o mosquitto /etc/letsencrypt/live/infontech.co.kr/privkey.pem   /etc/mosquitto/certs/privkey.pem
systemctl restart mosquitto
```

- 처음 한 번은 훅을 손으로 실행해 파일을 복사한다.
- 재시작하면 모든 단말이 끊겼다가 30초 안에 다시 붙는다(`persistence true` 라 승인 retain 은 남는다). 약 60일에 한 번이다.
- **단말은 아무것도 바꾸지 않는다.** 루트(ISRG Root X1)가 같기 때문이다.

### 2.5 확인

```bash
# 서버 인증서 사슬 : "Verify return code: 0 (ok)", 사슬 끝이 ISRG Root X1, Protocol TLSv1.2 로도 붙는지
openssl s_client -connect infontech.co.kr:8883 -servername infontech.co.kr -tls1_2 -showcerts </dev/null
```

PC(Windows)에서 단말과 같은 루트로 :

```powershell
cd Tools
python mqtt_test.py --host infontech.co.kr --port 8883 --tls --ca tls\isrgrootx1.pem --username <운영 시험 계정> --password <비밀번호>
```

`[MQTT] CONNACK: Success` 가 나오면 TLS 와 계정은 된 것이다(끝의 `TEST PASS` 는 그 계정 ACL 에
`iotlight/test` 발행·구독이 있어야 나온다). 서버 준비가 끝나면 단말 담당에게 알린다.

## 3. 단말 쪽 (참고)

- 루트 인증서는 펌웨어에 들어 있다(`FW/Modules/lte_ca.h`, `Tools/tls/make_ca_header.py` 가 만든다. 공개 인증서라 비밀 아님).
- 접속마다 모뎀 `/data/isrgx1.crt` 크기를 확인하고, 없거나 다르면 `AT*WFPUSH` 로 올린다. `/data` 는 재부팅해도 남는다.
- 모뎀 설정 : `endpoint ssl://infontech.co.kr`, `port 8883`, `cacerts /data/isrgx1.crt`, `selfsigned 0`, `servercertauth 1`.
- 빌드 스위치 `LTE_MQTTS_INCLUDE`. 서버 8883 이 준비되면 켠다.
- 인증서 문제로 접속이 안 되면 단말 로그 `[LTE] MQTT FAIL REASON=5 (certificate error)`.

## 4. 합격 기준 (서버 사양서 §1.2.4)

1. 운영 Broker 8883 접속, 1883 은 운영 계정 거부
2. 잘못된 인증서 서버에 접속하면 단말이 거부 (예 : 자체 서명 인증서를 건 시험 listener)
3. 3차 시나리오 1~5(§3.9.1) + 5차 명령을 8883 에서 다시 통과
4. TLS 포함 재접속 1회 데이터량 측정 → 월 사용량 표(§1.1.6) 갱신

## 5. 나중에 생길 수 있는 일

| 일 | 대응 |
|---|---|
| Let's Encrypt 가 사슬을 바꿔 **다른 루트**로 끝나게 됨 | 단말 루트를 바꿔야 한다 → `make_ca_header.py` 로 새 헤더, 펌웨어 교체(7차 OTA 전에는 현장 방문). Let's Encrypt 공지를 지켜본다 |
| ISRG Root X1 만료(2035-06-04) | 위와 같음. 그 전에 교체 |
| 상용 인증서로 바꿈 | 그 인증서의 **루트**로 `make_ca_header.py` 를 다시 돌려 펌웨어를 만든다 |
| 인증서 파일이 모뎀에서 지워짐 | 단말이 다음 접속 때 스스로 다시 올린다 |
