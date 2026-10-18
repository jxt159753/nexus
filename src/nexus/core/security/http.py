"""Session router and reusable HTTP/WS guards. No service or Node Link startup."""

import asyncio
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketException
from pydantic import Field, SecretStr, ValidationError

from nexus.contracts.base import StrictContract
from nexus.core.security.service import SecurityService
from nexus.core.security.types import AccessDenied, Principal

COOKIE = "nexus_session"


@dataclass(frozen=True)
class TransportPolicy:
    origins: frozenset[str]
    local_http: bool = False

    def __post_init__(self) -> None:
        if not self.origins:
            raise ValueError("explicit origins required")
        for origin in self.origins:
            parsed = urlsplit(origin)
            if parsed.netloc == "" or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("invalid origin")
            if parsed.scheme == "https" and not self.local_http:
                continue
            if not (self.local_http and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
                raise ValueError("HTTPS required except explicit loopback development")


class LoginBody(StrictContract):
    token: SecretStr = Field(min_length=43, max_length=128)


class SecurityHTTP:
    def __init__(self, service: SecurityService, policy: TransportPolicy):
        self.service = service
        self.policy = policy
        self.router = APIRouter()
        self.router.add_api_route("/api/v1/session", self.login, methods=["POST"])
        self.router.add_api_route("/api/v1/session", self.logout, methods=["DELETE"])
        self.router.add_api_route("/api/v1/session", self.session_info, methods=["GET"])

    async def session_info(self, request: Request, response: Response) -> dict:
        await self.owner_http(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            return await self.service.session_info(SecretStr(request.cookies.get(COOKIE, "")))
        except AccessDenied as exc:
            raise self._http_error(exc) from None

    def _transport(self, connection: Request | WebSocket, *, origin: bool) -> None:
        scheme = connection.scope["scheme"]
        host = connection.url.hostname
        if not (scheme in {"https", "wss"} or self.policy.local_http and scheme in {"http", "ws"} and host in {"127.0.0.1", "localhost", "::1"}):
            raise AccessDenied()
        if origin and connection.headers.get("origin") not in self.policy.origins:
            raise AccessDenied()

    @staticmethod
    def _http_error(exc: AccessDenied) -> HTTPException:
        return HTTPException(401 if exc.code == "AUTH_REQUIRED" else 429 if exc.code == "BUSY" else 403, detail=exc.code)

    async def login(self, request: Request, response: Response) -> dict:
        try:
            self._transport(request, origin=True)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 4096:
                    raise HTTPException(413, detail="INVALID_MESSAGE")
                body.extend(chunk)
            try:
                credentials = LoginBody.model_validate_json(body)
            except ValidationError:
                raise HTTPException(400, detail="INVALID_MESSAGE") from None
            session = await self.service.login(credentials.token)
        except AccessDenied as exc:
            raise self._http_error(exc) from None
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(COOKIE, session.token.get_secret_value(), max_age=8 * 60 * 60,
                            httponly=True, secure=not self.policy.local_http, samesite="strict", path="/")
        return {"csrf_token": session.csrf.get_secret_value(), "expires_at": session.expires_at.isoformat()}

    async def owner_http(self, request: Request) -> Principal:
        try:
            write = request.method not in {"GET", "HEAD", "OPTIONS"}
            self._transport(request, origin=write)
            csrf = request.headers.get("x-csrf-token")
            if write and not csrf:
                raise AccessDenied()
            return await self.service.authenticate_session(SecretStr(request.cookies.get(COOKIE, "")), csrf=SecretStr(csrf) if write else None)
        except AccessDenied as exc:
            raise self._http_error(exc) from None

    async def logout(self, request: Request, response: Response) -> dict:
        principal = await self.owner_http(request)
        try:
            await self.service.logout(principal)
        except AccessDenied as exc:
            raise self._http_error(exc) from None
        response.delete_cookie(COOKIE, path="/", secure=not self.policy.local_http, httponly=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return {"logged_out": True}

    async def browser_ws(self, websocket: WebSocket) -> Principal:
        try:
            self._transport(websocket, origin=True)
            return await self.service.authenticate_session(SecretStr(websocket.cookies.get(COOKIE, "")))
        except AccessDenied:
            raise WebSocketException(code=1008, reason="unauthorized") from None

    async def node_ws(self, websocket: WebSocket) -> Principal:
        try:
            self._transport(websocket, origin=False)
            authorization = websocket.headers.get("authorization", "")
            if not authorization.startswith("Bearer "):
                raise AccessDenied("AUTH_REQUIRED")
            return await self.service.authenticate_token(SecretStr(authorization[7:]), "node")
        except AccessDenied:
            raise WebSocketException(code=1008, reason="unauthorized") from None

    async def check_ws_message(self, websocket: WebSocket, principal: Principal) -> None:
        """Call before each operation; expired/revoked credentials close this connection."""
        try:
            await self.service.validate_principal(principal)
        except AccessDenied:
            raise WebSocketException(code=1008, reason="unauthorized") from None

    async def receive_text(self, websocket: WebSocket, principal: Principal) -> str:
        """Recheck idle connections at most every second and before each operation."""
        while True:
            await self.check_ws_message(websocket, principal)
            try:
                async with asyncio.timeout(1):
                    message = await websocket.receive_text()
            except TimeoutError:
                continue
            await self.check_ws_message(websocket, principal)
            return message
