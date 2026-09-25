# 시드 passwd 만드는 법

`/mosquitto/data/passwd` 가 mosquitto 가 실제로 읽는 비밀번호 파일이다. 만들어지는 경로는 셋이고, 우선순위도 이 순서다.

| 순서 | 출처 | 언제 |
|---|---|---|
| 1 | `mqtt-dynamic` 볼륨의 `passwd.generated` (백엔드가 DB 에서 생성) | 2차부터. 있으면 무조건 이것 |
| 2 | 이미 설치된 `/mosquitto/data/passwd` | 재기동. 이전 내용 유지 |
| 3 | `infra/mosquitto/config/passwd` (이 디렉터리의 시드) | 볼륨이 비어 있는 첫 기동 |
| 4 | 빈 파일 | 시드도 없을 때. 아무도 못 붙는다 |

## 1차: 시드에 계정 넣기

시드 파일 `infra/mosquitto/config/passwd` 는 **커밋하지 않는다**(.gitignore). 서버에서 다음으로 만든다. 컨테이너 안의 `mosquitto_passwd` 를 쓰므로 호스트에 mosquitto 를 설치할 필요가 없다.

```bash
cd /opt/solar_power_control
# 1차 공용 시험 계정 (사양서 §1.1.2.1)
bash infra/scripts/mosquitto-passwd-add.sh solarlte-test solarlte-test-2026
# 서버 계정 (.env 의 MQTT_USERNAME / MQTT_PASSWORD 와 같은 값)
bash infra/scripts/mosquitto-passwd-add.sh server '<.env 의 MQTT_PASSWORD>'
```

스크립트는 (1) 실행 중인 컨테이너의 `/mosquitto/data/passwd` 에 계정을 추가·갱신하고 (2) SIGHUP 으로 리로드하고 (3) 그 파일을 `infra/mosquitto/config/passwd` 로 복사해 시드를 최신으로 맞춘다. 볼륨을 지우고 다시 띄워도 시드에서 복구된다.

컨테이너가 아직 없으면(최초 셋업 직후) 먼저 `docker compose up -d mosquitto` 로 띄운다. 빈 passwd 로 기동돼도 무방하다 — 아무도 못 붙을 뿐이다.

## 형식

한 줄에 `username:$7$101$<salt>$<hash>` (mosquitto 2.x PBKDF2-SHA512). `mosquitto_passwd -b` 가 만든다. 손으로 편집하지 않는다. 2차 백엔드도 같은 형식을 만들어 `passwd.generated` 로 내보낸다(DB 에는 이 해시만 저장, 평문은 PC 설정 도구 CSV 에만).

## 2차 이후

백엔드가 `passwd.generated` 를 쓰기 시작하면 시드는 부트스트랩 폴백일 뿐이다. `server` 계정과 (`MQTT_TEST_ACCOUNT_ENABLED=true` 면) `solarlte-test` 도 백엔드가 generated 에 같이 넣는다. 그 뒤로 이 스크립트로 직접 계정을 추가해도 다음 generated 갱신 때 덮어써진다.
