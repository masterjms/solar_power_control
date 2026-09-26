# ADR-003 단말 인증 — HMAC 계산 비밀번호 + 브로커 인증 플러그인

- 날짜: 2026-09-26
- 상태: 확정 (사양서 §1.1.2.2 2026-09-26 개정 반영)

## 맥락
사양서가 바뀌었다. 단말 비밀번호는 더 이상 "PC 도구가 만든 무작위 문자열을 CSV 로 import" 가 아니라
**`HMAC-SHA256(K, UUID)` 앞 16바이트 hex 32자** 계산값이다. 단말은 부팅 때 계산하고 아무도 저장하지 않는다.
브로커는 접속 순간 같은 값을 계산해 비교해야 한다 — `password_file` 에 미리 넣을 수 없다(새 단말은 REGISTER 뒤에야 UUID 를 안다).

## 결정
- 브로커 이미지를 `eclipse-mosquitto:2` → **`iegomez/mosquitto-go-auth`** (Mosquitto 2.0.x + go-auth 플러그인)로 바꾼다.
- 백엔드 조합: `files` (서버 계정 `server`, 1차 공용 `solarlte-test`) + `http` (단말 UUID → 백엔드 `/internal/mqtt/auth`·`/acl`).
- 백엔드가 인증 API 를 제공한다:
  - `POST /internal/mqtt/auth` `{username, password, clientid}` → username 이 `^[0-9A-F]{24}$` 이고 `password == hmac(K_i, username)` (활성 키 중 하나) 이면 200, 아니면 403. `hmac.compare_digest`.
  - `POST /internal/mqtt/acl` `{username, topic, clientid, acc}` → 사양서 ACL 표(`%u` = UUID)를 코드로. write: `device/<u>/{register,status,result,event}`, read: `device/<u>/{cmd,config}`, `group/#`, `all/cmd`.
  - `POST /internal/mqtt/superuser` → 항상 403.
  - **승인 상태로 접속을 막지 않는다** (PENDING/REJECTED 도 접속 허용, 사양서 §1.1.2.2 #5).
- 키는 `.env` 의 `MQTT_HMAC_KEYS="K1:<hex64>"` (쉼표로 여러 개 = 교체 중). 저장소·DB·로그에 남기지 않는다. `.env.example` 에는 사양서의 **공개 시험 키**만.
- go-auth 캐시(`go-cache`, 인증 600초·ACL 600초 + 지터)로 1만 대 재접속 폭주 때 HTTP 호출을 줄인다.
- `files` 백엔드 passwd 는 go-auth 형식(`PBKDF2$sha512$100000$<salt b64>$<hash b64>`)이라 백엔드 내보내기 해시를 이 형식으로 바꾼다. 단말 행은 더 이상 passwd 에 넣지 않는다 → `device.mqtt_password_hash` 컬럼 삭제.

## 결과
- 백엔드가 죽어 있으면 **단말 신규 접속이 안 된다**(캐시된 단말은 TTL 동안 됨). 백엔드 기동 시간이 짧고 `restart: unless-stopped` 라 감수한다. 4차 이후 필요하면 go-auth `plugin`(Go)으로 HMAC 을 브로커 안에 넣어 의존을 없앤다.
- `/internal/*` 는 컨테이너 네트워크 안에서만 닿아야 한다. 운영 compose 는 backend 포트를 호스트에 열지 않는다. 추가로 `MQTT_AUTH_SHARED_SECRET` 헤더를 검사한다.
- 전환 순서(사양서 §1.1.2.2): 플러그인 설치(공용 계정 유지) → 시험 키로 확인 → K1 전환 → 단말 HMAC 펌웨어 → 공용 계정 삭제(`MQTT_TEST_ACCOUNT_ENABLED=false`).
