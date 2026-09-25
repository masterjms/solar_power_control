#!/usr/bin/env bash
# mqtt-sub.sh / mqtt-pub.sh 공통: 계정 인자(AUTH 배열) 결정.
#   1) 호출 인자에 -u 또는 -P 가 있으면 아무것도 안 붙인다 (사용자가 직접 지정)
#   2) 환경변수 MQTT_USERNAME / MQTT_PASSWORD
#   3) .env 의 MQTT_USERNAME / MQTT_PASSWORD (서버 계정)
# 직접 실행하는 파일이 아니다. source 해서 쓴다.

AUTH=()

_has_user_arg=0
for _a in "$@"; do
    case "$_a" in
        -u|-P|--username|--pw) _has_user_arg=1 ;;
    esac
done

if [ "$_has_user_arg" -eq 0 ]; then
    if [ -z "${MQTT_USERNAME:-}" ] && [ -f .env ]; then
        MQTT_USERNAME="$(grep -E '^MQTT_USERNAME=' .env | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d "\"'" || true)"
        MQTT_PASSWORD="$(grep -E '^MQTT_PASSWORD=' .env | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d "\"'" || true)"
    fi
    if [ -n "${MQTT_USERNAME:-}" ]; then
        AUTH=(-u "$MQTT_USERNAME" -P "${MQTT_PASSWORD:-}")
    else
        echo "!! 계정이 없다. -u/-P 를 주거나 MQTT_USERNAME/MQTT_PASSWORD 환경변수, 또는 .env 를 준비할 것" >&2
        exit 2
    fi
fi
unset _a _has_user_arg
