# ADR-002 retain 정책과 CONFIG 재전송

- 날짜: 2026-09-25
- 상태: 확정

## 결정
- retain 은 `iotlight/device/<uuid>/config` 의 **REGISTER_ACK 하나뿐**(사양서 §3.3). 같은 topic 으로 나가는 CONFIG_SET 은 retain=0.
- `cmd` topic 은 절대 retain 하지 않는다(재접속 단말에 옛 명령이 되살아남).
- CONFIG_SET 재전송 트리거는 타이머가 아니라 **Telemetry 수신 시점**에 `cv_server != cv_device` 검사(사양서 §1.1.7). 같은 단말 60초 쿨다운.
- 관리자가 `ti`/`lat`/`lon` 을 바꾸면 `cv_server += 1` 하고 **즉시** CONFIG_SET 1회 발행(§16.5). 이후는 위 규칙.

## aircast 와 다른 점
aircast 는 CONFIG 자체를 retain 하고 주기 재조정(reconcile) 태스크를 돌렸다. 여기서는 단말이 `cv` 를 Flash 에 저장하고 Telemetry 에 echo 하므로 retain 이 필요 없고, 오히려 사양서가 "retain 에 설정값 섞이면 재부팅 시 옛 값 먼저 도착"이라고 금지한다.

## 주의
같은 topic 에 retain 메시지(REGISTER_ACK)와 비retain 메시지(CONFIG_SET)가 섞인다. 브로커는 마지막 retain 메시지만 보관하므로 CONFIG_SET 이 REGISTER_ACK retain 을 지우지 않는다(retain=0 발행은 보관본에 영향 없음). 시나리오 `S3-03` 에서 확인한다.
