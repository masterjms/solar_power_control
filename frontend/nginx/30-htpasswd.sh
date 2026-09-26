#!/bin/sh
# nginx:alpine 공식 entrypoint 가 /docker-entrypoint.d/ 의 스크립트를 이름순으로 실행한다
# (20-envsubst-on-templates.sh 다음). 여기서 .env 의 ADMIN_USER / ADMIN_PASSWORD 로
# /etc/nginx/.htpasswd 를 만든다. 비밀번호가 비었거나 견본 값이면 기동을 거부한다.
set -eu

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

if [ -z "$ADMIN_PASSWORD" ] || [ "$ADMIN_PASSWORD" = "change-me-admin" ]; then
    echo "!! ADMIN_PASSWORD 가 비어 있거나 견본 값(change-me-admin)이다. .env 에서 바꾼 뒤 다시 띄울 것." >&2
    exit 1
fi

HASH="$(openssl passwd -apr1 "$ADMIN_PASSWORD")"
# worker 프로세스는 nginx 사용자(uid 101)로 돈다 — 파일은 root 소유, 그룹 nginx 읽기만 허용
printf '%s:%s\n' "$ADMIN_USER" "$HASH" > /etc/nginx/.htpasswd
chown root:nginx /etc/nginx/.htpasswd
chmod 640 /etc/nginx/.htpasswd
echo "htpasswd 생성: 사용자 $ADMIN_USER"
