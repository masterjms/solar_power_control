#!/usr/bin/env bash
#
# 납품 묶음 설치·업데이트 (문제점 53번) — 인터넷 없이. 묶음을 푼 폴더에서 실행한다.
#
#   처음:     tar -xzf iotlight-<버전>.tar.gz && cd iotlight-<버전> && bash scripts/install-offline.sh
#             (처음엔 .env 를 만들고 멈춘다 → 값을 채우고 다시 실행)
#   업데이트: 쓰던 폴더 위에 새 묶음을 덮어 푼다(.env·infra/certs 는 묶음에 없으므로 그대로 남는다)
#             tar -xzf iotlight-<새버전>.tar.gz --strip-components=1 -C /opt/solar_power_control
#             cd /opt/solar_power_control && bash scripts/install-offline.sh
#
# 하는 일: 이미지 불러오기(docker load) → .env 의 APP_VERSION 을 이 묶음 버전으로 → 컨테이너 교체(빌드·내려받기 없음)
#          → 점검. DB 마이그레이션은 backend 가 뜰 때 스스로 한다(backend/entrypoint.sh).
# 필요한 것: Docker Engine + compose 플러그인(기관·클라우드가 설치, 또는 infra/scripts/setup-server.sh).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

log() { echo -e "\n\033[1m== $*\033[0m"; }
[ -f VERSION ] && [ -f images.tar.gz ] || { echo "!! VERSION·images.tar.gz 가 없다 — 납품 묶음을 푼 폴더에서 실행할 것"; exit 1; }
VER="$(cat VERSION)"
docker compose version >/dev/null 2>&1 || { echo "!! Docker / compose 플러그인이 없다 — docs/08 의 '준비' 참고"; exit 1; }

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "!! .env 를 새로 만들었다. 비밀번호·키·도메인을 채운 뒤 다시 실행할 것(docs/08 '설정')."
    exit 1
fi

# 인증서 자리 — 비어 있어도 마운트되어야 한다(인증서 전엔 비어 있는 게 정상, docs/04 §6)
mkdir -p infra/certs infra/letsencrypt

log "이미지 불러오기 ($VER)"
gunzip -c images.tar.gz | docker load | sed 's/^/   /'

# compose 가 이 버전의 backend·web 이미지를 쓰도록
if grep -q '^APP_VERSION=' .env; then
    sed -i "s/^APP_VERSION=.*/APP_VERSION=$VER/" .env
else
    printf '\n# 납품 묶음 버전(install-offline.sh 가 맞춘다)\nAPP_VERSION=%s\n' "$VER" >> .env
fi

log "컨테이너 교체"
docker compose up -d --no-build --pull never

log "점검 — backend 가 뜰 때까지(첫 설치는 DB 준비·구조 만들기로 1~2분)"
for _ in $(seq 1 60); do
    st="$(docker inspect -f '{{.State.Health.Status}}' iotlight-backend 2>/dev/null || echo none)"
    [ "$st" = healthy ] && break
    sleep 3
done
echo "   backend: ${st:-?}"
docker compose ps
if bash scripts/healthcheck.sh; then
    log "설치 완료 ($VER)"
else
    echo "!! 점검에서 이상 — docker compose logs backend --tail 50 으로 확인"
    exit 1
fi
