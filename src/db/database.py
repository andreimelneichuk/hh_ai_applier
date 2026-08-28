import sqlite3
from datetime import datetime
import os
from typing import Optional, List, Dict, Any
from src.core.paths import get_app_data_dir, get_bundle_dir
import shutil

DB_PATH = os.getenv("HH_DB_PATH", os.path.join(get_app_data_dir(), "jobs.db"))

DEFAULT_SYSTEM_PROMPT = """Вы — профессиональный IT-рекрутер и карьерный консультант. Ваша цель — оценить релевантность вакансии резюме кандидата и составить краткое, сильное и персонализированное сопроводительное письмо (Cover Letter) для hh.ru.

РЕЗЮМЕ КАНДИДАТА:
{resume_text}

---

ВАКАНСИЯ:
Название: {vacancy_title}
Компания: {company}
Зарплата: {salary}
Ключевые навыки: {skills}
Описание:
{description}

---

ТРЕБОВАНИЯ К ОЦЕНКЕ:
1. Оцените совпадение стека и опыта кандидата с требованиями вакансии от 0 до 100 (match_score).
2. Установите `is_match = True` ТОЛЬКО если `match_score` >= {threshold}, иначе `False`.
3. В `reasoning` кратко (1-2 емких предложения) обоснуйте решение.

ПРАВИЛА СОПРОВОДИТЕЛЬНОГО ПИСЬМА (ТОЛЬКО ЕСЛИ is_match = True):
- ОБЪЕМ: строго 3-5 емких предложений (до 600-800 символов). Рекрутеры тратят на чтение 30 секунд.
- БЕЗ ШАБЛОНОВ И ВОДЫ: Запрещены фразы «Прошу рассмотреть мою кандидатуру», «Я коммуникабельный и стрессоустойчивый», «С большим интересом ознакомился».
- СТРУКТУРА:
  1. Приветствие и цель: «Здравствуйте! Откликаюсь на вакансию [Название позиции] в [Компания].»
  2. Релевантный стек и опыт: 1-2 предложения с точечным совпадением по ключевому стеку и конкретным опытом кандидата, закрывающим задачи из вакансии.
  3. Готовность к диалогу и подпись: «Буду рад подробнее обсудить задачи на интервью. С уважением, [Имя кандидата из резюме]».
- ТОН: деловой, уверенный, живой, на русском языке.
- ПРАВДИВОСТЬ: используйте только факты, опыт и стек, которые РЕАЛЬНО есть в резюме кандидата. Не выдумывайте опыт.

Верните строго JSON в формате:
{{
  "match_score": int,
  "is_match": bool,
  "reasoning": "string",
  "cover_letter": "string"
}}"""

def init_db():
    """Инициализирует базу данных SQLite и создает таблицы, если они не существуют."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_vacancies (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            status TEXT NOT NULL,
            match_score INTEGER,
            analysis_reason TEXT,
            cover_letter TEXT,
            questions_data TEXT,
            applied_resume_id TEXT,
            applied_resume_title TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Миграция: проверяем наличие колонок questions_data, applied_resume_id, applied_resume_title
    cursor.execute("PRAGMA table_info(processed_vacancies)")
    columns = [col[1] for col in cursor.fetchall()]
    if "questions_data" not in columns:
        cursor.execute("ALTER TABLE processed_vacancies ADD COLUMN questions_data TEXT")
    if "applied_resume_id" not in columns:
        cursor.execute("ALTER TABLE processed_vacancies ADD COLUMN applied_resume_id TEXT")
    if "applied_resume_title" not in columns:
        cursor.execute("ALTER TABLE processed_vacancies ADD COLUMN applied_resume_title TEXT")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_profile_answers (
            key TEXT PRIMARY KEY,
            question_hint TEXT NOT NULL,
            answer TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON processed_vacancies(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_vacancies(processed_at)")
    
    # Инициализация дефолтных системных настроек, если они еще не созданы
    default_settings = {
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "primary_provider": "gemini",
        "fallback_enabled": "true",
        "temperature": "0.2"
    }
    for k, v in default_settings.items():
        cursor.execute("SELECT 1 FROM system_settings WHERE key = ?", (k,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO system_settings (key, value) VALUES (?, ?)", (k, v))

    # Инициализация типовых профильных ответов
    default_profile_answers = [
        ("location_city", "Город проживания / Локация", "Россия, г. Пермь (готов к удаленной работе)"),
        ("salary_min", "Зарплатные ожидания", "Рассматриваю предложения от 150 000 руб. на руки в зависимости от задач"),
        ("it_accreditation", "Критична ли IT-аккредитация работодателя?", "Нет, IT-аккредитация не критична"),
        ("test_task", "Готовность к выполнению тестового задания", "Да, готов выполнить адекватное тестовое задание (до 2-4 часов)"),
        ("work_format", "Формат работы", "Удаленная работа (полная занятость)"),
        ("employment_type", "Форма оформления", "ТК РФ, ИП, самозанятость или ГПХ")
    ]
    for key, hint, ans in default_profile_answers:
        cursor.execute("SELECT 1 FROM user_profile_answers WHERE key = ?", (key,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO user_profile_answers (key, question_hint, answer) VALUES (?, ?, ?)", (key, hint, ans))
            
    conn.commit()
    conn.close()

def merge_from_db(source_db_path: str, target_db_path: str = None) -> int:
    """
    Безопасно объединяет данные из другой БД SQLite в целевую БД приложения без перезаписи данных.
    Возвращает количество добавленных новых вакансий.
    """
    if not source_db_path or not os.path.exists(source_db_path):
        return 0
        
    target_path = target_db_path or DB_PATH
    if os.path.abspath(source_db_path) == os.path.abspath(target_path):
        return 0
        
    src_conn = sqlite3.connect(source_db_path)
    src_cur = src_conn.cursor()
    
    tgt_conn = sqlite3.connect(target_path)
    tgt_cur = tgt_conn.cursor()
    
    added_vacancies = 0
    try:
        # 1. Объединяем processed_vacancies
        src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='processed_vacancies'")
        if src_cur.fetchone():
            src_cur.execute("PRAGMA table_info(processed_vacancies)")
            src_cols = [c[1] for c in src_cur.fetchall()]
            
            src_cur.execute("SELECT * FROM processed_vacancies")
            rows = src_cur.fetchall()
            for r in rows:
                row_dict = dict(zip(src_cols, r))
                v_id = row_dict.get("id")
                if not v_id:
                    continue
                tgt_cur.execute("SELECT 1 FROM processed_vacancies WHERE id = ?", (v_id,))
                if not tgt_cur.fetchone():
                    tgt_cur.execute("""
                        INSERT INTO processed_vacancies (
                            id, title, company, status, match_score, analysis_reason, 
                            cover_letter, questions_data, applied_resume_id, applied_resume_title, processed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        row_dict.get("id"),
                        row_dict.get("title", ""),
                        row_dict.get("company", ""),
                        row_dict.get("status", "new"),
                        row_dict.get("match_score", 0),
                        row_dict.get("analysis_reason"),
                        row_dict.get("cover_letter"),
                        row_dict.get("questions_data"),
                        row_dict.get("applied_resume_id"),
                        row_dict.get("applied_resume_title"),
                        row_dict.get("processed_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ))
                    added_vacancies += 1
                    
        # 2. Объединяем app_config (если ключа нет в целевой БД)
        src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='app_config'")
        if src_cur.fetchone():
            src_cur.execute("SELECT key, value FROM app_config")
            for k, v in src_cur.fetchall():
                tgt_cur.execute("SELECT 1 FROM app_config WHERE key = ?", (k,))
                if not tgt_cur.fetchone():
                    tgt_cur.execute("INSERT INTO app_config (key, value) VALUES (?, ?)", (k, v))
                    
        # 3. Объединяем system_settings
        src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='system_settings'")
        if src_cur.fetchone():
            src_cur.execute("SELECT key, value FROM system_settings")
            for k, v in src_cur.fetchall():
                tgt_cur.execute("SELECT 1 FROM system_settings WHERE key = ?", (k,))
                if not tgt_cur.fetchone():
                    tgt_cur.execute("INSERT INTO system_settings (key, value) VALUES (?, ?)", (k, v))
                    
        # 4. Объединяем user_profile_answers
        src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_profile_answers'")
        if src_cur.fetchone():
            src_cur.execute("SELECT key, question_hint, answer FROM user_profile_answers")
            for k, qh, ans in src_cur.fetchall():
                tgt_cur.execute("SELECT 1 FROM user_profile_answers WHERE key = ?", (k,))
                if not tgt_cur.fetchone():
                    tgt_cur.execute("INSERT INTO user_profile_answers (key, question_hint, answer) VALUES (?, ?, ?)", (k, qh, ans))

        tgt_conn.commit()
    except Exception as e:
        tgt_conn.rollback()
        raise e
    finally:
        src_conn.close()
        tgt_conn.close()
        
    return added_vacancies

def is_vacancy_processed(vacancy_id: str) -> bool:
    """Проверяет, была ли вакансия уже обработана ранее (проигнорирована или отправлена)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed_vacancies WHERE id = ?", (vacancy_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def save_vacancy(vacancy_id: str, title: str, company: str, status: str, 
                 match_score: int = None, analysis_reason: str = None, 
                 cover_letter: str = None, questions_data: str = None,
                 applied_resume_id: str = None, applied_resume_title: str = None):
    """Сохраняет или обновляет информацию о вакансии в базе данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT OR REPLACE INTO processed_vacancies 
        (id, title, company, status, match_score, analysis_reason, cover_letter, questions_data, applied_resume_id, applied_resume_title, processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vacancy_id, title, company, status, match_score, analysis_reason, cover_letter, questions_data, applied_resume_id, applied_resume_title, now))
    conn.commit()
    conn.close()

# Алиас для единообразия
save_processed_vacancy = save_vacancy

def get_all_processed():
    """Возвращает все записи из базы данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM processed_vacancies ORDER BY processed_at ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows

get_all_vacancies = get_all_processed

def get_processed_paginated(status: Optional[str] = None, limit: int = 20, offset: int = 0):
    """Возвращает отфильтрованные вакансии порциями."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    query = "SELECT id, title, company, status, match_score, analysis_reason, cover_letter, questions_data, applied_resume_id, applied_resume_title, processed_at FROM processed_vacancies"
    params = []
    
    if status == "matched":
        query += " WHERE status IN ('new', 'needs_answers', 'applied', 'already_applied')"
    elif status == "applied":
        query += " WHERE status IN ('applied', 'already_applied')"
    elif status and status != "all":
        query += " WHERE status = ?"
        params.append(status)
        
    query += " ORDER BY processed_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_processed_count(status: Optional[str] = None):
    """Возвращает количество вакансий по фильтру."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    query = "SELECT COUNT(*) FROM processed_vacancies"
    params = []
    
    if status == "matched":
        query += " WHERE status IN ('new', 'needs_answers', 'applied', 'already_applied')"
    elif status == "applied":
        query += " WHERE status IN ('applied', 'already_applied')"
    elif status and status != "all":
        query += " WHERE status = ?"
        params.append(status)
        
    cursor.execute(query, params)
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_user_profile_answers() -> list:
    """Возвращает список всех сохраненных профильных ответов."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT key, question_hint, answer, updated_at FROM user_profile_answers ORDER BY updated_at ASC")
    rows = cursor.fetchall()
    conn.close()
    return [{"key": r[0], "question_hint": r[1], "answer": r[2], "updated_at": r[3]} for r in rows]

def set_user_profile_answer(key: str, question_hint: str, answer: str):
    """Сохраняет или обновляет профильный ответ."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO user_profile_answers (key, question_hint, answer, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    """, (key, question_hint, answer))
    conn.commit()
    conn.close()

def delete_user_profile_answer(key: str):
    """Удаляет профильный ответ."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_profile_answers WHERE key = ?", (key,))
    conn.commit()
    conn.close()

def update_vacancy_questions(vacancy_id: str, questions_data: str, status: Optional[str] = None):
    """Обновляет JSON вопросов и при необходимости статус вакансии."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if status:
        cursor.execute("UPDATE processed_vacancies SET questions_data = ?, status = ? WHERE id = ?", (questions_data, status, vacancy_id))
    else:
        cursor.execute("UPDATE processed_vacancies SET questions_data = ? WHERE id = ?", (questions_data, vacancy_id))
    conn.commit()
    conn.close()

def update_vacancy_status(vacancy_id: str, status: str):
    """Обновляет статус вакансии."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE processed_vacancies SET status = ? WHERE id = ?", (status, vacancy_id))
    conn.commit()
    conn.close()

def delete_vacancy(vacancy_id: str):
    """Удаляет запись о вакансии из БД (для повторного анализа)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM processed_vacancies WHERE id = ?", (vacancy_id,))
    conn.commit()
    conn.close()

def get_vacancy(vacancy_id: str):
    """Возвращает одну запись о вакансии по ID."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM processed_vacancies WHERE id = ?", (vacancy_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def set_config_value(key: str, value: str):
    """Сохраняет или обновляет значение конфигурации (например, токены)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO app_config (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (key, value))
    conn.commit()
    conn.close()

def get_config_value(key: str) -> str:
    """Возвращает значение конфигурации по ключу."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM app_config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_system_setting(key: str, value: str):
    """Сохраняет системную настройку (например, системный промпт)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO system_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (key, value))
    conn.commit()
    conn.close()

def get_system_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Возвращает системную настройку по ключу или дефолтное значение."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0] is not None:
        return row[0]
    return default

def get_all_system_settings() -> dict:
    """Возвращает словарь всех системных настроек."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM system_settings")
    rows = cursor.fetchall()
    conn.close()
    settings = {r[0]: r[1] for r in rows}
    if "system_prompt" not in settings or not settings["system_prompt"]:
        settings["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    if "primary_provider" not in settings:
        settings["primary_provider"] = "gemini"
    if "fallback_enabled" not in settings:
        settings["fallback_enabled"] = "true"
    if "temperature" not in settings:
        settings["temperature"] = "0.2"
    return settings

def reset_system_prompt_to_default() -> str:
    """Сбрасывает системный промпт к эталонному значению."""
    set_system_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    return DEFAULT_SYSTEM_PROMPT
