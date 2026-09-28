# ADR-006 4차 — MQTTS 8883 과 운영/시험 브로커 분리

- 날짜: 2026-09-27
- 상태: 확정 — **준비 완료, 서버 미적용**. 5차 로직이 시험 Broker(1883)에서 통과한 뒤 켠다(사양서 "다음 전달 범위" S-21·S-22, §1.2)

## 맥락
사양서 §1.2.2: 운영 Broker 8883(TLS 1.2, 서버 인증서 검증, 클라이언트 인증서 없음)과 시험 Broker 1883 을 분리하고, **시험 Broker 에는 운영 단말 계정을 두지 않는다.** 합격 기준 §1.2.4 #1 "1883 은 운영 계정 거부". 서버 코드는 "접속 설정만" 바뀌어야 한다.

지금 브로커는 하나(`iegomez/mosquitto-go-auth`, ADR-003)이고 인증은 go-auth 플러그인(files + http → backend HMAC)이 리스너 구분 없이 한다. 인증서가 있으면 entrypoint 가 8883 리스너를 붙인다.

## 결정

### 1. 시험 브로커 = 별도 컨테이너 (`mosquitto-test`), per_listener_settings 아님
- 새 서비스 `mosquitto-test`: 순정 `eclipse-mosquitto:2`, 호스트 1883, `allow_anonymous false`, password_file 에 **`solarlte-test` 한 줄만**(기동 때마다 다시 만든다), ACL `iotlight/#` readwrite, `persistence true`, 자기 볼륨. backend 는 붙지 않는다(PC 도구·레거시 시험용).
- 운영 브로커 `mosquitto`: 호스트에는 **8883 만** 공개(`ports: !override`). 1883 리스너는 그대로 두되 컨테이너망 안 backend 전용.
- 왜 `per_listener_settings true` 로 한 브로커 안에서 1883/8883 을 나누지 않는가:
  - go-auth 는 전역 플러그인이다. `per_listener_settings true` 면 플러그인·`auth_opt_*` 를 리스너마다 따로 선언해야 하고, 1883 에서 "운영 계정 거부"를 하려면 1883 용 인증 설정을 따로 만들어야 한다. 그런데 **backend 도 1883 으로 붙는다** — 1883 에서 server 는 받고 단말은 거부하는 조합을 files/http 순서로 표현하는 건 설정이 복잡하고 틀리기 쉽다.
  - 같은 프로세스·같은 persistence 를 공유하면 "시험"이 운영 retain(REGISTER_ACK)·세션을 보고 건드린다. 사양서의 "분리" 의도와 어긋난다.
  - 별도 컨테이너면 "운영 계정이 없다"가 **구조로** 성립한다(설정 실수로 뚫릴 수 없음). 비용은 컨테이너 1개(메모리 수 MB).
- 시험 브로커에는 인증 플러그인이 필요 없으므로 순정 이미지를 쓴다. 비밀번호는 `.env MQTT_TEST_BROKER_PASSWORD`, 비면 사양서 공개값.

### 2. 켜고 끄기 = compose 오버라이드 (`docker-compose.tls.yml`)
- 운영자가 `.env` 에 `COMPOSE_FILE=docker-compose.yml:docker-compose.https.yml:docker-compose.tls.yml` 한 줄(2026-09-28: 관리 화면 443 은 `docker-compose.https.yml` 로 분리 — 브로커 분리 전에 먼저 켤 수 있다). 이후 `deploy.sh`·`healthcheck.sh`·수동 `docker compose` 가 모두 같은 구성을 본다.
- **롤백 = 그 줄을 주석 처리하고 `docker compose up -d --remove-orphans`.** 코드·DB·인증서는 그대로.
- backend 설정은 바뀌지 않는다: `MQTT_HOST=mosquitto`, `MQTT_PORT=1883`, `MQTT_TLS=false`(컨테이너망, 호스트 밖으로 안 나감). "접속 설정만 바뀐다"는 원칙의 최소형 — 이번엔 서버 접속 설정조차 안 바뀐다. `docs/02` 의 "4차 = backend `MQTT_TLS=true`, 8883" 은 backend 를 브로커와 다른 호스트로 옮길 때의 경로로 남는다(그때 `connection.py` 밖은 여전히 무변경).

### 3. 인증서 수명 주기
- Let's Encrypt, `certbot/certbot` 컨테이너(`certbot` 프로파일, 1회성), **`--webroot`** — HTTP-01 을 이미 80 을 쓰는 web(nginx)의 `/.well-known/acme-challenge/`(무인증, `certbot-www` 볼륨)가 답한다. 발급·갱신에 서비스 중단이 없다.
- 저장: `./infra/letsencrypt`(certbot 정본, 0700 root) → 복사 → `./infra/certs/server.crt`(fullchain, 644) / `server.key`(640 **root:1000**). 복사는 certbot 컨테이너 안에서 한다(호스트 사용자는 letsencrypt/ 를 못 읽는다). 둘 다 커밋 안 함.
- 키 권한: mosquitto 2.0.15 는 **권한을 내려놓은 뒤(uid 1000) 키를 읽는다** — `0600 root` 면 `Unable to load server key file ... Permission denied` 로 기동 실패(2026-09-27 실측). nginx 는 master(root)가 읽는다.
- **mosquitto 2.0.15 는 리스너 인증서를 실행 중에 다시 읽지 않는다**(SIGHUP 은 conf 일부·로그만). 새 인증서 반영 = 브로커 재시작 = 단말 전부 재접속. 그래서:
  - `cert-renew.sh` 는 `certbot renew` 후 `live/fullchain.pem` 과 `server.crt` 의 **sha256 이 다를 때만** 복사·`restart mosquitto`·`nginx -s reload`. certbot 은 만료 30일 전부터만 갱신하므로 재시작은 약 60일에 1번.
  - cron: 03:30 KST 전체 실행, 15:30 KST `--renew-only`(갱신만, 적용은 그날 밤). 낮에 브로커가 재시작되는 일이 없다.
  - 루트(ISRG Root X1)는 그대로라 단말 루트 CA 는 갱신과 무관(P-5).
- 관리 화면 HTTPS: 같은 인증서. `frontend/nginx/40-tls.sh` 가 `/etc/nginx/certs/server.crt` 유무로 평문/TLS 모드를 고른다(nginx 템플릿은 envsubst 뿐이라 조건을 스크립트로). TLS 모드는 443 + 80→301(acme·`localhost` 예외). 인증서는 오버라이드에서만 web 에 물린다 — 기본 compose 는 인증서가 있어도 평문 모드.

## 결과
- 로컬 실측(2026-09-27, 자체 CA 2단 체인): 8883 HMAC 단말 CA 검증 ON 접속 성공 / 다른 CA → 클라이언트 검증 실패 / 호스트 1883(시험 브로커)에서 HMAC 단말·server 거부, `solarlte-test` 허용 / backend 는 내부 1883 유지 / 443 은 올바른 CA 로 401→200, 80 은 301.
- 재접속 1회 데이터량(TLS 1.2 전체 핸드셰이크, RSA-2048 2단 체인): 핸드셰이크 2,566 B + MQTT 포함 2,757 B(TCP/IP 헤더 제외), 평문 104 B. 모뎀 실측으로 §1.1.6 갱신 필요(단말측).
- 운영 브로커의 8883 에서도 `solarlte-test` 는 `MQTT_TEST_ACCOUNT_ENABLED` 를 따른다 — 공용 계정 폐기(ADR-003 전환 순서 5)는 별개로 진행.
- 전환·롤백·갱신 적용은 모두 브로커 재시작(단말 전부 재접속)이다. 단말 backoff 지터 확인 전엔 시험 단말만 있을 때 한다.
- 런북: docs/04 §6.
