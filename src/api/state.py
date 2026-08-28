import asyncio
import queue
import threading
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

# Глобальный статус выполнения фонового поиска
pipeline_status = {
    "is_running": False,
    "stop_requested": False,
    "last_run_stats": None,
    "last_error": None,
    "last_status": None,
    "currently_processing": None
}

# Флаг открытия браузера для входа
login_browser_active = False

# Кэширование статуса входа
last_login_check_time = 0.0
cached_login_status = False
cached_user_info = None

class SearchSettings(BaseModel):
    queries: List[str] = []
    area_id: str
    threshold: int
    resume_id: str
    dry_run: bool
    gemini_api_keys: str = ""
    gemini_model: str = "gemini-3.6-flash"
    mistral_api_keys: str = ""
    mistral_model: str = "mistral-small-latest"

class SystemSettingsPayload(BaseModel):
    system_prompt: Optional[str] = None
    primary_provider: Optional[str] = "gemini"
    fallback_enabled: Optional[bool] = True
    temperature: Optional[float] = 0.2
    gemini_model: Optional[str] = None
    mistral_model: Optional[str] = None

class UserProfileAnswerPayload(BaseModel):
    key: str
    question_hint: str
    answer: str

class QuickApplyPayload(BaseModel):
    url_or_id: str
    resume_id: Optional[str] = None

class ApplyPayload(BaseModel):
    vacancy_id: str
    resume_id: str
    cover_letter: str
    answers: Optional[Dict[str, Any]] = None

async def run_in_clean_thread(func, *args, **kwargs):
    """Выполняет синхронную функцию в чистом потоке без asyncio-окружения."""
    q = queue.Queue()
    
    def worker():
        try:
            res = func(*args, **kwargs)
            q.put((True, res))
        except Exception as err:
            q.put((False, err))
            
    thread = threading.Thread(target=worker)
    thread.start()
    
    while thread.is_alive():
        await asyncio.sleep(0.05)
        
    success, val = q.get()
    if not success:
        raise val
    return val
