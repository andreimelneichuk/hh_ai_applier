"""
Защита локального API от обращений со сторонних сайтов.

Сервер слушает 127.0.0.1, поэтому любая вкладка браузера пользователя может
отправить на него запрос. Защищаемся двумя проверками:
  1. Host-заголовок должен быть локальным (защита от DNS rebinding —
     иначе чужой домен, указывающий на 127.0.0.1, стал бы "same-origin").
  2. Каждый запрос к /api/* обязан нести X-App-Token — случайный токен,
     который выдаётся только внутри index.html. Сторонний сайт прочитать
     его не может (Same-Origin Policy), поэтому не может и вызвать API (CSRF).
"""
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

API_TOKEN = secrets.token_urlsafe(32)
TOKEN_HEADER = "X-App-Token"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _hostname(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


async def local_only_middleware(request: Request, call_next):
    if _hostname(request.headers.get("host", "")) not in ALLOWED_HOSTS:
        return JSONResponse(status_code=403, content={"detail": "Forbidden host"})

    if request.url.path.startswith("/api/"):
        token = request.headers.get(TOKEN_HEADER, "")
        if not secrets.compare_digest(token, API_TOKEN):
            return JSONResponse(status_code=403, content={"detail": "Invalid or missing app token"})

    return await call_next(request)
