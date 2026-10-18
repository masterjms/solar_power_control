"""GET /ota/<항목 id>/<uuid>/<token> — 단말이 pkg 를 받는 곳(문제점 56번, 사양서 §24.2·§24.5).

로그인 없이 연다(nginx /ota/ 는 관리 화면 IP 제한·로그인 밖). 대신
  · token 이 그 항목(단말·명령 하나)의 무작위 22자와 같아야 하고 — 주소가 새도 다른 단말·다른 명령으로는 못 받는다
  · 처음 보낸 때부터 2시간 안이고, 항목이 보냄·진행 중이어야 한다 — 끝난 뒤에는 못 받는다
  · 2시간 안에서는 몇 번이든 Range 로 받는다(조각마다 같은 주소로 약 48번, 끊기면 이어받기)
틀린 주소는 이유를 알리지 않고 404. 단말 모뎀은 인증서를 검증하지 못하므로 위조는 MQTT 로 준 sha256 이 막는다.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import UUID_RE
from app.core import ota_rules as orl
from app.db import get_db
from app.models.ota import OtaItem, OtaJob, OtaPackage

log = logging.getLogger(__name__)

router = APIRouter(tags=["ota"])

#: pkg 본문 캐시(파일은 올린 뒤 바뀌지 않는다). 10대 × 48조각을 DB 에서 매번 읽지 않게.
_cache: dict[int, bytes] = {}
_CACHE_MAX = 4


async def _pkg(db: AsyncSession, package_id: int) -> bytes | None:
    if package_id in _cache:
        return _cache[package_id]
    data = await db.scalar(select(OtaPackage.data).where(OtaPackage.id == package_id))
    if data is None:
        return None
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[package_id] = data.encode("ascii")
    return _cache[package_id]


def _plain(status: int, text: str = "", headers: dict[str, str] | None = None) -> Response:
    return Response(text, status_code=status, media_type="text/plain", headers=headers)


@router.api_route("/ota/{item_id}/{uuid}/{token}", methods=["GET", "HEAD"], include_in_schema=False)
async def download(item_id: int, uuid: str, token: str, request: Request,
                   db: AsyncSession = Depends(get_db)) -> Response:
    now = dt.datetime.now(dt.timezone.utc)
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    it = await db.get(OtaItem, item_id) if UUID_RE.match(uuid) else None
    if it is None or it.uuid != uuid or not orl.token_ok(it.token, token):
        log.warning("OTA 다운로드 거부(주소 틀림) #%s %s from %s", item_id, uuid, ip)
        return _plain(404, "not found")
    if it.status not in ("sending", "running") or not orl.url_valid(it.first_sent_at, now):
        log.warning("OTA 다운로드 거부(끝남·시간 지남 %s) #%d %s from %s", it.status, it.id, uuid, ip)
        return _plain(410, "gone")
    job = await db.get(OtaJob, it.job_id)
    body = await _pkg(db, job.package_id) if job and job.package_id else None
    if body is None:
        return _plain(410, "gone")
    size = len(body)
    try:
        rng = orl.parse_range(request.headers.get("range"), size)
    except ValueError:
        return _plain(416, "", {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
    await db.execute(update(OtaItem).where(OtaItem.id == it.id)
                     .values(downloads=OtaItem.downloads + 1))
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if rng is None:
        part, code = body, 200
    else:
        a, b = rng
        part, code = body[a:b + 1], 206
        headers["Content-Range"] = f"bytes {a}-{b}/{size}"
    headers["Content-Length"] = str(len(part))
    if request.method == "HEAD":
        return Response(status_code=code, media_type="text/plain", headers=headers)
    return Response(part, status_code=code, media_type="text/plain", headers=headers)
