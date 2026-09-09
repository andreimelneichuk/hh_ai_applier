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
    mistral_model: str = "open-mistral-nemo"
    openai_api_keys: Optional[str] = ""
    openai_provider_preset: Optional[str] = "groq"
    openai_base_url: Optional[str] = "https://api.groq.com/openai/v1"
    openai_model: Optional[str] = "llama-3.3-70b-versatile"
    stop_condition: Optional[str] = "both"
    limit_applications: Optional[int] = 10
    limit_processed: Optional[int] = 20

class SystemSettingsPayload(BaseModel):
    system_prompt: Optional[str] = None
    cover_letter_postfix: Optional[str] = None
    primary_provider: Optional[str] = "gemini"
    fallback_enabled: Optional[bool] = True
    temperature: Optional[float] = 0.2
    gemini_model: Optional[str] = None
    mistral_model: Optional[str] = None
    openai_provider_preset: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_model: Optional[str] = None
    openai_api_keys: Optional[str] = None

class UserProfileAnswerPayload(BaseModel):
    key: str
    question_hint: str
    answer: str

class ModelSyncPayload(BaseModel):
    provider: Optional[str] = "all"

class ModelSelectPayload(BaseModel):
    provider: str
    model_id: str

class ProviderConfigPayload(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_keys: Optional[Any] = None  # List[str] or multiline/comma string
    active_model: Optional[str] = None
    temperature: Optional[float] = None
    is_enabled: Optional[bool] = None
    description: Optional[str] = None
    get_key_url: Optional[str] = None

class ProviderProbePayload(BaseModel):
    api_key: Optional[str] = None

class CustomProviderCreatePayload(BaseModel):
    name: str
    base_url: str = "http://localhost:11434/v1"
    api_keys: Optional[Any] = []
    active_model: Optional[str] = ""
    temperature: Optional[float] = 0.2
    is_enabled: Optional[bool] = True
    description: Optional[str] = "Пользовательский OpenAI-совместимый провайдер"
    get_key_url: Optional[str] = ""


class QuickApplyPayload(BaseModel):
    url_or_id: str
    resume_id: Optional[str] = None

class ApplyPayload(BaseModel):
    vacancy_id: str
    resume_id: str
    cover_letter: str
    answers: Optional[Dict[str, Any]] = None

class SaveDraftPayload(BaseModel):
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
