#!/usr/bin/env bash
#
# Let's Encrypt 최초 발급 (4차, ADR-006). 서버에서 담당자가 한 번 실행한다.
#
#   bash infra/scripts/cert-issue.sh <도메인> <이메일>
#   STAGING=1 bash infra/scripts/cert-issue.sh <도메인> <이메일>    # 연습(스테이징 CA, 발급 한도 소모 없음)
#
# 방식: certbot --webroot. HTTP-01 확인 요청은 이미 80 을 쓰고 있는 web(nginx) 의
#       /.well-known/acme-challenge/ 가 certbot-www 볼륨에서 답한다 — 서비스를 멈추지 않는다.
# 키: **RSA 2048** (--key-type rsa). certbot 2.x 기본은 ECDSA 인데 WD-N522S 모뎀의 ECDSA 지원이 확인되지
#     않아 단말 담당이 RSA 를 요구했다(문제점 #9, TLS_인증서_준비.md §2.2). 갱신 때도 이 설정이 유지된다.
# 결과: ./infra/letsencrypt/live/<도메인>/{fullchain,privkey}.pem
#       → ./infra/certs/server.crt (644) / server.key (640, root:1000 — go-auth 이미지 mosquitto uid 1000)
# 브로커·nginx 는 재시작하지 않는다. 적용은 런북 다음 단계(COMPOSE_FILE 에 tls 오버라이드 추가 →
# docker compose up -d) 에서 컨테이너가 다시 만들어지며 된다.
#
# 전제: DNS <도메인> → 이 서버 EIP, SG/ufw 80 인바운드 0.0.0.0/0, web 컨테이너 기동 중.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

DOMAIN="${1:-}"; EMAIL="${2:-}"
if [ -z "$DOMAIN" ] || [ -z "$EMAIL" ]; then
    echo "사용법: bash infra/scripts/cert-issue.sh <도메인> <이메일>" >&2
    exit 2
fi

log() { echo "[$(date '+%F %T')] $*"; }

mkdir -p infra/letsencrypt infra/certs

# ── 0. 사전 확인: web 이 떠 있고, 밖에서 도메인:80 의 acme 경로가 이 서버로 오는가 ──
if ! docker compose ps --status running --services 2>/dev/null | grep -qx web; then
    echo "!! web 컨테이너가 떠 있지 않다. docker compose up -d web 후 다시." >&2
    exit 1
fi
TOKEN="preflight-$(date +%s)"
docker compose run --rm --no-deps --quiet-pull --entrypoint sh certbot -c \
    "mkdir -p /var/www/certbot/.well-known/acme-challenge && echo $TOKEN > /var/www/certbot/.well-known/acme-challenge/$TOKEN" >/dev/null
GOT="$(curl -s --max-time 10 "http://$DOMAIN/.well-known/acme-challenge/$TOKEN" || true)"
docker compose run --rm --no-deps --quiet-pull --entrypoint sh certbot -c \
    "rm -f /var/www/certbot/.well-known/acme-challenge/$TOKEN" >/dev/null
if [ "$GOT" != "$TOKEN" ]; then
    echo "!! http://$DOMAIN/.well-known/acme-challenge/ 가 이 서버의 web 으로 오지 않는다." >&2
    echo "   DNS(A → EIP), SG/ufw 80, web 컨테이너를 확인할 것. 받은 응답: '${GOT:0:80}'" >&2
    exit 1
fi
log "사전 확인 통과: http://$DOMAIN/.well-known/acme-challenge/ → web"

# ── 1. 발급 ─────────────────────────────────────────────────────────
EXTRA=()
[ "${STAGING:-0}" = "1" ] && EXTRA+=(--staging) && log "STAGING=1 — 스테이징 CA(신뢰되지 않는 인증서, 연습용)"
docker compose run --rm --no-deps --quiet-pull certbot certonly \
    --webroot -w /var/www/certbot \
    -d "$DOMAIN" \
    --key-type rsa --rsa-key-size 2048 \
    --email "$EMAIL" --agree-tos --no-eff-email \
    --non-interactive --keep-until-expiring \
    "${EXTRA[@]}"

# ── 2. infra/certs 로 복사 (재시작 없음) ────────────────────────────
CERT_DOMAIN="$DOMAIN" bash infra/scripts/cert-renew.sh --install-only --no-restart

# ── 3. 키 종류 확인 — RSA 가 아니면(예: 예전에 ECDSA 로 받은 것이 남아 --keep-until-expiring 로 재사용됨) 멈춘다 ──
if ! openssl x509 -in infra/certs/server.crt -noout -text 2>/dev/null | grep -q "Public Key Algorithm: rsaEncryption"; then
    echo "!! 인증서 키가 RSA 가 아니다. 모뎀은 RSA 만 확인됐다. 다시 받기:" >&2
    echo "   docker compose run --rm --no-deps certbot certonly --webroot -w /var/www/certbot -d $DOMAIN \\" >&2
    echo "     --key-type rsa --rsa-key-size 2048 --force-renewal --non-interactive --agree-tos --email $EMAIL" >&2
    exit 1
fi

log "완료 (RSA 2048). 확인:"
echo "   openssl x509 -in infra/certs/server.crt -noout -subject -issuer -dates"
echo "   다음: docs/04 §6.3 '전환'(COMPOSE_FILE)"
