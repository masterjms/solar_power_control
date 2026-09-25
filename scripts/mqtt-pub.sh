#!/usr/bin/env bash
#
# mosquitto 컨테이너 안의 mosquitto_pub 로 발행한다. 호스트에 mosquitto-clients 불필요.
#
#   bash scripts/mqtt-pub.sh -t 'iotlight/server/test' -m 'server-ready' -q 1      # 브로커 자체 확인
#   bash scripts/mqtt-pub.sh -t 'iotlight/device/<UUID>/cmd' -q 1 -m '{"type":"PING","seq":1}'
#
# 인자는 그대로 mosquitto_pub 에 넘어간다. 계정 결정 규칙은 mqtt-sub.sh 와 같다.
# ⚠ -r(retain) 은 쓰지 말 것 — 이 프로젝트에서 retain 은 REGISTER_ACK(3차, 백엔드) 하나뿐이다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# shellcheck disable=SC1091
source scripts/_mqtt-common.sh

for a in "$@"; do
    [ "$a" = "-r" ] && { echo "!! retain(-r) 발행은 금지 — cmd 는 절대 retain 하지 않는다(CLAUDE.md)"; exit 2; }
done

exec docker compose exec -T mosquitto mosquitto_pub -h localhost -p 1883 "${AUTH[@]}" "$@"
