# 관리 화면 이미지: Vite 빌드(dist/) → nginx 가 정적 서빙 + /api,/health 를 backend 로 프록시.
# 전 경로 HTTP Basic auth (로그인 기능이 붙기 전까지의 임시 보호). 자세한 건 nginx/ 참고.

# ── 1단계: 빌드 ─────────────────────────────────────────────────────
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# ── 2단계: 서빙 ─────────────────────────────────────────────────────
FROM nginx:alpine
# htpasswd 생성용(openssl passwd -apr1). nginx:alpine 에는 openssl 바이너리가 없다(2026-09 실측).
# 기본 server 블록 제거 — 우리 템플릿이 유일한 server 가 되게
RUN apk add --no-cache openssl && rm -f /etc/nginx/conf.d/default.conf
# 공식 entrypoint 가 /etc/nginx/templates/*.template 를 envsubst 해서 conf.d/ 에 놓는다
COPY nginx/nginx.conf.template /etc/nginx/templates/default.conf.template
# 공통 라우트(80·443 공용). 템플릿이 아니다(envsubst 안 거침).
COPY nginx/routes.conf /etc/nginx/iotlight/routes.conf
# /docker-entrypoint.d/ 의 스크립트는 envsubst(20-envsubst-on-templates.sh) 뒤에 이름순으로 실행된다
#   30-htpasswd.sh  .env 계정(ADMIN_*, OPERATOR_*) → /etc/nginx/.htpasswd
#   40-tls.sh       /etc/nginx/certs/server.crt 유무로 평문/TLS 모드 결정(4차)
COPY nginx/30-htpasswd.sh /docker-entrypoint.d/30-htpasswd.sh
COPY nginx/40-tls.sh /docker-entrypoint.d/40-tls.sh
# Windows 체크아웃이면 CRLF 가 섞일 수 있다 — sh 가 못 읽으므로 벗긴다.
# /var/www/certbot = certbot --webroot 토큰 자리(compose 가 certbot-www 볼륨을 물린다).
RUN sed -i 's/\r$//' /docker-entrypoint.d/30-htpasswd.sh /docker-entrypoint.d/40-tls.sh /etc/nginx/iotlight/routes.conf \
    && chmod +x /docker-entrypoint.d/30-htpasswd.sh /docker-entrypoint.d/40-tls.sh \
    && mkdir -p /var/www/certbot
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80 443
