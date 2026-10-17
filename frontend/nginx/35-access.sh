#!/bin/sh
# 관리 화면 접근 제한·보안 헤더 (문제점 54번 — 공공 기본 항목). /docker-entrypoint.d/ 에서 30 다음, 40-tls 앞에 돈다.
#
#   ADMIN_ALLOW_IPS   관리 화면·API 를 열 수 있는 IP(쉼표·공백으로 여러 개, CIDR 가능 — 예 "203.0.113.10, 10.20.0.0/16").
#                     비우면 전체 허용(지금 운영 기본). 이 서버 자신(127.0.0.1·::1)은 늘 허용(점검 스크립트).
#                     막지 않는 곳: Let's Encrypt 확인(/.well-known), 외부 감시(/healthz) — 둘 다 밖에서 와야 한다.
#
# 만드는 파일(routes.conf 가 include):
#   /etc/nginx/iotlight/admin-allow.conf       allow …; deny all;  (비었으면 주석만)
#   /etc/nginx/iotlight/security-headers.conf  nosniff·frame·referrer·permissions (40-tls.sh 가 TLS 모드면 HSTS 를 덧붙인다)
set -eu

DIR=/etc/nginx/iotlight
mkdir -p "$DIR"

ALLOW="$DIR/admin-allow.conf"
IPS="$(echo "${ADMIN_ALLOW_IPS:-}" | tr ',' ' ')"
if [ -z "$(echo "$IPS" | tr -d ' ')" ]; then
    echo "# 자동 생성(35-access.sh) — ADMIN_ALLOW_IPS 비어 있음: 전체 허용" > "$ALLOW"
    echo "관리 화면 접근: 전체 허용 (ADMIN_ALLOW_IPS 비어 있음)"
else
    {
        echo "# 자동 생성(35-access.sh) — ADMIN_ALLOW_IPS"
        echo "allow 127.0.0.1;"
        echo "allow ::1;"
        for ip in $IPS; do
            # IPv4/IPv6 와 CIDR 만 — 오타로 nginx 설정이 깨지지 않게
            if echo "$ip" | grep -Eq '^[0-9A-Fa-f:.]+(/[0-9]{1,3})?$'; then
                echo "allow $ip;"
            else
                echo "!! ADMIN_ALLOW_IPS 의 '$ip' 는 IP 가 아니다 — 건너뜀" >&2
            fi
        done
        echo "deny all;"
    } > "$ALLOW"
    echo "관리 화면 접근: $(grep -c '^allow' "$ALLOW")개 주소만 허용"
fi

# always = 오류 응답(401·404 등)에도 붙인다.
cat > "$DIR/security-headers.conf" <<'CONF'
# 자동 생성(35-access.sh) — 보안 헤더(문제점 54번). add_header 가 있는 location 은 서버 단계 헤더를 물려받지 않으므로
# routes.conf 가 그런 location 안에서도 이 파일을 다시 include 한다.
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "SAMEORIGIN" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
CONF
