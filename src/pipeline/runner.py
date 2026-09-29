import os
import sys
import logging
from typing import List, Dict, Any
from src.core.config import Config
from src.core.paths import get_app_data_dir, get_bundle_dir
from src.db import database
from src.clients.browser import HHBrowserClient
from src.clients.llm import LLMAnalyzer, QuotaExceededError
from src.pipeline.processing import decide_and_apply

# Настройка логирования
log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    log_dir = get_app_data_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "applier.log")
    log_handlers.append(logging.FileHandler(log_file, encoding='utf-8'))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger("MainPipeline")

# Локальный резервный путь к резюме
RESUME_FILENAME = "resume.md"
RESUME_ALT_FILENAME = "optimized_resume.md"

def load_resume_text() -> str:
    """Загружает текст резюме из локального файла в случае отсутствия токена или ошибки API."""
    bundle_dir = get_bundle_dir()
    app_data_dir = get_app_data_dir()
    cwd_dir = os.getcwd()
    
    local_paths = [
        os.path.join(bundle_dir, RESUME_FILENAME),
        os.path.join(bundle_dir, RESUME_ALT_FILENAME),
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
    from unittest.mock import MagicMock
    raw_first = resume_data.get('first_name')
    first = str(raw_first).strip() if (raw_first is not None and not isinstance(raw_first, MagicMock)) else ""
    raw_last = resume_data.get('last_name')
    last = str(raw_last).strip() if (raw_last is not None and not isinstance(raw_last, MagicMock)) else ""
    raw_middle = resume_data.get('middle_name')
    middle = str(raw_middle).strip() if (raw_middle is not None and not isinstance(raw_middle, MagicMock)) else ""
    name = " ".join(p for p in [first, middle, last] if p)
    if not name or name.lower() in ("кандидат", "candidate"):
        try:
            from src.db import database
            for item in database.get_user_profile_answers():
                if item.get("key") == "candidate_name" and item.get("answer"):
                    name = item.get("answer").strip()
                    break
        except Exception:
            pass

    if name:
        parts.append(f"ФИО: {name}")

    gender = resume_data.get('gender')
    if not gender or isinstance(gender, MagicMock):
        try:
            from src.db import database
            for item in database.get_user_profile_answers():
                if item.get("key") == "candidate_gender" and item.get("answer"):
                    gender = item.get("answer").strip()
                    break
        except Exception:
            gender = None

    if gender:
        parts.append(f"Пол: {gender}")

    raw_title = resume_data.get('title')
    title = str(raw_title).strip() if (raw_title is not None and not isinstance(raw_title, MagicMock)) else 'Специалист'
    parts.append(f"Желаемая должность: {title}")

    total_exp = resume_data.get('total_experience')
    if total_exp:
        parts.append(f"Общий стаж работы: {total_exp}")
        
    location = resume_data.get('location') or resume_data.get('city')
    if location:
        parts.append(f"Город / Локация: {location}")
        
    relocation = resume_data.get('relocation')
    relocation_cities = resume_data.get('relocation_cities')
    
    # Резервный источник: сохраненные ответы профиля пользователя
    if not relocation and not relocation_cities:
        try:
            from src.db import database
            user_saved = database.get_user_profile_answers()
            for item in user_saved:
                k = item.get("key", "").lower()
                if k in ("relocation_cities", "relocate", "relocation", "target_cities"):
                    relocation = item.get("answer")
                    break
        except Exception:
            pass

    if relocation:
        parts.append(f"Готовность к переезду: {relocation}")
        
    if relocation_cities:
        if isinstance(relocation_cities, list):
            cities_str = ", ".join(relocation_cities)
        else:
            cities_str = str(relocation_cities)
        if cities_str:
            parts.append(f"Города, куда готов переехать: {cities_str}")
        
    employment_pref = resume_data.get('employment') or resume_data.get('employment_preference')
    if employment_pref:
        parts.append(f"Предпочитаемая занятость: {employment_pref}")
        
    schedule_pref = resume_data.get('schedule') or resume_data.get('schedule_preference')
    if schedule_pref:
        parts.append(f"Предпочитаемый график: {schedule_pref}")
    
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
            period = exp.get('period', '')
            start = exp.get('start', '')
            end = exp.get('end', 'по настоящее время')
            date_info = period if period else (f"{start} - {end}" if start else "")
            date_str = f" ({date_info})" if date_info else ""
            parts.append(f"- {position} в {company}{date_str}")
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

def _resume_entry(resume_id: str, resume_data: Dict[str, Any], listed: Dict[str, Any] = None) -> Dict[str, Any]:
    listed = listed or {}
    return {
        "id": resume_id,
        "title": listed.get("title") or resume_data.get("title") or "Резюме",
        "text": format_hh_resume_to_text(resume_data),
        # Имя и пол нужны ИИ для подписи и согласования рода в сопроводительном письме
        "first_name": resume_data.get("first_name") or listed.get("first_name"),
        "last_name": resume_data.get("last_name") or listed.get("last_name"),
        "gender": resume_data.get("gender") or listed.get("gender"),
    }


def load_candidate_resumes(hh_client: HHBrowserClient, target_resume_id: str = None) -> List[Dict[str, Any]]:
    """Загружает резюме кандидата для анализа: все резюме профиля или одно выбранное, иначе локальный файл."""
    candidate_resumes: List[Dict[str, Any]] = []
    if not target_resume_id or target_resume_id.lower() in ("all", "__all__"):
        logger.info("Режим 'Все резюме (Автовыбор ИИ)'. Загрузка всех резюме пользователя...")
        for listed in hh_client.get_my_resumes():
            r_id = listed.get("id")
            if not r_id:
                continue
            r_data = hh_client.get_resume(r_id)
            if r_data:
                entry = _resume_entry(r_id, r_data, listed)
                if entry["text"]:
                    candidate_resumes.append(entry)
    else:
        logger.info(f"Загрузка выбранного резюме {target_resume_id} из браузера...")
        r_data = hh_client.get_resume(target_resume_id)
        if r_data:
            candidate_resumes.append(_resume_entry(target_resume_id, r_data))
        else:
            logger.warning("Не удалось получить резюме по сети. Попытка загрузить из локального файла.")

    if not candidate_resumes:
        local_text = load_resume_text()
        if local_text:
            candidate_resumes.append({
                "id": target_resume_id or "local",
                "title": "Локальное резюме",
                "text": local_text
            })
    return candidate_resumes


def run_pipeline(queries: List[str] = None, area_id: str = None, 
                 threshold: int = None, resume_id: str = None, 
                 dry_run: bool = None, max_process: int = 10,
                 stop_condition: str = None, limit_applications: int = None,
                 limit_processed: int = None,
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

    # Условия автоостановки
    target_stop_condition = (stop_condition or database.get_config_value("stop_condition") or "both").lower()
    
    if limit_applications is not None:
        target_limit_apps = limit_applications
    else:
        db_limit_apps = database.get_config_value("limit_applications")
        try:
            target_limit_apps = int(db_limit_apps) if db_limit_apps is not None else 10
        except ValueError:
            target_limit_apps = 10

    if limit_processed is not None:
        target_limit_proc = limit_processed
    elif max_process is not None and max_process != 10:
        target_limit_proc = max_process
    else:
        db_limit_proc = database.get_config_value("limit_processed")
        try:
            target_limit_proc = int(db_limit_proc) if db_limit_proc is not None else (max_process or 20)
        except ValueError:
            target_limit_proc = max_process or 20
    
    logger.info(f"Параметры автоостановки: критерий '{target_stop_condition}', лимит откликов: {target_limit_apps}, лимит оценок: {target_limit_proc}")
    
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
            return {"status": "error", "reason": "not_logged_in", "message": "Сессия hh.ru не активна. Войдите заново через кнопку входа."}
            
        # 3. Загрузка резюме пользователя (поддержка режима 'Все резюме')
        candidate_resumes = load_candidate_resumes(hh_client, target_resume_id)

        if not candidate_resumes:
            logger.error("Текст резюме отсутствует. Запуск конвейера невозможен.")
            return {"status": "error", "message": "Резюме не найдено: выберите резюме в настройках или проверьте вход в hh.ru."}
            
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
                    
                # Проверка лимитов остановки перед обработкой следующей вакансии
                stop_by_processed = (
                    target_stop_condition in ("processed", "both")
                    and target_limit_proc > 0
                    and stats["processed"] >= target_limit_proc
                )
                effective_applied = stats["applied"] if not target_dry_run else stats["matched"]
                stop_by_applied = (
                    target_stop_condition in ("applications", "both")
                    and target_limit_apps > 0
                    and effective_applied >= target_limit_apps
                )

                if stop_by_processed:
                    logger.info(f"🛑 Достигнут лимит оценки вакансий ({stats['processed']} из {target_limit_proc}). Автоостановка конвейера.")
                    stats["stopped_reason"] = "limit_processed"
                    break

                if stop_by_applied:
                    logger.info(f"🛑 Достигнут лимит откликов ({effective_applied} из {target_limit_apps}). Автоостановка конвейера.")
                    stats["stopped_reason"] = "limit_applications"
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
                
                decision = decide_and_apply(
                    hh_client, analyzer, vacancy_id, details, analysis, candidate_resumes,
                    threshold=target_threshold,
                    dry_run=target_dry_run,
                    user_saved_answers=user_saved_answers,
                    should_stop=should_stop
                )

                if decision.is_eligible:
                    stats["matched"] += 1
                    logger.info(f"ВАКАНСИЯ ПОДХОДИТ! Совпадение: {analysis.match_score}% (порог: {target_threshold}%). Выбранное резюме: '{decision.resume_title}' (ID: {decision.resume_id})")
                    logger.info(f"Причина: {analysis.reasoning}")
                    if decision.stopped:
                        logger.info("Обработка вакансий остановлена перед откликом.")
                        stopped_by_user = True
                        break
                    if decision.status == "needs_answers":
                        logger.info(f"Вакансия {vacancy_id} сохранена со статусом 'needs_answers' (требуются ответы).")
                    elif decision.status == "new":
                        logger.info("[Dry Run] Вакансия сохранена как релевантная (статус: new). Отклик не отправлялся.")
                    elif decision.status == "applied":
                        stats["applied"] += 1
                        logger.info(f"Успешный отклик отправлен с резюме '{decision.resume_title}'.")
                    elif decision.status == "already_applied":
                        logger.info("Уже откликнулись ранее.")
                    else:
                        stats["failed"] += 1
                        logger.error(f"Ошибка отклика на {vacancy_id}: {decision.error or decision.apply_error}")
                else:
                    stats["ignored"] += 1
                    blocker_msg = f" [Блокер: {analysis.blocker_reason}]" if analysis.has_hard_blocker and analysis.blocker_reason else ""
                    logger.info(f"Вакансию пропускаем. Совпадение: {analysis.match_score}% (порог: {target_threshold}%){blocker_msg}. Резюме: {decision.resume_title}")
                    logger.info(f"Причина отсева: {analysis.reasoning}")
                    # Письмо для неподходящей вакансии не нужно
                    decision.cover_letter = ""

                decision.save(vacancy_id, title, company)

                # Проверка достижения лимитов сразу после завершения обработки вакансии
                effective_applied = stats["applied"] if not target_dry_run else stats["matched"]
                if (target_stop_condition in ("applications", "both") 
                        and target_limit_apps > 0 
                        and effective_applied >= target_limit_apps):
                    logger.info(f"🛑 Достигнут лимит откликов ({effective_applied} из {target_limit_apps}). Автоостановка конвейера.")
                    stats["stopped_reason"] = "limit_applications"
                    break

                if (target_stop_condition in ("processed", "both") 
                        and target_limit_proc > 0 
                        and stats["processed"] >= target_limit_proc):
                    logger.info(f"🛑 Достигнут лимит оценки вакансий ({stats['processed']} из {target_limit_proc}). Автоостановка конвейера.")
                    stats["stopped_reason"] = "limit_processed"
                    break
                
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
