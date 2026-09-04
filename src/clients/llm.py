import os
import re
import json
import time
import logging
from typing import Dict, Any, List, Tuple, Optional
from pydantic import BaseModel, Field
import requests

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    class _DummyTypes:
        class GenerateContentConfig:
            def __init__(self, *args, **kwargs):
                pass
    types = _DummyTypes

from src.core.config import Config
from src.db import database

logger = logging.getLogger("LLMAnalyzer")

class QuotaExceededError(Exception):
    """Исключение при исчерпании лимитов / квот всех настроенных LLM провайдеров."""
    pass

class VacancyAnalysis(BaseModel):
    match_score: int = Field(description="Оценка соответствия вакансии резюме от 0 до 100")
    is_match: bool = Field(description="Подходит ли вакансия для отклика (True/False)")
    reasoning: str = Field(description="Краткий разбор (1-2 емких предложения): соответствие стека, релевантность опыта и почему выбрано именно это резюме")
    cover_letter: str = Field(description="Краткое (3-5 предложений), персонализированное сопроводительное письмо на русском языке на основе выбранного резюме. Заполняется только если is_match = True, иначе пустая строка.")
    selected_resume_id: Optional[str] = Field(default=None, description="ID выбранного лучшего резюме (если передано несколько резюме)")
    selected_resume_title: Optional[str] = Field(default=None, description="Название/должность выбранного лучшего резюме")

class QuestionAnswer(BaseModel):
    id: str = Field(description="Уникальный идентификатор вопроса (или имя поля формы, например task_335457717_text)")
    question_text: str = Field(description="Текст вопроса работодателя")
    answer: str = Field(description="Точный, профессиональный и емкий ответ на русском языке (или выбранный вариант ответа из вариантов options)")
    confidence: int = Field(description="Уверенность модели в правильности/соответствии ответа от 0 до 100")
    requires_user_input: bool = Field(description="True, если вопрос требует индивидуального решения пользователя (например, редкие личные условия, не указанные в резюме/профиле); False, если ответ понятен и уверенно следует из резюме или профиля")
    reasoning: str = Field(description="Краткое обоснование выбранного ответа")

class QuestionsAnalysisResult(BaseModel):
    answers: List[QuestionAnswer] = Field(description="Список ответов на каждый заданный вопрос")
    all_confident: bool = Field(description="True, если все вопросы закрыты уверенно и не требуют ручного ввода")

class CoverLetterResult(BaseModel):
    cover_letter: str = Field(description="Краткое (3-5 предложений), персонализированное сопроводительное письмо на русском языке на основе выбранного резюме.")

class LLMAnalyzer:
    _gemini_key_statuses: Dict[str, dict] = {}   # key -> {"status": "ok"|"error", "reason": ..., "detail": ..., "last_checked": float}
    _mistral_key_statuses: Dict[str, dict] = {}  # key -> {"status": "ok"|"error", "reason": ..., "detail": ..., "last_checked": float}
    _current_gemini_idx: int = 0
    _current_mistral_idx: int = 0
    _initialized_keys: bool = False

    # Обратная совместимость для _key_statuses
    @classmethod
    def get_key_statuses(cls) -> Dict[str, dict]:
        merged = dict(cls._gemini_key_statuses)
        merged.update(cls._mistral_key_statuses)
        return merged

    def __init__(self, gemini_api_keys: List[str] = None, mistral_api_keys: List[str] = None, api_keys: List[str] = None):
        # 1. Ключи Gemini
        raw_gemini = gemini_api_keys or api_keys
        if raw_gemini:
            self.gemini_keys = [k.strip() for k in raw_gemini if k.strip() and "your_gemini_api_key" not in k.lower()]
        else:
            db_gemini = database.get_config_value("gemini_api_keys")
            if db_gemini:
                self.gemini_keys = [k.strip() for k in re.split(r'[,\n;]+', db_gemini) if k.strip() and "your_gemini_api_key" not in k.lower()]
            else:
                self.gemini_keys = Config.GEMINI_API_KEYS or ([Config.GEMINI_API_KEY] if Config.GEMINI_API_KEY and "your_gemini_api_key" not in Config.GEMINI_API_KEY.lower() else [])

        # Для обратной совместимости
        self.api_keys = self.gemini_keys

        # 2. Ключи Mistral
        if mistral_api_keys:
            self.mistral_keys = [k.strip() for k in mistral_api_keys if k.strip() and "your_mistral_api_key" not in k.lower()]
        else:
            db_mistral = database.get_config_value("mistral_api_keys")
            if db_mistral:
                self.mistral_keys = [k.strip() for k in re.split(r'[,\n;]+', db_mistral) if k.strip() and "your_mistral_api_key" not in k.lower()]
            else:
                self.mistral_keys = Config.MISTRAL_API_KEYS or ([Config.MISTRAL_API_KEY] if Config.MISTRAL_API_KEY and "your_mistral_api_key" not in Config.MISTRAL_API_KEY.lower() else [])

        # Инициализируем клиенты Gemini
        self.gemini_clients: Dict[str, Any] = {}
        if genai:
            for k in self.gemini_keys:
                try:
                    self.gemini_clients[k] = genai.Client(api_key=k)
                except Exception as e:
                    logger.error(f"Не удалось инициализировать клиент Gemini для ключа ...{k[-6:] if len(k) > 6 else k}: {e}")

        # Обратная совместимость для self.clients
        self.clients = self.gemini_clients

    def check_availability(self, force_probe: bool = False) -> dict:
        """
        Проверяет доступность всех настроенных API-ключей Gemini и Mistral.
        Возвращает детальную статистику доступности по провайдерам.
        """
        has_gemini_keys = bool(self.gemini_keys and genai)
        has_mistral_keys = bool(self.mistral_keys)

        if not has_gemini_keys and not has_mistral_keys:
            return {
                "status": "mock",
                "available": 0,
                "total": 0,
                "gemini": {"available": 0, "total": 0, "keys": []},
                "mistral": {"available": 0, "total": 0, "keys": []},
                "reason": "No API keys configured or providers unavailable"
            }

        now = time.time()

        # Проверка Gemini
        if has_gemini_keys and (not LLMAnalyzer._initialized_keys or force_probe):
            logger.info(f"Проверка пула ключей Gemini API ({len(self.gemini_keys)} шт.)...")
            for k in self.gemini_keys:
                client = self.gemini_clients.get(k)
                if not client:
                    LLMAnalyzer._gemini_key_statuses[k] = {"status": "error", "reason": "client_init_failed", "last_checked": now}
                    continue
                try:
                    client.models.generate_content(
                        model='gemini-3.6-flash',
                        contents="Hi"
                    )
                    LLMAnalyzer._gemini_key_statuses[k] = {"status": "ok", "last_checked": now}
                except Exception as e:
                    err_msg = str(e)
                    reason = "error"
                    if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
                        reason = "rate_limit_or_quota"
                    elif "403" in err_msg or "API_KEY_INVALID" in err_msg or "API key not valid" in err_msg:
                        reason = "invalid_api_key"
                    LLMAnalyzer._gemini_key_statuses[k] = {"status": "error", "reason": reason, "detail": err_msg, "last_checked": now}

        # Проверка Mistral
        if has_mistral_keys and (not LLMAnalyzer._initialized_keys or force_probe):
            logger.info(f"Проверка пула ключей Mistral API ({len(self.mistral_keys)} шт.)...")
            target_mistral_model = database.get_config_value("mistral_model") or Config.MISTRAL_MODEL or "mistral-small-latest"
            for k in self.mistral_keys:
                try:
                    resp = requests.post(
                        "https://api.mistral.ai/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {k}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": target_mistral_model,
                            "messages": [{"role": "user", "content": "Hi"}],
                            "max_tokens": 5
                        },
                        timeout=10
                    )
                    if resp.status_code == 200:
                        LLMAnalyzer._mistral_key_statuses[k] = {"status": "ok", "last_checked": now}
                    elif resp.status_code == 429:
                        LLMAnalyzer._mistral_key_statuses[k] = {"status": "error", "reason": "rate_limit_or_quota", "detail": resp.text, "last_checked": now}
                    elif resp.status_code in (401, 403):
                        LLMAnalyzer._mistral_key_statuses[k] = {"status": "error", "reason": "invalid_api_key", "detail": resp.text, "last_checked": now}
                    else:
                        LLMAnalyzer._mistral_key_statuses[k] = {"status": "error", "reason": f"http_{resp.status_code}", "detail": resp.text, "last_checked": now}
                except Exception as e:
                    LLMAnalyzer._mistral_key_statuses[k] = {"status": "error", "reason": "network_error", "detail": str(e), "last_checked": now}

        LLMAnalyzer._initialized_keys = True

        # Подсчет доступных Gemini
        gemini_total = len(self.gemini_keys)
        gemini_available = sum(
            1 for k in self.gemini_keys
            if LLMAnalyzer._gemini_key_statuses.get(k, {}).get("status") == "ok" or (k not in LLMAnalyzer._gemini_key_statuses)
        )

        # Подсчет доступных Mistral
        mistral_total = len(self.mistral_keys)
        mistral_available = sum(
            1 for k in self.mistral_keys
            if LLMAnalyzer._mistral_key_statuses.get(k, {}).get("status") == "ok" or (k not in LLMAnalyzer._mistral_key_statuses)
        )

        total_keys = gemini_total + mistral_total
        total_available = gemini_available + mistral_available

        # Авто-сброс 429 если прошло больше 30 сек
        if total_keys > 0 and total_available == 0 and not force_probe:
            all_statuses = list(LLMAnalyzer._gemini_key_statuses.values()) + list(LLMAnalyzer._mistral_key_statuses.values())
            oldest_check = min([st.get("last_checked", 0) for st in all_statuses] or [0])
            if (now - oldest_check) > 30:
                logger.info("Прошло более 30 сек с момента блокировки квот 429. Выполняем повторную проверку...")
                return self.check_availability(force_probe=True)

        status_str = "ok" if total_available > 0 else "error"

        # Детализация по ключам Gemini
        gemini_keys_info = []
        for k in self.gemini_keys:
            st = LLMAnalyzer._gemini_key_statuses.get(k, {"status": "ok"})
            gemini_keys_info.append({
                "key": k,
                "provider": "gemini",
                "status": st.get("status", "ok"),
                "reason": st.get("reason"),
                "detail": st.get("detail")
            })

        # Детализация по ключам Mistral
        mistral_keys_info = []
        for k in self.mistral_keys:
            st = LLMAnalyzer._mistral_key_statuses.get(k, {"status": "ok"})
            mistral_keys_info.append({
                "key": k,
                "provider": "mistral",
                "status": st.get("status", "ok"),
                "reason": st.get("reason"),
                "detail": st.get("detail")
            })

        return {
            "status": status_str,
            "available": total_available,
            "total": total_keys,
            "gemini": {
                "available": gemini_available,
                "total": gemini_total,
                "keys": gemini_keys_info,
                "current_index": LLMAnalyzer._current_gemini_idx + 1 if gemini_total > 0 else 0
            },
            "mistral": {
                "available": mistral_available,
                "total": mistral_total,
                "keys": mistral_keys_info,
                "current_index": LLMAnalyzer._current_mistral_idx + 1 if mistral_total > 0 else 0
            },
            "current_index": LLMAnalyzer._current_gemini_idx + 1 if gemini_total > 0 else (LLMAnalyzer._current_mistral_idx + 1 if mistral_total > 0 else 0)
        }

    def _get_active_gemini_client(self) -> Tuple[Optional[str], Optional[Any]]:
        """Возвращает текущий рабочий ключ Gemini и его клиент."""
        if not self.gemini_keys:
            return None, None

        for i in range(len(self.gemini_keys)):
            idx = (LLMAnalyzer._current_gemini_idx + i) % len(self.gemini_keys)
            key = self.gemini_keys[idx]
            status = LLMAnalyzer._gemini_key_statuses.get(key, {})
            if status.get("status") != "error":
                LLMAnalyzer._current_gemini_idx = idx
                return key, self.gemini_clients.get(key)

        curr_key = self.gemini_keys[LLMAnalyzer._current_gemini_idx % len(self.gemini_keys)]
        return curr_key, self.gemini_clients.get(curr_key)

    def _get_active_mistral_key(self) -> Optional[str]:
        """Возвращает текущий рабочий ключ Mistral."""
        if not self.mistral_keys:
            return None

        for i in range(len(self.mistral_keys)):
            idx = (LLMAnalyzer._current_mistral_idx + i) % len(self.mistral_keys)
            key = self.mistral_keys[idx]
            status = LLMAnalyzer._mistral_key_statuses.get(key, {})
            if status.get("status") != "error":
                LLMAnalyzer._current_mistral_idx = idx
                return key

        return self.mistral_keys[LLMAnalyzer._current_mistral_idx % len(self.mistral_keys)]

    def get_available_models(self, provider: str = "all") -> Any:
        """Возвращает список доступных моделей для Gemini и Mistral."""
        result = {
            "gemini": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-latest", "gemini-3.1-pro-preview"],
            "mistral": ["mistral-small-latest", "mistral-large-latest", "mistral-medium-latest", "open-mistral-nemo", "codestral-latest"]
        }

        # Динамический опрос моделей Gemini
        if self.gemini_clients and genai and provider in ("all", "gemini"):
            models_set = set()
            active_key, active_client = self._get_active_gemini_client()
            clients_to_try = [active_client] if active_client else list(self.gemini_clients.values())

            for client in clients_to_try:
                try:
                    models = client.models.list()
                    for m in models:
                        name = m.name.replace("models/", "").strip()
                        if any(k in name.lower() for k in ["flash", "pro", "gemma"]) and not any(k in name.lower() for k in ["tts", "audio", "image", "embedding", "veo", "robotics", "banana"]):
                            models_set.add(name)
                    if models_set:
                        break
                except Exception as e:
                    logger.warning(f"Не удалось получить список моделей Gemini через API: {e}")

            if models_set:
                result["gemini"] = sorted(list(models_set), key=lambda x: (not x.startswith("gemini-3"), not "flash" in x, x))

        if provider == "gemini":
            return result["gemini"]
        elif provider == "mistral":
            return result["mistral"]
        return result

    def _build_prompt(self, resume_text: str, vacancy: Dict[str, Any], match_threshold: int, resumes: List[Dict[str, Any]] = None) -> str:
        """Формирует промпт для анализа вакансии на основе системного шаблона из БД."""
        template = database.get_system_setting("system_prompt") or database.DEFAULT_SYSTEM_PROMPT
        skills_raw = vacancy.get('skills', [])
        skills_str = ', '.join(skills_raw) if isinstance(skills_raw, list) else str(skills_raw or '')
        
        final_resume_text = str(resume_text or "")
        multi_resume_instructions = ""
        
        if resumes and len(resumes) > 1:
            res_blocks = []
            for idx, r in enumerate(resumes, 1):
                r_id = r.get("id", f"resume_{idx}")
                r_title = r.get("title", f"Резюме {idx}")
                r_text = r.get("text", "")
                res_blocks.append(f"=== [РЕЗЮМЕ #{idx}] ID: {r_id} | Должность: {r_title} ===\n{r_text}\n")
            
            final_resume_text = "\n".join(res_blocks)
            multi_resume_instructions = (
                "\n\nВНИМАНИЕ (КАНДИДАТ ИМЕЕТ НЕСКОЛЬКО РЕЗЮМЕ):\n"
                "1. Сравните требования вакансии со ВСЕМИ представленными выше резюме кандидата.\n"
                "2. Выберите ТОЛЬКО ОДНО наилучшим образом подходящее резюме: укажите его точный ID в поле `selected_resume_id` и должность в `selected_resume_title`.\n"
                "3. Рассчитайте `match_score` (0-100) и `is_match` строго на основе выбранного резюме.\n"
                "4. Составьте `cover_letter`, опираясь на факты, опыт и стек именно из выбранного резюме.\n"
                "5. В `reasoning` укажите, почему выбрано именно это резюме и как оно подходит."
            )
        elif resumes and len(resumes) == 1:
            final_resume_text = resumes[0].get("text", "")

        replacements = {
            "{resume_text}": final_resume_text,
            "{resume}": final_resume_text,
            "{vacancy_title}": str(vacancy.get('title', '')),
            "{title}": str(vacancy.get('title', '')),
            "{company}": str(vacancy.get('company', '')),
            "{salary}": str(vacancy.get('salary', 'Не указана') or 'Не указана'),
            "{skills}": skills_str,
            "{description}": str(vacancy.get('description', '')),
            "{experience_required}": str(vacancy.get('experience', 'Не указан') or 'Не указан'),
            "{experience}": str(vacancy.get('experience', 'Не указан') or 'Не указан'),
            "{employment}": str(vacancy.get('employment', 'Не указана') or 'Не указана'),
            "{schedule}": str(vacancy.get('schedule', 'Не указан') or 'Не указан'),
            "{location}": str(vacancy.get('location', 'Не указана') or 'Не указана'),
            "{threshold}": str(match_threshold),
            "{match_threshold}": str(match_threshold)
        }
        
        prompt = template
        for tag, val in replacements.items():
            prompt = prompt.replace(tag, val)
            
        if multi_resume_instructions:
            prompt += multi_resume_instructions
            
        return prompt

    def _call_gemini(self, prompt: str, target_model: str, vacancy: Dict[str, Any], temperature: float = 0.2) -> VacancyAnalysis:
        """Выполняет запрос к Gemini с ротацией ключей."""
        tried_keys = set()

        while len(tried_keys) < len(self.gemini_keys):
            active_key, active_client = self._get_active_gemini_client()
            if not active_client or active_key in tried_keys:
                break

            tried_keys.add(active_key)
            key_tag = f"...{active_key[-6:]}" if len(active_key) > 6 else active_key

            try:
                logger.info(f"Отправка запроса к Gemini [{target_model}, ключ {key_tag}, temp={temperature}] для вакансии '{vacancy.get('title')}' ({vacancy.get('company')})...")

                response = active_client.models.generate_content(
                    model=target_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=VacancyAnalysis,
                        temperature=temperature,
                    )
                )

                LLMAnalyzer._gemini_key_statuses[active_key] = {"status": "ok", "last_checked": time.time()}
                result = VacancyAnalysis.model_validate_json(response.text)
                logger.info(f"Анализ Gemini завершен успешно [ключ {key_tag}]. Совпадение: {result.match_score}%, Подходит: {result.is_match}, Резюме: {result.selected_resume_title or result.selected_resume_id or 'По умолчанию'}")
                return result

            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
                    logger.warning(f"Исчерпан лимит квоты Gemini на ключе {key_tag} (429). Переключаемся на следующий...")
                    LLMAnalyzer._gemini_key_statuses[active_key] = {
                        "status": "error",
                        "reason": "rate_limit_or_quota",
                        "detail": err_msg,
                        "last_checked": time.time()
                    }
                    LLMAnalyzer._current_gemini_idx = (LLMAnalyzer._current_gemini_idx + 1) % len(self.gemini_keys)
                    continue
                elif "403" in err_msg or "API_KEY_INVALID" in err_msg or "API key not valid" in err_msg:
                    logger.warning(f"Невалидный ключ Gemini {key_tag}. Переключаемся...")
                    LLMAnalyzer._gemini_key_statuses[active_key] = {
                        "status": "error",
                        "reason": "invalid_api_key",
                        "detail": err_msg,
                        "last_checked": time.time()
                    }
                    LLMAnalyzer._current_gemini_idx = (LLMAnalyzer._current_gemini_idx + 1) % len(self.gemini_keys)
                    continue
                elif "503" in err_msg or "UNAVAILABLE" in err_msg or "high demand" in err_msg.lower():
                    logger.warning(f"Сервер Gemini временно перегружен (503) на ключе {key_tag}. Пауза 2 сек...")
                    time.sleep(2)
                    LLMAnalyzer._current_gemini_idx = (LLMAnalyzer._current_gemini_idx + 1) % len(self.gemini_keys)
                    continue
                else:
                    logger.error(f"Ошибка выполнения запроса к Gemini: {e}")
                    raise e

        raise QuotaExceededError("Все ключи Gemini исчерпали квоту или вернули ошибку (429 RESOURCE_EXHAUSTED).")

    def _call_mistral(self, prompt: str, target_model: str, vacancy: Dict[str, Any], temperature: float = 0.2) -> VacancyAnalysis:
        """Выполняет запрос к Mistral AI API с ротацией ключей."""
        if not self.mistral_keys:
            raise QuotaExceededError("Ключи Mistral API не настроены.")

        tried_keys = set()

        while len(tried_keys) < len(self.mistral_keys):
            active_key = self._get_active_mistral_key()
            if not active_key or active_key in tried_keys:
                break

            tried_keys.add(active_key)
            key_tag = f"...{active_key[-6:]}" if len(active_key) > 6 else active_key

            try:
                logger.info(f"Отправка запроса к Mistral AI [{target_model}, ключ {key_tag}, temp={temperature}] для вакансии '{vacancy.get('title')}' ({vacancy.get('company')})...")

                headers = {
                    "Authorization": f"Bearer {active_key}",
                    "Content-Type": "application/json"
                }

                payload = {
                    "model": target_model,
                    "messages": [
                        {"role": "system", "content": "You are a professional HR recruiter evaluating candidate resumes against job vacancy. Always respond in JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": temperature
                }

                resp = requests.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=45
                )

                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    LLMAnalyzer._mistral_key_statuses[active_key] = {"status": "ok", "last_checked": time.time()}
                    
                    # Очищаем markdown обертки если есть
                    raw_content = content.strip()
                    if raw_content.startswith("```"):
                        lines = raw_content.splitlines()
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        raw_content = "\n".join(lines).strip()

                    result = VacancyAnalysis.model_validate_json(raw_content)
                    logger.info(f"Анализ Mistral завершен успешно [ключ {key_tag}]. Совпадение: {result.match_score}%, Подходит: {result.is_match}, Резюме: {result.selected_resume_title or result.selected_resume_id or 'По умолчанию'}")
                    return result

                elif resp.status_code == 429:
                    logger.warning(f"Исчерпан лимит квоты Mistral на ключе {key_tag} (429). Переключаемся...")
                    LLMAnalyzer._mistral_key_statuses[active_key] = {
                        "status": "error",
                        "reason": "rate_limit_or_quota",
                        "detail": resp.text,
                        "last_checked": time.time()
                    }
                    LLMAnalyzer._current_mistral_idx = (LLMAnalyzer._current_mistral_idx + 1) % len(self.mistral_keys)
                    continue

                elif resp.status_code in (401, 403):
                    logger.warning(f"Невалидный ключ Mistral {key_tag} ({resp.status_code}). Переключаемся...")
                    LLMAnalyzer._mistral_key_statuses[active_key] = {
                        "status": "error",
                        "reason": "invalid_api_key",
                        "detail": resp.text,
                        "last_checked": time.time()
                    }
                    LLMAnalyzer._current_mistral_idx = (LLMAnalyzer._current_mistral_idx + 1) % len(self.mistral_keys)
                    continue

                elif resp.status_code >= 500:
                    logger.warning(f"Сервер Mistral временно недоступен ({resp.status_code}) на ключе {key_tag}. Переключаемся...")
                    time.sleep(2)
                    LLMAnalyzer._current_mistral_idx = (LLMAnalyzer._current_mistral_idx + 1) % len(self.mistral_keys)
                    continue

                else:
                    logger.error(f"Непредвиденная ошибка Mistral API ({resp.status_code}): {resp.text}")
                    raise Exception(f"Mistral API error {resp.status_code}: {resp.text}")

            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "rate limit" in err_msg.lower() or "quota" in err_msg.lower():
                    LLMAnalyzer._mistral_key_statuses[active_key] = {
                        "status": "error",
                        "reason": "rate_limit_or_quota",
                        "detail": err_msg,
                        "last_checked": time.time()
                    }
                    LLMAnalyzer._current_mistral_idx = (LLMAnalyzer._current_mistral_idx + 1) % len(self.mistral_keys)
                    continue
                logger.error(f"Ошибка запроса к Mistral: {e}")
                raise e

        raise QuotaExceededError("Все ключи Mistral API исчерпали квоту или вернули ошибку.")

    def analyze_vacancy(self, resume_text: Any = "", vacancy: Dict[str, Any] = None, threshold: int = None, model: str = None, resumes: List[Dict[str, Any]] = None) -> VacancyAnalysis:
        """
        Анализирует вакансию на соответствие резюме (или списку резюме кандидата).
        Учитывает настройки primary_provider, fallback_enabled и temperature из базы данных.
        Если передан список resumes, LLM выбирает наиболее подходящее резюме.
        """
        if isinstance(resume_text, list):
            resumes = resume_text
            resume_text = ""

        actual_resumes = resumes if (resumes and isinstance(resumes, list)) else []

        match_threshold = threshold if threshold is not None else Config.MATCH_THRESHOLD
        prompt = self._build_prompt(resume_text, vacancy, match_threshold, resumes=actual_resumes)

        has_gemini = bool(self.gemini_keys and genai)
        has_mistral = bool(self.mistral_keys)

        if not has_gemini and not has_mistral:
            logger.warning("API-ключи LLM не настроены. Использование эвристического mock-анализа.")
            return self._mock_analysis(vacancy, match_threshold, resumes=actual_resumes)

        # Читаем системные настройки
        primary_provider = database.get_system_setting("primary_provider", "gemini").lower()
        fallback_enabled_str = database.get_system_setting("fallback_enabled", "true")
        fallback_enabled = fallback_enabled_str.lower() in ("true", "1", "yes") if fallback_enabled_str else True
        
        try:
            temp_val = float(database.get_system_setting("temperature", "0.2"))
        except (ValueError, TypeError):
            temp_val = 0.2

        target_gemini_model = model or database.get_config_value("gemini_model") or Config.GEMINI_MODEL or "gemini-3.6-flash"
        target_mistral_model = database.get_config_value("mistral_model") or Config.MISTRAL_MODEL or "mistral-small-latest"

        result = None

        # Сценарий 1: Основной провайдер - Mistral AI
        if primary_provider == "mistral" and has_mistral:
            try:
                result = self._call_mistral(prompt, target_mistral_model, vacancy, temperature=temp_val)
            except QuotaExceededError as me:
                if fallback_enabled and has_gemini:
                    logger.warning(f"Mistral AI вернул ошибку ({me}). Переход на резервный Gemini API...")
                    try:
                        result = self._call_gemini(prompt, target_gemini_model, vacancy, temperature=temp_val)
                    except QuotaExceededError as ge:
                        raise QuotaExceededError(f"Все LLM провайдеры (Mistral и Gemini) исчерпали квоты: {ge}")
                else:
                    raise me

        # Сценарий 2: Основной провайдер - Gemini (или fallback при отсутствии Mistral)
        elif has_gemini:
            try:
                result = self._call_gemini(prompt, target_gemini_model, vacancy, temperature=temp_val)
            except QuotaExceededError as qe:
                if fallback_enabled and has_mistral:
                    logger.warning(f"Gemini API вернул ошибку ({qe}). Переход на резервный Mistral AI...")
                    try:
                        result = self._call_mistral(prompt, target_mistral_model, vacancy, temperature=temp_val)
                    except QuotaExceededError as me:
                        raise QuotaExceededError(f"Все LLM провайдеры (Gemini и Mistral) исчерпали квоты: {me}")
                else:
                    raise qe

        # Если Gemini не настроен, но есть Mistral
        elif has_mistral:
            result = self._call_mistral(prompt, target_mistral_model, vacancy, temperature=temp_val)

        if not result:
            raise QuotaExceededError("Не удалось выполнить анализ: все настроенные LLM провайдеры недоступны.")

        # Постобработка: автодополнение selected_resume_id и selected_resume_title
        if actual_resumes:
            if len(actual_resumes) == 1:
                result.selected_resume_id = actual_resumes[0].get("id")
                result.selected_resume_title = actual_resumes[0].get("title")
            else:
                found = False
                if result.selected_resume_id:
                    for r in actual_resumes:
                        if str(r.get("id")).lower() == str(result.selected_resume_id).lower():
                            result.selected_resume_id = r.get("id")
                            result.selected_resume_title = r.get("title")
                            found = True
                            break
                if not found and result.selected_resume_title:
                    for r in actual_resumes:
                        r_title = r.get("title", "")
                        if r_title and (r_title.lower() in result.selected_resume_title.lower() or result.selected_resume_title.lower() in r_title.lower()):
                            result.selected_resume_id = r.get("id")
                            result.selected_resume_title = r.get("title")
                            found = True
                            break
                if not found and actual_resumes:
                    result.selected_resume_id = actual_resumes[0].get("id")
                    result.selected_resume_title = actual_resumes[0].get("title")

        # Применение постфикса сопроводительного письма (если настроен)
        postfix = database.get_system_setting("cover_letter_postfix") or ""
        if postfix and postfix.strip() and result.is_match and result.cover_letter:
            clean_letter = result.cover_letter.strip()
            clean_postfix = postfix.strip()
            if not clean_letter.endswith(clean_postfix):
                result.cover_letter = f"{clean_letter}\n\n{clean_postfix}"

        return result

    def _mock_analysis(self, vacancy: Dict[str, Any], match_threshold: int, resumes: List[Dict[str, Any]] = None) -> VacancyAnalysis:
        """Временный заглушечный анализатор для работы без API ключа."""
        title_lower = vacancy.get('title', '').lower()
        desc_lower = vacancy.get('description', '').lower()
        skills = [s.lower() for s in vacancy.get('skills', [])]

        has_python = 'python' in title_lower or 'python' in desc_lower or 'python' in skills
        has_ai = any(x in title_lower or x in desc_lower or x in skills for x in ['ai', 'llm', 'rag', 'agent', 'nlp'])

        match_score = 0
        if has_python:
            match_score += 50
        if has_ai:
            match_score += 30

        is_match = match_score >= match_threshold

        selected_id = None
        selected_title = None
        if resumes and len(resumes) > 0:
            selected_id = resumes[0].get("id")
            selected_title = resumes[0].get("title")

        reasoning = (
            f"[MOCK] Вакансия проанализирована эвристически. "
            f"Найден Python: {has_python}, Найден AI/LLM: {has_ai}. "
            f"Итоговый балл: {match_score}."
        )

        cover_letter = ""
        if is_match:
            cover_letter = (
                f"Здравствуйте!\n\n"
                f"Меня заинтересовала вакансия {vacancy.get('title')} в компании {vacancy.get('company')}.\n"
                f"У меня есть релевантный опыт, который, как мне кажется, будет полезен вашей команде.\n\n"
                f"Буду рад обсудить подробности на интервью.\n\n"
                f"С уважением,\nКандидат"
            )
            postfix = database.get_system_setting("cover_letter_postfix") or ""
            if postfix and postfix.strip():
                clean_letter = cover_letter.strip()
                clean_postfix = postfix.strip()
                if not clean_letter.endswith(clean_postfix):
                    cover_letter = f"{clean_letter}\n\n{clean_postfix}"

        return VacancyAnalysis(
            match_score=match_score,
            is_match=is_match,
            reasoning=reasoning,
            cover_letter=cover_letter,
            selected_resume_id=selected_id,
            selected_resume_title=selected_title
        )

    def _build_questions_prompt(self, resume_text: str, vacancy: Dict[str, Any], questions: List[Dict[str, Any]], user_saved_answers: List[Dict[str, Any]] = None) -> str:
        """Формирует промпт для генерации ответов на вопросы работодателя."""
        faq_parts = []
        if user_saved_answers:
            for item in user_saved_answers:
                hint = item.get("question_hint") or item.get("key")
                ans = item.get("answer")
                if hint and ans:
                    faq_parts.append(f"- {hint}: {ans}")
        faq_text = "\n".join(faq_parts) if faq_parts else "Не задано"

        questions_formatted = []
        for i, q in enumerate(questions, 1):
            q_id = q.get("id", f"q_{i}")
            q_text = q.get("text", "")
            q_type = q.get("type", "text")
            options = q.get("options", [])
            opt_str = f" (Варианты выбора: {', '.join(options)})" if options else ""
            questions_formatted.append(f"{i}. [ID: {q_id}] [Тип: {q_type}] Вопрос: \"{q_text}\"{opt_str}")
        questions_str = "\n".join(questions_formatted)

        return f"""Вы — профессиональный карьерный ассистент кандидата. Ваша задача — подготовить точные, емкие и выверенные ответы на вопросы работодателя при отклике на вакансию на hh.ru.

РЕЗЮМЕ КАНДИДАТА:
{resume_text}

---
БАЗОВЫЕ ПРЕДПОЧТЕНИЯ И ПРОФИЛЬ КАНДИДАТА (FAQ):
{faq_text}

---
ВАКАНСИЯ:
Название: {vacancy.get('title', '')}
Компания: {vacancy.get('company', '')}
Зарплата: {vacancy.get('salary', 'Не указана')}
Описание:
{vacancy.get('description', '')[:2500]}

---
СПИСОК ВОПРОСОВ РАБОТОДАТЕЛЯ:
{questions_str}

---
ПРАВИЛА И ИНСТРУКЦИИ ДЛЯ ОТВЕТОВ:
1. Используйте ТОЛЬКО достоверную информацию из резюме и базовых предпочтений кандидата. Не выдумывайте факты, которых нет.
2. Для вопросов о локации/городе/гражданстве: используйте город и страну из резюме или профиля кандидата.
3. Для вопросов об IT-аккредитации, тестовом задании, формате работы: опирайтесь на базовые предпочтения кандидата. Если в профиле указано «не критична» или «готов выполнить тестовое», формулируйте вежливый и четкий ответ.
4. Для открытых технических/опытных вопросов: кратко (1-3 емких предложения) опишите реальный опыт кандидата с релевантными технологиями.
5. Для вопросов с вариантами выбора (single_choice / multi_choice): выберите наиболее подходящий вариант из предложенных.
6. Флаг `requires_user_input`:
   - Установите `requires_user_input = False` (confidence 85-100%), если ответ однозначно следует из резюме, базы ответов кандидата (FAQ) или стандартных правил вежливости.
   - Установите `requires_user_input = True` (confidence < 85%), ТОЛЬКО если вопрос требует индивидуального личного решения кандидата, которого нет ни в резюме, ни в профиле (например: «Готовы ли вы выйти в офис в другом городе с понедельника?», «Какой размер опциона вас интересует?»).
7. Поле `all_confident`:
   - Должно быть `True`, ТОЛЬКО ЕСЛИ по ВСЕМ вопросам confidence >= 85% и `requires_user_input = False` для каждого вопроса. Если хотя бы на один вопрос ИИ не уверен — установите `all_confident = False`.

Верните строго валидный JSON в следующем формате (без лишнего текста вокруг):
{{
  "answers": [
    {{
      "id": "ID_вопроса",
      "question_text": "Текст вопроса",
      "answer": "Сформулированный ответ",
      "confidence": 95,
      "requires_user_input": false,
      "reasoning": "Пояснение"
    }}
  ],
  "all_confident": true
}}"""

    def _call_gemini_questions(self, prompt: str, target_model: str, temperature: float = 0.2) -> QuestionsAnalysisResult:
        """Запрос к Gemini для ответов на вопросы с ротацией ключей."""
        tried_keys = set()
        while len(tried_keys) < len(self.gemini_keys):
            active_key, active_client = self._get_active_gemini_client()
            if not active_client or active_key in tried_keys:
                break
            tried_keys.add(active_key)
            key_tag = f"...{active_key[-6:]}" if len(active_key) > 6 else active_key
            try:
                logger.info(f"Отправка запроса к Gemini [{target_model}, ключ {key_tag}] для генерации ответов на вопросы...")
                response = active_client.models.generate_content(
                    model=target_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=QuestionsAnalysisResult,
                        temperature=temperature,
                    )
                )
                LLMAnalyzer._gemini_key_statuses[active_key] = {"status": "ok", "last_checked": time.time()}
                result = QuestionsAnalysisResult.model_validate_json(response.text)
                logger.info(f"Ответы на вопросы от Gemini получены: {len(result.answers)} шт., all_confident={result.all_confident}")
                return result
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
                    logger.warning(f"Исчерпан лимит квоты Gemini на ключе {key_tag} (429). Переключаемся...")
                    LLMAnalyzer._gemini_key_statuses[active_key] = {"status": "error", "reason": "rate_limit_or_quota", "detail": err_msg, "last_checked": time.time()}
                    LLMAnalyzer._current_gemini_idx = (LLMAnalyzer._current_gemini_idx + 1) % len(self.gemini_keys)
                    continue
                elif "403" in err_msg or "API_KEY_INVALID" in err_msg:
                    LLMAnalyzer._gemini_key_statuses[active_key] = {"status": "error", "reason": "invalid_api_key", "detail": err_msg, "last_checked": time.time()}
                    LLMAnalyzer._current_gemini_idx = (LLMAnalyzer._current_gemini_idx + 1) % len(self.gemini_keys)
                    continue
                else:
                    logger.error(f"Ошибка Gemini при ответах на вопросы: {e}")
                    raise e
        raise QuotaExceededError("Все ключи Gemini исчерпали квоту.")

    def _call_mistral_questions(self, prompt: str, target_model: str, temperature: float = 0.2) -> QuestionsAnalysisResult:
        """Запрос к Mistral для ответов на вопросы с ротацией ключей."""
        if not self.mistral_keys:
            raise QuotaExceededError("Ключи Mistral API не настроены.")
        tried_keys = set()
        while len(tried_keys) < len(self.mistral_keys):
            active_key = self._get_active_mistral_key()
            if not active_key or active_key in tried_keys:
                break
            tried_keys.add(active_key)
            key_tag = f"...{active_key[-6:]}" if len(active_key) > 6 else active_key
            try:
                logger.info(f"Отправка запроса к Mistral [{target_model}, ключ {key_tag}] для генерации ответов на вопросы...")
                headers = {"Authorization": f"Bearer {active_key}", "Content-Type": "application/json"}
                payload = {
                    "model": target_model,
                    "messages": [
                        {"role": "system", "content": "You are a professional career assistant answering job applicant screening questions in Russian. Always return valid JSON matching schema: {\"answers\": [{\"id\": str, \"question_text\": str, \"answer\": str, \"confidence\": int, \"requires_user_input\": bool, \"reasoning\": str}], \"all_confident\": bool}."},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": temperature
                }
                resp = requests.post("https://api.mistral.ai/v1/chat/completions", headers=headers, json=payload, timeout=45)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    LLMAnalyzer._mistral_key_statuses[active_key] = {"status": "ok", "last_checked": time.time()}

                    # Очистка и нормализация JSON
                    raw_content = content.strip()
                    if raw_content.startswith("```"):
                        lines = raw_content.splitlines()
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        raw_content = "\n".join(lines).strip()

                    import json
                    parsed = json.loads(raw_content)
                    if isinstance(parsed, list):
                        parsed = {"answers": parsed, "all_confident": True}
                    elif isinstance(parsed, dict):
                        if "answers" not in parsed:
                            for alt_key in ("questions_analysis", "questions", "results", "items", "data"):
                                if alt_key in parsed and isinstance(parsed[alt_key], list):
                                    parsed["answers"] = parsed[alt_key]
                                    break
                        if "answers" not in parsed:
                            parsed = {"answers": [parsed], "all_confident": True}

                        if "all_confident" not in parsed:
                            parsed["all_confident"] = all(
                                isinstance(a, dict) and a.get("confidence", 100) >= 85 and not a.get("requires_user_input", False)
                                for a in parsed.get("answers", [])
                            )

                    result = QuestionsAnalysisResult.model_validate(parsed)
                    logger.info(f"Ответы на вопросы от Mistral получены: {len(result.answers)} шт., all_confident={result.all_confident}")
                    return result
                elif resp.status_code == 429:
                    LLMAnalyzer._mistral_key_statuses[active_key] = {"status": "error", "reason": "rate_limit_or_quota", "detail": resp.text, "last_checked": time.time()}
                    LLMAnalyzer._current_mistral_idx = (LLMAnalyzer._current_mistral_idx + 1) % len(self.mistral_keys)
                    continue
                else:
                    raise Exception(f"Mistral API error {resp.status_code}: {resp.text}")
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "rate limit" in err_msg.lower():
                    LLMAnalyzer._mistral_key_statuses[active_key] = {"status": "error", "reason": "rate_limit_or_quota", "detail": err_msg, "last_checked": time.time()}
                    LLMAnalyzer._current_mistral_idx = (LLMAnalyzer._current_mistral_idx + 1) % len(self.mistral_keys)
                    continue
                logger.warning(f"Ошибка Mistral при генерации ответов на вопросы: {e}")
                raise e
        raise QuotaExceededError("Все ключи Mistral API исчерпали квоту.")

    def _mock_questions_analysis(self, questions: List[Dict[str, Any]], user_saved_answers: List[Dict[str, Any]] = None) -> QuestionsAnalysisResult:
        """Эвристическая генерация ответов на вопросы при отсутствии LLM ключей."""
        answers = []
        user_faq = {item.get("key", ""): item.get("answer", "") for item in (user_saved_answers or [])}
        user_faq.update({(item.get("question_hint", "") or "").lower(): item.get("answer", "") for item in (user_saved_answers or [])})

        for q in questions:
            q_id = q.get("id", "")
            q_text = q.get("text", "")
            q_lower = q_text.lower()
            ans_text = ""
            confidence = 85
            req_user = False
            reason = "Эвристический ответ на основе профиля"

            if any(w in q_lower for w in ["город", "прожива", "локаци", "где вы", "рф"]):
                ans_text = user_faq.get("location_city") or "Россия, г. Пермь (готов к удаленной работе)"
            elif any(w in q_lower for w in ["сумм", "зарплат", "доход", "оплат", "денег", "руб"]):
                ans_text = user_faq.get("salary_min") or "Рассматриваю предложения от 150 000 руб. на руки"
            elif any(w in q_lower for w in ["аккредит", "ит-аккредит", "it"]):
                ans_text = user_faq.get("it_accreditation") or "Нет, IT-аккредитация не критична"
            elif any(w in q_lower for w in ["тестов", "задани"]):
                ans_text = user_faq.get("test_task") or "Да, готов выполнить небольшое тестовое задание"
            elif any(w in q_lower for w in ["формат", "удален", "офис", "гибрид"]):
                ans_text = user_faq.get("work_format") or "Удаленная работа"
            elif any(w in q_lower for w in ["оформлен", "ип", "самозанят", "тк", "гпх"]):
                ans_text = user_faq.get("employment_type") or "ТК РФ, ИП, самозанятость"
            else:
                ans_text = "Готов обсудить подробности на интервью."
                confidence = 50
                req_user = True
                reason = "Вопрос требует уточнения кандидата"

            answers.append(QuestionAnswer(
                id=q_id,
                question_text=q_text,
                answer=ans_text,
                confidence=confidence,
                requires_user_input=req_user,
                reasoning=reason
            ))

        all_conf = all(not a.requires_user_input for a in answers)
        return QuestionsAnalysisResult(answers=answers, all_confident=all_conf)

    def answer_questions(self, resume_text: str, vacancy: Dict[str, Any], questions: List[Dict[str, Any]], user_saved_answers: List[Dict[str, Any]] = None) -> QuestionsAnalysisResult:
        """
        Генерирует профессиональные ответы на вопросы работодателя на основе резюме и базы ответов пользователя.
        """
        if not questions:
            return QuestionsAnalysisResult(answers=[], all_confident=True)

        if not user_saved_answers:
            try:
                user_saved_answers = database.get_user_profile_answers()
            except Exception:
                user_saved_answers = []

        prompt = self._build_questions_prompt(resume_text, vacancy, questions, user_saved_answers)

        has_gemini = bool(self.gemini_keys and genai)
        has_mistral = bool(self.mistral_keys)

        if not has_gemini and not has_mistral:
            logger.warning("API ключи не настроены. Использование эвристического автоответа на вопросы.")
            return self._mock_questions_analysis(questions, user_saved_answers)

        primary_provider = database.get_system_setting("primary_provider", "gemini").lower()
        fallback_enabled_str = database.get_system_setting("fallback_enabled", "true")
        fallback_enabled = fallback_enabled_str.lower() in ("true", "1", "yes") if fallback_enabled_str else True

        try:
            temp_val = float(database.get_system_setting("temperature", "0.2"))
        except (ValueError, TypeError):
            temp_val = 0.2

        target_gemini_model = database.get_config_value("gemini_model") or Config.GEMINI_MODEL or "gemini-3.6-flash"
        target_mistral_model = database.get_config_value("mistral_model") or Config.MISTRAL_MODEL or "mistral-small-latest"

        if primary_provider == "mistral" and has_mistral:
            try:
                return self._call_mistral_questions(prompt, target_mistral_model, temperature=temp_val)
            except Exception as me:
                if fallback_enabled and has_gemini:
                    logger.warning(f"Mistral вернул ошибку при ответах на вопросы ({me}). Переход на Gemini...")
                    try:
                        return self._call_gemini_questions(prompt, target_gemini_model, temperature=temp_val)
                    except Exception as ge:
                        logger.warning(f"Gemini также вернул ошибку ({ge}). Переход на эвристические ответы.")
                        return self._mock_questions_analysis(questions, user_saved_answers)
                logger.warning(f"Ошибка Mistral ({me}). Переход на эвристические ответы.")
                return self._mock_questions_analysis(questions, user_saved_answers)

        if has_gemini:
            try:
                return self._call_gemini_questions(prompt, target_gemini_model, temperature=temp_val)
            except Exception as ge:
                if fallback_enabled and has_mistral:
                    logger.warning(f"Gemini вернул ошибку при ответах на вопросы ({ge}). Переход на Mistral...")
                    try:
                        return self._call_mistral_questions(prompt, target_mistral_model, temperature=temp_val)
                    except Exception as me:
                        logger.warning(f"Mistral также вернул ошибку ({me}). Переход на эвристические ответы.")
                        return self._mock_questions_analysis(questions, user_saved_answers)
                logger.warning(f"Ошибка Gemini ({ge}). Переход на эвристические ответы.")
                return self._mock_questions_analysis(questions, user_saved_answers)

        if has_mistral:
            try:
                return self._call_mistral_questions(prompt, target_mistral_model, temperature=temp_val)
            except Exception as me:
                logger.warning(f"Ошибка Mistral ({me}). Переход на эвристические ответы.")
                return self._mock_questions_analysis(questions, user_saved_answers)

        return self._mock_questions_analysis(questions, user_saved_answers)

    def _build_cover_letter_prompt(self, resume_text: str, vacancy: Dict[str, Any], resumes: List[Dict[str, Any]] = None) -> str:
        """Формирует целевой промпт для генерации сопроводительного письма."""
        skills_raw = vacancy.get('skills', [])
        skills_str = ', '.join(skills_raw) if isinstance(skills_raw, list) else str(skills_raw or '')
        
        final_resume_text = str(resume_text or "")
        multi_info = ""
        if resumes and len(resumes) > 1:
            res_blocks = []
            for idx, r in enumerate(resumes, 1):
                r_id = r.get("id", f"resume_{idx}")
                r_title = r.get("title", f"Резюме {idx}")
                r_text = r.get("text", "")
                res_blocks.append(f"=== [РЕЗЮМЕ #{idx}] ID: {r_id} | Должность: {r_title} ===\n{r_text}\n")
            final_resume_text = "\n".join(res_blocks)
            multi_info = "\nВыберите факты и стек из наиболее подходящего резюме кандидата."
        elif resumes and len(resumes) == 1:
            final_resume_text = resumes[0].get("text", "")

        return f"""Вы — профессиональный IT-рекрутер и карьерный консультант.
Ваша цель — составить убедительное, персонализированное и живое сопроводительное письмо (Cover Letter) для отклика на hh.ru от имени кандидата.

ВАЖНО: Даже если кандидат формально не подходит по всем критериям (например, вакансия была отсеяна автоматическим фильтром), найдите реальные точки соприкосновения, релевантный смежный опыт, владение технологиями и сильную мотивацию. Подчеркните готовность быстро включиться в работу и принести пользу. Ни в коем случае не пишите «я знаю, что не подхожу» или другие извиняющиеся фразы — тон должен быть уверенным и позитивным.

РЕЗЮМЕ КАНДИДАТА:
{final_resume_text}
{multi_info}

ВАКАНСИЯ:
Название: {vacancy.get('title', 'Вакансия')}
Компания: {vacancy.get('company', '')}
Зарплата: {vacancy.get('salary', 'Не указана')}
Требуемый опыт: {vacancy.get('experience', 'Не указан')}
Занятость: {vacancy.get('employment', 'Не указана')}
График: {vacancy.get('schedule', 'Не указан')}
Локация: {vacancy.get('location', 'Не указана')}
Ключевые навыки: {skills_str}
Описание:
{vacancy.get('description', '')}

СТРОГИЕ ПРАВИЛА СОПРОВОДИТЕЛЬНОГО ПИСЬМА:
1. ОБЪЕМ: ровно 3-5 емких предложений (до 600-800 символов). Рекрутеры читают письмо по диагонали.
2. БЕЗ ШАБЛОНОВ И ВОДЫ: Запрещены штампы «Прошу рассмотреть мою кандидатуру», «Я коммуникабельный и стрессоустойчивый», «С интересом ознакомился».
3. СТРУКТУРА:
   - Живой заход: «Здравствуйте! Откликаюсь на позицию {vacancy.get('title', 'вакансии')}. Мой бэкграунд отлично перекликается с вашими задачами:» (название компании напрямую не упоминайте, чтобы звучало естественно).
   - Суть и ценность: 1-2 предложения с точным попаданием в ключевой стек, релевантные проекты или смежные сильные стороны из резюме.
   - Завершение и диалог: «Буду рад подробнее обсудить задачи на интервью. С уважением, [Имя кандидата из резюме]».
4. ТОН: уверенный, профессиональный, на русском языке.
5. ПРАВДИВОСТЬ: опирайтесь строго на реальный опыт, навыки и проекты кандидата из резюме. Не выдумывайте опыт.

Верните строго JSON в формате:
{{
  "cover_letter": "текст сопроводительного письма"
}}
"""

    def _call_gemini_cover_letter(self, prompt: str, target_model: str, temperature: float = 0.2) -> str:
        """Запрос к Gemini для генерации сопроводительного письма с ротацией ключей."""
        tried_keys = set()
        while len(tried_keys) < len(self.gemini_keys):
            active_key, active_client = self._get_active_gemini_client()
            if not active_client or active_key in tried_keys:
                break
            tried_keys.add(active_key)
            key_tag = f"...{active_key[-6:]}" if len(active_key) > 6 else active_key
            try:
                logger.info(f"Отправка запроса к Gemini [{target_model}, ключ {key_tag}] для генерации сопроводительного письма...")
                response = active_client.models.generate_content(
                    model=target_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=CoverLetterResult,
                        temperature=temperature,
                    )
                )
                LLMAnalyzer._gemini_key_statuses[active_key] = {"status": "ok", "last_checked": time.time()}
                result = CoverLetterResult.model_validate_json(response.text)
                return result.cover_letter.strip()
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
                    logger.warning(f"Исчерпан лимит квоты Gemini на ключе {key_tag} (429). Переключаемся...")
                    LLMAnalyzer._gemini_key_statuses[active_key] = {"status": "error", "reason": "rate_limit_or_quota", "detail": err_msg, "last_checked": time.time()}
                    LLMAnalyzer._current_gemini_idx = (LLMAnalyzer._current_gemini_idx + 1) % len(self.gemini_keys)
                    continue
                elif "403" in err_msg or "API_KEY_INVALID" in err_msg:
                    LLMAnalyzer._gemini_key_statuses[active_key] = {"status": "error", "reason": "invalid_api_key", "detail": err_msg, "last_checked": time.time()}
                    LLMAnalyzer._current_gemini_idx = (LLMAnalyzer._current_gemini_idx + 1) % len(self.gemini_keys)
                    continue
                else:
                    logger.error(f"Ошибка Gemini при генерации письма: {e}")
                    raise e
        raise QuotaExceededError("Все ключи Gemini исчерпали квоту.")

    def _call_mistral_cover_letter(self, prompt: str, target_model: str, temperature: float = 0.2) -> str:
        """Запрос к Mistral для генерации сопроводительного письма."""
        if not self.mistral_keys:
            raise QuotaExceededError("Ключи Mistral API не настроены.")
        tried_keys = set()
        while len(tried_keys) < len(self.mistral_keys):
            active_key = self._get_active_mistral_key()
            if not active_key or active_key in tried_keys:
                break
            tried_keys.add(active_key)
            key_tag = f"...{active_key[-6:]}" if len(active_key) > 6 else active_key
            try:
                logger.info(f"Отправка запроса к Mistral [{target_model}, ключ {key_tag}] для генерации сопроводительного письма...")
                headers = {"Authorization": f"Bearer {active_key}", "Content-Type": "application/json"}
                payload = {
                    "model": target_model,
                    "messages": [
                        {"role": "system", "content": "You are a professional career consultant writing a personalized cover letter in Russian. Return valid JSON matching schema: {\"cover_letter\": str}."},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": temperature
                }
                resp = requests.post("https://api.mistral.ai/v1/chat/completions", headers=headers, json=payload, timeout=45)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    LLMAnalyzer._mistral_key_statuses[active_key] = {"status": "ok", "last_checked": time.time()}

                    raw_content = content.strip()
                    if raw_content.startswith("```"):
                        lines = raw_content.splitlines()
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        raw_content = "\n".join(lines).strip()

                    parsed = json.loads(raw_content)
                    if isinstance(parsed, dict) and "cover_letter" in parsed:
                        return parsed["cover_letter"].strip()
                    elif isinstance(parsed, dict):
                        return next(iter(parsed.values()), "").strip()
                    return str(parsed).strip()
                elif resp.status_code == 429:
                    logger.warning(f"Исчерпан лимит квоты Mistral на ключе {key_tag} (429). Переключаемся...")
                    LLMAnalyzer._mistral_key_statuses[active_key] = {"status": "error", "reason": "rate_limit_or_quota", "detail": resp.text, "last_checked": time.time()}
                    LLMAnalyzer._current_mistral_idx = (LLMAnalyzer._current_mistral_idx + 1) % len(self.mistral_keys)
                    continue
                else:
                    raise Exception(f"Mistral API error HTTP {resp.status_code}: {resp.text}")
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg:
                    continue
                logger.error(f"Ошибка Mistral при генерации письма: {e}")
                raise e
        raise QuotaExceededError("Все ключи Mistral исчерпали квоту.")

    def _mock_cover_letter(self, resume_text: str, vacancy: Dict[str, Any]) -> str:
        """Эвристическая генерация письма при отсутствии или сбое LLM API."""
        title = vacancy.get('title', 'позицию разработчика')
        company = vacancy.get('company', '')
        skills = vacancy.get('skills', [])
        skills_str = ', '.join(skills[:4]) if isinstance(skills, list) and skills else ""
        
        name = "Кандидат"
        for line in (resume_text or "").splitlines()[:5]:
            clean_l = line.strip()
            if clean_l and len(clean_l.split()) in (2, 3) and not any(ch in clean_l for ch in [":", "@", "/", "\\", "{", "}"]):
                name = clean_l
                break

        stack_sentence = f"Имею опыт решения прикладных задач с использованием {skills_str} и готов быстро включиться в работу над вашими проектами." if skills_str else "Мой практический опыт в разработке позволяет быстро погружаться в новые задачи и проектный контекст."
        
        letter = (
            f"Здравствуйте!\n\n"
            f"Меня заинтересовала вакансия {title}. {stack_sentence}\n\n"
            f"Буду рад подробнее обсудить задачи и требования на интервью.\n\n"
            f"С уважением,\n{name}"
        )
        return letter

    def generate_cover_letter(self, resume_text: str = "", vacancy: Dict[str, Any] = None, resumes: List[Dict[str, Any]] = None) -> str:
        """
        Генерирует качественное персонализированное сопроводительное письмо на основе вакансии и резюме.
        Применяет настроенный постфикс (cover_letter_postfix).
        """
        if not vacancy:
            vacancy = {}

        if resumes and not resume_text:
            resume_text = resumes[0].get("text", "")

        prompt = self._build_cover_letter_prompt(resume_text, vacancy, resumes)

        has_gemini = bool(self.gemini_keys and genai)
        has_mistral = bool(self.mistral_keys)

        letter = ""
        if not has_gemini and not has_mistral:
            logger.warning("API ключи не настроены. Использование эвристического сопроводительного письма.")
            letter = self._mock_cover_letter(resume_text, vacancy)
        else:
            primary_provider = database.get_system_setting("primary_provider", "gemini").lower()
            fallback_enabled_str = database.get_system_setting("fallback_enabled", "true")
            fallback_enabled = fallback_enabled_str.lower() in ("true", "1", "yes") if fallback_enabled_str else True

            try:
                temp_val = float(database.get_system_setting("temperature", "0.2"))
            except (ValueError, TypeError):
                temp_val = 0.2

            target_gemini_model = database.get_config_value("gemini_model") or Config.GEMINI_MODEL or "gemini-3.6-flash"
            target_mistral_model = database.get_config_value("mistral_model") or Config.MISTRAL_MODEL or "mistral-small-latest"

            if primary_provider == "mistral" and has_mistral:
                try:
                    letter = self._call_mistral_cover_letter(prompt, target_mistral_model, temperature=temp_val)
                except Exception as me:
                    if fallback_enabled and has_gemini:
                        logger.warning(f"Mistral вернул ошибку при генерации письма ({me}). Переход на Gemini...")
                        try:
                            letter = self._call_gemini_cover_letter(prompt, target_gemini_model, temperature=temp_val)
                        except Exception as ge:
                            logger.warning(f"Gemini также вернул ошибку ({ge}). Переход на шаблон.")
                            letter = self._mock_cover_letter(resume_text, vacancy)
                    else:
                        letter = self._mock_cover_letter(resume_text, vacancy)
            elif has_gemini:
                try:
                    letter = self._call_gemini_cover_letter(prompt, target_gemini_model, temperature=temp_val)
                except Exception as ge:
                    if fallback_enabled and has_mistral:
                        logger.warning(f"Gemini вернул ошибку при генерации письма ({ge}). Переход на Mistral...")
                        try:
                            letter = self._call_mistral_cover_letter(prompt, target_mistral_model, temperature=temp_val)
                        except Exception as me:
                            logger.warning(f"Mistral также вернул ошибку ({me}). Переход на шаблон.")
                            letter = self._mock_cover_letter(resume_text, vacancy)
                    else:
                        letter = self._mock_cover_letter(resume_text, vacancy)
            elif has_mistral:
                try:
                    letter = self._call_mistral_cover_letter(prompt, target_mistral_model, temperature=temp_val)
                except Exception as me:
                    letter = self._mock_cover_letter(resume_text, vacancy)

        if not letter or not letter.strip():
            letter = self._mock_cover_letter(resume_text, vacancy)

        # Применение постфикса
        postfix = database.get_system_setting("cover_letter_postfix") or ""
        if postfix and postfix.strip():
            clean_letter = letter.strip()
            clean_postfix = postfix.strip()
            if not clean_letter.endswith(clean_postfix):
                letter = f"{clean_letter}\n\n{clean_postfix}"

        return letter


