#!/usr/bin/env bash
#
# 배포 (서버에서 실행. 담당자가 돌린다 — 개발자는 서버에 직접 접근하지 않는다)
#
#   bash scripts/deploy.sh                 평소 배포: pull → pull mosquitto → build(backend, web) → migrate → up → 점검
#   bash scripts/deploy.sh --no-pull       현재 체크아웃 그대로 재빌드·재기동
#   bash scripts/deploy.sh --infra-only    backend 없이 mosquitto/postgres 만 (1차, 또는 conf 만 바꿨을 때)
#
# 순서에 이유가 있다:
#   1. 마이그레이션을 컨테이너 교체 *전에* 돌린다
#        실패하면 기존 컨테이너가 그대로 살아 있어 서비스가 안 죽는다.
#        교체부터 하면 새 코드가 옛 스키마 위에서 도는 구간이 생긴다.
#   2. mosquitto 는 conf/entrypoint 가 바뀌었을 때만 재생성된다(compose 가 판단).
#        재생성되면 단말 1만 대가 전부 재접속한다 — 단말 backoff 지터가 있는지
#        확인 전엔 낮 시간에 하지 말 것(docs/00 §7).
#   3. 헬스체크로 확인하고, 실패하면 되돌릴 방법을 알려준다
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PULL=1; INFRA_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --no-pull)    PULL=0 ;;
        --infra-only) INFRA_ONLY=1 ;;
        *) echo "알 수 없는 옵션: $arg"; exit 2 ;;
    esac
done

log() { echo -e "\n\033[1m== $*\033[0m"; }

[ -f .env ] || { echo "!! .env 가 없다. cp .env.example .env 후 값을 채울 것."; exit 1; }
[ -f backend/Dockerfile ] || INFRA_ONLY=1   # backend 가 아직 없으면 자동으로 인프라만

PREV_SHA="$(git rev-parse --short HEAD)"

# ── 1. 코드 갱신 ────────────────────────────────────────────────────
if [ "$PULL" -eq 1 ]; then
    log "코드 갱신"
    git pull --ff-only
fi
NEW_SHA="$(git rev-parse --short HEAD)"
echo "   $PREV_SHA → $NEW_SHA"

if [ "$INFRA_ONLY" -eq 1 ]; then
    # ── 인프라만 ────────────────────────────────────────────────────
    log "mosquitto / postgres 갱신 (backend 제외)"
    docker compose up -d mosquitto postgres
else
    # ── 2. 빌드 ─────────────────────────────────────────────────────
    log "mosquitto 이미지 갱신"
    docker compose pull mosquitto
    log "backend / web 이미지 빌드"
    docker compose build backend web

    # ── 3. 마이그레이션 (컨테이너 교체 전) ──────────────────────────
    log "DB 마이그레이션"
    if ! docker compose run --rm --no-deps backend python -m alembic upgrade head; then
        echo "!! 마이그레이션 실패 — 컨테이너를 교체하지 않고 중단한다."
        echo "   현재 서비스는 이전 버전으로 계속 돌고 있다."
        exit 1
    fi

    # ── 4. 교체 ─────────────────────────────────────────────────────
    log "컨테이너 교체"
    docker compose up -d
fi

# ── 5. 확인 ─────────────────────────────────────────────────────────
log "헬스체크"
sleep 3
docker compose ps
echo
if bash scripts/healthcheck.sh; then
    log "배포 완료 ($NEW_SHA)"
    exit 0
fi

echo
echo "!! 점검에서 이상이 보고됐다."
echo "   로그:      docker compose logs --tail 100 backend mosquitto web"
echo "   되돌리기:  git checkout $PREV_SHA && bash scripts/deploy.sh --no-pull"
exit 1
