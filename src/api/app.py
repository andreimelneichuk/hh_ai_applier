import os
import logging
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from src.core.paths import get_bundle_dir
from src.db import database
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
        version="1.0.0"
    )

    # Инициализация базы данных
    database.init_db()

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
            return FileResponse(index_path)
        return {"message": "HH AI Applier API is running. UI not found in static/"}

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app

app = create_app()
