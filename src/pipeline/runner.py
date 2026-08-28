import os
import sys
import logging
from typing import List, Dict, Any
from src.core.config import Config
from src.db import database
from src.clients.browser import HHBrowserClient
from src.clients.llm import LLMAnalyzer

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "applier.log"), encoding='utf-8')
    ]
)
logger = logging.getLogger("MainPipeline")

# Локальный резервный путь к резюме
RESUME_FILENAME = "resume.md"
RESUME_ALT_FILENAME = "optimized_resume.md"

def load_resume_text() -> str:
    """Загружает текст резюме из локального файла в случае отсутствия токена или ошибки API."""
    from src.core.paths import get_app_data_dir, get_bundle_dir
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app_data_dir = get_app_data_dir()
    cwd_dir = os.getcwd()
    
    local_paths = [
        os.path.join(base_dir, RESUME_FILENAME),
        os.path.join(base_dir, RESUME_ALT_FILENAME),
        os.path.join(app_data_dir, RESUME_FILENAME),
        os.path.join(app_data_dir, RESUME_ALT_FILENAME),
        os.path.join(cwd_dir, RESUME_FILENAME),
        os.path.join(cwd_dir, RESUME_ALT_FILENAME),
    ]
    
    path_to_use = None
    for p in local_paths:
        if os.path.exists(p):
            path_to_use = p
            break

    if not path_to_use:
        logger.error(f"Локальный файл резюме не найден! Проверены пути: {', '.join(set(local_paths))}")
        return ""
        
    try:
        with open(path_to_use, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.exception(f"Не удалось прочитать локальный файл резюме {path_to_use}: {e}")
        return ""

def format_hh_resume_to_text(resume_data: Dict[str, Any]) -> str:
    """Форматирует JSON-структуру резюме с hh.ru в структурированный текст для LLM."""
    if not resume_data:
        return ""
    
    parts = []
    
    # ФИО и заголовок
    name = f"{resume_data.get('first_name', '')} {resume_data.get('middle_name', '')} {resume_data.get('last_name', '')}".strip()
    title = resume_data.get('title', 'Специалист')
    parts.append(f"ФИО: {name}")
    parts.append(f"Желаемая должность: {title}")
    
    # Обо мне
    skills_description = resume_data.get('skills', '')
    if skills_description:
        parts.append(f"\nОбо мне / Навыки:\n{skills_description}")
        
    # Ключевые навыки
    key_skills = [s.get('name') for s in resume_data.get('key_skills', []) if s.get('name')]
    if key_skills:
        parts.append(f"\nКлючевые навыки: {', '.join(key_skills)}")
        
    # Опыт работы
    experience = resume_data.get('experience', [])
    if experience:
        parts.append("\nОпыт работы:")
        for exp in experience:
            company = exp.get('company', 'Не указано')
            position = exp.get('position', 'Не указано')
            description = exp.get('description', '')
            start = exp.get('start', '')
            end = exp.get('end', 'по настоящее время')
            parts.append(f"- {position} в {company} ({start} - {end})")
            if description:
                parts.append(f"  Обязанности:\n  {description}")
                
    # Образование
    education = resume_data.get('education', {})
    primary_edu = education.get('primary', [])
    if primary_edu:
        parts.append("\nОбразование:")
        for edu in primary_edu:
            name = edu.get('name', 'Не указано')
            organization = edu.get('organization', 'Не указано')
            result = edu.get('result', '')
            year = edu.get('year', '')
            parts.append(f"- {name} ({organization}), специальность: {result}, год окончания: {year}")
            
    # Если структурированный опыт пуст, добавляем полный текст со страницы резюме
    if not experience and resume_data.get('raw_text'):
        parts.append(f"\nПолный текст резюме со страницы:\n{resume_data.get('raw_text')}")
        
    return "\n".join(parts)

def run_pipeline(queries: List[str] = None, area_id: str = None, 
                 threshold: int = None, resume_id: str = None, 
                 dry_run: bool = None, max_process: int = 10,
                 on_step_change = None, should_stop = None) -> Dict[str, Any]:
    """
    Запускает конвейер поиска, анализа и отправки откликов через браузерную автоматизацию.
    """
    logger.info("=== Запуск сервиса откликов на вакансии hh.ru (Браузерная версия) ===")
    
    # 1. Разрешение параметров
    target_queries = queries if queries is not None else Config.SEARCH_QUERIES
    target_area = area_id if area_id is not None else Config.SEARCH_AREA
    target_threshold = threshold if threshold is not None else Config.MATCH_THRESHOLD
    target_dry_run = dry_run if dry_run is not None else Config.DRY_RUN
    
    # 2. Инициализация БД
    database.init_db()
    
    # Создаем клиенты
    hh_client = HHBrowserClient()
    analyzer = LLMAnalyzer()
    
    try:
        hh_client.start()
        
        # Определение ID резюме (берем переданный, или из БД, или из конфига/env)
        target_resume_id = resume_id
        if not target_resume_id:
            target_resume_id = database.get_config_value("resume_id")
        if not target_resume_id:
            target_resume_id = Config.HH_RESUME_ID
            
        if target_resume_id and target_resume_id.startswith("your_"):
            target_resume_id = ""
            
        # Проверка авторизации перед запуском
        if not hh_client.is_logged_in():
            logger.error("Пользователь не авторизован в браузере. Запуск невозможен. Пройдите авторизацию через интерфейс.")
            return {"status": "error", "message": "User is not logged in. Open login browser first."}
            
        # 3. Загрузка резюме пользователя (поддержка режима 'Все резюме')
        is_all_resumes = (not target_resume_id or target_resume_id.lower() in ("all", "__all__"))
        candidate_resumes: List[Dict[str, Any]] = []

        if is_all_resumes:
            logger.info("Включен режим 'Все резюме (Автовыбор ИИ)'. Загрузка всех резюме пользователя...")
            all_my_resumes = hh_client.get_my_resumes()
            for r in all_my_resumes:
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
            if candidate_resumes:
                logger.info(f"Успешно загружено {len(candidate_resumes)} резюме пользователя для сравнительного анализа.")
        else:
            logger.info(f"Загрузка выбранного резюме {target_resume_id} из браузера...")
            resume_data = hh_client.get_resume(target_resume_id)
            if resume_data:
                r_text = format_hh_resume_to_text(resume_data)
                candidate_resumes.append({
                    "id": target_resume_id,
                    "title": resume_data.get("title") or "Резюме",
                    "text": r_text
                })
                logger.info(f"Резюме '{resume_data.get('title')}' успешно загружено.")
            else:
                logger.warning("Не удалось получить резюме по сети. Попытка загрузить из локального файла.")

        if not candidate_resumes:
            logger.info("Загрузка текста резюме из локального файла...")
            local_text = load_resume_text()
            if local_text:
                candidate_resumes.append({
                    "id": target_resume_id or "local",
                    "title": "Локальное резюме",
                    "text": local_text
                })

        if not candidate_resumes:
            logger.error("Текст резюме отсутствует. Запуск конвейера невозможен.")
            return {"status": "error", "message": "Resume content is empty"}
            
        logger.info(f"К анализу готово резюме: {len(candidate_resumes)} шт. ({', '.join(r['title'] for r in candidate_resumes)})")
        if target_dry_run:
            logger.info("[РЕЖИМ DRY RUN] Скрипт запущен в тестовом режиме. Реальных откликов отправлено не будет.")
            
        # Счётчики для итоговой статистики
        stats = {
            "searched": 0,
            "processed": 0,
            "already_known": 0,
            "matched": 0,
            "ignored": 0,
            "applied": 0,
            "failed": 0
        }
        
        # 4. Сбор вакансий: рекомендации hh.ru по всем/выбранным резюме
        all_found_vacancies = {}
        stopped_by_user = False
        
        for cand_res in candidate_resumes:
            if should_stop and should_stop():
                stopped_by_user = True
                break
            r_id = cand_res.get("id")
            if not r_id or r_id == "local":
                continue
            logger.info(f"Сбор рекомендованных вакансий hh.ru для резюме '{cand_res.get('title')}' (ID: {r_id})...")
            recommended = hh_client.get_recommended_vacancies(
                resume_id=r_id,
                area_id=target_area,
                period_days=3,
                max_pages=2 if len(candidate_resumes) > 1 else 3
            )
            for v in recommended:
                all_found_vacancies[v["id"]] = v
            logger.info(f"Найдено {len(recommended)} рекомендаций для резюме '{cand_res.get('title')}'. Всего уникальных: {len(all_found_vacancies)}")
                
        # Дополнительный поиск по текстовым запросам (если указаны)
        if not stopped_by_user and target_queries:
            for query in target_queries:
                if not query or not query.strip():
                    continue
                if should_stop and should_stop():
                    logger.info("Поиск вакансий остановлен по запросу пользователя.")
                    stopped_by_user = True
                    break
                logger.info(f"Выполняется дополнительный поиск по запросу: '{query}'...")
                found = hh_client.search_vacancies(query, area_id=target_area, period_days=3, max_pages=1)
                for v in found:
                    all_found_vacancies[v["id"]] = v
                
        stats["searched"] = len(all_found_vacancies)
        logger.info(f"Всего уникальных вакансий для анализа: {stats['searched']}")
        
        # 5. Анализ каждой найденной вакансии
        if not stopped_by_user:
            user_saved_answers = database.get_user_profile_answers()
            for vacancy_id, base_info in all_found_vacancies.items():
                if should_stop and should_stop():
                    logger.info("Обработка вакансий остановлена по запросу пользователя.")
                    stopped_by_user = True
                    break
                    
                title = base_info["title"]
                company = base_info["company"]
                
                if database.is_vacancy_processed(vacancy_id):
                    logger.info(f"Пропуск: вакансия {vacancy_id} ({title} - {company}) уже есть в БД.")
                    stats["already_known"] += 1
                    continue
                    
                if stats["processed"] >= max_process:
                    logger.info(f"Достигнут лимит в {max_process} новых вакансий за один запуск. Прерываем обработку остальных.")
                    break
                    
                # Мгновенная проверка остановки пользователем
                if should_stop and should_stop():
                    logger.info("Сканирование немедленно остановлено по кнопке 'Остановить'.")
                    stopped_by_user = True
                    break
                    
                stats["processed"] += 1
                logger.info(f"\n--- Обработка вакансии [{stats['processed']}]: {title} ({company}) [ID: {vacancy_id}] ---")
                
                if on_step_change:
                    on_step_change({"id": vacancy_id, "title": title, "company": company})
                    
                # Получаем полные детали вакансии через браузер
                details = hh_client.get_vacancy_details(vacancy_id)
                if not details:
                    logger.warning(f"Не удалось получить детали для вакансии {vacancy_id}, пропускаем.")
                    continue
                    
                # Мгновенная проверка остановки перед запросом к LLM
                if should_stop and should_stop():
                    logger.info("Сканирование немедленно остановлено перед запросом к LLM.")
                    stopped_by_user = True
                    break

                # Проверяем, откликнулись ли уже на эту вакансию ранее
                if details.get("already_applied"):
                    logger.info(f"На вакансию {vacancy_id} ({title} - {company}) уже откликнулись ранее.")
                    database.save_vacancy(
                        vacancy_id=vacancy_id,
                        title=title,
                        company=company,
                        status="already_applied",
                        match_score=100,
                        analysis_reason="Уже откликнулись ранее (обнаружено на странице вакансии)",
                        cover_letter=""
                    )
                    continue
                    
                # Анализируем вакансию через LLM со сравнением всех резюме кандидата
                from llm_analyzer import QuotaExceededError
                try:
                    analysis = analyzer.analyze_vacancy(
                        resumes=candidate_resumes,
                        vacancy=details,
                        threshold=target_threshold
                    )
                except QuotaExceededError as qe:
                    logger.error(f"Превышена квота запросов к Gemini API (429): {qe}. Принудительная остановка сканирования.")
                    database.save_vacancy(
                        vacancy_id=vacancy_id,
                        title=title,
                        company=company,
                        status="failed",
                        match_score=0,
                        analysis_reason="Превышена квота запросов к Gemini API (429 Quota Exceeded)",
                        cover_letter=""
                    )
                    stats["failed"] += 1
                    return {
                        "status": "error",
                        "error": "Превышена квота запросов к Gemini API (429 Quota Exceeded). Сканирование остановлено.",
                        "stats": stats
                    }
                except Exception as llm_err:
                    logger.error(f"Ошибка LLM при анализе вакансии {vacancy_id} ({title}): {llm_err}")
                    database.save_vacancy(
                        vacancy_id=vacancy_id,
                        title=title,
                        company=company,
                        status="failed",
                        match_score=0,
                        analysis_reason=f"Ошибка LLM: {str(llm_err)}",
                        cover_letter=""
                    )
                    stats["failed"] += 1
                    continue
                
                chosen_resume_id = analysis.selected_resume_id or (candidate_resumes[0]["id"] if candidate_resumes else target_resume_id)
                chosen_resume_title = analysis.selected_resume_title or (candidate_resumes[0]["title"] if candidate_resumes else "Резюме")
                
                # Находим текст выбранного резюме для возможных ответов на вопросы
                chosen_resume_text = next((r["text"] for r in candidate_resumes if r["id"] == chosen_resume_id), candidate_resumes[0]["text"] if candidate_resumes else "")

                if analysis.is_match:
                    stats["matched"] += 1
                    logger.info(f"ВАКАНСИЯ ПОДХОДИТ! Совпадение: {analysis.match_score}%. Выбранное резюме: '{chosen_resume_title}' (ID: {chosen_resume_id})")
                    logger.info(f"Причина: {analysis.reasoning}")
                    
                    # Проверяем остановку перед откликом
                    if should_stop and should_stop():
                        logger.info("Обработка вакансий остановлена перед откликом.")
                        stopped_by_user = True
                        break

                    # Проверяем наличие вопросов/теста от работодателя
                    questions = hh_client.get_vacancy_questions(vacancy_id)
                    questions_data_str = None
                    answers_dict = None
                    needs_user_answers = False

                    if questions:
                        logger.info(f"Обнаружено {len(questions)} вопросов от работодателя. Генерация ответов через ИИ на основе резюме '{chosen_resume_title}'...")
                        import json
                        q_res = analyzer.answer_questions(chosen_resume_text, details, questions, user_saved_answers)
                        questions_data_str = json.dumps([a.model_dump() for a in q_res.answers], ensure_ascii=False)
                        answers_dict = {a.id: a.answer for a in q_res.answers}
                        
                        if not q_res.all_confident or any(a.requires_user_input or a.confidence < 85 for a in q_res.answers):
                            logger.info(f"Вопросы требуют личного подтверждения кандидата. Перевод в статус 'needs_answers'.")
                            needs_user_answers = True

                    # Откликаемся или сохраняем как готовую к отклику при Dry Run / needs_answers
                    if needs_user_answers:
                        status = "needs_answers"
                        logger.info(f"Вакансия {vacancy_id} сохранена со статусом 'needs_answers' (требуются ответы).")
                    elif target_dry_run:
                        logger.info(f"[Dry Run] Вакансия сохранена как релевантная (статус: new). Отклик не отправлялся.")
                        status = "new"
                    else:
                        if not chosen_resume_id or chosen_resume_id == "local":
                            logger.error(f"Невозможно отправить отклик на {vacancy_id}: Резюме не выбрано или только локальный файл!")
                            database.save_vacancy(
                                vacancy_id=vacancy_id,
                                title=title,
                                company=company,
                                status="failed",
                                match_score=analysis.match_score,
                                analysis_reason="Резюме не найдено в профиле HH для отклика",
                                cover_letter=analysis.cover_letter,
                                questions_data=questions_data_str,
                                applied_resume_id=chosen_resume_id,
                                applied_resume_title=chosen_resume_title
                            )
                            stats["failed"] += 1
                            continue
                            
                        success, err_msg = hh_client.apply_to_vacancy(
                            vacancy_id=vacancy_id,
                            resume_title_or_id=chosen_resume_id,
                            cover_letter=analysis.cover_letter,
                            answers=answers_dict,
                            dry_run=False
                        )
                        
                        if success:
                            if err_msg == "ALREADY_APPLIED":
                                status = "already_applied"
                                logger.info(f"Уже откликнулись ранее.")
                            else:
                                status = "applied"
                                stats["applied"] += 1
                                logger.info(f"Успешный отклик отправлен с резюме '{chosen_resume_title}'.")
                        else:
                            status = "failed"
                            stats["failed"] += 1
                            logger.error(f"Ошибка отклика на {vacancy_id}: {err_msg}")
                        
                    database.save_vacancy(
                        vacancy_id=vacancy_id,
                        title=title,
                        company=company,
                        status=status,
                        match_score=analysis.match_score,
                        analysis_reason=analysis.reasoning,
                        cover_letter=analysis.cover_letter,
                        questions_data=questions_data_str,
                        applied_resume_id=chosen_resume_id,
                        applied_resume_title=chosen_resume_title
                    )
                else:
                    stats["ignored"] += 1
                    logger.info(f"Вакансию пропускаем. Совпадение: {analysis.match_score}%. Резюме: {chosen_resume_title}")
                    logger.info(f"Причина отсева: {analysis.reasoning}")
                    
                    database.save_vacancy(
                        vacancy_id=vacancy_id,
                        title=title,
                        company=company,
                        status="ignored",
                        match_score=analysis.match_score,
                        analysis_reason=analysis.reasoning,
                        cover_letter="",
                        applied_resume_id=chosen_resume_id,
                        applied_resume_title=chosen_resume_title
                    )
                
        # 6. Итоговый отчет
        logger.info("\n=== РАБОТА СЕРВИСА ЗАВЕРШЕНА ===")
        logger.info(f"Найдено вакансий в поиске: {stats['searched']}")
        logger.info(f"Уже были обработаны ранее: {stats['already_known']}")
        logger.info(f"Новых обработано в этой сессии: {stats['processed']}")
        logger.info(f"  - Из них подошли по стеку и опыту: {stats['matched']}")
        logger.info(f"  - Из них не подошли (отсеяны): {stats['ignored']}")
        logger.info(f"  - Успешных откликов: {stats['applied']}")
        logger.info(f"  - Ошибок при отклике: {stats['failed']}")
        
        if on_step_change:
            on_step_change(None)
            
        return {
            "status": "stopped" if stopped_by_user else "success",
            "message": "Сканирование остановлено пользователем" if stopped_by_user else "Успешно завершено",
            "stats": stats
        }
    finally:
        hh_client.stop()

def run():
    """Совместимая точка входа для консольного запуска."""
    # Валидация
    warnings = Config.validate()
    for warning in warnings:
        logger.warning(warning)
    run_pipeline()

if __name__ == "__main__":
    run()
