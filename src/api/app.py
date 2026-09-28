import os
import logging
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.core.paths import get_bundle_dir
from src.db import database
from src.api.security import API_TOKEN, local_only_middleware
from src.clients.browser import BrowserBusyError
from src.api.routes import (
    auth_router,
    settings_router,
    vacancies_router,
    pipeline_router
)

logger = logging.getLogger("HHWebServer")

def create_app() -> FastAPI:
    """Создает и настраивает экземпляр FastAPI приложения."""
    app = FastAPI(
        title="HeadHunter Job Applier Dashboard",
        description="Модульный ассистент поиска и авто-откликов для hh.ru",
        version="1.0.3"
    )

    # Инициализация базы данных
    database.init_db()

    # Только локальные запросы с токеном приложения (CORS намеренно не включаем:
    # фронтенд работает с того же origin, а сторонним сайтам доступ не нужен)
    app.middleware("http")(local_only_middleware)

    @app.exception_handler(BrowserBusyError)
    async def browser_busy_handler(request, exc: BrowserBusyError):
        return JSONResponse(status_code=409, content={"detail": str(exc), "message": str(exc)})

    # Подключение роутеров
    app.include_router(auth_router)
    app.include_router(settings_router)
    app.include_router(vacancies_router)
    app.include_router(pipeline_router)

    # Статические файлы фронтенда
    static_dir = os.path.join(get_bundle_dir(), "static")
    if not os.path.exists(static_dir):
        os.makedirs(static_dir, exist_ok=True)

    @app.get("/")
    def read_root():
        """Главная страница веб-интерфейса."""
        index_path = os.path.join(static_dir, "index.html")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                html = f.read()
            token_meta = f'<meta name="app-token" content="{API_TOKEN}">'
            html = html.replace("<head>", f"<head>\n    {token_meta}", 1)
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        return {"message": "HH AI Applier API is running. UI not found in static/"}

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app

app = create_app()
