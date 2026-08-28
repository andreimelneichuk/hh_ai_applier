import os
from fastapi import APIRouter
from src.core.config import Config
from src.db import database
from src.clients.browser import HHBrowserClient
from src.clients.llm import LLMAnalyzer
from src.api.state import SearchSettings, SystemSettingsPayload, UserProfileAnswerPayload

router = APIRouter(tags=["Settings"])

@router.get("/api/settings")
def get_settings():
    """Возвращает текущие настройки поиска из базы данных."""
    queries_str = database.get_config_value("search_queries")
    if queries_str is not None:
        queries = [q.strip() for q in queries_str.split(",") if q.strip()]
    else:
        queries = [q.strip() for q in Config.SEARCH_QUERIES if q.strip()] if Config.SEARCH_QUERIES else []
        
    area_id = database.get_config_value("search_area") or Config.SEARCH_AREA
    
    threshold_str = database.get_config_value("match_threshold")
    threshold = int(threshold_str) if threshold_str else Config.MATCH_THRESHOLD
    
    resume_id = database.get_config_value("resume_id") or Config.HH_RESUME_ID
    if resume_id.startswith("your_"):
        resume_id = ""
        
    dry_run_str = database.get_config_value("dry_run")
    if dry_run_str:
        dry_run = dry_run_str.lower() in ("true", "1", "yes")
    else:
        dry_run = Config.DRY_RUN
        
    gemini_api_keys = database.get_config_value("gemini_api_keys") or os.getenv("GEMINI_API_KEYS", "") or Config.GEMINI_API_KEY
    if gemini_api_keys.startswith("your_"):
        gemini_api_keys = ""

    gemini_model = database.get_config_value("gemini_model") or Config.GEMINI_MODEL or "gemini-3.6-flash"

    mistral_api_keys = database.get_config_value("mistral_api_keys") or os.getenv("MISTRAL_API_KEYS", "") or Config.MISTRAL_API_KEY
    if mistral_api_keys.startswith("your_"):
        mistral_api_keys = ""

    mistral_model = database.get_config_value("mistral_model") or Config.MISTRAL_MODEL or "mistral-small-latest"

    return {
        "queries": queries,
        "area_id": area_id,
        "threshold": threshold,
        "resume_id": resume_id,
        "dry_run": dry_run,
        "gemini_api_keys": gemini_api_keys,
        "gemini_model": gemini_model,
        "mistral_api_keys": mistral_api_keys,
        "mistral_model": mistral_model
    }

@router.post("/api/settings")
def save_settings(settings: SearchSettings):
    """Сохраняет настройки в базу данных."""
    database.set_config_value("search_queries", ",".join(settings.queries))
    database.set_config_value("search_area", settings.area_id)
    database.set_config_value("match_threshold", str(settings.threshold))
    database.set_config_value("resume_id", settings.resume_id)
    database.set_config_value("dry_run", str(settings.dry_run))
    if settings.gemini_model:
        database.set_config_value("gemini_model", settings.gemini_model)
    if settings.gemini_api_keys is not None:
        database.set_config_value("gemini_api_keys", settings.gemini_api_keys)
    if settings.mistral_model:
        database.set_config_value("mistral_model", settings.mistral_model)
    if settings.mistral_api_keys is not None:
        database.set_config_value("mistral_api_keys", settings.mistral_api_keys)
        
    # Сбрасываем кэш проверки LLM чтобы перепроверить новые ключи
    LLMAnalyzer._initialized_keys = False
    return {"status": "ok"}

@router.get("/api/system-settings")
def get_system_settings():
    """Возвращает системные настройки LLM, стратегию и редактируемый промпт."""
    all_settings = database.get_all_system_settings()
    gemini_model = database.get_config_value("gemini_model") or Config.GEMINI_MODEL or "gemini-3.6-flash"
    mistral_model = database.get_config_value("mistral_model") or Config.MISTRAL_MODEL or "mistral-small-latest"
    
    fallback_str = all_settings.get("fallback_enabled", "true")
    fallback_bool = fallback_str.lower() in ("true", "1", "yes") if fallback_str else True
    
    try:
        temp_float = float(all_settings.get("temperature", "0.2"))
    except (ValueError, TypeError):
        temp_float = 0.2

    return {
        "system_prompt": all_settings.get("system_prompt", database.DEFAULT_SYSTEM_PROMPT),
        "default_system_prompt": database.DEFAULT_SYSTEM_PROMPT,
        "primary_provider": all_settings.get("primary_provider", "gemini"),
        "fallback_enabled": fallback_bool,
        "temperature": temp_float,
        "gemini_model": gemini_model,
        "mistral_model": mistral_model
    }

@router.post("/api/system-settings")
def save_system_settings(payload: SystemSettingsPayload):
    """Сохраняет измененные системные настройки LLM."""
    if payload.system_prompt is not None:
        database.set_system_setting("system_prompt", payload.system_prompt)
    if payload.primary_provider is not None:
        database.set_system_setting("primary_provider", payload.primary_provider)
    if payload.fallback_enabled is not None:
        database.set_system_setting("fallback_enabled", str(payload.fallback_enabled).lower())
    if payload.temperature is not None:
        database.set_system_setting("temperature", str(payload.temperature))
    if payload.gemini_model:
        database.set_config_value("gemini_model", payload.gemini_model)
    if payload.mistral_model:
        database.set_config_value("mistral_model", payload.mistral_model)
        
    return {"status": "ok"}

@router.post("/api/system-settings/reset-prompt")
def reset_system_prompt():
    """Сбрасывает системный промпт к дефолтному заводскому виду."""
    default_prompt = database.reset_system_prompt_to_default()
    return {"status": "ok", "system_prompt": default_prompt}

@router.get("/api/user-profile-answers")
def get_user_profile_answers():
    """Возвращает список сохраненных ответов пользователя на частые вопросы работодателей."""
    answers = database.get_user_profile_answers()
    return {"answers": answers}

@router.post("/api/user-profile-answers")
def save_user_profile_answer(payload: UserProfileAnswerPayload):
    """Сохраняет или обновляет ответ в профиле пользователя."""
    database.set_user_profile_answer(payload.key, payload.question_hint, payload.answer)
    return {"status": "ok"}

@router.delete("/api/user-profile-answers/{key}")
def delete_user_profile_answer(key: str):
    """Удаляет сохраненный ответ из профиля пользователя."""
    database.delete_user_profile_answer(key)
    return {"status": "ok"}

@router.get("/api/models")
def get_available_models(provider: str = "all"):
    """Возвращает список доступных для текущих ключей моделей Gemini и Mistral."""
    analyzer = LLMAnalyzer()
    models = analyzer.get_available_models(provider=provider)
    if isinstance(models, list):
        return {"models": models}
    return models

@router.get("/api/model-status")
def get_model_status(probe: bool = False):
    """Возвращает статус доступности LLM (Gemini и Mistral API) и подробный статус ключей."""
    analyzer = LLMAnalyzer()
    res = analyzer.check_availability(force_probe=probe)
    all_keys = []
    if "gemini" in res and "keys" in res["gemini"]:
        all_keys.extend(res["gemini"]["keys"])
    if "mistral" in res and "keys" in res["mistral"]:
        all_keys.extend(res["mistral"]["keys"])
    res["keys"] = all_keys
    return res

@router.get("/api/resumes")
def get_resumes():
    """Возвращает список резюме со страницы пользователя."""
    hh_client = HHBrowserClient()
    try:
        resumes = hh_client.get_my_resumes()
    finally:
        hh_client.stop()
    return {"resumes": resumes}
