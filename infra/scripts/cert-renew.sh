#!/usr/bin/env bash
#
# Let's Encrypt 갱신 + 적용 (4차, ADR-006). cron 으로 돈다(docs/04 §6.6).
#
#   bash infra/scripts/cert-renew.sh                  갱신 시도 → 바뀌었으면 infra/certs 에 설치 →
#                                                     mosquitto 재시작 + nginx reload
#   bash infra/scripts/cert-renew.sh --renew-only     갱신 시도만(설치·재시작 안 함). 낮 cron 용
#   bash infra/scripts/cert-renew.sh --install-only   갱신 없이 설치만(cert-issue.sh 가 부른다)
#   ... --no-restart                                  설치해도 컨테이너는 건드리지 않는다
#   CERT_DOMAIN=<도메인>                              lineage 지정(없으면 live/ 의 첫 도메인)
#
# 왜 "바뀌었을 때만" 재시작인가: mosquitto 2.0.15(go-auth 이미지)는 리스너 인증서를 실행 중에
# 다시 읽지 않는다(SIGHUP 은 conf 일부·로그만). 새 인증서를 쓰게 하려면 재시작해야 하고,
# 재시작 = 1만 대 단말 전부 재접속이다. certbot 은 만료 30일 전부터만 실제로 갱신하므로
# 재시작은 약 60일에 한 번, 03:30 KST cron 에서만 일어난다(낮 cron 은 --renew-only).
# 판정은 live/<도메인>/fullchain.pem 과 infra/certs/server.crt 의 sha256 비교다 — 낮에 갱신된
# 인증서는 그날 밤 03:30 실행에서 "다름" 으로 잡혀 설치·재시작된다.
#
# 파일 권한: letsencrypt/ 는 certbot 컨테이너(root)가 만든 0700 이라 호스트 사용자가 못 읽는다.
# 그래서 복사도 certbot 컨테이너 안에서 한다(/iotlight-certs = ./infra/certs).
#   server.crt 644 root:root, server.key 640 root:1000 (go-auth 이미지의 mosquitto = uid/gid 1000.
#   nginx 는 master 가 root 로 읽는다)
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

RENEW=1; INSTALL=1; RESTART=1
for arg in "$@"; do
    case "$arg" in
        --renew-only)   INSTALL=0 ;;
        --install-only) RENEW=0 ;;
        --no-restart)   RESTART=0 ;;
        *) echo "알 수 없는 옵션: $arg" >&2; exit 2 ;;
    esac
done

log() {
    echo "[$(date '+%F %T %Z')] $*"
    command -v logger >/dev/null 2>&1 && logger -t iotlight-cert -- "$*" || true
}

if [ "$RENEW" = 1 ]; then
    log "certbot renew 시도"
    if ! docker compose run --rm --no-deps --quiet-pull certbot renew --webroot -w /var/www/certbot --quiet; then
        log "!! certbot renew 실패 — docker compose run --rm certbot renew --dry-run 으로 원인 확인"
        exit 1
    fi
fi

[ "$INSTALL" = 1 ] || { log "갱신만(--renew-only) — 설치는 다음 03:30 실행에서"; exit 0; }

# certbot 컨테이너 안에서 비교·복사. 출력 마지막 줄 = CHANGED | UNCHANGED | NOCERT
OUT="$(docker compose run --rm --no-deps --quiet-pull --entrypoint sh certbot -c '
set -e
D="$1"
if [ -z "$D" ]; then D="$(ls /etc/letsencrypt/live 2>/dev/null | grep -v README | head -1)"; fi
L="/etc/letsencrypt/live/$D"
if [ -z "$D" ] || [ ! -f "$L/fullchain.pem" ]; then echo NOCERT; exit 0; fi
new="$(sha256sum "$L/fullchain.pem" | cut -d" " -f1)"
old="$(sha256sum /iotlight-certs/server.crt 2>/dev/null | cut -d" " -f1)"
echo "domain=$D new=${new:0:16} old=${old:0:16}"
if [ "$new" = "$old" ]; then echo UNCHANGED; exit 0; fi
cp "$L/fullchain.pem" /iotlight-certs/server.crt.tmp
cp "$L/privkey.pem"   /iotlight-certs/server.key.tmp
chown 0:0    /iotlight-certs/server.crt.tmp; chmod 644 /iotlight-certs/server.crt.tmp
chown 0:1000 /iotlight-certs/server.key.tmp; chmod 640 /iotlight-certs/server.key.tmp
mv /iotlight-certs/server.key.tmp /iotlight-certs/server.key
mv /iotlight-certs/server.crt.tmp /iotlight-certs/server.crt
echo CHANGED
' sh "${CERT_DOMAIN:-}" 2>&1)"
RC=$?
echo "$OUT" | sed 's/^/   /'
[ $RC -eq 0 ] || { log "!! 인증서 복사 실패"; exit 1; }
STATE="$(echo "$OUT" | tail -1 | tr -d '[:space:]')"

case "$STATE" in
    NOCERT)    log "!! live/ 에 인증서가 없다 — cert-issue.sh 를 먼저"; exit 1 ;;
    UNCHANGED) log "인증서 변화 없음 — 재시작 안 함"; exit 0 ;;
    CHANGED)   log "새 인증서 설치: $(openssl x509 -in infra/certs/server.crt -noout -enddate 2>/dev/null || echo '?')" ;;
    *)         log "!! 알 수 없는 결과: $STATE"; exit 1 ;;
esac

[ "$RESTART" = 1 ] || { log "--no-restart — 컨테이너는 그대로"; exit 0; }

RUNNING="$(docker compose ps --status running --services 2>/dev/null)"
if echo "$RUNNING" | grep -qx mosquitto; then
    # 리스너 인증서는 재시작해야 반영된다 → 단말 전부 재접속(단말 backoff 지터로 분산).
    log "mosquitto 재시작 (8883 새 인증서 반영, 단말 전부 재접속)"
    docker compose restart mosquitto
fi
if echo "$RUNNING" | grep -qx web; then
    # nginx 는 reload 로 인증서를 다시 읽는다. 기존 접속은 끊지 않는다.
    log "web nginx reload"
    docker compose exec -T web nginx -s reload || log "!! nginx reload 실패 — docker compose restart web"
fi
log "완료"
