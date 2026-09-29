import logging
from typing import List
from fastapi import APIRouter, BackgroundTasks
from src.pipeline.runner import run_pipeline
from src.api.routes.settings import get_settings
import src.api.state as state

logger = logging.getLogger("PipelineRoutes")
router = APIRouter(tags=["Pipeline"])

def run_pipeline_task(queries: List[str], area_id: str, threshold: int, resume_id: str, dry_run: bool,
                      stop_condition: str = None, limit_applications: int = None, limit_processed: int = None):
    """Фоновая задача выполнения сканирования. is_running уже выставлен через try_claim_pipeline()."""
    def on_step(job_info):
        state.update_pipeline(currently_processing=job_info)
        
    try:
        res = run_pipeline(
            queries=queries,
            area_id=area_id,
            threshold=threshold,
            resume_id=resume_id,
            dry_run=dry_run,
            stop_condition=stop_condition,
            limit_applications=limit_applications,
            limit_processed=limit_processed,
            on_step_change=on_step,
            should_stop=state.is_stop_requested
        )
        state.update_pipeline(
            last_run_stats=res.get("stats"),
            last_status=res.get("status"),
            last_error=(res.get("message") or res.get("error")) if res.get("status") == "error" else None
        )
        if res.get("status") == "error":
            if res.get("reason") == "not_logged_in":
                # Сбрасываем кэш, чтобы UI не показывал "залогинен" при реально слетевшей сессии
                state.cached_login_status = False
                state.cached_user_info = None
                state.last_login_check_time = 0.0
    except Exception as e:
        logger.exception(f"Error in pipeline background task: {e}")
        state.update_pipeline(last_error=str(e))
    finally:
        state.release_pipeline()

@router.post("/api/search")
def trigger_search(background_tasks: BackgroundTasks):
    """Запускает процесс фонового сканирования."""
    if state.login_browser_active:
        return {"status": "error", "message": "Открыто окно входа в hh.ru — завершите вход и закройте его."}
    if not state.try_claim_pipeline():
        return {"status": "error", "message": "Search is already running"}

    try:
        settings = get_settings()
    except Exception:
        state.release_pipeline()
        raise
    background_tasks.add_task(
        run_pipeline_task,
        queries=settings["queries"],
        area_id=settings["area_id"],
        threshold=settings["threshold"],
        resume_id=settings["resume_id"],
        dry_run=settings["dry_run"],
        stop_condition=settings.get("stop_condition"),
        limit_applications=settings.get("limit_applications"),
        limit_processed=settings.get("limit_processed")
    )
    return {"status": "started"}

@router.post("/api/stop")
def stop_search():
    """Запрашивает безопасную остановку текущего процесса анализа/сканирования."""
    if not state.request_stop():
        return {"status": "ok", "message": "Сканирование не запущено"}
    logger.info("Получен запрос на остановку сканирования/анализа.")
    return {"status": "stopping", "message": "Запрос на остановку отправлен"}
