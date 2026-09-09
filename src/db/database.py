import sqlite3
from datetime import datetime
import os
import json
import re
import uuid
from typing import Optional, List, Dict, Any
from src.core.paths import get_app_data_dir, get_bundle_dir
import shutil

DB_PATH = os.getenv("HH_DB_PATH", os.path.join(get_app_data_dir(), "jobs.db"))

DEFAULT_SYSTEM_PROMPT = """Вы — профессиональный IT-рекрутер и эксперт. Оцените соответствие резюме вакансии по 5 шкалам и при совпадении составьте лаконичное сопроводительное письмо.

РЕЗЮМЕ КАНДИДАТА:
{resume_text}

ВАКАНСИЯ:
{vacancy_title} | {company}
Зарплата: {salary} | Опыт: {experience} | Формат: {employment}, {schedule}, {location}
Навыки: {skills}
Описание:
{description}

ОЦЕНКА СООТВЕТСТВИЯ (краткий разбор в reasoning перед выставлением scores):
1. СТЕК И КЛЮЧЕВЫЕ ТЕХНОЛОГИИ (stack_score, 0–30):
   - СТРОГИЙ ЗАПРЕТ: Если вакансия требует ДРУГОЙ ключевой язык или стек (например, C/C++, Embedded Linux, Linux Kernel, Oracle PL/SQL, 1C, Go, Java, Rust, Swift, PHP), а у кандидата в резюме только Python/AI/LLM:
     СТРОГО ставьте stack_score: 0–5, match_score < 40, is_match: false и cover_letter: "". Откликаться на такие вакансии КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
   - 25-30 идеал, 15-24 частичный, 0-14 нет ключевого стека.
2. РЕЛЕВАНТНЫЙ ОПЫТ И СТАЖ В ГОДАХ (experience_score, 0–25): оценивайте именно РЕЛЕВАНТНЫЙ коммерческий опыт под задачи вакансии (20-25 закрывает требование, 10-19 чуть меньше; для неинженерных/непрофильных ролей вроде репетиторства, преподавания, 1С, сисадминства ставьте 0-5).
3. ГРЕЙД И УРОВЕНЬ ОТВЕТСТВЕННОСТИ (grade_score, 0–20): Junior/Middle/Senior/Lead (16-20 точное, 8-15 смежный, 0-7 сильный разрыв; если Senior идет в школьные учителя — ставьте 0-5 из-за Overqualified).
4. ЗАДАЧИ И ДОМЕННАЯ СПЕЦИФИКА (domain_score, 0–15): AI/LLM, финтех, highload и т.д. (12-15 идентично, 6-11 смежно, 0-5 чуждый домен вроде биоинформатики/NGS, школ, курсов, геймдев-видеодизайна).
5. ФОРМАТ РАБОТЫ, ЛОКАЦИЯ И УСЛОВИЯ (format_score, 0–10): 10 удаленка или город проживания кандидата (из резюме/профиля), 5-8 гибрид, 0-2 офис в чужом городе.
6. БЛОКИРУЮЩИЕ ФАКТОРЫ (has_hard_blocker, blocker_reason):
   - ВНИМАНИЕ: Локация, город и формат работы (офис, гибрид) СТРОГО НЕ ЯВЛЯЮТСЯ блокерами! По ним просто выставляется низкий format_score (0-2), чтобы при высоком совпадении стека кандидат мог откликнуться и договориться об удаленке на собеседовании. has_hard_blocker из-за офиса или города ставить ЗАПРЕЩЕНО.
   - has_hard_blocker = true допустимо ТОЛЬКО при абсолютной юридической/технической невозможности: строгое иностранное гражданство, закрытая гостайна.

ПРАВИЛА СОПРОВОДИТЕЛЬНОГО ПИСЬМА (3–5 предложений, до 500 символов, ТОЛЬКО если is_match = true, иначе СТРОГО пустая строка ""):
- СТРОГИЙ ЗАПРЕТ: НЕ писать название позиции («Откликаюсь на...»), НЕ использовать штампы («мой опыт решает задачи», «прошу рассмотреть», «Обратил внимание на задачу по...»), НЕ писать имя компании в лоб.
- СТРУКТУРА:
  1. Приветствие и вход в техническую суть (своими словами, без клише «Обратил внимание на...»).
  2. Фактура и инструмент из РЕЗЮМЕ кандидата (1–2 предложения с подтвержденным кейсом под задачу работодателя).
  3. Партнерское приглашение к диалогу («Буду рад созвониться и обсудить технические задачи команды. С уважением, {candidate_name}»).
- ПОДПИСЬ: СТРОГО «С уважением, {candidate_name}» (НЕ писать «Кандидат»).
- ТОН: уверенный диалог сильного инженера с нанимающим тимлидом, только факты из резюме.

Верните строго JSON:
{{
  "reasoning": "string (краткий разбор совпадения стека, стажа, грейда и формата)",
  "scores": {{
    "stack_score": int,
    "experience_score": int,
    "grade_score": int,
    "domain_score": int,
    "format_score": int
  }},
  "has_hard_blocker": bool,
  "blocker_reason": "string или null",
  "match_score": int,
  "is_match": bool,
  "cover_letter": "string"
}}"""

DEAD_OPENROUTER_MODELS = {
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "google/gemini-2.0-pro-exp-02-05:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "deepseek/deepseek-r1:free",
    "mistralai/mistral-7b-instruct:free",
    "z-ai/glm-5.2:free",
    "meta-llama/llama-3.2-1b-instruct:free",
    "meta-llama/llama-3.2-3b-instruct:free",
}

DEFAULT_PROVIDERS = [
    {
        "id": "gemini",
        "name": "Google Gemini",
        "protocol": "gemini",
        "base_url": "",
        "get_key_url": "https://aistudio.google.com/app/apikey",
        "description": "Мультимодальные модели Google высокой скорости и точности для анализа вакансий.",
        "active_model": "gemini-3.6-flash",
        "temperature": 0.2,
        "legacy_key_names": ["gemini_api_keys", "gemini_api_key", "google_api_keys"],
        "legacy_model_name": "gemini_model",
    },
    {
        "id": "groq",
        "name": "Groq LPU",
        "protocol": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "get_key_url": "https://console.groq.com/keys",
        "description": "Сверхбыстрый LPU-инференс моделей семейства Llama 3 с мгновенным откликом.",
        "active_model": "llama-3.3-70b-versatile",
        "temperature": 0.2,
        "legacy_key_names": ["groq_api_keys", "groq_api_key"],
        "legacy_model_name": "groq_model",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "protocol": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "get_key_url": "https://openrouter.ai/keys",
        "description": "Единый API-шлюз ко множеству современных нейросетей с автоматической маршрутизацией моделей.",
        "active_model": "openrouter/free",
        "temperature": 0.2,
        "legacy_key_names": ["openrouter_api_keys", "openai_api_keys", "openai_api_key"],
        "legacy_model_name": "openrouter_model",
    },
    {
        "id": "mistral",
        "name": "Mistral AI",
        "protocol": "mistral",
        "base_url": "https://api.mistral.ai/v1",
        "get_key_url": "https://console.mistral.ai/api-keys/",
        "description": "Европейский провайдер моделей Mistral для анализа стека и генерации писем.",
        "active_model": "ministral-8b-latest",
        "temperature": 0.2,
        "legacy_key_names": ["mistral_api_keys", "mistral_api_key"],
        "legacy_model_name": "mistral_model",
    },
    {
        "id": "github",
        "name": "GitHub Models",
        "protocol": "openai",
        "base_url": "https://models.inference.ai.azure.com",
        "get_key_url": "https://github.com/settings/tokens",
        "description": "Инференс моделей Azure AI по персональному токену GitHub (PAT classic).",
        "active_model": "gpt-4o-mini",
        "temperature": 0.2,
        "legacy_key_names": ["github_api_keys", "github_token"],
        "legacy_model_name": "github_model",
    },
    {
        "id": "groq",
        "name": "Groq Cloud",
        "protocol": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "get_key_url": "https://console.groq.com/keys",
        "description": "Сверхбыстрый LPU-инференс моделей семейства Llama 3 с мгновенным откликом.",
        "active_model": "llama-3.3-70b-versatile",
        "temperature": 0.2,
        "legacy_key_names": ["groq_api_keys", "groq_api_key"],
        "legacy_model_name": "groq_model",
    },
    {
        "id": "cerebras",
        "name": "Cerebras Cloud",
        "protocol": "openai",
        "base_url": "https://api.cerebras.ai/v1",
        "get_key_url": "https://cloud.cerebras.ai/",
        "description": "Аппаратный инференс моделей Llama на специализированных чипах Wafer-Scale Engine.",
        "active_model": "llama-3.3-70b",
        "temperature": 0.2,
        "legacy_key_names": ["cerebras_api_keys", "cerebras_api_key"],
        "legacy_model_name": "cerebras_model",
    }
]

def _clean_key(key: Any) -> str:
    """Очищает строку ключа от случайных кавычек, скобок и пробелов."""
    if not key:
        return ""
    s = str(key).strip()
    # Удаляем обрамляющие квадратные скобки и кавычки, если они попали в строку при некорректном парсинге
    s = s.strip("[]\"' \t\r\n")
    return s

def _parse_keys_field(raw: Any) -> List[str]:
    """Универсально парсит ключи из JSON-массива, строки через запятую/перенос или списка."""
    if not raw:
        return []
    items = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        trimmed = raw.strip()
        if trimmed.startswith("[") and trimmed.endswith("]"):
            try:
                parsed = json.loads(trimmed)
                if isinstance(parsed, list):
                    items = parsed
                else:
                    items = [parsed]
            except Exception:
                items = trimmed.replace(",", "\n").splitlines()
        else:
            items = trimmed.replace(",", "\n").splitlines()
    else:
        items = [raw]

    seen = set()
    result = []
    for item in items:
        cleaned = _clean_key(item)
        if cleaned and cleaned not in seen and not cleaned.lower().startswith("your_"):
            seen.add(cleaned)
            result.append(cleaned)
    return result

def _init_default_providers(cursor):
    """Инициализирует дефолтные провайдеры и мигрирует ключи из app_config."""
    global_temp = 0.2
    try:
        cursor.execute("SELECT value FROM system_settings WHERE key = 'temperature'")
        t_row = cursor.fetchone()
        if t_row and t_row[0]:
            global_temp = float(t_row[0])
    except Exception:
        pass

    for prov in DEFAULT_PROVIDERS:
        p_id = prov["id"]
        cursor.execute("SELECT id, api_keys, active_model, get_key_url, description FROM providers_config WHERE id = ?", (p_id,))
        row = cursor.fetchone()
        if not row:
            keys_list = []
            for lk in prov["legacy_key_names"]:
                cursor.execute("SELECT value FROM app_config WHERE key = ?", (lk,))
                r = cursor.fetchone()
                if r and r[0]:
                    keys_list.extend(_parse_keys_field(r[0]))
            seen = set()
            final_keys = [k for k in keys_list if not (k in seen or seen.add(k))]

            # Проверяем модель в app_config
            cursor.execute("SELECT value FROM app_config WHERE key = ?", (prov["legacy_model_name"],))
            mr = cursor.fetchone()
            active_model = mr[0].strip() if (mr and mr[0] and mr[0].strip()) else prov["active_model"]

            cursor.execute("""
                INSERT INTO providers_config (
                    id, name, protocol, base_url, api_keys, active_model, temperature, is_enabled, is_custom, description, get_key_url, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, '{}')
            """, (
                p_id, prov["name"], prov["protocol"], prov["base_url"],
                json.dumps(final_keys), active_model, global_temp,
                prov["description"], prov["get_key_url"]
            ))
        else:
            cur_keys, cur_model, cur_url, cur_desc = row[1], row[2], row[3], row[4]
            existing_keys = _parse_keys_field(cur_keys)
            updates = []
            params = []
            # Очищаем поврежденные ключи (например, если в базе были кавычки/скобки)
            if existing_keys:
                cleaned_json = json.dumps(existing_keys)
                if cur_keys != cleaned_json:
                    updates.append("api_keys = ?")
                    params.append(cleaned_json)

            if not cur_url and prov["get_key_url"]:
                updates.append("get_key_url = ?")
                params.append(prov["get_key_url"])
            if prov["description"] and (not cur_desc or cur_desc != prov["description"]):
                updates.append("description = ?")
                params.append(prov["description"])
            if p_id == "openrouter" and (not cur_model or cur_model in DEAD_OPENROUTER_MODELS):
                updates.append("active_model = ?")
                params.append("openrouter/free")
                cur_model = "openrouter/free"
                set_config_value("openrouter_model", "openrouter/free")
                set_config_value("openai_model", "openrouter/free")
                cursor.execute(
                    f"DELETE FROM provider_models WHERE provider = 'openrouter' AND model_id IN ({','.join(['?']*len(DEAD_OPENROUTER_MODELS))})",
                    tuple(DEAD_OPENROUTER_MODELS)
                )
            if updates:
                params.append(p_id)
                cursor.execute(f"UPDATE providers_config SET {', '.join(updates)} WHERE id = ?", tuple(params))

def init_db():
    """Инициализирует базу данных SQLite и создает таблицы, если они не существуют."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
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
            scores_data TEXT,
            analyzed_by_provider TEXT,
            analyzed_by_model TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Миграция: проверяем наличие колонок questions_data, applied_resume_id, applied_resume_title, scores_data, analyzed_by_provider, analyzed_by_model
    cursor.execute("PRAGMA table_info(processed_vacancies)")
    columns = [col[1] for col in cursor.fetchall()]
    if "questions_data" not in columns:
        cursor.execute("ALTER TABLE processed_vacancies ADD COLUMN questions_data TEXT")
    if "applied_resume_id" not in columns:
        cursor.execute("ALTER TABLE processed_vacancies ADD COLUMN applied_resume_id TEXT")
    if "applied_resume_title" not in columns:
        cursor.execute("ALTER TABLE processed_vacancies ADD COLUMN applied_resume_title TEXT")
    if "scores_data" not in columns:
        cursor.execute("ALTER TABLE processed_vacancies ADD COLUMN scores_data TEXT")
    if "analyzed_by_provider" not in columns:
        cursor.execute("ALTER TABLE processed_vacancies ADD COLUMN analyzed_by_provider TEXT")
    if "analyzed_by_model" not in columns:
        cursor.execute("ALTER TABLE processed_vacancies ADD COLUMN analyzed_by_model TEXT")

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS provider_models (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            description TEXT DEFAULT '',
            context_window INTEGER DEFAULT 0,
            is_free BOOLEAN DEFAULT 0,
            is_default BOOLEAN DEFAULT 0,
            last_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS providers_config (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            protocol TEXT NOT NULL,
            base_url TEXT DEFAULT '',
            api_keys TEXT DEFAULT '[]',
            active_model TEXT DEFAULT '',
            temperature REAL DEFAULT 0.2,
            is_enabled BOOLEAN DEFAULT 1,
            is_custom BOOLEAN DEFAULT 0,
            description TEXT DEFAULT '',
            get_key_url TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON processed_vacancies(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_vacancies(processed_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_provider_models_provider ON provider_models(provider)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_providers_config_custom ON providers_config(is_custom)")
    
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

    # Проверяем, содержит ли сохраненный промпт актуальные правила и шкалы
    cursor.execute("SELECT value FROM system_settings WHERE key = 'system_prompt'")
    current_prompt_row = cursor.fetchone()
    if current_prompt_row:
        current_prompt = current_prompt_row[0] or ""
        if "stack_score" not in current_prompt or "format_score" not in current_prompt or "ПРАВИЛА СОПРОВОДИТЕЛЬНОГО ПИСЬМА" not in current_prompt or "{candidate_name}" not in current_prompt:
            cursor.execute("UPDATE system_settings SET value = ? WHERE key = 'system_prompt'", (DEFAULT_SYSTEM_PROMPT,))

    # Инициализация типовых профильных ответов
    default_profile_answers = [
        ("candidate_name", "Имя соискателя (если не указано в резюме)", ""),
        ("candidate_gender", "Пол соискателя (если не указано в резюме: Мужской или Женский)", ""),
        ("location_city", "Город проживания / Локация", ""),
        ("relocation_cities", "Города для переезда / релокации", ""),
        ("salary_min", "Зарплатные ожидания", ""),
        ("it_accreditation", "Критична ли IT-аккредитация работодателя?", "Нет, IT-аккредитация не критична"),
        ("test_task", "Готовность к выполнению тестового задания", "Да, готов выполнить адекватное тестовое задание (до 2-4 часов)"),
        ("work_format", "Формат работы", ""),
        ("employment_type", "Форма оформления", "ТК РФ, ИП, самозанятость или ГПХ")
    ]
    for key, hint, ans in default_profile_answers:
        cursor.execute("SELECT 1 FROM user_profile_answers WHERE key = ?", (key,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO user_profile_answers (key, question_hint, answer) VALUES (?, ?, ?)", (key, hint, ans))

    # Инициализация провайдеров
    _init_default_providers(cursor)
            
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
                            cover_letter, questions_data, applied_resume_id, applied_resume_title, 
                            scores_data, analyzed_by_provider, analyzed_by_model, processed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        row_dict.get("scores_data"),
                        row_dict.get("analyzed_by_provider"),
                        row_dict.get("analyzed_by_model"),
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
                 applied_resume_id: str = None, applied_resume_title: str = None,
                 scores_data: str = None,
                 analyzed_by_provider: str = None,
                 analyzed_by_model: str = None):
    """Сохраняет или обновляет информацию о вакансии в базе данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT OR REPLACE INTO processed_vacancies 
        (id, title, company, status, match_score, analysis_reason, cover_letter, questions_data, applied_resume_id, applied_resume_title, scores_data, analyzed_by_provider, analyzed_by_model, processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vacancy_id, title, company, status, match_score, analysis_reason, cover_letter, questions_data, applied_resume_id, applied_resume_title, scores_data, analyzed_by_provider, analyzed_by_model, now))
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
    
    query = "SELECT id, title, company, status, match_score, analysis_reason, cover_letter, questions_data, applied_resume_id, applied_resume_title, processed_at, scores_data, analyzed_by_provider, analyzed_by_model FROM processed_vacancies"
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

def get_all_counts() -> dict:
    """Возвращает все счетчики одним быстрым агрегирующим запросом."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            COUNT(*),
            SUM(CASE WHEN status IN ('new', 'needs_answers', 'applied', 'already_applied') THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'needs_answers' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status IN ('applied', 'already_applied') THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'ignored' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
        FROM processed_vacancies
    """)
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {"total": 0, "matched": 0, "needs_answers": 0, "applied": 0, "ignored": 0, "failed": 0}
    return {
        "total": row[0] or 0,
        "matched": row[1] or 0,
        "needs_answers": row[2] or 0,
        "applied": row[3] or 0,
        "ignored": row[4] or 0,
        "failed": row[5] or 0
    }

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
    conn.row_factory = sqlite3.Row
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

def save_provider_models(provider: str, models: List[Dict[str, Any]]) -> int:
    """Сохраняет или обновляет список моделей для указанного провайдера в БД."""
    if not models:
        return 0
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    saved_count = 0
    for m in models:
        model_id = m.get("id") or m.get("model_id")
        if not model_id:
            continue
        record_id = f"{provider}:{model_id}"
        display_name = m.get("name") or m.get("display_name") or model_id
        desc = m.get("description", "")
        cw_val = m.get("context_window") or m.get("context_length") or 0
        if isinstance(cw_val, str):
            cw_str = cw_val.lower().strip()
            if cw_str.endswith("k"):
                try:
                    context_window = int(float(cw_str[:-1]) * 1000)
                except Exception:
                    context_window = 0
            elif cw_str.endswith("m"):
                try:
                    context_window = int(float(cw_str[:-1]) * 1000000)
                except Exception:
                    context_window = 0
            else:
                try:
                    context_window = int(cw_str)
                except Exception:
                    context_window = 0
        else:
            try:
                context_window = int(cw_val)
            except Exception:
                context_window = 0

        is_free = 1 if m.get("is_free") else 0
        is_default = 1 if m.get("is_default") else 0
        cursor.execute("""
            INSERT INTO provider_models (id, provider, model_id, display_name, description, context_window, is_free, is_default, last_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                description=excluded.description,
                context_window=excluded.context_window,
                is_free=excluded.is_free,
                is_default=excluded.is_default,
                last_synced=CURRENT_TIMESTAMP
        """, (record_id, provider, model_id, display_name, desc, context_window, is_free, is_default))
        saved_count += 1
    conn.commit()
    conn.close()
    return saved_count

def get_provider_models(provider: Optional[str] = None, is_free_only: bool = False, free_only: bool = False) -> List[Dict[str, Any]]:
    """Возвращает сохраненные модели из БД с возможностью фильтрации."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    query = "SELECT provider, model_id, display_name, description, context_window, is_free, is_default, last_synced FROM provider_models"
    params = []
    conditions = []
    if provider and provider != "all":
        conditions.append("provider = ?")
        params.append(provider)
    if is_free_only or free_only:
        conditions.append("is_free = 1")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY is_default DESC, is_free DESC, display_name ASC"
    cursor.execute(query, tuple(params))
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "provider": r[0],
            "id": r[1],
            "model_id": r[1],
            "name": r[2],
            "display_name": r[2],
            "description": r[3],
            "context_window": r[4],
            "is_free": bool(r[5]),
            "is_default": bool(r[6]),
            "last_synced": r[7]
        }
        for r in rows
    ]

def set_active_provider_model(provider: str, model_id: str):
    """Устанавливает активную модель для провайдера в app_config и providers_config."""
    norm_id = "gemini" if provider == "google" else provider
    set_config_value(f"{norm_id}_model", model_id)
    # Также обновляем флаг is_default в таблице provider_models и active_model в providers_config
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE provider_models SET is_default = 0 WHERE provider = ?", (norm_id,))
    cursor.execute("UPDATE provider_models SET is_default = 1 WHERE provider = ? AND model_id = ?", (norm_id, model_id))
    cursor.execute("UPDATE providers_config SET active_model = ? WHERE id = ?", (model_id, norm_id))
    conn.commit()
    conn.close()

def get_active_provider_model(provider: str) -> Optional[str]:
    """Возвращает активную модель для провайдера из app_config или providers_config."""
    norm_id = "gemini" if provider == "google" else provider
    val = get_config_value(f"{norm_id}_model")
    if val:
        return val
    p_cfg = get_provider_config(norm_id)
    return p_cfg.get("active_model") if p_cfg else None

def mask_api_key(key: str) -> str:
    """Маскирует ключ для безопасного отображения в интерфейсе."""
    if not key:
        return ""
    k = _clean_key(key)
    if len(k) <= 10:
        return k[:2] + "••••••••" + k[-2:] if len(k) >= 4 else "••••••••"
    return k[:6] + "••••••••" + k[-4:]

def get_all_providers_config() -> List[Dict[str, Any]]:
    """Возвращает список всех провайдеров (системных и кастомных) с их настройками."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, protocol, base_url, api_keys, active_model, temperature, is_enabled, is_custom, description, get_key_url, metadata, updated_at
        FROM providers_config
        ORDER BY is_custom ASC, id ASC
    """)
    rows = cursor.fetchall()
    conn.close()

    result = []
    for r in rows:
        keys = _parse_keys_field(r[4])
        try:
            meta = json.loads(r[11]) if r[11] else {}
        except Exception:
            meta = {}

        result.append({
            "id": r[0],
            "name": r[1],
            "protocol": r[2],
            "base_url": r[3] or "",
            "api_keys": keys,
            "keys_count": len(keys),
            "has_keys": len(keys) > 0,
            "masked_keys": [mask_api_key(k) for k in keys],
            "active_model": r[5] or "",
            "temperature": float(r[6]) if r[6] is not None else 0.2,
            "is_enabled": bool(r[7]),
            "is_custom": bool(r[8]),
            "description": r[9] or "",
            "get_key_url": r[10] or "",
            "metadata": meta,
            "updated_at": r[12]
        })
    return result

def get_provider_config(provider_id: str) -> Optional[Dict[str, Any]]:
    """Возвращает конфигурацию конкретного провайдера по ID (поддерживает псевдоним google -> gemini)."""
    norm_id = "gemini" if provider_id == "google" else provider_id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, protocol, base_url, api_keys, active_model, temperature, is_enabled, is_custom, description, get_key_url, metadata, updated_at
        FROM providers_config
        WHERE id = ?
    """, (norm_id,))
    r = cursor.fetchone()
    conn.close()
    if not r:
        return None

    keys = _parse_keys_field(r[4])
    try:
        meta = json.loads(r[11]) if r[11] else {}
    except Exception:
        meta = {}

    return {
        "id": r[0],
        "name": r[1],
        "protocol": r[2],
        "base_url": r[3] or "",
        "api_keys": keys,
        "keys_count": len(keys),
        "has_keys": len(keys) > 0,
        "masked_keys": [mask_api_key(k) for k in keys],
        "active_model": r[5] or "",
        "temperature": float(r[6]) if r[6] is not None else 0.2,
        "is_enabled": bool(r[7]),
        "is_custom": bool(r[8]),
        "description": r[9] or "",
        "get_key_url": r[10] or "",
        "metadata": meta,
        "updated_at": r[12]
    }

def save_provider_config(provider_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Обновляет настройки провайдера и синхронизирует их с app_config для обратной совместимости.
    """
    norm_id = "gemini" if provider_id == "google" else provider_id
    existing = get_provider_config(norm_id)
    if not existing:
        raise ValueError(f"Provider '{provider_id}' not found")

    name = str(data.get("name", existing["name"])).strip()
    base_url = str(data.get("base_url", existing["base_url"])).strip()
    
    raw_keys = data.get("api_keys")
    if raw_keys is not None:
        new_keys = _parse_keys_field(raw_keys)
    else:
        new_keys = existing["api_keys"]

    active_model = str(data.get("active_model", existing["active_model"])).strip()
    temperature = float(data.get("temperature", existing["temperature"]))
    temperature = max(0.0, min(1.0, temperature))
    is_enabled = bool(data.get("is_enabled", existing["is_enabled"]))
    description = str(data.get("description", existing["description"])).strip()
    get_key_url = str(data.get("get_key_url", existing["get_key_url"])).strip()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE providers_config
        SET name = ?, base_url = ?, api_keys = ?, active_model = ?, temperature = ?,
            is_enabled = ?, description = ?, get_key_url = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (name, base_url, json.dumps(new_keys), active_model, temperature, int(is_enabled), description, get_key_url, norm_id))
    conn.commit()
    conn.close()

    # Синхронизация с app_config для старых модулей
    set_config_value(f"{norm_id}_api_keys", ",".join(new_keys))
    set_config_value(f"{norm_id}_api_key", new_keys[0] if new_keys else "")
    current_openai_preset = get_config_value("openai_provider_preset")
    if norm_id in ("openrouter", "groq", "github", "cerebras") or norm_id == current_openai_preset:
        set_config_value("openai_api_keys", ",".join(new_keys))
        set_config_value("openai_api_key", new_keys[0] if new_keys else "")
    if active_model:
        set_config_value(f"{norm_id}_model", active_model)
        if norm_id == "openrouter" or norm_id == current_openai_preset:
            set_config_value("openai_model", active_model)

    return get_provider_config(norm_id)

def create_custom_provider(data: Dict[str, Any]) -> Dict[str, Any]:
    """Создает новый пользовательский OpenAI-совместимый провайдер."""
    name = str(data.get("name", "Custom OpenAI")).strip()
    if not name:
        name = "Custom OpenAI"
        
    slug = re.sub(r'[^a-zA-Z0-9_]+', '', name.lower().replace(" ", "_"))[:16] or "openai"
    short_uid = uuid.uuid4().hex[:6]
    provider_id = f"custom_{slug}_{short_uid}"

    protocol = "openai"
    base_url = str(data.get("base_url", "http://localhost:11434/v1")).strip()
    
    raw_keys = data.get("api_keys", [])
    keys = _parse_keys_field(raw_keys)

    active_model = str(data.get("active_model", "")).strip()
    temperature = float(data.get("temperature", 0.2))
    temperature = max(0.0, min(1.0, temperature))
    is_enabled = bool(data.get("is_enabled", True))
    description = str(data.get("description", "Пользовательский OpenAI-совместимый провайдер")).strip()
    get_key_url = str(data.get("get_key_url", "")).strip()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO providers_config (
            id, name, protocol, base_url, api_keys, active_model, temperature, is_enabled, is_custom, description, get_key_url, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, '{}')
    """, (
        provider_id, name, protocol, base_url, json.dumps(keys),
        active_model, temperature, int(is_enabled), description, get_key_url
    ))
    conn.commit()
    conn.close()

    # Сохраняем ключи и модель в app_config
    set_config_value(f"{provider_id}_api_keys", ",".join(keys))
    if keys:
        set_config_value(f"{provider_id}_api_key", keys[0])
    if active_model:
        set_config_value(f"{provider_id}_model", active_model)

    return get_provider_config(provider_id)

def delete_custom_provider(provider_id: str) -> bool:
    """Удаляет пользовательский провайдер (системные удалять нельзя)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT is_custom FROM providers_config WHERE id = ?", (provider_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    if not row[0]:
        conn.close()
        raise ValueError("Cannot delete built-in system provider")

    cursor.execute("DELETE FROM providers_config WHERE id = ?", (provider_id,))
    cursor.execute("DELETE FROM provider_models WHERE provider = ?", (provider_id,))
    conn.commit()
    conn.close()
    return True

