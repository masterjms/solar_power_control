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


class PayloadTooLarge(ApiError):
    """MQTT payload 가 단말 AT 버퍼(384B)를 넘었을 때. 보내봐야 못 받으므로 발행 전에 막는다."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "MQTT_PAYLOAD_TOO_LARGE"
    message = "payload 가 단말 수신 한계를 초과했습니다."


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
