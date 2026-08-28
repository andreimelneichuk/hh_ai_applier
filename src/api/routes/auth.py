import time
import logging
from fastapi import APIRouter, BackgroundTasks
from src.clients.browser import HHBrowserClient
from src.api.state import (
    pipeline_status,
    login_browser_active,
    last_login_check_time,
    cached_login_status,
    cached_user_info
)
import src.api.state as state

logger = logging.getLogger("AuthRoutes")
router = APIRouter(tags=["Auth"])

def run_login_browser_task():
    """Фоновая задача для запуска браузера авторизации."""
    state.login_browser_active = True
    try:
        hh_client = HHBrowserClient()
        hh_client.open_login_browser()
        # Сбрасываем кэш, чтобы при следующем запросе проверилось мгновенно
        state.last_login_check_time = 0.0
    except Exception as e:
        logger.exception(f"Error in login browser task: {e}")
    finally:
        state.login_browser_active = False

@router.post("/api/browser/login")
def open_login_browser(background_tasks: BackgroundTasks):
    """Запускает видимый браузер для авторизации."""
    if state.login_browser_active:
        return {"status": "already_open"}
        
    background_tasks.add_task(run_login_browser_task)
    return {"status": "opened"}

@router.get("/api/status")
def get_status():
    """Проверяет состояние авторизации в браузере с надежным кешированием."""
    now = time.time()
    # Во время работы пайплайна или если уже авторизованы, не дергаем браузер повторно
    should_probe = (not state.cached_login_status and (now - state.last_login_check_time > 45))
    if state.pipeline_status.get("is_running"):
        should_probe = False
        
    if should_probe:
        hh_client = HHBrowserClient()
        try:
            state.cached_login_status = hh_client.is_logged_in()
            if state.cached_login_status:
                state.cached_user_info = hh_client.get_my_info()
            else:
                state.cached_user_info = None
            state.last_login_check_time = now
        finally:
            hh_client.stop()
        
    if not state.cached_login_status:
        return {
            "authorized": False,
            "login_active": state.login_browser_active,
            "user": None,
            "pipeline": state.pipeline_status
        }
        
    return {
        "authorized": True,
        "login_active": state.login_browser_active,
        "user": state.cached_user_info,
        "pipeline": state.pipeline_status
    }
