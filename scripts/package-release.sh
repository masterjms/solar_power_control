#!/usr/bin/env bash
#
# 납품 묶음 만들기 (문제점 53번) — 인터넷이 막힌 기관 서버에도 설치할 수 있는 파일 하나.
#
#   bash scripts/package-release.sh            버전 = git 태그 또는 커밋
#   bash scripts/package-release.sh 1.0.0      버전 직접
#
# 결과: dist/iotlight-<버전>.tar.gz  (+ .sha256)
#   iotlight-<버전>/
#     images.tar.gz          쓰는 Docker 이미지 전부(backend·web 빌드본 + mosquitto·postgres·certbot·autoheal)
#     docker-compose*.yml    실행 구성
#     .env.example           설정 견본
#     infra/ scripts/        브로커 설정·인증서·백업·점검 스크립트
#     VERSION                버전
#     설치_가이드.md          docs/08 사본
#
# 이 PC(개발·빌드용)는 인터넷이 되어야 한다(이미지 받기·빌드). 설치하는 쪽은 인터넷이 없어도 된다.
# 설치·업데이트: 묶음을 풀고 scripts/install-offline.sh (docs/08).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VER="${1:-$(git describe --tags --always --dirty 2>/dev/null || echo dev)}"
NAME="iotlight-$VER"
OUT="dist/$NAME"
log() { echo -e "\n\033[1m== $*\033[0m"; }
docker info >/dev/null 2>&1 || { echo "!! Docker 가 꺼져 있다 — Docker 를 켠 뒤 다시"; exit 1; }

[ -z "$(git status --porcelain -- backend frontend docker-compose.yml infra scripts)" ] \
    || echo "!! 커밋 안 된 변경이 있다 — 묶음의 코드는 지금 작업 폴더 기준이다(버전 이름에 -dirty)."

export APP_VERSION="$VER"
export GIT_SHA="$(git rev-parse --short HEAD)"
PROFILES=(--profile local-db --profile certbot)

log "backend·web 빌드 ($VER)"
docker compose build backend web

log "외부 이미지 받기"
mapfile -t IMAGES < <(docker compose "${PROFILES[@]}" config --images | sort -u)
for img in "${IMAGES[@]}"; do
    case "$img" in iotlight-*) ;; *) docker pull -q "$img" >/dev/null && echo "   $img" ;; esac
done

log "묶음 만들기 → $OUT"
rm -rf "$OUT" && mkdir -p "$OUT"
docker save "${IMAGES[@]}" | gzip -1 > "$OUT/images.tar.gz"
# 실행에 필요한 파일만(소스·문서·시험 도구는 빼고). .gitignore 에 걸린 것(.env·인증서·비밀번호 파일)은 들어가지 않는다.
git ls-files -z --cached --others --exclude-standard -- \
        docker-compose.yml docker-compose.https.yml docker-compose.tls.yml .env.example infra scripts \
    | tar --null -T - -cf - | tar -x -C "$OUT"
cp "docs/08_설치_인계_가이드.md" "$OUT/설치_가이드.md"
echo "$VER" > "$OUT/VERSION"
printf '%s\n' "${IMAGES[@]}" > "$OUT/IMAGES"

tar -C dist -czf "dist/$NAME.tar.gz" "$NAME"
( cd dist && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )
rm -rf "$OUT"
log "완료: dist/$NAME.tar.gz ($(du -h "dist/$NAME.tar.gz" | cut -f1))"
cat "dist/$NAME.tar.gz.sha256"
