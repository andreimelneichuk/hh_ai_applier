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
        # Сразу после закрытия окна проверяем сессию и заполняем кэш
        try:
            is_auth, u_info = hh_client.check_session_and_get_info()
            state.cached_login_status = is_auth
            state.cached_user_info = u_info if is_auth else None
            state.last_login_check_time = time.time()
        except Exception as probe_err:
            logger.warning(f"Ошибка проверки сессии после закрытия окна: {probe_err}")
            state.last_login_check_time = 0.0
        finally:
            hh_client.stop()
    except Exception as e:
        logger.exception(f"Error in login browser task: {e}")
        state.last_login_check_time = 0.0
    finally:
        state.login_browser_active = False

@router.post("/api/browser/login")
def open_login_browser(background_tasks: BackgroundTasks):
    """Запускает видимый браузер для авторизации."""
    if state.login_browser_active:
        return {"status": "already_open"}
    if state.pipeline_status.get("is_running"):
        return {"status": "error", "message": "Идёт сканирование — остановите его перед повторным входом."}
        
    background_tasks.add_task(run_login_browser_task)
    return {"status": "opened"}

@router.get("/api/status")
def get_status():
    """Проверяет состояние авторизации в браузере с надежным кешированием."""
    now = time.time()
    # Во время активного окна логина, работы пайплайна или если уже авторизованы, не дергаем браузер повторно
    should_probe = (
        not state.cached_login_status
        and not state.login_browser_active
        and not state.pipeline_status.get("is_running")
        and not HHBrowserClient.is_busy()
        and (now - state.last_login_check_time > 45)
    )
        
    if should_probe:
        hh_client = HHBrowserClient()
        try:
            is_auth, u_info = hh_client.check_session_and_get_info()
            state.cached_login_status = is_auth
            state.cached_user_info = u_info if is_auth else None
            state.last_login_check_time = now
        except Exception as e:
            logger.warning(f"Ошибка проверки статуса авторизации: {e}")
        finally:
            hh_client.stop()
        
    if not state.cached_login_status:
        return {
            "authorized": False,
            "login_active": state.login_browser_active,
            "user": None,
            "pipeline": state.pipeline_snapshot()
        }
        
    return {
        "authorized": True,
        "login_active": state.login_browser_active,
        "user": state.cached_user_info,
        "pipeline": state.pipeline_snapshot()
    }
