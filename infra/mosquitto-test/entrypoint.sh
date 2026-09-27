#!/bin/sh
# 시험 브로커 기동 래퍼 (eclipse-mosquitto:2, ADR-006).
# passwd 를 기동 때마다 solarlte-test 한 줄로 다시 만든다 — 다른 계정이 섞일 틈을 두지 않는다.
# 비밀번호는 MQTT_TEST_BROKER_PASSWORD, 비면 사양서 §1.1.2.1 공개값 solarlte-test-2026.
set -eu

DATA=/mosquitto/data
PW="${MQTT_TEST_BROKER_PASSWORD:-}"
[ -n "$PW" ] || PW=solarlte-test-2026

mkdir -p "$DATA"
rm -f "$DATA/passwd.tmp"
mosquitto_passwd -b -c "$DATA/passwd.tmp" solarlte-test "$PW" 2>/dev/null
chown mosquitto:mosquitto "$DATA/passwd.tmp"
chmod 600 "$DATA/passwd.tmp"
mv "$DATA/passwd.tmp" "$DATA/passwd"
chown -R mosquitto:mosquitto "$DATA"
echo "시험 브로커: 계정 solarlte-test 하나 (운영 단말·server 계정 없음)" >&2

# conf 는 :ro 마운트라 CRLF 를 벗긴 사본으로 기동한다(Windows 체크아웃 대비).
tr -d '\r' < /mosquitto-test/mosquitto.conf > "$DATA/mosquitto.effective.conf"
tr -d '\r' < /mosquitto-test/aclfile > "$DATA/aclfile"
chown mosquitto:mosquitto "$DATA/mosquitto.effective.conf" "$DATA/aclfile"
chmod 600 "$DATA/aclfile"

exec /usr/sbin/mosquitto -c "$DATA/mosquitto.effective.conf"
