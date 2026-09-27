#!/bin/sh
# 관리 화면 HTTPS 스위치 (4차). nginx:alpine 템플릿은 envsubst 만 되고 조건문이 없어서
# 인증서 유무에 따른 분기를 여기서 한다. /docker-entrypoint.d/ 에서 30-htpasswd.sh 다음에 돈다.
#
#   인증서 없음 → 80 이 그대로 관리 화면(routes.conf). 443 없음.   (1차~3차, 로컬 개발)
#   인증서 있음 → 443 에 관리 화면, 80 은 acme-challenge 만 남기고 301 https.
#                 단 Host 가 localhost / 127.0.0.1 인 요청은 80 에서도 그대로 서빙한다 —
#                 compose healthcheck(wget 127.0.0.1)와 scripts/healthcheck.sh(curl localhost)가
#                 인증서 이름과 무관하게 계속 돌게 하려는 것. 브라우저는 도메인으로 오므로 리다이렉트된다.
#
# 인증서는 docker-compose.tls.yml 이 ./infra/certs 를 /etc/nginx/certs 로 물린다(기본 compose 는 안 물림).
# 파일 내용만 바뀌면(갱신) `docker compose exec web nginx -s reload` 로 충분하다 — 기존 접속을 끊지 않는다.
# 인증서가 처음 생기거나 없어지면(모드 전환) 컨테이너를 다시 띄워야 이 스크립트가 다시 돈다.
set -eu

CRT=/etc/nginx/certs/server.crt
KEY=/etc/nginx/certs/server.key
DIR=/etc/nginx/iotlight
TLS_CONF=/etc/nginx/conf.d/zz-tls.conf
mkdir -p "$DIR"

if [ -s "$CRT" ] && [ -s "$KEY" ] && [ -r "$CRT" ] && [ -r "$KEY" ]; then
    cat > "$DIR/http80.conf" <<'CONF'
# 자동 생성(40-tls.sh) — TLS 모드: 80 은 https 로 보낸다.
location / {
    return 301 https://$host$request_uri;
}
CONF
    cat > "$TLS_CONF" <<'CONF'
# 자동 생성(40-tls.sh) — 인증서가 있을 때만 존재한다.
server {
    listen 443 ssl;
    http2 on;
    server_name _;

    ssl_certificate     /etc/nginx/certs/server.crt;
    ssl_certificate_key /etc/nginx/certs/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:1m;
    ssl_session_timeout 1h;

    include /etc/nginx/iotlight/routes.conf;
}

# 컨테이너 안·호스트 로컬 점검용 평문 (Host: localhost / 127.0.0.1). 이유는 40-tls.sh 머리말.
server {
    listen 80;
    server_name localhost 127.0.0.1;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
        auth_basic off;
        default_type text/plain;
        try_files $uri =404;
    }

    include /etc/nginx/iotlight/routes.conf;
}
CONF
    echo "TLS 모드: 443 활성, 80 → https 리다이렉트 ($(openssl x509 -noout -subject -enddate -in "$CRT" 2>/dev/null | tr '\n' ' '))"
else
    if [ -e "$CRT" ] || [ -e "$KEY" ]; then
        echo "!! $CRT / $KEY 중 하나가 없거나 비었거나 읽을 수 없다 — 평문 모드로 기동한다" >&2
    fi
    cat > "$DIR/http80.conf" <<'CONF'
# 자동 생성(40-tls.sh) — 평문 모드: 80 이 관리 화면이다.
include /etc/nginx/iotlight/routes.conf;
CONF
    rm -f "$TLS_CONF"
    echo "평문 모드: 80 만 (인증서 없음 — 4차 전 정상)"
fi
