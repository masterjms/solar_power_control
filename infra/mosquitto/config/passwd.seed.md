# passwd 는 어떻게 만들어지나 (go-auth files 백엔드)

`/mosquitto/data/passwd` 가 go-auth `files` 백엔드가 실제로 읽는 비밀번호 파일이다. 여기에는 **files 계정만** 들어간다: 서버 계정 `server` 와 1차 공용 `solarlte-test`. 단말(UUID) 계정은 들어가지 않는다 — 접속 순간 백엔드 `/internal/mqtt/auth` 가 HMAC 으로 판정한다(ADR-003).

만들어지는 경로는 셋이고, 우선순위도 이 순서다.

| 순서 | 출처 | 언제 |
|---|---|---|
| 1 | `mqtt-dynamic` 볼륨의 `passwd.generated` (백엔드가 `.env` 기준으로 생성) | 백엔드가 뜬 뒤. 있으면 무조건 이것 |
| 2 | 이미 설치된 `/mosquitto/data/passwd` | 재기동. 이전 내용 유지 |
| 3 | entrypoint.sh 가 `/mosquitto/pw` 로 즉석 생성 | 볼륨이 비어 있는 첫 기동. `solarlte-test` + (compose 가 `MQTT_PASSWORD` 를 넘겼으면) `server` |

리포에 비밀번호 파일(시드)을 두지 않는다. 예전 `infra/mosquitto/config/passwd` 시드 파일은 없어졌다(.gitignore 항목은 남겨 둔다).

## 형식

한 줄에 `username:PBKDF2$sha512$100000$<salt b64>$<hash b64>` — go-auth `pw` 도구 기본값(hasher pbkdf2, sha512, 100000회, salt 16바이트, 키 64바이트, base64). `mosquitto_passwd` 의 `$7$101$…` 과 **호환되지 않는다**. 예:

```
solarlte-test:PBKDF2$sha512$100000$F89/eatxiSOfMsR5HXlweg==$/Rl5JNMeZqsd0GIQlK5lH0lhTD0oPi5UboWMS6k3877w4QZQlFz3v/ZvEr25Zr82nSTKnLSkKOWRHbgop/ZfNw==
```

해시 하나 만들기(컨테이너 안): `docker compose exec mosquitto /mosquitto/pw -p '<비밀번호>'` — 해시만 찍으므로 `username:` 은 앞에 직접 붙인다. 백엔드(`backend/app/core/mqtt_accounts.py`)도 같은 형식으로 `passwd.generated` 를 내보낸다.

## 반영 시점 — HUP 가 아니라 재시작

go-auth 플러그인은 SIGHUP 으로 passwd/aclfile 을 다시 읽지 않는다(플러그인의 `mosquitto_auth_security_init(reload)` 가 빈 함수, 2026-09-26 실측). entrypoint 감시 루프는 `*.generated` 가 바뀌면 `/mosquitto/data/` 에 설치하고 `*.applied` 에 md5 를 적은 뒤:

- **aclfile 내용이 바뀌었거나 passwd 의 계정 집합이 바뀌었으면** 브로커를 재시작한다(`kill -TERM 1` → compose `restart: unless-stopped` 가 되살린다). 단말 전부 재접속. 공용 계정 폐기(`MQTT_TEST_ACCOUNT_ENABLED=false`)가 이 경우다.
- **계정 집합은 같고 해시만 바뀌었으면** 설치만 한다. 백엔드는 기동마다 무작위 salt 로 passwd 를 새로 내보내므로, 이걸 재시작 조건으로 삼으면 백엔드 배포마다 브로커가 재시작된다. 서버 비밀번호(`MQTT_PASSWORD`)를 실제로 바꿨을 때는 운영자가 `docker compose restart mosquitto` 를 한다(docs/04).

수동으로 계정을 넣을 때는 `infra/scripts/mosquitto-passwd-add.sh` — 안에서 `pw` 로 해시를 만들고 브로커를 재시작한다.
