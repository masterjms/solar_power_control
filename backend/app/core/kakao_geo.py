"""카카오 로컬 주소 검색 → 법정동 트리 재료 (사양서 §3.10.5, ADR-005).

운영자는 법정동코드를 손으로 치지 않는다. 주소를 검색하면 카카오 응답의 **`address.b_code`**
(법정동) 앞 10자리와 시도·시군구·법정동 이름·좌표가 한꺼번에 나오고, 그것으로 트리를
find-or-create 한다. `h_code`(행정동)는 쓰지 않는다 — 행정동은 통폐합으로 바뀌지만 법정동은
거의 안 바뀌어 group_id 가 오래 간다.

REST 키는 서버 전용 비밀값이라 브라우저가 카카오를 직접 부르지 않고 이 프록시를 거친다.
키는 로그·응답·예외 메시지 어디에도 싣지 않는다(헤더로만 나간다).

응답 → GeoResult 매핑은 순수 함수(map_documents)라 fixture JSON 으로 시험한다.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import settings
from app.constants import BJD_CODE_RE
from app.errors import GeoUnavailable

log = logging.getLogger(__name__)

SEARCH_URL = "https://dapi.kakao.com/v2/local/search/address.json"
REGION_URL = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json"
COORD_ADDR_URL = "https://dapi.kakao.com/v2/local/geo/coord2address.json"
TIMEOUT_SEC = 5.0
#: 화면 드롭다운에 그 이상은 소음이다.
MAX_RESULTS = 10

#: 카카오 주소 검색은 시도를 줄여 쓴다("경기", "서울"). 트리에는 정식 이름으로 넣는다 — 화면
#: 표시와 역지오코딩(coord2regioncode, 정식 이름)이 같은 노드를 가리키게.
_SIDO_FULL = {
    "서울": "서울특별시",
    "부산": "부산광역시",
    "대구": "대구광역시",
    "인천": "인천광역시",
    "광주": "광주광역시",
    "대전": "대전광역시",
    "울산": "울산광역시",
    "세종": "세종특별자치시",
    "세종시": "세종특별자치시",
    "경기": "경기도",
    "강원": "강원특별자치도",
    "강원도": "강원특별자치도",
    "충북": "충청북도",
    "충남": "충청남도",
    "전북": "전북특별자치도",
    "전라북도": "전북특별자치도",
    "전남": "전라남도",
    "경북": "경상북도",
    "경남": "경상남도",
    "제주": "제주특별자치도",
    "제주도": "제주특별자치도",
}


class GeoResult(BaseModel):
    """검색 결과 한 건(docs/05). 화면이 고르면 이 값 그대로 from-address 의 `pick` 이 된다."""

    address_name: str
    #: 법정동코드 10자리(카카오 b_code 앞 10자리).
    bjd_code: str
    sido: str
    sigungu: str
    #: 법정동 이름(region_3depth_name). 리 주소면 "읍면 리" 가 함께 온다.
    dong: str
    lat: float
    lon: float


def normalize_sido(name: str) -> str:
    name = (name or "").strip()
    return _SIDO_FULL.get(name, name)


def map_document(doc: dict[str, Any]) -> GeoResult | None:
    """카카오 documents[i] → GeoResult. 법정동코드나 좌표가 없으면 None(트리에 못 넣는다).

    지번 `address` 블록에만 b_code 가 있다. 도로명만 있는 행(드묾)은 버린다.
    """
    addr = doc.get("address") or {}
    b_code = str(addr.get("b_code") or "")[:10]
    if not BJD_CODE_RE.match(b_code):
        return None
    try:
        # 카카오 좌표: x = 경도, y = 위도 (문자열).
        lon = float(doc.get("x") if doc.get("x") not in (None, "") else addr["x"])
        lat = float(doc.get("y") if doc.get("y") not in (None, "") else addr["y"])
    except (KeyError, TypeError, ValueError):
        return None
    sido = normalize_sido(str(addr.get("region_1depth_name") or ""))
    sigungu = str(addr.get("region_2depth_name") or "").strip()
    dong = str(addr.get("region_3depth_name") or "").strip()
    if not sido or not dong:
        return None
    if not sigungu:
        # 세종특별자치시는 시군구가 없다. 트리는 항상 3단이라 시도 이름을 시군구 자리에도 둔다.
        sigungu = sido
    return GeoResult(
        address_name=str(doc.get("address_name") or addr.get("address_name") or ""),
        bjd_code=b_code, sido=sido, sigungu=sigungu, dong=dong, lat=lat, lon=lon,
    )


def map_documents(data: dict[str, Any]) -> list[GeoResult]:
    """응답 전체 → 결과 목록. 같은 법정동이 여러 번 나오면(번지만 다름) 첫 것만."""
    out: list[GeoResult] = []
    seen: set[str] = set()
    for doc in data.get("documents") or []:
        if not isinstance(doc, dict):
            continue
        item = map_document(doc)
        if item is None or item.bjd_code in seen:
            continue
        seen.add(item.bjd_code)
        out.append(item)
    return out


async def _kakao_get(
    url: str, params: dict[str, Any], *, what: str, client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """카카오 로컬 GET 한 번. 키 없음·HTTP 오류·시간 초과는 전부 503 GEO_UNAVAILABLE."""
    key = settings.kakao_rest_api_key
    if not key:
        raise GeoUnavailable(
            "카카오 REST 키가 설정되지 않았습니다(KAKAO_REST_API_KEY).",
            detail={"reason": "NO_KEY"},
        )
    headers = {"Authorization": f"KakaoAK {key}"}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as own:
                resp = await own.get(url, params=params, headers=headers)
        else:
            resp = await client.get(url, params=params, headers=headers, timeout=TIMEOUT_SEC)
    except httpx.TimeoutException as e:
        log.warning("카카오 %s 시간 초과(%.0f초)", what, TIMEOUT_SEC)
        raise GeoUnavailable(f"카카오 {what} 시간이 초과됐습니다.",
                             detail={"reason": "TIMEOUT"}) from e
    except httpx.HTTPError as e:
        # 예외 문자열에 요청 헤더가 섞이지 않게 종류만 남긴다.
        log.warning("카카오 %s 연결 실패: %s", what, e.__class__.__name__)
        raise GeoUnavailable(f"카카오 {what} 서버에 연결하지 못했습니다.",
                             detail={"reason": "CONNECT"}) from e
    if resp.status_code != 200:
        # 401 키 오류, 403 앱 [카카오맵] 사용 설정 꺼짐, 429 쿼터 소진.
        log.warning("카카오 %s HTTP %s", what, resp.status_code)
        raise GeoUnavailable(
            f"카카오 {what}이 거부됐습니다(HTTP {resp.status_code}).",
            detail={"reason": "HTTP", "status": resp.status_code},
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise GeoUnavailable("카카오 응답을 읽지 못했습니다.", detail={"reason": "BODY"}) from e
    return data if isinstance(data, dict) else {}


async def search_address(query: str, *, client: httpx.AsyncClient | None = None) -> list[GeoResult]:
    """카카오 주소 검색."""
    data = await _kakao_get(SEARCH_URL, {"query": query, "size": MAX_RESULTS},
                            what="주소 검색", client=client)
    return map_documents(data)


def map_reverse(region: dict[str, Any], address: dict[str, Any], lat: float, lon: float) -> GeoResult | None:
    """coord2regioncode + coord2address 응답 → GeoResult(지도에서 핀을 옮긴 자리).

    법정동은 region_type "B" 문서의 code 앞 10자리. 주소 이름은 도로명이 있으면 도로명, 없으면 지번.
    법정동을 못 찾으면(바다 등) None."""
    doc = next((d for d in region.get("documents") or []
                if isinstance(d, dict) and d.get("region_type") == "B"), None)
    if doc is None:
        return None
    code = str(doc.get("code") or "")[:10]
    if not BJD_CODE_RE.match(code):
        return None
    sido = normalize_sido(str(doc.get("region_1depth_name") or ""))
    sigungu = str(doc.get("region_2depth_name") or "").strip() or sido
    dong = " ".join(x for x in (str(doc.get("region_3depth_name") or "").strip(),
                                str(doc.get("region_4depth_name") or "").strip()) if x)
    if not sido or not dong:
        return None
    name = str(doc.get("address_name") or "")
    for a in address.get("documents") or []:
        if not isinstance(a, dict):
            continue
        road = (a.get("road_address") or {}).get("address_name")
        jibun = (a.get("address") or {}).get("address_name")
        name = str(road or jibun or name)
        break
    return GeoResult(address_name=name, bjd_code=code, sido=sido, sigungu=sigungu, dong=dong,
                     lat=lat, lon=lon)


async def reverse(lat: float, lon: float, *, client: httpx.AsyncClient | None = None) -> GeoResult | None:
    """좌표 → 법정동·주소(지도 위치 보정). 카카오 좌표계 x = 경도, y = 위도."""
    params = {"x": lon, "y": lat}
    region = await _kakao_get(REGION_URL, params, what="좌표 변환", client=client)
    address = await _kakao_get(COORD_ADDR_URL, params, what="좌표 변환", client=client)
    return map_reverse(region, address, lat, lon)
