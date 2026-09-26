#!/usr/bin/env bash
#
# mosquitto files 계정 하나 추가/갱신 (백엔드가 없을 때 쓰는 수동 경로 — 1차, 또는 비상시)
#
#   bash infra/scripts/mosquitto-passwd-add.sh <username> <password>
#   bash infra/scripts/mosquitto-passwd-add.sh solarlte-test solarlte-test-2026
#
# 브로커 이미지가 iegomez/mosquitto-go-auth 라(ADR-003) passwd 형식은 mosquitto_passwd 의
# `$7$…` 이 아니라 go-auth 의 `user:PBKDF2$sha512$100000$<salt b64>$<hash b64>` 다.
# 이미지의 /mosquitto/pw 로 해시를 만든다(호스트에 mosquitto 설치 불필요).
#
# 하는 일:
#   1. 실행 중인 mosquitto 컨테이너 안에서 /mosquitto/data/passwd 의 해당 계정 줄을 교체/추가
#   2. 소유·권한을 mosquitto 0600 으로 되돌린다
#   3. **브로커 재시작** — go-auth 는 SIGHUP 으로 passwd 를 다시 읽지 않는다(2026-09-26 실측).
#      단말이 붙어 있으면 전부 재접속한다. 시험 단말만 있을 때 한다.
#
# 단말(UUID) 계정은 여기 넣지 않는다 — 접속 때 백엔드가 HMAC 으로 판정한다.
# 백엔드가 떠 있으면 passwd.generated 가 정본이라 여기서 넣은 계정은 다음 내보내기 때
# 덮어써진다(server / solarlte-test 는 백엔드가 같이 넣는다).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

USER_NAME="${1:-}"
PASSWORD="${2:-}"
if [ -z "$USER_NAME" ] || [ -z "$PASSWORD" ]; then
    echo "사용법: $0 <username> <password>"
    exit 2
fi
case "$USER_NAME" in
    *:*) echo "!! username 에 ':' 는 쓸 수 없다"; exit 2 ;;
esac

if ! docker compose ps --status running mosquitto 2>/dev/null | grep -q mosquitto; then
    echo "!! mosquitto 컨테이너가 떠 있지 않다. 먼저:  docker compose up -d mosquitto"
    exit 1
fi

PASSWD=/mosquitto/data/passwd

docker compose exec -T mosquitto sh -c "
    set -e
    [ -f $PASSWD ] || : > $PASSWD
    hash=\$(/mosquitto/pw -p '$PASSWORD')
    { grep -v '^$USER_NAME:' $PASSWD || true; echo \"$USER_NAME:\$hash\"; } > $PASSWD.tmp
    chown mosquitto:mosquitto $PASSWD.tmp
    chmod 600 $PASSWD.tmp
    mv $PASSWD.tmp $PASSWD
"

echo "계정 '$USER_NAME' 설치. 현재 계정:"
docker compose exec -T mosquitto sh -c "cut -d: -f1 $PASSWD | sed 's/^/   /'"
echo
echo "go-auth 는 HUP 로 passwd 를 다시 읽지 않는다 — 브로커를 재시작한다(접속 중인 단말 전부 재접속)."
docker compose restart mosquitto
echo
echo "확인:  bash scripts/mqtt-sub.sh -u '$USER_NAME' -P '<pw>' -t 'iotlight/#' -v"
