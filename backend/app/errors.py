"""API 에러 규약.

응답은 항상 이 모양이다:
    {"error": {"code": "DEVICE_NOT_FOUND", "message": "...", "detail": {...}}}

code 는 프론트(3차)가 분기할 안정적인 식별자다. message 는 사람이 읽는 한국어이고
언제든 바뀔 수 있으니 문자열 매칭을 하면 안 된다.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """도메인 에러의 베이스. 서비스 계층에서 던지면 핸들러가 응답으로 바꾼다."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"
    message: str = "잘못된 요청입니다."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.detail = detail or {}
        super().__init__(self.message)

    def to_response(self) -> JSONResponse:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return JSONResponse(status_code=self.status_code, content={"error": body})


class NotFound(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    message = "대상을 찾을 수 없습니다."


class DeviceNotFound(NotFound):
    code = "DEVICE_NOT_FOUND"
    message = "등록되지 않은 단말입니다."


class ProfileNotFound(NotFound):
    code = "PROFILE_NOT_FOUND"
    message = "없는 설정 프로필입니다."


class Conflict(ApiError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    message = "현재 상태와 충돌합니다."


class ProfileInUse(Conflict):
    code = "PROFILE_IN_USE"
    message = "단말이 쓰고 있는 프로필은 지울 수 없습니다."


class InvalidStateTransition(Conflict):
    """docs/05 상태 전이 표에 없는 전이. 예: RETIRED → ACTIVE 는 PENDING 을 거쳐야 한다."""

    code = "INVALID_STATE_TRANSITION"
    message = "허용되지 않는 상태 전이입니다."


class ValidationFailed(ApiError):
    # Starlette 이 상수명을 바꾸는 중이라(ENTITY -> CONTENT) 정수로 고정한다.
    status_code = 422
    code = "VALIDATION_FAILED"
    message = "입력값이 올바르지 않습니다."


class ServiceUnavailable(ApiError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "SERVICE_UNAVAILABLE"
    message = "일시적으로 사용할 수 없습니다."


class MqttUnavailable(ServiceUnavailable):
    """브로커와 끊긴 동안의 발행 시도. 조용히 삼키면 "보냈는데 아무 일도 없음"이 된다."""

    code = "MQTT_UNAVAILABLE"
    message = "MQTT 브로커에 연결되어 있지 않아 명령을 보낼 수 없습니다."


# ── 5차 (docs/05 "에러 코드 추가") ────────────────────────────────────────
class Forbidden(ApiError):
    """최고관리자 전용(전체 명령, 트리 편집)을 관리자가 부름 (ADR-005 권한)."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"
    message = "최고관리자만 할 수 있습니다."


class GeoUnavailable(ServiceUnavailable):
    """카카오 키 없음·카카오 오류·시간 초과. 운영자가 코드를 손으로 치게 두지 않는다(§3.10.5)."""

    code = "GEO_UNAVAILABLE"
    message = "주소 검색을 사용할 수 없습니다."


class RegionNotFound(NotFound):
    code = "REGION_NOT_FOUND"
    message = "없는 법정동 트리 노드입니다."


class RegionInUse(Conflict):
    code = "REGION_IN_USE"
    message = "하위 노드나 배정된 단말이 있어 지울 수 없습니다."


class NodeNotLeaf(ValidationFailed):
    """단말은 말단 법정동(그룹)에만 넣는다(§3.9.3 #2)."""

    code = "NODE_NOT_LEAF"
    message = "단말은 말단 법정동에만 배정할 수 있습니다."


class NodeRequired(Conflict):
    """APPROVE_REQUIRES_NODE=true 인데 말단 없이 승인하려 함.
    §3.10.4 "주소 없는 단말은 두지 않는다" — 그룹 명령을 영영 못 받는다."""

    code = "NODE_REQUIRED"
    message = "승인하려면 먼저 말단 법정동을 배정해야 합니다."


class CommandNotFound(NotFound):
    code = "COMMAND_NOT_FOUND"
    message = "없는 명령입니다."


class CommandFinished(Conflict):
    code = "COMMAND_FINISHED"
    message = "이미 종료된 명령입니다."


class NoTargets(Conflict):
    """대상 범위 안에 ACTIVE 단말이 없다 — 보내도 받을 단말이 없다(PENDING 등은 STATE 로 거부)."""

    code = "NO_TARGETS"
    message = "명령을 받을 승인(ACTIVE) 단말이 없습니다."


class PayloadTooLarge(ApiError):
    """MQTT payload 가 단말 수신 한계(900B, ADR-007)를 넘었을 때. 보내봐야 못 받으므로
    발행 전에 막는다."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "MQTT_PAYLOAD_TOO_LARGE"
    message = "payload 가 단말 수신 한계를 초과했습니다."


# ── S-23 단말 설정 (docs/05 "단말 설정 API", ADR-007) ──────────────────────
class InvalidState(Conflict):
    """승인 상태가 요청을 허용하지 않음 — 읽기는 PENDING·ACTIVE, 쓰기는 ACTIVE 만(명세 8.1)."""

    code = "INVALID_STATE"
    message = "지금 승인 상태에서는 할 수 없습니다."


class SettingsPending(Conflict):
    code = "SETTINGS_PENDING"
    message = "이 단말에 응답을 기다리는 설정 요청이 있습니다."


class SettingsNotRead(Conflict):
    """한 번도 읽지 않은 단말에 쓰기 — 현장 설정을 덮어쓴다(명세 8.5 "읽고 나서 쓴다")."""

    code = "SETTINGS_NOT_READ"
    message = "단말 설정을 먼저 읽어야 합니다."


class SettingsNotChanged(Conflict):
    code = "SETTINGS_NOT_CHANGED"
    message = "받아들이거나 되돌릴 차이가 없습니다."


class SettingsIncomplete(ValidationFailed):
    code = "SETTINGS_INCOMPLETE"
    message = "25개 항목이 모두 있어야 합니다."


class SettingsRange(ValidationFailed):
    code = "SETTINGS_RANGE"
    message = "범위를 벗어난 항목이 있습니다."


class SettingsRule(ValidationFailed):
    code = "SETTINGS_RULE"
    message = "설정 규칙을 어겼습니다."


def register_exception_handlers(app: FastAPI) -> None:
    """앱 전역 예외 핸들러 등록. main.py 에서 한 번 호출한다."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI 기본 422 형식을 위 규약으로 통일한다.
        errors = jsonable_encoder(exc.errors())
        return ValidationFailed(detail={"errors": errors}).to_response()
