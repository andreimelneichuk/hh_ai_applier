import json
import logging
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException, BackgroundTasks
from src.core.config import Config
from src.db import database
from src.clients.browser import HHBrowserClient
from src.clients.llm import LLMAnalyzer, QuotaExceededError
from src.pipeline.runner import format_hh_resume_to_text, load_resume_text
from src.api.state import (
    ApplyPayload,
    QuickApplyPayload,
    pipeline_status,
    run_in_clean_thread
)
import src.api.state as state

logger = logging.getLogger("VacanciesRoutes")
router = APIRouter(tags=["Vacancies"])

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
            r_text = format_hh_resume_to_text(r_data) if r_data else ""
            if r_text:
                candidate_resumes.append({
                    "id": r_id,
                    "title": r.get("title") or r_data.get("title") or "Резюме",
                    "text": r_text
                })
    else:
        r_data = hh_client.get_resume(target_resume_id)
        if r_data:
            r_text = format_hh_resume_to_text(r_data)
            candidate_resumes.append({
                "id": target_resume_id,
                "title": r_data.get("title") or "Резюме",
                "text": r_text
            })
            
    if not candidate_resumes:
        local_text = load_resume_text()
        if local_text:
            candidate_resumes.append({
                "id": target_resume_id or "local",
                "title": "Локальное резюме",
                "text": local_text
            })
            
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
        
    stats = {
        "total": database.get_processed_count("all"),
        "matched": database.get_processed_count("matched"),
        "needs_answers": database.get_processed_count("needs_answers"),
        "applied": database.get_processed_count("applied"),
        "ignored": database.get_processed_count("ignored"),
        "failed": database.get_processed_count("failed")
    }
    
    return {"jobs": jobs, "stats": stats}

@router.get("/api/vacancies/{vacancy_id}/questions")
async def get_vacancy_questions(vacancy_id: str):
    """Извлекает вопросы работодателя со страницы вакансии и генерирует ИИ-ответы."""
    def _fetch():
        hh_client = HHBrowserClient()
        try:
            questions = hh_client.get_vacancy_questions(vacancy_id)
            if not questions:
                return {"questions": [], "answers": []}
                
            target_resume_id = database.get_config_value("resume_id") or Config.HH_RESUME_ID
            if target_resume_id and target_resume_id.startswith("your_"):
                target_resume_id = ""
                
            candidate_resumes = load_candidate_resumes(hh_client, target_resume_id)
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
    
    import sqlite3
    conn = sqlite3.connect(database.DB_PATH)
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

@router.post("/api/quick-apply")
async def quick_apply(payload: QuickApplyPayload):
    """Быстрый ИИ-отклик по ссылке/ID вакансии."""
    vacancy_id = extract_vacancy_id_from_url(payload.url_or_id)
    if not vacancy_id or not vacancy_id.isdigit():
        raise HTTPException(status_code=400, detail="Некорректная ссылка или ID вакансии")

    def _do_quick_apply():
        hh_client = HHBrowserClient()
        try:
            target_resume_id = payload.resume_id or database.get_config_value("resume_id") or Config.HH_RESUME_ID
            if target_resume_id and target_resume_id.startswith("your_"):
                target_resume_id = ""
                
            candidate_resumes = load_candidate_resumes(hh_client, target_resume_id)
            if not candidate_resumes:
                return {"status": "error", "message": "Резюме не найдено ни в профиле HH, ни локально"}

            details = hh_client.get_vacancy_details(vacancy_id)
            if not details or not details.get("title"):
                return {"status": "error", "message": f"Не удалось получить информацию о вакансии {vacancy_id}"}

            title = details.get("title", "Без названия")
            company = details.get("company", "")

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

            questions = hh_client.get_vacancy_questions(vacancy_id)
            questions_data_str = None
            answers_dict = None
            needs_user_answers = False
            q_answers_list = []

            if questions and isinstance(questions, list) and len(questions) > 0:
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

@router.post("/api/reanalyze/{vacancy_id}")
async def reanalyze_vacancy(vacancy_id: str):
    """Повторный LLM-анализ вакансии, которая завершилась с ошибкой."""
    row = database.get_vacancy(vacancy_id)
    if not row:
        raise HTTPException(status_code=404, detail="Вакансия не найдена")
    
    if state.pipeline_status["is_running"]:
        raise HTTPException(status_code=409, detail="Сканирование уже запущено, подождите")
    
    def run_reanalyze():
        try:
            state.pipeline_status["currently_processing"] = {
                "id": vacancy_id,
                "title": row[1] if len(row) > 1 else "Переоценка...",
                "company": row[2] if len(row) > 2 else ""
            }
            
            hh_client = HHBrowserClient()
            target_resume_id = database.get_config_value("resume_id") or Config.HH_RESUME_ID
            if target_resume_id and target_resume_id.startswith("your_"):
                target_resume_id = ""
                
            candidate_resumes = load_candidate_resumes(hh_client, target_resume_id)
            if not candidate_resumes:
                return {"status": "error", "message": "Резюме не найдено ни в профиле HH, ни локально"}

            vacancy_details = hh_client.get_vacancy_details(vacancy_id)
            if not vacancy_details or not vacancy_details.get("description"):
                logger.error(f"Не удалось получить детали вакансии {vacancy_id}")
                return {"status": "error", "message": "Не удалось получить детали вакансии с hh.ru"}
            
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

            questions_data_str = None
            answers_dict = None
            needs_user_answers = False

            if analysis.is_match:
                questions = hh_client.get_vacancy_questions(vacancy_id)
                if questions and isinstance(questions, list) and len(questions) > 0:
                    user_saved_answers = database.get_user_profile_answers()
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
            logger.info(f"Переоценка вакансии {vacancy_id}: статус={status}, score={analysis.match_score}, резюме={chosen_resume_title}")
            return {"status": "ok", "new_status": status, "score": analysis.match_score, "resume": chosen_resume_title}
        except Exception as e:
            logger.error(f"Ошибка при переоценке вакансии {vacancy_id}: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}
        finally:
            if 'hh_client' in locals():
                hh_client.stop()
            state.pipeline_status["currently_processing"] = None
    
    result = await run_in_clean_thread(run_reanalyze)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result["message"])
    
    return result

@router.post("/api/reanalyze-all-failed")
def reanalyze_all_failed(background_tasks: BackgroundTasks):
    """Повторный анализ всех вакансий с ошибками по очереди в фоне."""
    if state.pipeline_status["is_running"]:
        raise HTTPException(status_code=409, detail="Сканирование уже запущено, подождите")
        
    failed_rows = database.get_processed_paginated(status="failed", limit=100, offset=0)
    if not failed_rows:
        return {"status": "ok", "processed": 0, "message": "Нет вакансий со статусом Ошибка"}
        
    def process_all_task(failed_rows):
        state.pipeline_status["is_running"] = True
        state.pipeline_status["stop_requested"] = False
        state.pipeline_status["last_status"] = None
        state.pipeline_status["last_error"] = None
        state.pipeline_status["last_run_stats"] = None
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
                state.pipeline_status["last_error"] = "Резюме не найдено при переоценке"
                return
                
            analyzer = LLMAnalyzer()
            user_saved_answers = database.get_user_profile_answers()
            
            for row in failed_rows:
                if state.pipeline_status.get("stop_requested"):
                    logger.info("Переоценка ошибок остановлена по запросу пользователя.")
                    stopped_by_user = True
                    break
                vacancy_id = row[0]
                try:
                    state.pipeline_status["currently_processing"] = {
                        "id": vacancy_id,
                        "title": row[1] if len(row) > 1 else "Переоценка...",
                        "company": row[2] if len(row) > 2 else ""
                    }
                    
                    vacancy_details = hh_client.get_vacancy_details(vacancy_id)
                    if not vacancy_details or not vacancy_details.get("description"):
                        continue
                    
                    if state.pipeline_status.get("stop_requested"):
                        logger.info("Переоценка ошибок остановлена перед анализом LLM.")
                        stopped_by_user = True
                        break

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
                    state.pipeline_status["last_error"] = "Превышена квота запросов к Gemini API (429 Quota Exceeded). Переоценка остановлена."
                    stats["failed"] += 1
                    break
                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"Не удалось переоценить вакансию {vacancy_id}: {e}")
                    
            state.pipeline_status["last_run_stats"] = stats
            state.pipeline_status["last_status"] = "stopped" if stopped_by_user else "success"
        except Exception as outer_e:
            logger.error(f"Глобальная ошибка в фоновой переоценке: {outer_e}")
            state.pipeline_status["last_error"] = str(outer_e)
        finally:
            hh_client.stop()
            state.pipeline_status["currently_processing"] = None
            state.pipeline_status["is_running"] = False
            state.pipeline_status["stop_requested"] = False

    background_tasks.add_task(process_all_task, failed_rows)
    return {"status": "started", "message": f"Запущена переоценка {len(failed_rows)} вакансий"}
