import logging
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException, BackgroundTasks
from src.core.config import Config
from src.db import database
from src.clients.browser import HHBrowserClient, BrowserBusyError
from src.clients.llm import LLMAnalyzer, QuotaExceededError
from src.pipeline.runner import load_candidate_resumes
from src.pipeline.processing import decide_and_apply
from src.api.state import (
    ApplyPayload,
    QuickApplyPayload,
    SaveDraftPayload,
    run_in_clean_thread,
    ensure_browser_available
)
import src.api.state as state

logger = logging.getLogger("VacanciesRoutes")
router = APIRouter(tags=["Vacancies"])

def _target_resume_id(preferred: str = None) -> str:
    target = preferred or database.get_config_value("resume_id") or Config.HH_RESUME_ID
    return "" if target and target.startswith("your_") else target

def _match_threshold() -> int:
    value = database.get_config_value("match_threshold")
    return int(value) if value else Config.MATCH_THRESHOLD

def _is_dry_run() -> bool:
    value = database.get_config_value("dry_run")
    return value.lower() in ("true", "1", "yes") if value is not None else Config.DRY_RUN

RESUME_NOT_FOUND = "Резюме не найдено ни в профиле HH, ни локально. Выберите резюме в настройках."

def _require_resumes(hh_client: HHBrowserClient, target_resume_id: str) -> List[Dict[str, Any]]:
    candidate_resumes = load_candidate_resumes(hh_client, target_resume_id)
    if not candidate_resumes:
        raise HTTPException(status_code=400, detail=RESUME_NOT_FOUND)
    return candidate_resumes

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

@router.get("/api/jobs")
def get_jobs(status: str = "all", limit: int = 50, offset: int = 0):
    """Возвращает список обработанных вакансий порциями и общие счётчики."""
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
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
            "processed_at": r[10] if len(r) > 10 else "",
            "scores_data": r[11] if len(r) > 11 else None,
            "analyzed_by_provider": r[12] if len(r) > 12 else None,
            "analyzed_by_model": r[13] if len(r) > 13 else None
        })
        
    stats = database.get_all_counts()
    
    return {"jobs": jobs, "stats": stats}

@router.get("/api/vacancies/{vacancy_id}/questions")
async def get_vacancy_questions(vacancy_id: str):
    """Извлекает вопросы работодателя со страницы вакансии и генерирует ИИ-ответы."""
    ensure_browser_available()
    def _fetch():
        hh_client = HHBrowserClient()
        try:
            questions = hh_client.get_vacancy_questions(vacancy_id)
            if not questions:
                return {"questions": [], "answers": []}
                
            candidate_resumes = load_candidate_resumes(hh_client, _target_resume_id())
            resume_text = candidate_resumes[0]["text"] if candidate_resumes else ""
                
            details = hh_client.get_vacancy_details(vacancy_id) or {"title": "", "company": ""}
            
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

@router.post("/api/apply")
def apply_vacancy(payload: ApplyPayload):
    """Ручной отклик на вакансию в браузере с вопросами и сопроводительным письмом."""
    ensure_browser_available()
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
    
    if success:
        status = "already_applied" if err_msg == "ALREADY_APPLIED" else "applied"
    else:
        status = "failed"

    # Сохраняем письмо и ответы, которые реально ушли работодателю (или были подготовлены при ошибке)
    database.update_vacancy_user_data(
        payload.vacancy_id,
        cover_letter=payload.cover_letter,
        answers=payload.answers,
        status=status
    )
    
    if not success:
        raise HTTPException(status_code=400, detail=err_msg)
        
    return {"status": "ok", "vacancy_status": status}

@router.post("/api/quick-apply")
async def quick_apply(payload: QuickApplyPayload):
    """Быстрый ИИ-отклик по ссылке/ID вакансии."""
    vacancy_id = extract_vacancy_id_from_url(payload.url_or_id)
    if not vacancy_id or not vacancy_id.isdigit():
        raise HTTPException(status_code=400, detail="Некорректная ссылка или ID вакансии")
    ensure_browser_available()

    def _do_quick_apply():
        hh_client = HHBrowserClient()
        try:
            candidate_resumes = _require_resumes(hh_client, _target_resume_id(payload.resume_id))

            details = hh_client.get_vacancy_details(vacancy_id)
            if not details or not details.get("title"):
                raise HTTPException(status_code=404, detail=f"Вакансия {vacancy_id} не найдена на hh.ru или недоступна")

            title = details.get("title", "Без названия")
            company = details.get("company", "")
            threshold = _match_threshold()
            is_dry_run = _is_dry_run()

            analyzer = LLMAnalyzer()
            try:
                analysis = analyzer.analyze_vacancy(resumes=candidate_resumes, vacancy=details, threshold=threshold)
            except Exception as e:
                logger.warning(f"LLM ошибка при быстром отклике ({e}). Используем базовое сопроводительное письмо.")
                analysis = analyzer._mock_analysis(details, match_threshold=threshold, resumes=candidate_resumes)

            cover_letter = (payload.cover_letter or "").strip() or analysis.cover_letter
            if not cover_letter or not cover_letter.strip():
                chosen_text = next((r.get("text", "") for r in candidate_resumes if r.get("id") == analysis.selected_resume_id), candidate_resumes[0].get("text", ""))
                cover_letter = analyzer.generate_cover_letter(chosen_text, details, resumes=candidate_resumes)
            postfix = (database.get_system_setting("cover_letter_postfix") or "").strip()
            if postfix and not cover_letter.endswith(postfix):
                cover_letter = f"{cover_letter.strip()}\n\n{postfix}"

            # В Dry Run отклик не уходит, поэтому неподходящую вакансию можно подготовить без подтверждения
            decision = decide_and_apply(
                hh_client, analyzer, vacancy_id, details, analysis, candidate_resumes,
                threshold=threshold,
                dry_run=is_dry_run,
                cover_letter=cover_letter,
                force=payload.force or is_dry_run,
                allow_local_resume=True
            )
            decision.save(vacancy_id, title, company)

            response = {
                "vacancy_id": vacancy_id,
                "title": title,
                "company": company,
                "match_score": analysis.match_score,
                "reasoning": analysis.reasoning,
                "cover_letter": decision.cover_letter,
                "questions_data": decision.question_answers,
                "applied_resume_id": decision.resume_id,
                "applied_resume_title": decision.resume_title,
                "scores_data": decision.scores,
            }
            if decision.status == "ignored":
                # Не отправляем отклик на неподходящую вакансию без явного подтверждения пользователя
                return {
                    **response,
                    "status": "not_eligible",
                    "threshold": threshold,
                    "has_hard_blocker": analysis.has_hard_blocker,
                    "blocker_reason": analysis.blocker_reason,
                    "message": "Вакансия не прошла порог соответствия — отклик не отправлен."
                }
            if decision.status == "needs_answers":
                return {**response, "status": "needs_answers",
                        "message": f"ИИ выбрал резюме '{decision.resume_title}' и подготовил ответы, но некоторые требуют вашей проверки перед отправкой."}
            if decision.status == "new":
                return {**response, "status": "dry_run",
                        "message": f"[Тестовый режим Dry Run] Отклик сформирован для резюме '{decision.resume_title}' и сохранен."}
            if decision.status == "failed":
                if decision.error:
                    raise HTTPException(status_code=400, detail=decision.error)
                raise HTTPException(status_code=502, detail=f"hh.ru не принял отклик: {decision.apply_error}")
            return {**response, "status": decision.status,
                    "message": f"Отклик с резюме '{decision.resume_title}' и ответы успешно отправлены работодателю!"}
        finally:
            hh_client.stop()

    try:
        return await run_in_clean_thread(_do_quick_apply)
    except (HTTPException, BrowserBusyError):
        raise
    except Exception as e:
        logger.error(f"Ошибка в quick_apply: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/reanalyze/{vacancy_id}")
async def reanalyze_vacancy(vacancy_id: str):
    """Повторный LLM-анализ вакансии, которая завершилась с ошибкой."""
    row = database.get_vacancy(vacancy_id)
    if not row:
        raise HTTPException(status_code=404, detail="Вакансия не найдена")
    
    ensure_browser_available()
    if not state.try_claim_pipeline():
        raise HTTPException(status_code=409, detail="Сканирование или переоценка уже запущены, подождите")
    
    def run_reanalyze():
        hh_client = HHBrowserClient()
        try:
            state.update_pipeline(currently_processing={
                "id": vacancy_id,
                "title": row["title"] or "Переоценка...",
                "company": row["company"] or ""
            })
            candidate_resumes = _require_resumes(hh_client, _target_resume_id())

            vacancy_details = hh_client.get_vacancy_details(vacancy_id)
            if not vacancy_details or not vacancy_details.get("description"):
                logger.error(f"Не удалось получить детали вакансии {vacancy_id}")
                raise HTTPException(status_code=404, detail="Вакансия не найдена на hh.ru или недоступна")

            threshold = _match_threshold()
            analyzer = LLMAnalyzer()
            analysis = analyzer.analyze_vacancy(resumes=candidate_resumes, vacancy=vacancy_details, threshold=threshold)
            decision = decide_and_apply(
                hh_client, analyzer, vacancy_id, vacancy_details, analysis, candidate_resumes,
                threshold=threshold,
                dry_run=_is_dry_run()
            )
            decision.save(vacancy_id, vacancy_details.get("title", "Без названия"), vacancy_details.get("company", ""))
            logger.info(f"Переоценка вакансии {vacancy_id}: статус={decision.status}, score={analysis.match_score}, резюме={decision.resume_title}")
            return {"status": "ok", "new_status": decision.status, "score": analysis.match_score, "resume": decision.resume_title}
        finally:
            hh_client.stop()
            state.update_pipeline(currently_processing=None)

    try:
        return await run_in_clean_thread(run_reanalyze)
    except (HTTPException, BrowserBusyError):
        raise
    except QuotaExceededError as e:
        raise HTTPException(status_code=429, detail=f"Превышена квота запросов к ИИ: {e}")
    except Exception as e:
        logger.error(f"Ошибка при переоценке вакансии {vacancy_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        state.release_pipeline()

@router.post("/api/generate-cover-letter/{vacancy_id}")
async def generate_cover_letter_endpoint(vacancy_id: str):
    """Генерация персонализированного сопроводительного письма с ИИ для конкретной вакансии."""
    ensure_browser_available()
    row = database.get_vacancy(vacancy_id)

    def _do_generate():
        hh_client = HHBrowserClient()
        try:
            # По имени колонки: в БД, мигрированных со старых версий, порядок колонок другой
            applied_resume_id = row["applied_resume_id"] if row else None
            candidate_resumes = _require_resumes(hh_client, _target_resume_id(applied_resume_id))

            details = None
            try:
                details = hh_client.get_vacancy_details(vacancy_id)
            except Exception as e:
                logger.warning(f"Не удалось загрузить страницу вакансии {vacancy_id} через браузер: {e}")

            if not details or not details.get("title") or not details.get("description"):
                details = {
                    "title": (row["title"] if row else None) or "Вакансия",
                    "company": (row["company"] if row else None) or "",
                    "description": "",
                    "skills": []
                }

            chosen_resume = next((r for r in candidate_resumes if r.get("id") == applied_resume_id), candidate_resumes[0])
            chosen_resume_text = chosen_resume.get("text", "")

            analyzer = LLMAnalyzer()
            letter = analyzer.generate_cover_letter(chosen_resume_text, details, resumes=candidate_resumes)

            if row:
                database.update_vacancy_user_data(vacancy_id, cover_letter=letter)

            logger.info(f"Сопроводительное письмо для вакансии {vacancy_id} успешно сгенерировано ({len(letter)} симв.).")
            return {
                "status": "ok",
                "cover_letter": letter,
                "vacancy_id": vacancy_id
            }
        finally:
            hh_client.stop()

    try:
        return await run_in_clean_thread(_do_generate)
    except (HTTPException, BrowserBusyError):
        raise
    except QuotaExceededError as e:
        raise HTTPException(status_code=429, detail=f"Превышена квота запросов к ИИ: {e}")
    except Exception as e:
        logger.error(f"Ошибка при генерации сопроводительного письма для {vacancy_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e) or "Ошибка генерации письма")

@router.post("/api/vacancies/{vacancy_id}/save-draft")
def save_vacancy_draft(vacancy_id: str, payload: SaveDraftPayload):
    """Сохраняет отредактированное пользователем сопроводительное письмо и ответы в БД как черновик."""
    if not database.update_vacancy_user_data(vacancy_id, cover_letter=payload.cover_letter, answers=payload.answers):
        raise HTTPException(status_code=404, detail="Вакансия не найдена")
    return {"status": "ok"}

@router.post("/api/reanalyze-all-failed")
def reanalyze_all_failed(background_tasks: BackgroundTasks):
    """Повторный анализ всех вакансий с ошибками по очереди в фоне."""
    ensure_browser_available()
        
    # Берём снимок всех вакансий с ошибкой: повторно упавшие не попадут в этот же прогон второй раз
    failed_count = database.get_processed_count("failed")
    failed_rows = database.get_processed_paginated(status="failed", limit=max(failed_count, 1), offset=0)
    if not failed_rows:
        return {"status": "ok", "processed": 0, "message": "Нет вакансий со статусом Ошибка"}
    if not state.try_claim_pipeline():
        raise HTTPException(status_code=409, detail="Сканирование или переоценка уже запущены, подождите")
        
    def process_all_task(failed_rows):
        hh_client = HHBrowserClient()
        
        stats = {
            "processed": 0,
            "matched": 0,
            "applied": 0,
            "ignored": 0,
            "failed": 0,
            "skipped": 0
        }
        stopped_by_user = False
        
        try:
            hh_client.start()
            candidate_resumes = load_candidate_resumes(hh_client, _target_resume_id())
            if not candidate_resumes:
                logger.error("Резюме не найдено при переоценке.")
                state.update_pipeline(last_error="Резюме не найдено при переоценке")
                return
                
            analyzer = LLMAnalyzer()
            user_saved_answers = database.get_user_profile_answers()
            
            for row in failed_rows:
                if state.is_stop_requested():
                    logger.info("Переоценка ошибок остановлена по запросу пользователя.")
                    stopped_by_user = True
                    break
                vacancy_id = row[0]
                try:
                    state.update_pipeline(currently_processing={
                        "id": vacancy_id,
                        "title": row[1] or "Переоценка...",
                        "company": row[2] or ""
                    })
                    
                    vacancy_details = hh_client.get_vacancy_details(vacancy_id)
                    if not vacancy_details or not vacancy_details.get("description"):
                        # Вакансия удалена или в архиве — оставляем статус ошибки, но показываем в итогах
                        logger.warning(f"Вакансия {vacancy_id} недоступна на hh.ru, пропускаем.")
                        stats["skipped"] += 1
                        continue
                    
                    if state.is_stop_requested():
                        logger.info("Переоценка ошибок остановлена перед анализом LLM.")
                        stopped_by_user = True
                        break

                    threshold = _match_threshold()
                    analysis = analyzer.analyze_vacancy(
                        resumes=candidate_resumes,
                        vacancy=vacancy_details,
                        threshold=threshold
                    )
                    decision = decide_and_apply(
                        hh_client, analyzer, vacancy_id, vacancy_details, analysis, candidate_resumes,
                        threshold=threshold,
                        dry_run=_is_dry_run(),
                        user_saved_answers=user_saved_answers,
                        should_stop=state.is_stop_requested
                    )
                    if decision.stopped:
                        logger.info("Переоценка ошибок остановлена перед откликом.")
                        stopped_by_user = True
                        break
                    decision.save(vacancy_id, vacancy_details.get("title", "Без названия"), vacancy_details.get("company", ""))

                    stats["processed"] += 1
                    if decision.is_eligible:
                        stats["matched"] += 1
                    if decision.status == "applied":
                        stats["applied"] += 1
                    elif decision.status == "ignored":
                        stats["ignored"] += 1
                    elif decision.status == "failed":
                        stats["failed"] += 1
                except QuotaExceededError as qe:
                    logger.error(f"Превышена квота запросов к Gemini API (429) при переоценке: {qe}")
                    state.update_pipeline(last_error="Превышена квота запросов к ИИ (429 Quota Exceeded). Переоценка остановлена.")
                    stats["failed"] += 1
                    break
                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"Не удалось переоценить вакансию {vacancy_id}: {e}")
                    
            state.update_pipeline(last_run_stats=stats, last_status="stopped" if stopped_by_user else "success")
        except Exception as outer_e:
            logger.error(f"Глобальная ошибка в фоновой переоценке: {outer_e}")
            state.update_pipeline(last_error=str(outer_e))
        finally:
            hh_client.stop()
            state.release_pipeline()

    background_tasks.add_task(process_all_task, failed_rows)
    return {"status": "started", "message": f"Запущена переоценка {len(failed_rows)} вакансий"}
