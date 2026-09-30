#!/bin/sh
# nginx:alpine 공식 entrypoint 가 /docker-entrypoint.d/ 의 스크립트를 이름순으로 실행한다
# (20-envsubst-on-templates.sh 다음). 여기서 .env 의 계정을 검사하고 /etc/nginx/.htpasswd 를 만든다.
# 문제점 16번부터 로그인 판정은 backend(/api/auth/check, 로그인 화면·세션)가 하고 nginx 는 이 파일을 읽지 않는다.
# 계정이 비었거나 견본 값이면 기동을 거부하는 점검 역할로 남겨 둔다(파일은 롤백 대비).
#
#   ADMIN_USER / ADMIN_PASSWORD        필수. 비었거나 견본 값이면 기동을 거부한다.
#   OPERATOR_USER / OPERATOR_PASSWORD  선택. 두 번째 계정(최고관리자가 아닌 관리자).
#                                      둘 다 채웠을 때만 추가한다.
#
# 누가 최고관리자인지는 여기서 정하지 않는다 — nginx 는 인증된 사용자명을 X-Remote-User 로
# backend 에 넘기고(routes.conf), backend 가 .env SUPER_ADMIN_USERS 로 판정한다(ADR-005).
set -eu

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
OPERATOR_USER="${OPERATOR_USER:-}"
OPERATOR_PASSWORD="${OPERATOR_PASSWORD:-}"

if [ -z "$ADMIN_PASSWORD" ] || [ "$ADMIN_PASSWORD" = "change-me-admin" ]; then
    echo "!! ADMIN_PASSWORD 가 비어 있거나 견본 값(change-me-admin)이다. .env 에서 바꾼 뒤 다시 띄울 것." >&2
    exit 1
fi

# 사용자명에 ':' 이 들어가면 htpasswd 줄이 깨진다
case "$ADMIN_USER$OPERATOR_USER" in
    *:*) echo "!! ADMIN_USER / OPERATOR_USER 에 ':' 을 쓸 수 없다." >&2; exit 1 ;;
esac

TMP=/etc/nginx/.htpasswd.tmp
printf '%s:%s\n' "$ADMIN_USER" "$(openssl passwd -apr1 "$ADMIN_PASSWORD")" > "$TMP"
USERS="$ADMIN_USER"

if [ -n "$OPERATOR_USER" ] || [ -n "$OPERATOR_PASSWORD" ]; then
    if [ -z "$OPERATOR_USER" ] || [ -z "$OPERATOR_PASSWORD" ]; then
        echo "!! OPERATOR_USER 와 OPERATOR_PASSWORD 는 둘 다 채우거나 둘 다 비워야 한다." >&2
        rm -f "$TMP"; exit 1
    fi
    if [ "$OPERATOR_USER" = "$ADMIN_USER" ]; then
        echo "!! OPERATOR_USER 가 ADMIN_USER 와 같다($ADMIN_USER). 다른 이름을 쓸 것." >&2
        rm -f "$TMP"; exit 1
    fi
    printf '%s:%s\n' "$OPERATOR_USER" "$(openssl passwd -apr1 "$OPERATOR_PASSWORD")" >> "$TMP"
    USERS="$USERS $OPERATOR_USER"
fi

# worker 프로세스는 nginx 사용자(uid 101)로 돈다 — 파일은 root 소유, 그룹 nginx 읽기만 허용
chown root:nginx "$TMP"
chmod 640 "$TMP"
mv "$TMP" /etc/nginx/.htpasswd
echo "htpasswd 생성: 사용자 $USERS"
