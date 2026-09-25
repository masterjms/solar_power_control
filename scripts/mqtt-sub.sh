#!/usr/bin/env bash
#
# mosquitto 컨테이너 안의 mosquitto_sub 로 구독한다. 호스트에 mosquitto-clients 불필요.
#
#   bash scripts/mqtt-sub.sh -t 'iotlight/#' -v                    # 서버 계정(.env)으로
#   bash scripts/mqtt-sub.sh -t 'iotlight/device/+/status' -v      # 1차-C Telemetry 확인
#   MQTT_USERNAME=solarlte-test MQTT_PASSWORD=solarlte-test-2026 \
#       bash scripts/mqtt-sub.sh -t 'iotlight/#' -v                # 공용 시험 계정으로
#   bash scripts/mqtt-sub.sh -u solarlte-test -P solarlte-test-2026 -t 'iotlight/#' -v   # 같은 뜻
#
# 인자는 그대로 mosquitto_sub 에 넘어간다. -u/-P 를 직접 주면 그게 우선이고,
# 없으면 환경변수 MQTT_USERNAME/MQTT_PASSWORD, 그것도 없으면 .env 의 값을 쓴다.
# 접속 대상은 항상 컨테이너 자신(localhost:1883) — 방화벽·DNS 를 거치지 않는다.
# 외부에서 붙는 시험(DNS·SG 검증)은 PC 에서 mosquitto_sub -h infontech.co.kr 로 따로 한다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# shellcheck disable=SC1091
source scripts/_mqtt-common.sh

exec docker compose exec -T mosquitto mosquitto_sub -h localhost -p 1883 "${AUTH[@]}" "$@"
