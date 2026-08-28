import os
import logging
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException, Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import Config
import database
from hh_browser_client import HHBrowserClient
from llm_analyzer import LLMAnalyzer
import main

# Настройка логирования для веб-сервера
logger = logging.getLogger("HHWebServer")

app = FastAPI(title="HeadHunter Job Applier Browser Dashboard")

# Убедимся, что БД инициализирована при старте
database.init_db()

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
    import queue
    import asyncio
    import threading
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


from paths import get_bundle_dir, get_app_data_dir

# Монтируем директорию для статических файлов
static_dir = os.path.join(get_bundle_dir(), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)

@app.get("/")
def read_root():
    """Служит главной страницей интерфейса."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
# Кэширование статуса входа
last_login_check_time = 0.0
cached_login_status = False
cached_user_info = None

def run_login_browser_task():
    """Фоновая задача для запуска браузера авторизации."""
    global login_browser_active, last_login_check_time
    login_browser_active = True
    try:
        hh_client = HHBrowserClient()
        hh_client.open_login_browser()
        # Сбрасываем кэш, чтобы при следующем запросе проверилось мгновенно
        last_login_check_time = 0.0
    except Exception as e:
        logger.exception(f"Error in login browser task: {e}")
    finally:
        login_browser_active = False

@app.post("/api/browser/login")
def open_login_browser(background_tasks: BackgroundTasks):
    """Запускает видимый браузер для авторизации."""
    global login_browser_active
    if login_browser_active:
        return {"status": "already_open"}
        
    background_tasks.add_task(run_login_browser_task)
    return {"status": "opened"}

@app.get("/api/status")
def get_status():
    """Проверяет состояние авторизации в браузере с надежным кешированием."""
    global last_login_check_time, cached_login_status, cached_user_info
    import time
    
    now = time.time()
    # Во время работы пайплайна или если уже авторизованы, не дергаем браузер повторно
    should_probe = (not cached_login_status and (now - last_login_check_time > 45))
    if pipeline_status.get("is_running"):
        should_probe = False
        
    if should_probe:
        hh_client = HHBrowserClient()
        try:
            cached_login_status = hh_client.is_logged_in()
            if cached_login_status:
                cached_user_info = hh_client.get_my_info()
            else:
                cached_user_info = None
            last_login_check_time = now
        finally:
            hh_client.stop()
        
    if not cached_login_status:
        return {
            "authorized": False,
            "login_active": login_browser_active,
            "user": None,
            "pipeline": pipeline_status
        }
        
    return {
        "authorized": True,
        "login_active": login_browser_active,
        "user": cached_user_info,
        "pipeline": pipeline_status
    }

@app.get("/api/model-status")
def get_model_status(probe: bool = False):
    """Возвращает статус доступности LLM (Gemini и Mistral API) и подробный статус ключей."""
    from llm_analyzer import LLMAnalyzer
    analyzer = LLMAnalyzer()
    res = analyzer.check_availability(force_probe=probe)
    # Для обратной совместимости добавляем плоский список keys
    all_keys = []
    if "gemini" in res and "keys" in res["gemini"]:
        all_keys.extend(res["gemini"]["keys"])
    if "mistral" in res and "keys" in res["mistral"]:
        all_keys.extend(res["mistral"]["keys"])
    res["keys"] = all_keys
    return res

@app.get("/api/resumes")
def get_resumes():
    """Возвращает список резюме со страницы пользователя."""
    hh_client = HHBrowserClient()
    try:
        resumes = hh_client.get_my_resumes()
    finally:
        hh_client.stop()
    return {"resumes": resumes}

@app.get("/api/settings")
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

@app.get("/api/models")
def get_available_models(provider: str = "all"):
    """Возвращает список доступных для текущих ключей моделей Gemini и Mistral."""
    from llm_analyzer import LLMAnalyzer
    analyzer = LLMAnalyzer()
    models = analyzer.get_available_models(provider=provider)
    if isinstance(models, list):
        return {"models": models}
    return models

@app.post("/api/settings")
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
    from llm_analyzer import LLMAnalyzer
    LLMAnalyzer._initialized_keys = False
    return {"status": "ok"}

@app.get("/api/system-settings")
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

@app.post("/api/system-settings")
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

@app.post("/api/system-settings/reset-prompt")
def reset_system_prompt():
    """Сбрасывает системный промпт к дефолтному заводскому виду."""
    default_prompt = database.reset_system_prompt_to_default()
    return {"status": "ok", "system_prompt": default_prompt}

@app.get("/api/user-profile-answers")
def get_user_profile_answers():
    """Возвращает список сохраненных ответов пользователя на частые вопросы работодателей."""
    answers = database.get_user_profile_answers()
    return {"answers": answers}

@app.post("/api/user-profile-answers")
def save_user_profile_answer(payload: UserProfileAnswerPayload):
    """Сохраняет или обновляет ответ в профиле пользователя."""
    database.set_user_profile_answer(payload.key, payload.question_hint, payload.answer)
    return {"status": "ok"}

@app.delete("/api/user-profile-answers/{key}")
def delete_user_profile_answer(key: str):
    """Удаляет сохраненный ответ из профиля пользователя."""
    database.delete_user_profile_answer(key)
    return {"status": "ok"}

def load_candidate_resumes(hh_client: HHBrowserClient, target_resume_id: str = None) -> List[Dict[str, Any]]:
    """Загружает список резюме кандидата (всех или конкретного) для анализа."""
    is_all = (not target_resume_id or target_resume_id.lower() in ("all", "__all__"))
    candidate_resumes = []
    if is_all:
        my_resumes = hh_client.get_my_resumes()
        for r in my_resumes:
            r_id = r.get("id")
            if not r_id:
                continue
            r_data = hh_client.get_resume(r_id)
            r_text = main.format_hh_resume_to_text(r_data) if r_data else ""
            if r_text:
                candidate_resumes.append({
                    "id": r_id,
                    "title": r.get("title") or r_data.get("title") or "Резюме",
                    "text": r_text
                })
    else:
        r_data = hh_client.get_resume(target_resume_id)
        if r_data:
            r_text = main.format_hh_resume_to_text(r_data)
            candidate_resumes.append({
                "id": target_resume_id,
                "title": r_data.get("title") or "Резюме",
                "text": r_text
            })
            
    if not candidate_resumes:
        local_text = main.load_resume_text()
        if local_text:
            candidate_resumes.append({
                "id": target_resume_id or "local",
                "title": "Локальное резюме",
                "text": local_text
            })
            
    return candidate_resumes

@app.get("/api/vacancies/{vacancy_id}/questions")
async def get_vacancy_questions(vacancy_id: str):
    """Извлекает вопросы работодателя со страницы вакансии и генерирует ИИ-ответы."""
    def _fetch():
        hh_client = HHBrowserClient()
        try:
            questions = hh_client.get_vacancy_questions(vacancy_id)
            if not questions:
                return {"questions": [], "answers": []}
                
            # Получаем резюме
            target_resume_id = database.get_config_value("resume_id") or Config.HH_RESUME_ID
            if target_resume_id and target_resume_id.startswith("your_"):
                target_resume_id = ""
                
            candidate_resumes = load_candidate_resumes(hh_client, target_resume_id)
            resume_text = candidate_resumes[0]["text"] if candidate_resumes else ""
                
            # Детали вакансии
            details = hh_client.get_vacancy_details(vacancy_id) or {"title": "", "company": ""}
            
            # Генерация ИИ-ответов
            analyzer = LLMAnalyzer()
            user_saved_answers = database.get_user_profile_answers()
            res = analyzer.answer_questions(resume_text, details, questions, user_saved_answers)
            
            return {
                "questions": questions,
                "answers": [a.model_dump() for a in res.answers],
                "all_confident": res.all_confident
            }
        finally:
            hh_client.stop()
            
    result = await run_in_clean_thread(_fetch)
    return result

@app.get("/api/jobs")
def get_jobs(status: str = "all", limit: int = 50, offset: int = 0):
    """Возвращает список обработанных вакансий порциями и общие счётчики."""
    rows = database.get_processed_paginated(status=status, limit=limit, offset=offset)
    jobs = []
    for r in rows:
        jobs.append({
            "id": r[0],
            "title": r[1],
            "company": r[2],
            "status": r[3],
            "match_score": r[4],
            "reasoning": r[5],
            "cover_letter": r[6],
            "questions_data": r[7] if len(r) > 7 else None,
            "applied_resume_id": r[8] if len(r) > 8 else None,
            "applied_resume_title": r[9] if len(r) > 9 else None,
            "processed_at": r[10] if len(r) > 10 else (r[8] if len(r) > 8 else "")
        })
        
    # Считаем общую статистику по всей БД (быстрыми SQL COUNT-запросами)
    stats = {
        "total": database.get_processed_count("all"),
        "matched": database.get_processed_count("matched"),
        "needs_answers": database.get_processed_count("needs_answers"),
        "applied": database.get_processed_count("applied"),
        "ignored": database.get_processed_count("ignored"),
        "failed": database.get_processed_count("failed")
    }
    
    return {"jobs": jobs, "stats": stats}

def run_pipeline_task(queries: List[str], area_id: str, threshold: int, resume_id: str, dry_run: bool):
    """Фоновая задача выполнения сканирования."""
    global pipeline_status
    pipeline_status["is_running"] = True
    pipeline_status["stop_requested"] = False
    pipeline_status["last_error"] = None
    pipeline_status["last_status"] = None
    pipeline_status["currently_processing"] = None
    
    def on_step(job_info):
        pipeline_status["currently_processing"] = job_info
        
    try:
        res = main.run_pipeline(
            queries=queries,
            area_id=area_id,
            threshold=threshold,
            resume_id=resume_id,
            dry_run=dry_run,
            on_step_change=on_step,
            should_stop=lambda: pipeline_status.get("stop_requested", False)
        )
        pipeline_status["last_run_stats"] = res.get("stats")
        pipeline_status["last_status"] = res.get("status")
        if res.get("status") == "error":
            pipeline_status["last_error"] = res.get("message")
    except Exception as e:
        logger.exception(f"Error in pipeline background task: {e}")
        pipeline_status["last_error"] = str(e)
    finally:
        pipeline_status["is_running"] = False
        pipeline_status["stop_requested"] = False
        pipeline_status["currently_processing"] = None

@app.post("/api/search")
def trigger_search(background_tasks: BackgroundTasks):
    """Запускает процесс фонового сканирования."""
    global pipeline_status
    if pipeline_status["is_running"]:
        return {"status": "error", "message": "Search is already running"}
        
    settings = get_settings()
    background_tasks.add_task(
        run_pipeline_task,
        queries=settings["queries"],
        area_id=settings["area_id"],
        threshold=settings["threshold"],
        resume_id=settings["resume_id"],
        dry_run=settings["dry_run"]
    )
    return {"status": "started"}

@app.post("/api/stop")
def stop_search():
    """Запрашивает безопасную остановку текущего процесса анализа/сканирования."""
    global pipeline_status
    if not pipeline_status["is_running"]:
        return {"status": "ok", "message": "Сканирование не запущено"}
        
    pipeline_status["stop_requested"] = True
    logger.info("Получен запрос на остановку сканирования/анализа.")
    return {"status": "stopping", "message": "Запрос на остановку отправлен"}

@app.post("/api/apply")
def apply_vacancy(payload: ApplyPayload):
    """Ручной отклик на вакансию в браузере с вопросами и сопроводительным письмом."""
    hh_client = HHBrowserClient()
    try:
        success, err_msg = hh_client.apply_to_vacancy(
            vacancy_id=payload.vacancy_id,
            resume_title_or_id=payload.resume_id,
            cover_letter=payload.cover_letter,
            answers=payload.answers,
            dry_run=False
        )
    finally:
        hh_client.stop()
    
    status = "applied" if success else "failed"
    
    # Обновляем статус в БД
    conn = database.sqlite3.connect(database.DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE processed_vacancies SET status = ?, cover_letter = ? WHERE id = ?",
        (status, payload.cover_letter, payload.vacancy_id)
    )
    conn.commit()
    conn.close()
    
    if not success:
        raise HTTPException(status_code=400, detail=err_msg)
        
    return {"status": "ok"}

def extract_vacancy_id_from_url(url_or_id: str) -> str:
    """Извлекает числовой ID вакансии из URL или сырой строки."""
    url_or_id = url_or_id.strip()
    if url_or_id.isdigit():
        return url_or_id
    import re
    m = re.search(r'(?:vacancy/|vacancyId=)(\d+)', url_or_id)
    if m:
        return m.group(1)
    m2 = re.search(r'\b(\d{7,11})\b', url_or_id)
    if m2:
        return m2.group(1)
    return url_or_id

@app.post("/api/quick-apply")
async def quick_apply(payload: QuickApplyPayload):
    """
    Быстрый ИИ-отклик по ссылке/ID вакансии:
    1. Извлекает детали вакансии
    2. Генерирует сопроводительное письмо через LLM
    3. Проверяет наличие вопросов/теста
    4. Если есть вопросы — генерирует ответы
    5. Если ИИ уверен во всех ответах (или вопросов нет) и не dry_run -> отправляет отклик
    6. Если требуется подтверждение пользователя (или dry_run) -> сохраняет в needs_answers и возвращает для модалки
    """
    vacancy_id = extract_vacancy_id_from_url(payload.url_or_id)
    if not vacancy_id or not vacancy_id.isdigit():
        raise HTTPException(status_code=400, detail="Некорректная ссылка или ID вакансии")

    def _do_quick_apply():
        hh_client = HHBrowserClient()
        try:
            # 1. Получаем резюме (поддержка режима одного или всех резюме)
            target_resume_id = payload.resume_id or database.get_config_value("resume_id") or Config.HH_RESUME_ID
            if target_resume_id and target_resume_id.startswith("your_"):
                target_resume_id = ""
                
            candidate_resumes = load_candidate_resumes(hh_client, target_resume_id)
            if not candidate_resumes:
                return {"status": "error", "message": "Резюме не найдено ни в профиле HH, ни локально"}

            # 2. Получаем детали вакансии
            details = hh_client.get_vacancy_details(vacancy_id)
            if not details or not details.get("title"):
                return {"status": "error", "message": f"Не удалось получить информацию о вакансии {vacancy_id}"}

            title = details.get("title", "Без названия")
            company = details.get("company", "")

            # 3. Генерируем сопроводительное письмо через LLM с автовыбором резюме
            analyzer = LLMAnalyzer()
            try:
                analysis = analyzer.analyze_vacancy(resumes=candidate_resumes, vacancy=details, threshold=Config.MATCH_THRESHOLD)
            except Exception as e:
                logger.warning(f"LLM ошибка при быстром отклике ({e}). Используем базовое сопроводительное письмо.")
                analysis = analyzer._mock_analysis(details, match_threshold=Config.MATCH_THRESHOLD, resumes=candidate_resumes)
            
            chosen_resume_id = analysis.selected_resume_id or candidate_resumes[0]["id"]
            chosen_resume_title = analysis.selected_resume_title or candidate_resumes[0]["title"]
            chosen_resume_text = next((r["text"] for r in candidate_resumes if r["id"] == chosen_resume_id), candidate_resumes[0]["text"])

            cover_letter = analysis.cover_letter or f"Здравствуйте!\n\nМеня заинтересовала вакансия {title} в компании {company}.\nБуду рад обсудить подробности на интервью."

            # 4. Проверяем наличие вопросов/теста
            questions = hh_client.get_vacancy_questions(vacancy_id)
            questions_data_str = None
            answers_dict = None
            needs_user_answers = False
            q_answers_list = []

            if questions and isinstance(questions, list) and len(questions) > 0:
                import json
                user_saved_answers = database.get_user_profile_answers()
                q_res = analyzer.answer_questions(chosen_resume_text, details, questions, user_saved_answers)
                q_answers_list = [a.model_dump() for a in q_res.answers]
                questions_data_str = json.dumps(q_answers_list, ensure_ascii=False)
                answers_dict = {a.id: a.answer for a in q_res.answers}

                if not q_res.all_confident or any(a.requires_user_input or a.confidence < 85 for a in q_res.answers):
                    needs_user_answers = True

            dry_run_val = database.get_config_value("dry_run")
            is_dry_run = dry_run_val.lower() in ("true", "1", "yes") if dry_run_val is not None else Config.DRY_RUN

            if needs_user_answers:
                status = "needs_answers"
                database.delete_vacancy(vacancy_id)
                database.save_vacancy(
                    vacancy_id=vacancy_id,
                    title=title,
                    company=company,
                    status=status,
                    match_score=analysis.match_score,
                    analysis_reason=analysis.reasoning,
                    cover_letter=cover_letter,
                    questions_data=questions_data_str,
                    applied_resume_id=chosen_resume_id,
                    applied_resume_title=chosen_resume_title
                )
                return {
                    "status": "needs_answers",
                    "vacancy_id": vacancy_id,
                    "title": title,
                    "company": company,
                    "match_score": analysis.match_score,
                    "reasoning": analysis.reasoning,
                    "cover_letter": cover_letter,
                    "questions_data": q_answers_list,
                    "applied_resume_id": chosen_resume_id,
                    "applied_resume_title": chosen_resume_title,
                    "message": f"ИИ выбрал резюме '{chosen_resume_title}' и подготовил ответы, но некоторые требуют вашей проверки перед отправкой."
                }
            elif is_dry_run:
                status = "new"
                database.delete_vacancy(vacancy_id)
                database.save_vacancy(
                    vacancy_id=vacancy_id,
                    title=title,
                    company=company,
                    status=status,
                    match_score=analysis.match_score,
                    analysis_reason=analysis.reasoning,
                    cover_letter=cover_letter,
                    questions_data=questions_data_str,
                    applied_resume_id=chosen_resume_id,
                    applied_resume_title=chosen_resume_title
                )
                return {
                    "status": "dry_run",
                    "vacancy_id": vacancy_id,
                    "title": title,
                    "company": company,
                    "match_score": analysis.match_score,
                    "reasoning": analysis.reasoning,
                    "cover_letter": cover_letter,
                    "questions_data": q_answers_list,
                    "applied_resume_id": chosen_resume_id,
                    "applied_resume_title": chosen_resume_title,
                    "message": f"[Тестовый режим Dry Run] Отклик сформирован для резюме '{chosen_resume_title}' и сохранен."
                }
            else:
                # Все ответы уверены или вопросов нет -> авто-отклик!
                success, err_msg = hh_client.apply_to_vacancy(
                    vacancy_id=vacancy_id,
                    resume_title_or_id=chosen_resume_id,
                    cover_letter=cover_letter,
                    answers=answers_dict,
                    dry_run=False
                )
                if success:
                    status = "already_applied" if err_msg == "ALREADY_APPLIED" else "applied"
                else:
                    status = "failed"

                database.delete_vacancy(vacancy_id)
                database.save_vacancy(
                    vacancy_id=vacancy_id,
                    title=title,
                    company=company,
                    status=status,
                    match_score=analysis.match_score,
                    analysis_reason=analysis.reasoning,
                    cover_letter=cover_letter,
                    questions_data=questions_data_str,
                    applied_resume_id=chosen_resume_id,
                    applied_resume_title=chosen_resume_title
                )

                if not success:
                    return {"status": "error", "message": f"Ошибка отправки отклика: {err_msg}"}

                return {
                    "status": "applied",
                    "vacancy_id": vacancy_id,
                    "title": title,
                    "company": company,
                    "match_score": analysis.match_score,
                    "cover_letter": cover_letter,
                    "questions_data": q_answers_list,
                    "applied_resume_id": chosen_resume_id,
                    "applied_resume_title": chosen_resume_title,
                    "message": f"Отклик с резюме '{chosen_resume_title}' и ответы успешно отправлены работодателю!"
                }
        except Exception as e:
            logger.error(f"Ошибка в quick_apply: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}
        finally:
            hh_client.stop()

    result = await run_in_clean_thread(_do_quick_apply)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("message", "Ошибка"))
    return result

@app.post("/api/reanalyze/{vacancy_id}")
async def reanalyze_vacancy(vacancy_id: str):
    """Повторный LLM-анализ вакансии, которая завершилась с ошибкой."""
    import asyncio
    
    # Проверяем, что вакансия существует и имеет статус failed
    row = database.get_vacancy(vacancy_id)
    if not row:
        raise HTTPException(status_code=404, detail="Вакансия не найдена")
    
    # Проверяем что не запущен уже другой пайплайн
    if pipeline_status["is_running"]:
        raise HTTPException(status_code=409, detail="Сканирование уже запущено, подождите")
    
    # Получаем настройки из конфига
    from llm_analyzer import LLMAnalyzer
    from config import Config
    
    def run_reanalyze():
        try:
            # Устанавливаем текущую вакансию
            pipeline_status["currently_processing"] = {
                "id": vacancy_id,
                "title": row[1] if len(row) > 1 else "Переоценка...",
                "company": row[2] if len(row) > 2 else ""
            }
            
            hh_client = HHBrowserClient()
            
            # Получаем список резюме
            target_resume_id = database.get_config_value("resume_id") or Config.HH_RESUME_ID
            if target_resume_id and target_resume_id.startswith("your_"):
                target_resume_id = ""
                
            candidate_resumes = load_candidate_resumes(hh_client, target_resume_id)
            if not candidate_resumes:
                return {"status": "error", "message": "Резюме не найдено ни в профиле HH, ни локально"}

            # Получаем детали вакансии
            vacancy_details = hh_client.get_vacancy_details(vacancy_id)
            if not vacancy_details or not vacancy_details.get("description"):
                logger.error(f"Не удалось получить детали вакансии {vacancy_id}")
                return {"status": "error", "message": "Не удалось получить детали вакансии с hh.ru"}
            
            # Запускаем LLM-анализ со сравнением резюме
            analyzer = LLMAnalyzer()
            analysis = analyzer.analyze_vacancy(
                resumes=candidate_resumes,
                vacancy=vacancy_details,
                threshold=Config.MATCH_THRESHOLD
            )
            
            chosen_resume_id = analysis.selected_resume_id or candidate_resumes[0]["id"]
            chosen_resume_title = analysis.selected_resume_title or candidate_resumes[0]["title"]
            chosen_resume_text = next((r["text"] for r in candidate_resumes if r["id"] == chosen_resume_id), candidate_resumes[0]["text"])

            dry_run_val = database.get_config_value("dry_run")
            is_dry_run = dry_run_val.lower() in ("true", "1", "yes") if dry_run_val is not None else Config.DRY_RUN

            # Определяем финальный статус
            questions_data_str = None
            answers_dict = None
            needs_user_answers = False

            if analysis.is_match:
                # Проверяем наличие вопросов/теста
                questions = hh_client.get_vacancy_questions(vacancy_id)
                if questions and isinstance(questions, list) and len(questions) > 0:
                    import json
                    user_saved_answers = database.get_user_profile_answers()
                    q_res = analyzer.answer_questions(chosen_resume_text, vacancy_details, questions, user_saved_answers)
                    questions_data_str = json.dumps([a.model_dump() for a in q_res.answers], ensure_ascii=False)
                    answers_dict = {a.id: a.answer for a in q_res.answers}
                    if not q_res.all_confident or any(a.requires_user_input or a.confidence < 85 for a in q_res.answers):
                        needs_user_answers = True

                if needs_user_answers:
                    status = "needs_answers"
                elif is_dry_run:
                    status = "new"  # Режим Dry Run: сохраняем для ручного отклика
                else:
                    # Боевой режим: сразу отправляем отклик с сопроводительным письмом!
                    logger.info(f"Режим Dry Run выключен. Отправляем боевой отклик на {vacancy_id}...")
                    success, err_msg = hh_client.apply_to_vacancy(
                        vacancy_id=vacancy_id,
                        resume_title_or_id=chosen_resume_id,
                        cover_letter=analysis.cover_letter,
                        answers=answers_dict,
                        dry_run=False
                    )
                    if success:
                        status = "already_applied" if err_msg == "ALREADY_APPLIED" else "applied"
                    else:
                        status = "failed"
            else:
                status = "ignored"
            
            # Удаляем старую запись и сохраняем новую
            database.delete_vacancy(vacancy_id)
            database.save_vacancy(
                vacancy_id=vacancy_id,
                title=vacancy_details.get("title", "Без названия"),
                company=vacancy_details.get("company", ""),
                status=status,
                match_score=analysis.match_score,
                analysis_reason=analysis.reasoning,
                cover_letter=analysis.cover_letter,
                questions_data=questions_data_str,
                applied_resume_id=chosen_resume_id,
                applied_resume_title=chosen_resume_title
            )
            logger.info(f"Переоценка вакансии {vacancy_id}: статус={status}, score={analysis.match_score}, резюме={chosen_resume_title}")
            return {"status": "ok", "new_status": status, "score": analysis.match_score, "resume": chosen_resume_title}
        except Exception as e:
            logger.error(f"Ошибка при переоценке вакансии {vacancy_id}: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}
        finally:
            if 'hh_client' in locals():
                hh_client.stop()
            pipeline_status["currently_processing"] = None
    
    # Запускаем в изолированном потоке без asyncio-окружения
    result = await run_in_clean_thread(run_reanalyze)
    
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result["message"])
    
    return result

@app.post("/api/reanalyze-all-failed")
def reanalyze_all_failed(background_tasks: BackgroundTasks):
    """Повторный анализ всех вакансий с ошибками по очереди в фоне."""
    global pipeline_status
    
    # Проверяем, что не запущен другой сканер
    if pipeline_status["is_running"]:
        raise HTTPException(status_code=409, detail="Сканирование уже запущено, подождите")
        
    # Получаем все failed вакансии
    failed_rows = database.get_processed_paginated(status="failed", limit=100, offset=0)
    if not failed_rows:
        return {"status": "ok", "processed": 0, "message": "Нет вакансий со статусом Ошибка"}
        
    def process_all_task(failed_rows):
        global pipeline_status
        from llm_analyzer import LLMAnalyzer
        from config import Config
        
        pipeline_status["is_running"] = True
        pipeline_status["stop_requested"] = False
        pipeline_status["last_status"] = None
        pipeline_status["last_error"] = None
        pipeline_status["last_run_stats"] = None
        hh_client = HHBrowserClient()
        
        stats = {
            "processed": 0,
            "matched": 0,
            "ignored": 0,
            "failed": 0
        }
        stopped_by_user = False
        
        try:
            hh_client.start()
            target_resume_id = database.get_config_value("resume_id") or Config.HH_RESUME_ID
            if target_resume_id and target_resume_id.startswith("your_"):
                target_resume_id = ""
                
            candidate_resumes = load_candidate_resumes(hh_client, target_resume_id)
            if not candidate_resumes:
                logger.error("Резюме не найдено при переоценке.")
                pipeline_status["last_error"] = "Резюме не найдено при переоценке"
                return
                
            analyzer = LLMAnalyzer()
            user_saved_answers = database.get_user_profile_answers()
            
            for row in failed_rows:
                if pipeline_status.get("stop_requested"):
                    logger.info("Переоценка ошибок остановлена по запросу пользователя.")
                    stopped_by_user = True
                    break
                vacancy_id = row[0]
                try:
                    pipeline_status["currently_processing"] = {
                        "id": vacancy_id,
                        "title": row[1] if len(row) > 1 else "Переоценка...",
                        "company": row[2] if len(row) > 2 else ""
                    }
                    
                    # Получаем детали вакансии
                    vacancy_details = hh_client.get_vacancy_details(vacancy_id)
                    if not vacancy_details or not vacancy_details.get("description"):
                        continue
                    
                    if pipeline_status.get("stop_requested"):
                        logger.info("Переоценка ошибок остановлена перед анализом LLM.")
                        stopped_by_user = True
                        break

                    # Анализируем
                    analysis = analyzer.analyze_vacancy(
                        resumes=candidate_resumes,
                        vacancy=vacancy_details,
                        threshold=Config.MATCH_THRESHOLD
                    )
                    
                    chosen_resume_id = analysis.selected_resume_id or candidate_resumes[0]["id"]
                    chosen_resume_title = analysis.selected_resume_title or candidate_resumes[0]["title"]
                    chosen_resume_text = next((r["text"] for r in candidate_resumes if r["id"] == chosen_resume_id), candidate_resumes[0]["text"])

                    dry_run_val = database.get_config_value("dry_run")
                    is_dry_run = dry_run_val.lower() in ("true", "1", "yes") if dry_run_val is not None else Config.DRY_RUN

                    questions_data_str = None
                    answers_dict = None
                    needs_user_answers = False

                    if analysis.is_match:
                        questions = hh_client.get_vacancy_questions(vacancy_id)
                        if questions and isinstance(questions, list) and len(questions) > 0:
                            import json
                            q_res = analyzer.answer_questions(chosen_resume_text, vacancy_details, questions, user_saved_answers)
                            questions_data_str = json.dumps([a.model_dump() for a in q_res.answers], ensure_ascii=False)
                            answers_dict = {a.id: a.answer for a in q_res.answers}
                            if not q_res.all_confident or any(a.requires_user_input or a.confidence < 85 for a in q_res.answers):
                                needs_user_answers = True

                        if needs_user_answers:
                            status = "needs_answers"
                        elif is_dry_run:
                            status = "new"
                        else:
                            logger.info(f"Режим Dry Run выключен. Отправляем боевой отклик на {vacancy_id}...")
                            success, err_msg = hh_client.apply_to_vacancy(
                                vacancy_id=vacancy_id,
                                resume_title_or_id=chosen_resume_id,
                                cover_letter=analysis.cover_letter,
                                answers=answers_dict,
                                dry_run=False
                            )
                            if success:
                                status = "already_applied" if err_msg == "ALREADY_APPLIED" else "applied"
                                if status == "applied":
                                    stats["applied"] = stats.get("applied", 0) + 1
                            else:
                                status = "failed"
                    else:
                        status = "ignored"
                    
                    database.delete_vacancy(vacancy_id)
                    database.save_vacancy(
                        vacancy_id=vacancy_id,
                        title=vacancy_details.get("title", "Без названия"),
                        company=vacancy_details.get("company", ""),
                        status=status,
                        match_score=analysis.match_score,
                        analysis_reason=analysis.reasoning,
                        cover_letter=analysis.cover_letter,
                        questions_data=questions_data_str,
                        applied_resume_id=chosen_resume_id,
                        applied_resume_title=chosen_resume_title
                    )
                    
                    stats["processed"] += 1
                    if analysis.is_match:
                        stats["matched"] += 1
                except QuotaExceededError as qe:
                    logger.error(f"Превышена квота запросов к Gemini API (429) при переоценке: {qe}")
                    pipeline_status["last_error"] = "Превышена квота запросов к Gemini API (429 Quota Exceeded). Переоценка остановлена."
                    stats["failed"] += 1
                    break
                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"Не удалось переоценить вакансию {vacancy_id}: {e}")
                    
            pipeline_status["last_run_stats"] = stats
            pipeline_status["last_status"] = "stopped" if stopped_by_user else "success"
        except Exception as outer_e:
            logger.error(f"Глобальная ошибка в фоновой переоценке: {outer_e}")
            pipeline_status["last_error"] = str(outer_e)
        finally:
            hh_client.stop()
            pipeline_status["currently_processing"] = None
            pipeline_status["is_running"] = False
            pipeline_status["stop_requested"] = False

    background_tasks.add_task(process_all_task, failed_rows)
    return {"status": "started", "message": f"Запущена переоценка {len(failed_rows)} вакансий"}

# Монтируем статические файлы
app.mount("/static", StaticFiles(directory=static_dir), name="static")
