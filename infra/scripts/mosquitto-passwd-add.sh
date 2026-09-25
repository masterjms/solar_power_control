#!/usr/bin/env bash
#
# mosquitto 계정 하나 추가/갱신 (1차 — 백엔드가 없을 때 쓰는 수동 경로)
#
#   bash infra/scripts/mosquitto-passwd-add.sh <username> <password>
#   bash infra/scripts/mosquitto-passwd-add.sh solarlte-test solarlte-test-2026
#
# 하는 일:
#   1. 실행 중인 mosquitto 컨테이너 안에서 mosquitto_passwd -b 로
#      /mosquitto/data/passwd 에 계정을 넣는다 (있으면 비밀번호 갱신)
#   2. 소유·권한을 mosquitto 0600 으로 되돌린다
#      (exec 는 root 로 돌아서 mosquitto_passwd 가 파일을 root 소유로 다시 만든다.
#       그대로 두면 mosquitto(uid 1883) 가 리로드 때 못 읽는다)
#   3. SIGHUP 으로 리로드 — 접속 유지한 채 계정만 다시 읽는다
#   4. 결과를 infra/mosquitto/config/passwd(시드, 커밋 안 됨)로 복사해 둔다
#      → 볼륨을 지우고 다시 띄워도 entrypoint 가 시드에서 복구
#
# 2차부터는 백엔드가 passwd.generated 를 내보내므로 여기서 넣은 계정은 다음
# generated 갱신 때 덮어써진다. server / solarlte-test 도 백엔드가 같이 넣는다.
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

# -b : 비밀번호를 인자로 (대화식 아님). -c 는 절대 쓰지 않는다 — 파일을 통째로 덮어쓴다.
docker compose exec -T mosquitto sh -c "
    set -e
    [ -f $PASSWD ] || : > $PASSWD
    mosquitto_passwd -b $PASSWD '$USER_NAME' '$PASSWORD'
    chown mosquitto:mosquitto $PASSWD
    chmod 600 $PASSWD
    kill -HUP 1
"

# 시드 갱신 (호스트 파일, .gitignore 대상)
mkdir -p infra/mosquitto/config
docker compose cp mosquitto:$PASSWD infra/mosquitto/config/passwd >/dev/null
chmod 600 infra/mosquitto/config/passwd

echo "계정 '$USER_NAME' 설치 + 리로드 완료. 현재 계정:"
docker compose exec -T mosquitto sh -c "cut -d: -f1 $PASSWD | sed 's/^/   /'"
echo
echo "확인:  bash scripts/mqtt-sub.sh -u '$USER_NAME' -P '<pw>' -t 'iotlight/#' -v"
