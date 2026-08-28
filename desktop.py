import os
import sys
import time
import socket
import threading
import logging
import requests
import uvicorn
import webview

from paths import get_bundle_dir, get_app_data_dir
import database
from app import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("HHDesktopApp")

def find_free_port(default_port: int = 8000) -> int:
    """Проверяет default_port, если занят — находит свободный динамический порт."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', default_port))
            return default_port
        except OSError:
            pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def run_uvicorn_server(host: str, port: int):
    """Запускает ASGI сервер FastAPI."""
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False
    )
    server = uvicorn.Server(config)
    server.run()

def wait_for_server(url: str, timeout: float = 12.0) -> bool:
    """Ожидает доступности локального сервера перед открытием окна."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            res = requests.get(url, timeout=0.6)
            if res.status_code in (200, 404):
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False

def main():
    logger.info(f"Запуск HH AI Applier Desktop...")
    logger.info(f"Директория данных: {get_app_data_dir()}")
    logger.info(f"Директория ресурсов: {get_bundle_dir()}")

    # Инициализация базы данных
    database.init_db()

    host = "127.0.0.1"
    port = find_free_port(8000)
    server_url = f"http://{host}:{port}"

    # Запускаем FastAPI в фоновом потоке
    server_thread = threading.Thread(target=run_uvicorn_server, args=(host, port), daemon=True)
    server_thread.start()

    # Ждем готовности сервера
    if not wait_for_server(server_url):
        logger.error(f"Не удалось запустить локальный сервер по адресу {server_url}")
        sys.exit(1)

    logger.info(f"Сервер готов: {server_url}. Открытие окна приложения...")

    # Создаем нативное окно через pywebview
    window = webview.create_window(
        title="HH.ru AI Job Applier",
        url=server_url,
        width=1280,
        height=850,
        min_size=(1024, 700),
        background_color="#0a0b10",
        text_select=True
    )

    # Запуск GUI цикла окна
    webview.start()
    logger.info("Приложение закрыто пользователем.")

if __name__ == "__main__":
    main()
