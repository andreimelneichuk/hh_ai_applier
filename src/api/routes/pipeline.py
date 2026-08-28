import logging
from typing import List
from fastapi import APIRouter, BackgroundTasks
from src.pipeline.runner import run_pipeline
from src.api.routes.settings import get_settings
from src.api.state import pipeline_status
import src.api.state as state

logger = logging.getLogger("PipelineRoutes")
router = APIRouter(tags=["Pipeline"])

def run_pipeline_task(queries: List[str], area_id: str, threshold: int, resume_id: str, dry_run: bool):
    """Фоновая задача выполнения сканирования."""
    state.pipeline_status["is_running"] = True
    state.pipeline_status["stop_requested"] = False
    state.pipeline_status["last_error"] = None
    state.pipeline_status["last_status"] = None
    state.pipeline_status["currently_processing"] = None
    
    def on_step(job_info):
        state.pipeline_status["currently_processing"] = job_info
        
    try:
        res = run_pipeline(
            queries=queries,
            area_id=area_id,
            threshold=threshold,
            resume_id=resume_id,
            dry_run=dry_run,
            on_step_change=on_step,
            should_stop=lambda: state.pipeline_status.get("stop_requested", False)
        )
        state.pipeline_status["last_run_stats"] = res.get("stats")
        state.pipeline_status["last_status"] = res.get("status")
        if res.get("status") == "error":
            state.pipeline_status["last_error"] = res.get("message")
    except Exception as e:
        logger.exception(f"Error in pipeline background task: {e}")
        state.pipeline_status["last_error"] = str(e)
    finally:
        state.pipeline_status["is_running"] = False
        state.pipeline_status["stop_requested"] = False
        state.pipeline_status["currently_processing"] = None

@router.post("/api/search")
def trigger_search(background_tasks: BackgroundTasks):
    """Запускает процесс фонового сканирования."""
    if state.pipeline_status["is_running"]:
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

@router.post("/api/stop")
def stop_search():
    """Запрашивает безопасную остановку текущего процесса анализа/сканирования."""
    if not state.pipeline_status["is_running"]:
        return {"status": "ok", "message": "Сканирование не запущено"}
        
    state.pipeline_status["stop_requested"] = True
    logger.info("Получен запрос на остановку сканирования/анализа.")
    return {"status": "stopping", "message": "Запрос на остановку отправлен"}
