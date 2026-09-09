import os
import sys
import re
import json
import time
import logging
from typing import Dict, Any, List, Tuple, Optional
from pydantic import BaseModel, Field, field_validator
import requests

def _coerce_to_str(v: Any) -> str:
    """Преобразует строку, словарь или список от LLM в валидную строку для Pydantic."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        # Если модель вложила текст под типичными ключами
        for key in ["text", "cover_letter", "letter", "content", "body", "suggestion", "summary", "answer", "reasoning", "note"]:
            if key in v and isinstance(v[key], str) and v[key].strip():
                return v[key].strip()
        # Иначе формируем читаемый текст из полей словаря
        lines = []
        for k, val in v.items():
            if isinstance(val, dict):
                sub = "; ".join(f"{sk}: {sv}" for sk, sv in val.items())
                lines.append(f"{k}: {sub}")
            elif isinstance(val, list):
                sub = ", ".join(str(item) for item in val)
                lines.append(f"{k}: {sub}")
            else:
                lines.append(f"{k}: {val}")
        return "\n".join(lines).strip()
    if isinstance(v, list):
        return "\n".join(str(item) for item in v if item is not None).strip()
    return str(v)

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

class EvaluationScores(BaseModel):
    stack_score: int = Field(default=0, ge=0, le=30, description="Совпадение стека технологий (от 0 до 30 баллов)")
    experience_score: int = Field(default=0, ge=0, le=25, description="Релевантный профильный опыт и стаж в годах (от 0 до 25 баллов)")
    grade_score: int = Field(default=0, ge=0, le=20, description="Соответствие грейду и уровню ответственности (от 0 до 20 баллов)")
    domain_score: int = Field(default=0, ge=0, le=15, description="Совпадение задач и специфики домена (от 0 до 15 баллов)")
    format_score: int = Field(default=0, ge=0, le=10, description="Формат работы, локация и условия (от 0 до 10 баллов)")

    @property
    def total(self) -> int:
        return self.stack_score + self.experience_score + self.grade_score + self.domain_score + self.format_score

class VacancyAnalysis(BaseModel):
    reasoning: str = Field(description="Подробный критический разбор (рассуждение вслух, Chain of Thought): сопоставление стека, стажа в годах, грейда, домена и формата работы ПЕРЕД выставлением оценок")
    scores: Optional[EvaluationScores] = Field(default=None, description="Оценки по 5 шкалам соответствия (суммарно от 0 до 100)")
    has_hard_blocker: bool = Field(default=False, description="True, если есть критическое непреодолимое противоречие (очный офис без переезда, гражданство/гостайна, полное несовпадение направления)")
    blocker_reason: Optional[str] = Field(default=None, description="Описание блокирующего фактора, если has_hard_blocker = True")
    match_score: int = Field(default=0, description="Суммарная оценка соответствия от 0 до 100")
    is_match: bool = Field(default=False, description="Подходит ли вакансия для отклика (True/False)")
    cover_letter: str = Field(default="", description="Краткое (3-5 предложений), персонализированное сопроводительное письмо на русском языке на основе выбранного резюме. Заполняется только если нет блокеров и кандидат подходит.")
    selected_resume_id: Optional[str] = Field(default=None, description="ID выбранного лучшего резюме (если передано несколько резюме)")
    selected_resume_title: Optional[str] = Field(default=None, description="Название/должность выбранного лучшего резюме")
    analyzed_by_provider: Optional[str] = Field(default=None, description="ID или имя провайдера LLM, выполнившего анализ")
    analyzed_by_model: Optional[str] = Field(default=None, description="Название модели LLM, выполнившей анализ")

    @field_validator("reasoning", "cover_letter", mode="before")
    @classmethod
    def _coerce_analysis_strings(cls, v: Any) -> str:
        return _coerce_to_str(v)

    @field_validator("blocker_reason", mode="before")
    @classmethod
    def _coerce_blocker_reason(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        res = _coerce_to_str(v)
        return res if res.strip() and res.lower() != "null" and res.lower() != "none" else None

    @field_validator("scores", mode="before")
    @classmethod
    def _coerce_scores(cls, v: Any) -> Optional[EvaluationScores]:
        if isinstance(v, EvaluationScores):
            return v
        if isinstance(v, dict):
            return EvaluationScores(
                stack_score=max(0, min(30, int(v.get("stack_score", 0)))),
                experience_score=max(0, min(25, int(v.get("experience_score", 0)))),
                grade_score=max(0, min(20, int(v.get("grade_score", 0)))),
                domain_score=max(0, min(15, int(v.get("domain_score", 0)))),
                format_score=max(0, min(10, int(v.get("format_score", 0))))
            )
        return None

    def calculate_total_score(self) -> int:
        """Пересчитывает match_score как сумму баллов по 5 шкалам, если scores задан."""
        if self.scores:
            calculated = self.scores.total
            self.match_score = max(0, min(100, calculated))
            if self.scores.stack_score < 15:
                self.is_match = False
                self.cover_letter = ""
        return self.match_score

class QuestionAnswer(BaseModel):
    id: str = Field(description="Уникальный идентификатор вопроса (или имя поля формы, например task_335457717_text)")
    question_text: str = Field(description="Текст вопроса работодателя")
    answer: str = Field(description="Точный, профессиональный и емкий ответ на русском языке (или выбранный вариант ответа из вариантов options)")
    confidence: int = Field(description="Уверенность модели в правильности/соответствии ответа от 0 до 100")
    requires_user_input: bool = Field(description="True, если вопрос требует индивидуального решения пользователя (например, редкие личные условия, не указанные в резюме/профиле); False, если ответ понятен и уверенно следует из резюме или профиля")
    reasoning: str = Field(description="Краткое обоснование выбранного ответа")

    @field_validator("answer", "reasoning", mode="before")
    @classmethod
    def _coerce_answer_strings(cls, v: Any) -> str:
        return _coerce_to_str(v)

class QuestionsAnalysisResult(BaseModel):
    answers: List[QuestionAnswer] = Field(description="Список ответов на каждый заданный вопрос")
    all_confident: bool = Field(description="True, если все вопросы закрыты уверенно и не требуют ручного ввода")

class CoverLetterResult(BaseModel):
    cover_letter: str = Field(description="Краткое (3-5 предложений), персонализированное сопроводительное письмо на русском языке на основе выбранного резюме.")

    @field_validator("cover_letter", mode="before")
    @classmethod
    def _coerce_cover_letter(cls, v: Any) -> str:
        return _coerce_to_str(v)

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

UNIFIED_PROVIDERS = {
    "gemini": {
        "id": "gemini",
        "name": "🔵 Google Gemini API",
        "icon": "✨",
        "protocol": "gemini",
        "base_url": "https://generativelanguage.googleapis.com",
        "default_model": "gemini-3.6-flash",
        "is_free": True,
        "models": [
            {"id": "gemini-3.6-flash", "name": "gemini-3.6-flash (Рекомендуемая, актуальная)", "is_free": True, "is_default": True, "context_window": 1000000},
            {"id": "gemini-2.5-flash", "name": "gemini-2.5-flash", "is_free": True, "context_window": 1000000},
            {"id": "gemini-2.0-flash", "name": "gemini-2.0-flash", "is_free": True, "context_window": 1000000},
            {"id": "gemini-3.5-flash", "name": "gemini-3.5-flash", "is_free": True, "context_window": 1000000},
            {"id": "gemini-flash-latest", "name": "gemini-flash-latest", "is_free": True, "context_window": 1000000},
            {"id": "gemini-3.1-pro-preview", "name": "gemini-3.1-pro-preview", "is_free": True, "context_window": 2000000}
        ],
        "get_key_url": "https://aistudio.google.com/app/apikey",
        "key_prefix_hint": "AIzaSy...",
        "description": "Мультимодальные модели Google Gemini с длинным контекстом для детального анализа резюме.",
        "badge": "Gemini Flash"
    },
    "mistral": {
        "id": "mistral",
        "name": "🟠 Mistral AI",
        "icon": "🌊",
        "protocol": "mistral",
        "base_url": "https://api.mistral.ai/v1",
        "default_model": "ministral-8b-latest",
        "is_free": True,
        "models": [
            {"id": "ministral-8b-latest", "name": "ministral-8b-latest (8B, быстрая и точная)", "is_free": True, "is_default": True, "context_window": 128000},
            {"id": "open-mistral-nemo", "name": "open-mistral-nemo (12B)", "is_free": True, "context_window": 128000},
            {"id": "ministral-3b-latest", "name": "ministral-3b-latest (3B)", "is_free": True, "context_window": 128000},
            {"id": "codestral-latest", "name": "codestral-latest (IT и код)", "is_free": False, "context_window": 256000},
            {"id": "mistral-small-latest", "name": "mistral-small-latest", "is_free": False, "context_window": 128000},
            {"id": "mistral-large-latest", "name": "mistral-large-latest (Макс. качество)", "is_free": False, "context_window": 128000}
        ],
        "get_key_url": "https://console.mistral.ai/api-keys/",
        "key_prefix_hint": "mistral_...",
        "description": "Европейский провайдер моделей Mistral для анализа стека и генерации писем.",
        "badge": "Mistral AI"
    },
    "groq": {
        "id": "groq",
        "name": "⚡ Groq (Llama 3.3 70B)",
        "icon": "⚡",
        "protocol": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "is_free": True,
        "models": [
            {"id": "llama-3.3-70b-versatile", "name": "llama-3.3-70b-versatile (Рекомендуемая, 70B)", "is_free": True, "is_default": True, "context_window": 128000},
            {"id": "deepseek-r1-distill-llama-70b", "name": "deepseek-r1-distill-llama-70b (Рассуждающая R1)", "is_free": True, "context_window": 128000},
            {"id": "llama-3.1-8b-instant", "name": "llama-3.1-8b-instant (Сверхбыстрая 8B)", "is_free": True, "context_window": 128000},
            {"id": "gemma2-9b-it", "name": "gemma2-9b-it (Google Gemma 2)", "is_free": True, "context_window": 8192}
        ],
        "get_key_url": "https://console.groq.com/keys",
        "key_prefix_hint": "gsk_...",
        "description": "Сверхбыстрый LPU-инференс моделей семейства Llama 3 с мгновенным откликом.",
        "badge": "Groq LPU"
    },
    "openrouter": {
        "id": "openrouter",
        "name": "🌐 OpenRouter",
        "icon": "🌐",
        "protocol": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openrouter/free",
        "is_free": True,
        "models": [
            {"id": "openrouter/free", "name": "Free Models Router (Авто-выбор)", "is_free": True, "is_default": True, "context_window": 200000},
            {"id": "nvidia/nemotron-3-super-120b-a12b:free", "name": "NVIDIA Nemotron 3 Super 120B", "is_free": True, "context_window": 262144},
            {"id": "minimax/minimax-m2.7:free", "name": "MiniMax M2.7", "is_free": True, "context_window": 196608},
            {"id": "minimax/minimax-m3:free", "name": "MiniMax M3", "is_free": True, "context_window": 1048576},
            {"id": "google/gemma-4-31b-it:free", "name": "Google Gemma 4 31B", "is_free": True, "context_window": 262144},
            {"id": "liquid/lfm-2.5-2.6b:free", "name": "LiquidAI LFM 2.5 2.6B", "is_free": True, "context_window": 65536},
            {"id": "cohere/north-mini-code:free", "name": "Cohere North Mini Code", "is_free": True, "context_window": 256000}
        ],
        "get_key_url": "https://openrouter.ai/keys",
        "key_prefix_hint": "sk-or-v1-...",
        "description": "Единый API-шлюз ко множеству нейросетей с автоматической маршрутизацией моделей.",
        "badge": "OpenRouter"
    },
    "github": {
        "id": "github",
        "name": "🐙 GitHub Models (Azure AI)",
        "icon": "🐙",
        "protocol": "openai",
        "base_url": "https://models.inference.ai.azure.com",
        "default_model": "gpt-4o-mini",
        "is_free": True,
        "models": [
            {"id": "gpt-4o-mini", "name": "gpt-4o-mini (Рекомендуемая)", "is_free": True, "is_default": True, "context_window": 128000},
            {"id": "meta-llama-3.3-70b-instruct", "name": "meta-llama-3.3-70b-instruct (70B)", "is_free": True, "context_window": 128000},
            {"id": "gpt-4o", "name": "gpt-4o (Флагман OpenAI)", "is_free": True, "context_window": 128000},
            {"id": "Mistral-large-2411", "name": "Mistral-large-2411", "is_free": True, "context_window": 128000}
        ],
        "get_key_url": "https://github.com/settings/tokens",
        "key_prefix_hint": "ghp_...",
        "description": "Инференс моделей Azure AI по персональному токену GitHub (PAT classic).",
        "badge": "GitHub Models"
    },
    "cerebras": {
        "id": "cerebras",
        "name": "🧠 Cerebras Cloud",
        "icon": "🧠",
        "protocol": "openai",
        "base_url": "https://api.cerebras.ai/v1",
        "default_model": "llama-3.3-70b",
        "is_free": True,
        "models": [
            {"id": "llama-3.3-70b", "name": "llama-3.3-70b (Рекомендуемая, 70B)", "is_free": True, "is_default": True, "context_window": 128000},
            {"id": "llama3.1-8b", "name": "llama3.1-8b (Быстрая 8B)", "is_free": True, "context_window": 128000}
        ],
        "get_key_url": "https://cloud.cerebras.ai/",
        "key_prefix_hint": "csk-...",
        "description": "Аппаратный инференс моделей Llama на специализированных чипах Wafer-Scale Engine.",
        "badge": "Cerebras WSE"
    },
    "custom": {
        "id": "custom",
        "name": "🛠️ Пользовательский OpenAI / Ollama",
        "icon": "🛠️",
        "protocol": "openai",
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3.2",
        "is_free": True,
        "models": [
            {"id": "llama3.2", "name": "llama3.2 (локальная)", "is_free": True, "is_default": True, "context_window": 128000},
            {"id": "qwen2.5:14b", "name": "qwen2.5:14b (локальная)", "is_free": True, "context_window": 32768},
            {"id": "gpt-4o-mini", "name": "gpt-4o-mini", "is_free": False, "context_window": 128000}
        ],
        "get_key_url": "https://platform.openai.com/api-keys",
        "key_prefix_hint": "sk-...",
        "description": "Любой совместимый с OpenAI API сервер (локальный Ollama, LM Studio, vLLM или прокси).",
        "badge": "Custom API"
    }
}

# Обратная совместимость для OpenAI пресетов
OPENAI_PROVIDER_PRESETS = {k: v for k, v in UNIFIED_PROVIDERS.items() if v.get("protocol") == "openai"}

class LLMAnalyzer:
    _gemini_key_statuses: Dict[str, dict] = {}   # key -> {"status": "ok"|"error", "reason": ..., "detail": ..., "last_checked": float}
    _mistral_key_statuses: Dict[str, dict] = {}  # key -> {"status": "ok"|"error", "reason": ..., "detail": ..., "last_checked": float}
    _openai_key_statuses: Dict[str, dict] = {}   # key -> {"status": "ok"|"error", "reason": ..., "detail": ..., "last_checked": float}
    _provider_key_statuses: Dict[str, dict] = {}
    _current_gemini_idx: int = 0
    _current_mistral_idx: int = 0
    _current_openai_idx: int = 0
    _initialized_keys: bool = False

    # Обратная совместимость для _key_statuses
    @classmethod
    def get_key_statuses(cls) -> Dict[str, dict]:
        merged = dict(cls._gemini_key_statuses)
        merged.update(cls._mistral_key_statuses)
        merged.update(cls._openai_key_statuses)
        merged.update(cls._provider_key_statuses)
        return merged

    @classmethod
    def record_key_status(cls, key: str, status: str, reason: str = "", detail: str = "", provider_id: str = "", latency_ms: int = 0):
        """Записывает актуальный статус API-ключа."""
        if not key:
            return
        now = time.time()
        info = {
            "status": status,
            "reason": reason,
            "detail": detail,
            "last_checked": now,
            "provider_id": provider_id,
            "latency_ms": latency_ms
        }
        cls._provider_key_statuses[key] = info
        if provider_id == "gemini":
            cls._gemini_key_statuses[key] = info
        elif provider_id == "mistral":
            cls._mistral_key_statuses[key] = info
        else:
            cls._openai_key_statuses[key] = info

    @classmethod
    def remove_key_status(cls, key: str):
        """Удаляет статус удаленного API-ключа из кэша."""
        if not key:
            return
        cls._gemini_key_statuses.pop(key, None)
        cls._mistral_key_statuses.pop(key, None)
        cls._openai_key_statuses.pop(key, None)
        cls._provider_key_statuses.pop(key, None)

    @classmethod
    def get_provider_status(cls, provider_id: str, keys: List[str], is_enabled: bool = True) -> dict:
        """
        Возвращает операционный статус провайдера:
        - active: готов к работе, ключи настроены и не исчерпаны
        - rate_limited: ключи настроены, но вендор вернул 429 Quota Exceeded
        - unconfigured: вообще нет ключей в пуле (0 ключей)
        - disabled: выключен пользователем
        """
        keys_list = [k for k in (keys or []) if k and not k.lower().startswith("your_")]
        if not is_enabled:
            return {
                "status": "disabled",
                "label": "Отключен",
                "color": "gray",
                "badge_class": "status-badge-disabled",
                "ready": False,
                "rate_limited": False,
                "unconfigured": False,
                "keys_count": len(keys_list),
                "available_keys_count": 0,
                "rate_limited_keys_count": 0,
                "message": "Провайдер выключен в настройках"
            }
        if not keys_list:
            return {
                "status": "unconfigured",
                "label": "Не настроен",
                "color": "neutral",
                "badge_class": "status-badge-neutral",
                "ready": False,
                "rate_limited": False,
                "unconfigured": True,
                "keys_count": 0,
                "available_keys_count": 0,
                "rate_limited_keys_count": 0,
                "message": "API-ключи не добавлены. Нажмите для настройки."
            }

        all_statuses = cls.get_key_statuses()
        rate_limited_count = 0
        error_count = 0
        ok_count = 0
        now = time.time()

        for k in keys_list:
            st = all_statuses.get(k)
            if st:
                is_rate_limit = (
                    st.get("status") == "rate_limited"
                    or st.get("reason") in ("rate_limit_or_quota", "rate_limited", "quota_exceeded")
                )
                if is_rate_limit:
                    # Если с момента 429 прошло больше 60 сек, считаем возможным повторный запрос
                    if now - st.get("last_checked", 0) > 60:
                        ok_count += 1
                    else:
                        rate_limited_count += 1
                elif st.get("status") in ("error", "invalid"):
                    error_count += 1
                else:
                    ok_count += 1
            else:
                ok_count += 1

        if rate_limited_count > 0 and (rate_limited_count == len(keys_list)):
            return {
                "status": "rate_limited",
                "label": "Лимит исчерпан (429)",
                "color": "amber",
                "badge_class": "status-badge-warning",
                "ready": False,
                "rate_limited": True,
                "unconfigured": False,
                "keys_count": len(keys_list),
                "available_keys_count": 0,
                "rate_limited_keys_count": rate_limited_count,
                "message": f"Исчерпаны лимиты запросов (429). Включен автоматический Fallback."
            }
        elif error_count > 0 and (error_count + rate_limited_count == len(keys_list)):
            return {
                "status": "error",
                "label": "Ошибка ключей",
                "color": "red",
                "badge_class": "status-badge-danger",
                "ready": False,
                "rate_limited": False,
                "unconfigured": False,
                "keys_count": len(keys_list),
                "available_keys_count": 0,
                "rate_limited_keys_count": rate_limited_count,
                "message": "Ключи не прошли валидацию. Проверьте правильность ключей."
            }
        else:
            avail = len(keys_list) - rate_limited_count - error_count
            return {
                "status": "active",
                "label": "Активен",
                "color": "green",
                "badge_class": "status-badge-active",
                "ready": True,
                "rate_limited": False,
                "unconfigured": False,
                "keys_count": len(keys_list),
                "available_keys_count": max(1, avail),
                "rate_limited_keys_count": rate_limited_count,
                "message": f"Готов к работе. В пуле {len(keys_list)} {('ключ' if len(keys_list) == 1 else 'ключа' if len(keys_list) in (2,3,4) else 'ключей')}."
            }

    def __init__(self, gemini_api_keys: List[str] = None, mistral_api_keys: List[str] = None, openai_api_keys: List[str] = None, api_keys: List[str] = None):
        # 1. Ключи Gemini
        raw_gemini = gemini_api_keys or api_keys
        if raw_gemini:
            self.gemini_keys = database._parse_keys_field(raw_gemini)
        else:
            gem_cfg = database.get_provider_config("gemini")
            if gem_cfg is not None:
                self.gemini_keys = gem_cfg.get("api_keys", [])
            else:
                db_gemini = database.get_config_value("gemini_api_keys")
                if db_gemini is not None:
                    self.gemini_keys = database._parse_keys_field(db_gemini)
                else:
                    self.gemini_keys = database._parse_keys_field(Config.GEMINI_API_KEYS or ([Config.GEMINI_API_KEY] if Config.GEMINI_API_KEY else []))

        # Для обратной совместимости
        self.api_keys = self.gemini_keys

        # 2. Ключи Mistral
        if mistral_api_keys:
            self.mistral_keys = database._parse_keys_field(mistral_api_keys)
        else:
            mis_cfg = database.get_provider_config("mistral")
            if mis_cfg is not None:
                self.mistral_keys = mis_cfg.get("api_keys", [])
            else:
                db_mistral = database.get_config_value("mistral_api_keys")
                if db_mistral is not None:
                    self.mistral_keys = database._parse_keys_field(db_mistral)
                else:
                    self.mistral_keys = database._parse_keys_field(Config.MISTRAL_API_KEYS or ([Config.MISTRAL_API_KEY] if Config.MISTRAL_API_KEY else []))

        # 3. Ключи OpenAI-совместимого провайдера (Groq, OpenRouter, GitHub Models и др.)
        self.openai_provider_preset = database.get_config_value("openai_provider_preset") or Config.OPENAI_PROVIDER_PRESET or "groq"
        preset_info = OPENAI_PROVIDER_PRESETS.get(self.openai_provider_preset, {})
        default_base_url = preset_info.get("base_url", "https://api.groq.com/openai/v1")

        if openai_api_keys:
            self.openai_keys = database._parse_keys_field(openai_api_keys)
        else:
            oa_cfg = database.get_provider_config(self.openai_provider_preset)
            if oa_cfg is not None:
                self.openai_keys = oa_cfg.get("api_keys", [])
            else:
                db_openai = database.get_config_value("openai_api_keys")
                if db_openai is not None:
                    self.openai_keys = database._parse_keys_field(db_openai)
                else:
                    self.openai_keys = database._parse_keys_field(Config.OPENAI_API_KEYS or ([Config.OPENAI_API_KEY] if Config.OPENAI_API_KEY else []))

        self.openai_base_url = (oa_cfg.get("base_url") if (not openai_api_keys and oa_cfg) else None) or database.get_config_value("openai_base_url") or Config.OPENAI_BASE_URL or default_base_url
        self.openai_model = (oa_cfg.get("active_model") if (not openai_api_keys and oa_cfg) else None) or database.get_config_value("openai_model") or Config.OPENAI_MODEL or preset_info.get("default_model", "llama-3.3-70b-versatile")

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
        Проверяет доступность всех настроенных API-ключей Gemini, Mistral и OpenAI-совместимых сервисов.
        Возвращает детальную статистику доступности по провайдерам.
        """
        has_gemini_keys = bool(self.gemini_keys and (genai or "pytest" in sys.modules or "unittest" in sys.modules))
        has_mistral_keys = bool(self.mistral_keys)
        has_openai_keys = bool(self.openai_keys)

        if not has_gemini_keys and not has_mistral_keys and not has_openai_keys:
            return {
                "status": "mock",
                "available": 0,
                "total": 0,
                "gemini": {"available": 0, "total": 0, "keys": []},
                "mistral": {"available": 0, "total": 0, "keys": []},
                "openai": {"available": 0, "total": 0, "keys": [], "preset": self.openai_provider_preset},
                "reason": "No API keys configured or providers unavailable"
            }

        now = time.time()

        # 1. Проверка Gemini
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

        # 2. Проверка Mistral
        if has_mistral_keys and (not LLMAnalyzer._initialized_keys or force_probe):
            logger.info(f"Проверка пула ключей Mistral API ({len(self.mistral_keys)} шт.)...")
            target_mistral_model = database.get_config_value("mistral_model") or Config.MISTRAL_MODEL or "open-mistral-nemo"
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

        # 3. Проверка OpenAI-совместимого провайдера (Groq, OpenRouter, GitHub Models и др.)
        if has_openai_keys and (not LLMAnalyzer._initialized_keys or force_probe):
            preset_name = OPENAI_PROVIDER_PRESETS.get(self.openai_provider_preset, {}).get("name", self.openai_provider_preset)
            url = f"{self.openai_base_url.rstrip('/')}/chat/completions"
            test_model = self.openai_model
            if "openrouter.ai" in url and (not test_model or test_model in DEAD_OPENROUTER_MODELS):
                test_model = "openrouter/free"
            for k in self.openai_keys:
                try:
                    headers = {
                        "Authorization": f"Bearer {k}",
                        "Content-Type": "application/json"
                    }
                    if "openrouter.ai" in url:
                        headers["HTTP-Referer"] = "https://github.com/andreimelneichuk/hh_job_applier"
                        headers["X-Title"] = "HH Job Applier"

                    resp = requests.post(
                        url,
                        headers=headers,
                        json={
                            "model": test_model,
                            "messages": [{"role": "user", "content": "Hi"}],
                            "max_tokens": 5
                        },
                        timeout=10
                    )
                    if resp.status_code == 200:
                        LLMAnalyzer._openai_key_statuses[k] = {"status": "ok", "last_checked": now}
                    elif resp.status_code == 429:
                        LLMAnalyzer._openai_key_statuses[k] = {"status": "error", "reason": "rate_limit_or_quota", "detail": resp.text, "last_checked": now}
                    elif resp.status_code in (401, 403):
                        LLMAnalyzer._openai_key_statuses[k] = {"status": "error", "reason": "invalid_api_key", "detail": resp.text, "last_checked": now}
                    else:
                        LLMAnalyzer._openai_key_statuses[k] = {"status": "error", "reason": f"http_{resp.status_code}", "detail": resp.text, "last_checked": now}
                except Exception as e:
                    LLMAnalyzer._openai_key_statuses[k] = {"status": "error", "reason": "network_error", "detail": str(e), "last_checked": now}

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

        # Подсчет доступных OpenAI-совместимых
        openai_total = len(self.openai_keys)
        openai_available = sum(
            1 for k in self.openai_keys
            if LLMAnalyzer._openai_key_statuses.get(k, {}).get("status") == "ok" or (k not in LLMAnalyzer._openai_key_statuses)
        )

        total_keys = gemini_total + mistral_total + openai_total
        total_available = gemini_available + mistral_available + openai_available

        # Авто-сброс 429 если прошло больше 30 сек
        if total_keys > 0 and total_available == 0 and not force_probe:
            all_statuses = list(LLMAnalyzer._gemini_key_statuses.values()) + list(LLMAnalyzer._mistral_key_statuses.values()) + list(LLMAnalyzer._openai_key_statuses.values())
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

        # Детализация по ключам OpenAI-совместимого провайдера
        openai_keys_info = []
        for k in self.openai_keys:
            st = LLMAnalyzer._openai_key_statuses.get(k, {"status": "ok"})
            openai_keys_info.append({
                "key": k,
                "provider": "openai",
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
            "openai": {
                "available": openai_available,
                "total": openai_total,
                "keys": openai_keys_info,
                "preset": self.openai_provider_preset,
                "base_url": self.openai_base_url,
                "model": self.openai_model,
                "current_index": LLMAnalyzer._current_openai_idx + 1 if openai_total > 0 else 0
            },
            "current_index": LLMAnalyzer._current_openai_idx + 1 if openai_total > 0 else (LLMAnalyzer._current_gemini_idx + 1 if gemini_total > 0 else (LLMAnalyzer._current_mistral_idx + 1 if mistral_total > 0 else 0))
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

    def _get_active_openai_key(self) -> Optional[str]:
        """Возвращает текущий рабочий ключ OpenAI-совместимого сервиса."""
        if not self.openai_keys:
            return None

        for i in range(len(self.openai_keys)):
            idx = (LLMAnalyzer._current_openai_idx + i) % len(self.openai_keys)
            key = self.openai_keys[idx]
            status = LLMAnalyzer._openai_key_statuses.get(key, {})
            if status.get("status") != "error":
                LLMAnalyzer._current_openai_idx = idx
                return key

        return self.openai_keys[LLMAnalyzer._current_openai_idx % len(self.openai_keys)]

    def sync_provider_models(self, provider: str = "all") -> List[Dict[str, Any]]:
        """
        Запрашивает актуальный список моделей из API провайдера и сохраняет в SQLite БД.
        Поддерживает 'all', 'gemini', 'mistral', 'groq', 'openrouter', 'github', 'cerebras', 'custom'.
        """
        target_providers = list(UNIFIED_PROVIDERS.keys()) if provider in ("all", "*", None) else [provider]
        all_synced = []

        for p in target_providers:
            info = UNIFIED_PROVIDERS.get(p)
            db_cfg = database.get_provider_config(p)
            if not info and not db_cfg:
                continue
            if not info and db_cfg:
                info = {
                    "id": db_cfg["id"],
                    "name": db_cfg["name"],
                    "protocol": db_cfg["protocol"],
                    "base_url": db_cfg["base_url"],
                    "default_model": db_cfg["active_model"],
                    "models": []
                }

            fetched_models = []
            try:
                if p == "gemini":
                    # Запрос моделей Gemini через SDK или REST
                    active_key, active_client = self._get_active_gemini_client()
                    if active_client and genai:
                        try:
                            models = active_client.models.list()
                            for m in models:
                                name = m.name.replace("models/", "").strip()
                                if any(k in name.lower() for k in ["flash", "pro", "gemma"]) and not any(k in name.lower() for k in ["tts", "audio", "image", "embedding", "veo", "robotics", "banana"]):
                                    desc = getattr(m, "description", "") or "Google Gemini multimodal model"
                                    ctx = getattr(m, "input_token_limit", 1000000) or 1000000
                                    is_def = (name == info.get("default_model"))
                                    label = f"{name} (Flash, быстрая)" if "flash" in name.lower() else f"{name} (Pro)"
                                    fetched_models.append({
                                        "id": name,
                                        "name": label,
                                        "description": desc,
                                        "context_window": ctx,
                                        "is_free": True,
                                        "is_default": is_def
                                    })
                        except Exception as e:
                            logger.warning(f"Ошибка получения моделей Gemini через SDK: {e}")

                elif p == "mistral":
                    # Запрос к Mistral API /v1/models
                    key = self._get_active_mistral_key()
                    if key:
                        try:
                            resp = requests.get(f"{info['base_url']}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
                            if resp.status_code == 200:
                                data = resp.json().get("data", [])
                                for m in data:
                                    m_id = m.get("id", "")
                                    if not any(bad in m_id.lower() for bad in ["embed", "moderation", "tts", "ocr"]):
                                        is_free = "nemo" in m_id.lower() or "8b" in m_id.lower() or "3b" in m_id.lower()
                                        is_def = (m_id == info.get("default_model"))
                                        label = m_id
                                        if is_free: label += " (Бесплатная)"
                                        fetched_models.append({
                                            "id": m_id,
                                            "name": label,
                                            "description": m.get("description", "Mistral AI foundation model"),
                                            "context_window": m.get("max_context_length", 128000),
                                            "is_free": is_free,
                                            "is_default": is_def
                                        })
                        except Exception as e:
                            logger.warning(f"Ошибка запроса моделей Mistral: {e}")

                elif p == "groq":
                    # Запрос к Groq API /v1/models
                    key = self._get_active_openai_key()
                    if key:
                        try:
                            resp = requests.get(f"{info['base_url']}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
                            if resp.status_code == 200:
                                data = resp.json().get("data", [])
                                for m in data:
                                    m_id = m.get("id", "")
                                    if not any(bad in m_id.lower() for bad in ["whisper", "guard", "distil-whisper", "audio", "embed"]):
                                        is_def = (m_id == info.get("default_model"))
                                        ctx = m.get("context_window", 128000) or 128000
                                        fetched_models.append({
                                            "id": m_id,
                                            "name": m_id + (" (Рекомендуемая)" if is_def else ""),
                                            "description": f"Groq LPU инференс. Контекст: {ctx} токенов.",
                                            "context_window": ctx,
                                            "is_free": True,
                                            "is_default": is_def
                                        })
                        except Exception as e:
                            logger.warning(f"Ошибка запроса моделей Groq: {e}")

                elif p == "openrouter":
                    # Запрос к OpenRouter API
                    key = (db_cfg.get("api_keys") or [None])[0] if (db_cfg and db_cfg.get("api_keys")) else self._get_active_openai_key()
                    headers = {"Authorization": f"Bearer {key}"} if key else {}
                    try:
                        resp = requests.get(f"{info['base_url']}/models", headers=headers, timeout=15)
                        if resp.status_code == 200:
                            data = resp.json().get("data", [])
                            has_free_router = False
                            for m in data:
                                m_id = m.get("id", "")
                                if m_id in DEAD_OPENROUTER_MODELS:
                                    continue
                                pricing = m.get("pricing", {}) or {}
                                prompt_price = str(pricing.get("prompt", "")).strip()
                                is_free = m_id == "openrouter/free" or m_id.endswith(":free") or m_id.endswith("/free") or prompt_price in ("0", "0.0", "0.000000", "0.00")
                                if is_free or any(pop in m_id.lower() for pop in ["nemotron", "gemma", "minimax", "qwen", "liquid", "cohere", "llama-3"]):
                                    is_def = (m_id == "openrouter/free")
                                    if is_def:
                                        has_free_router = True
                                    ctx = m.get("context_length", 128000) or 128000
                                    fetched_models.append({
                                        "id": m_id,
                                        "name": m.get("name") or m_id,
                                        "description": (m.get("description", "") or "OpenRouter model")[:180],
                                        "context_window": ctx,
                                        "is_free": is_free,
                                        "is_default": is_def
                                    })
                            if not has_free_router:
                                fetched_models.insert(0, {
                                    "id": "openrouter/free",
                                    "name": "Free Models Router (Авто-выбор)",
                                    "description": "Автоматическая маршрутизация к доступным бесплатным моделям",
                                    "context_window": 200000,
                                    "is_free": True,
                                    "is_default": True
                                })
                    except Exception as e:
                        logger.warning(f"Ошибка запроса моделей OpenRouter: {e}")

                elif p in ("github", "cerebras", "custom") or info.get("protocol") == "openai":
                    key = (db_cfg.get("api_keys") or [None])[0] if (db_cfg and db_cfg.get("api_keys")) else self._get_active_openai_key()
                    base = db_cfg.get("base_url") if db_cfg else (self.openai_base_url if p == "custom" else info.get("base_url", ""))
                    headers = {"Authorization": f"Bearer {key}"} if key else {}
                    try:
                        resp = requests.get(f"{base.rstrip('/')}/models", headers=headers, timeout=10)
                        if resp.status_code == 200:
                            data = resp.json()
                            model_list = data.get("data", []) if isinstance(data, dict) else []
                            if not model_list and isinstance(data, dict) and "models" in data:
                                model_list = data["models"]
                            for m in model_list:
                                m_id = m.get("id") or m.get("name") or str(m)
                                is_def = (m_id == info.get("default_model"))
                                fetched_models.append({
                                    "id": m_id,
                                    "name": m_id,
                                    "description": f"{info['name']} model",
                                    "context_window": 128000,
                                    "is_free": True,
                                    "is_default": is_def
                                })
                    except Exception as e:
                        logger.warning(f"Ошибка запроса моделей для {p}: {e}")

            except Exception as e:
                logger.error(f"Не удалось синхронизировать модели для {p}: {e}")

            # Если API не вернул данные, сохраняем стандартные зашитые модели
            if not fetched_models:
                fetched_models = info.get("models", [])

            database.save_provider_models(p, fetched_models)
            all_synced.extend(fetched_models)

        return all_synced

    def get_available_models(self, provider: str = "all", free_only: bool = False) -> Any:
        """Возвращает список доступных моделей для провайдеров из БД или встроенных пресетов."""
        # Проверяем кэш в БД
        db_models = database.get_provider_models(provider if provider not in ("all", "catalog") else None, is_free_only=free_only)

        # Если БД пуста, инициализируем базовыми пресетами
        if not db_models:
            for p, p_info in UNIFIED_PROVIDERS.items():
                database.save_provider_models(p, p_info.get("models", []))
            db_models = database.get_provider_models(provider if provider not in ("all", "catalog") else None, is_free_only=free_only)

        if provider == "catalog":
            return db_models

        if provider != "all" and provider in UNIFIED_PROVIDERS:
            matched = [m["id"] for m in db_models if m["provider"] == provider]
            if matched:
                return matched
            return [m["id"] for m in UNIFIED_PROVIDERS[provider].get("models", [])]

        # Группируем для обратной совместимости
        preset_info = UNIFIED_PROVIDERS.get(self.openai_provider_preset, UNIFIED_PROVIDERS["groq"])
        openai_preset_models = [m["id"] for m in db_models if m["provider"] == (self.openai_provider_preset or "groq")]
        if not openai_preset_models:
            openai_preset_models = [m["id"] for m in preset_info.get("models", [])]

        gemini_models = [m["id"] for m in db_models if m["provider"] == "gemini"] or ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-latest", "gemini-3.1-pro-preview"]
        mistral_models = [m["id"] for m in db_models if m["provider"] == "mistral"] or ["open-mistral-nemo", "ministral-8b-latest", "ministral-3b-latest", "mistral-small-latest", "codestral-latest", "mistral-large-latest"]

        return {
            "gemini": gemini_models,
            "mistral": mistral_models,
            "openai": openai_preset_models,
            "catalog": db_models
        }

    @staticmethod
    def _extract_candidate_meta(resume_text: str = "", resumes: List[Dict[str, Any]] = None, user_saved_answers: List[Dict[str, Any]] = None) -> Dict[str, str]:
        """Извлекает имя (first_name, full_name) и пол кандидата из резюме или ответов профиля."""
        from unittest.mock import MagicMock
        first_name = ""
        full_name = ""
        gender = ""

        # 1. Из структурированного списка резюме
        if resumes and isinstance(resumes, list):
            for r in resumes:
                if isinstance(r, dict):
                    f = r.get("first_name")
                    if f and not isinstance(f, MagicMock):
                        first_name = str(f).strip()
                    l = r.get("last_name")
                    if l and not isinstance(l, MagicMock):
                        full_name = f"{first_name} {str(l).strip()}".strip()
                    g = r.get("gender")
                    if g and not isinstance(g, MagicMock):
                        gender = str(g).strip()
                    if first_name or gender:
                        break

        # 2. Из текста резюме (по меткам ФИО: и Пол:)
        if resume_text and (not first_name or not gender):
            match_fio = re.search(r'ФИО:\s*([^\n]+)', resume_text)
            if match_fio and not first_name:
                fio = match_fio.group(1).strip()
                full_name = fio
                parts = fio.split()
                if parts:
                    first_name = parts[0]

            match_gender = re.search(r'Пол:\s*(Мужской|Женский|Мужчина|Женщина)', resume_text, re.IGNORECASE)
            if match_gender and not gender:
                g_raw = match_gender.group(1).lower()
                gender = "Женский" if "жен" in g_raw else "Мужской"

        # 3. Из ответов профиля (user_saved_answers или БД)
        if not first_name or not gender:
            answers = user_saved_answers
            if answers is None:
                try:
                    answers = database.get_user_profile_answers()
                except Exception:
                    answers = []
            if answers:
                for item in answers:
                    k = (item.get("key") or "").lower()
                    ans = str(item.get("answer") or "").strip()
                    if k in ("candidate_name", "first_name", "name") and ans and not first_name:
                        first_name = ans.split()[0]
                        full_name = ans
                    elif k in ("candidate_gender", "gender", "пол") and ans and not gender:
                        gender = "Женский" if "жен" in ans.lower() else "Мужской"

        return {
            "first_name": first_name,
            "full_name": full_name or first_name,
            "gender": gender
        }

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
                "2. Выберите ТОЛЬКО ТО резюме, которое РЕАЛЬНО соответствует технологическому стеку и специализации вакансии.\n"
                "3. КРИТИЧЕСКОЕ ПРАВИЛО: Если НИ ОДНО резюме кандидата НЕ соответствует ключевому стеку вакансии "
                "(например, вакансия требует C/C++, Linux Kernel, Embedded, Oracle PL/SQL, 1С, биоинформатику NGS, геймдев/видеодизайн, преподавание, "
                "а в резюме только Python Backend / Data Engineering / LLM / ML):\n"
                "   - НЕ ПЫТАЙТЕСЬ искусственно привязать неподходящее резюме!\n"
                "   - Обязательно укажите: stack_score: 0-5, match_score < 40, is_match: false, cover_letter: \"\"\n"
                "   - В reasoning напишите: 'Ни одно резюме не содержит требуемого стека'\n"
                "4. Если подходящее резюме найдено, укажите его точный ID в поле `selected_resume_id` и должность в `selected_resume_title`.\n"
                "5. Рассчитайте `match_score` (0-100) и `is_match` строго на основе выбранного резюме.\n"
                "6. Составьте `cover_letter`, опираясь ТОЛЬКО на факты, опыт и стек из выбранного резюме. Категорически запрещено выдумывать опыт, которого нет в резюме.\n"
                "7. В `reasoning` укажите, почему выбрано именно это резюме и как оно подходит."
            )
        elif resumes and len(resumes) == 1:
            if resumes[0].get("text"):
                final_resume_text = resumes[0].get("text")

        meta = self._extract_candidate_meta(final_resume_text, resumes)
        candidate_name = meta.get("first_name") or ""

        prompt = template
        if candidate_name:
            prompt = prompt.replace("{candidate_name}", candidate_name)
        else:
            # Если имя неизвестно, заменяем связку ", {candidate_name}" на пустую строку, чтобы осталось просто "С уважением"
            prompt = prompt.replace(", {candidate_name}", "").replace("{candidate_name}", "")

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

    def _get_execution_providers(self, primary_provider_id: str = None, fallback_enabled: bool = None) -> List[Dict[str, Any]]:
        """
        Возвращает упорядоченный список доступных провайдеров для выполнения запроса:
        первым идет primary_provider, затем остальные активные провайдеры при fallback_enabled=True.
        """
        if primary_provider_id is None:
            primary_provider_id = database.get_system_setting("primary_provider", "gemini").lower()
        if fallback_enabled is None:
            fb_str = database.get_system_setting("fallback_enabled", "true")
            fallback_enabled = fb_str.lower() in ("true", "1", "yes") if fb_str else True

        norm_primary = "gemini" if primary_provider_id == "google" else primary_provider_id
        all_configs = database.get_all_providers_config()
        cfg_by_id = {c["id"]: c for c in all_configs}

        valid_providers = []
        for c in all_configs:
            if not c.get("is_enabled", True):
                continue
            p_id = c["id"]
            protocol = c["protocol"]
            keys = c.get("api_keys", [])
            has_keys = bool(keys)
            if not has_keys:
                if protocol == "gemini" and self.gemini_keys:
                    c["api_keys"] = list(self.gemini_keys)
                    has_keys = True
                elif protocol == "mistral" and self.mistral_keys:
                    c["api_keys"] = list(self.mistral_keys)
                    has_keys = True
                elif protocol == "openai" and p_id in ("groq", "openrouter", "github", "cerebras") and self.openai_keys:
                    c["api_keys"] = list(self.openai_keys)
                    has_keys = True
            if has_keys:
                valid_providers.append(c)

        ordered = []
        primary_cfg = cfg_by_id.get(norm_primary)
        if primary_cfg and primary_cfg in valid_providers:
            ordered.append(primary_cfg)

        if fallback_enabled:
            for p in valid_providers:
                if p not in ordered:
                    ordered.append(p)
        elif not ordered and valid_providers:
            ordered = [valid_providers[0]]

        return ordered

    def _call_openai_compatible(
        self,
        prompt: str,
        target_model: str,
        vacancy: Dict[str, Any],
        temperature: float = 0.2,
        base_url: str = None,
        keys: List[str] = None,
        preset_name: str = None
    ) -> VacancyAnalysis:
        """Выполняет запрос к OpenAI-совместимому API (Groq, OpenRouter, GitHub Models и др.) с ротацией ключей."""
        effective_keys = keys if (keys is not None and len(keys) > 0) else self.openai_keys
        if not effective_keys:
            raise QuotaExceededError("Ключи OpenAI-совместимого API не настроены.")

        effective_base_url = (base_url or self.openai_base_url).rstrip('/')
        endpoint_url = f"{effective_base_url}/chat/completions"
        effective_name = preset_name or OPENAI_PROVIDER_PRESETS.get(self.openai_provider_preset, {}).get("name", self.openai_provider_preset)
        if "openrouter.ai" in endpoint_url and (not target_model or target_model in DEAD_OPENROUTER_MODELS):
            target_model = "openrouter/free"

        tried_keys = set()
        for active_key in effective_keys:
            if not active_key or active_key in tried_keys:
                continue
            tried_keys.add(active_key)
            key_tag = f"...{active_key[-6:]}" if len(active_key) > 6 else active_key

            try:
                logger.info(f"Отправка запроса к OpenAI-совместимому сервису [{effective_name}, модель {target_model}, ключ {key_tag}, temp={temperature}] для вакансии '{vacancy.get('title')}' ({vacancy.get('company')})...")

                headers = {
                    "Authorization": f"Bearer {active_key}",
                    "Content-Type": "application/json"
                }
                if "openrouter.ai" in endpoint_url:
                    headers["HTTP-Referer"] = "https://github.com/andreimelneichuk/hh_job_applier"
                    headers["X-Title"] = "HH Job Applier"

                payload = {
                    "model": target_model,
                    "messages": [
                        {"role": "system", "content": "You are a professional HR recruiter evaluating candidate resumes against job vacancy. Always respond strictly in valid JSON format."},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": temperature
                }

                resp = requests.post(
                    endpoint_url,
                    headers=headers,
                    json=payload,
                    timeout=45
                )

                if resp.status_code == 200:
                    data = resp.json()
                    msg_obj = data["choices"][0]["message"]
                    content = msg_obj.get("content") or msg_obj.get("reasoning") or ""
                    LLMAnalyzer._openai_key_statuses[active_key] = {"status": "ok", "last_checked": time.time()}

                    raw_content = content.strip()
                    if raw_content.startswith("```"):
                        lines = raw_content.splitlines()
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        raw_content = "\n".join(lines).strip()

                    result = VacancyAnalysis.model_validate_json(raw_content)
                    logger.info(f"Анализ {effective_name} завершен успешно [ключ {key_tag}]. Совпадение: {result.match_score}%, Подходит: {result.is_match}, Резюме: {result.selected_resume_title or result.selected_resume_id or 'По умолчанию'}")
                    return result

                elif resp.status_code == 429:
                    logger.warning(f"Исчерпан лимит квоты {effective_name} на ключе {key_tag} (429). Переключаемся...")
                    LLMAnalyzer._openai_key_statuses[active_key] = {
                        "status": "error",
                        "reason": "rate_limit_or_quota",
                        "detail": resp.text,
                        "last_checked": time.time()
                    }
                    continue

                elif resp.status_code in (401, 403):
                    logger.warning(f"Невалидный ключ {effective_name} {key_tag} ({resp.status_code}). Переключаемся...")
                    LLMAnalyzer._openai_key_statuses[active_key] = {
                        "status": "error",
                        "reason": "invalid_api_key",
                        "detail": resp.text,
                        "last_checked": time.time()
                    }
                    continue

                elif resp.status_code >= 500:
                    logger.warning(f"Сервер {effective_name} временно недоступен ({resp.status_code}) на ключе {key_tag}. Переключаемся...")
                    time.sleep(1)
                    continue

                else:
                    logger.error(f"Ошибка {effective_name} API ({resp.status_code}): {resp.text}")
                    raise Exception(f"{effective_name} API error {resp.status_code}: {resp.text}")

            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "rate limit" in err_msg.lower() or "quota" in err_msg.lower():
                    LLMAnalyzer._openai_key_statuses[active_key] = {
                        "status": "error",
                        "reason": "rate_limit_or_quota",
                        "detail": err_msg,
                        "last_checked": time.time()
                    }
                    continue
                logger.error(f"Ошибка запроса к {effective_name}: {e}")
                raise e

        raise QuotaExceededError(f"Все ключи {effective_name} API исчерпали квоту или вернули ошибку.")

    def analyze_vacancy(self, resume_text: Any = "", vacancy: Dict[str, Any] = None, threshold: int = None, model: str = None, resumes: List[Dict[str, Any]] = None) -> VacancyAnalysis:
        """
        Анализирует вакансию на соответствие резюме (или списку резюме кандидата).
        Учитывает индивидуальные настройки модели и температуры каждого настроенного провайдера.
        Если передан список resumes, LLM выбирает наиболее подходящее резюме.
        """
        if isinstance(resume_text, list):
            resumes = resume_text
            resume_text = ""

        actual_resumes = resumes if (resumes and isinstance(resumes, list)) else []
        match_threshold = threshold if threshold is not None else Config.MATCH_THRESHOLD
        prompt = self._build_prompt(resume_text, vacancy, match_threshold, resumes=actual_resumes)

        execution_providers = self._get_execution_providers()
        if not execution_providers:
            logger.warning("API-ключи LLM не настроены. Использование эвристического mock-анализа.")
            return self._mock_analysis(vacancy, match_threshold, resumes=actual_resumes)

        result = None
        last_error = None

        for prov_cfg in execution_providers:
            p_id = prov_cfg["id"]
            protocol = prov_cfg["protocol"]
            target_model = (model if p_id == "gemini" and model else None) or prov_cfg.get("active_model")
            p_temp = prov_cfg.get("temperature", 0.2)
            p_keys = prov_cfg.get("api_keys", [])
            p_base_url = prov_cfg.get("base_url", "")
            p_name = prov_cfg.get("name", p_id)

            try:
                if protocol == "gemini":
                    result = self._call_gemini(prompt, target_model or "gemini-2.5-flash", vacancy, temperature=p_temp)
                    if result:
                        result.analyzed_by_provider = p_name
                        result.analyzed_by_model = target_model or "gemini-2.5-flash"
                    break
                elif protocol == "mistral":
                    result = self._call_mistral(prompt, target_model or "open-mistral-nemo", vacancy, temperature=p_temp)
                    if result:
                        result.analyzed_by_provider = p_name
                        result.analyzed_by_model = target_model or "open-mistral-nemo"
                    break
                elif protocol == "openai":
                    chosen_m = target_model or "llama-3.3-70b-versatile"
                    result = self._call_openai_compatible(
                        prompt, target_model=chosen_m, vacancy=vacancy,
                        temperature=p_temp, base_url=p_base_url, keys=p_keys or self.openai_keys, preset_name=p_name
                    )
                    if result:
                        result.analyzed_by_provider = p_name
                        result.analyzed_by_model = chosen_m
                    break
            except QuotaExceededError as qe:
                last_error = qe
                logger.warning(f"Провайдер {p_name} исчерпал лимиты/ошибся ({qe}). Переходим к следующему провайдеру...")
                continue
            except Exception as e:
                last_error = e
                logger.warning(f"Ошибка провайдера {p_name} ({e}). Переходим к следующему провайдеру...")
                continue

        if not result:
            if last_error:
                raise QuotaExceededError(f"Все настроенные LLM провайдеры исчерпали квоты или недоступны: {last_error}")
            raise QuotaExceededError("Не удалось выполнить анализ: нет доступных LLM провайдеров.")

        # Сбрасываем блокировку по локации/офису (политика пользователя: по локации только штраф по баллам, но не блокировать)
        if result.has_hard_blocker and result.blocker_reason:
            b_low = result.blocker_reason.lower()
            if any(w in b_low for w in ("локаци", "город", "офис", "удаленк", "релокац", "переезд", "москв", "петербург", "спб")):
                logger.info(f"Сброшен hard blocker по локации ('{result.blocker_reason}'): разрешен отклик для дальнейшего обсуждения.")
                result.has_hard_blocker = False
                result.blocker_reason = None

        # Строгий пересчет итогового балла и флага is_match
        result.calculate_total_score()
        if result.has_hard_blocker:
            result.is_match = False
        else:
            result.is_match = (result.match_score >= match_threshold)

        # Программный предохранитель: если стек не подходит (< 15 из 30), отклик запрещен
        if result.scores and result.scores.stack_score < 15:
            logger.info(f"Программный фильтр: stack_score={result.scores.stack_score} < 15 — стек не подходит, отклик отклонен.")
            result.is_match = False

        if not result.is_match:
            result.cover_letter = ""

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

        stack_score = 0
        if has_python: stack_score += 20
        if has_ai: stack_score += 10

        exp_score = 15 if (has_python or has_ai) else 5
        grade_score = 15
        domain_score = 12 if has_ai else 8
        format_score = 10

        scores = EvaluationScores(
            stack_score=stack_score,
            experience_score=exp_score,
            grade_score=grade_score,
            domain_score=domain_score,
            format_score=format_score
        )
        total_score = scores.total
        is_match = total_score >= match_threshold

        selected_id = None
        selected_title = None
        if resumes and len(resumes) > 0:
            target_text = f"{vacancy.get('title', '')} {' '.join(vacancy.get('skills', []))}".lower()
            best_res = resumes[0]
            best_score = -1
            for r in resumes:
                r_title = (r.get("title") or "").lower()
                score = sum(1 for word in re.findall(r'\w+', r_title) if len(word) > 2 and word in target_text)
                if score > best_score:
                    best_score = score
                    best_res = r
            selected_id = best_res.get("id")
            selected_title = best_res.get("title")

        reasoning = (
            f"[MOCK] Детальный разбор: Стек Python={has_python} (+{stack_score}/30), "
            f"AI/LLM={has_ai} (домен +{domain_score}/15). Релевантный опыт: {exp_score}/25, "
            f"Грейд: {grade_score}/20, Формат: {format_score}/10. Итоговый балл: {total_score}%."
        )

        cover_letter = ""
        if is_match:
            candidate_meta = self._extract_candidate_meta(resume_text=None, resumes=resumes)
            cand_name = candidate_meta.get("first_name") or "Кандидат"
            cand_gender = candidate_meta.get("gender")
            glad_word = "рада" if cand_gender == "Женский" else "рад"
            cover_letter = (
                f"Здравствуйте!\n\n"
                f"Меня заинтересовала вакансия {vacancy.get('title')} в компании {vacancy.get('company')}.\n"
                f"У меня есть релевантный опыт, который, как мне кажется, будет полезен вашей команде.\n\n"
                f"Буду {glad_word} обсудить подробности на интервью.\n\n"
                f"С уважением,\n{cand_name}"
            )
            postfix = database.get_system_setting("cover_letter_postfix") or ""
            if postfix and postfix.strip():
                clean_letter = cover_letter.strip()
                clean_postfix = postfix.strip()
                if not clean_letter.endswith(clean_postfix):
                    cover_letter = f"{clean_letter}\n\n{clean_postfix}"

        return VacancyAnalysis(
            match_score=total_score,
            is_match=is_match,
            reasoning=reasoning,
            cover_letter=cover_letter,
            selected_resume_id=selected_id,
            selected_resume_title=selected_title,
            analyzed_by_provider="heuristic",
            analyzed_by_model="heuristic"
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

        meta = self._extract_candidate_meta(resume_text, None, user_saved_answers)
        gender = meta.get("gender", "").lower()
        if "жен" in gender:
            gender_rule = (
                "1. ПОЛ КАНДИДАТА — ЖЕНСКИЙ: Все глаголы, местоимения и формулировки ответов формулируйте СТРОГО в женском роде "
                "(например: «разрабатывала», «настраивала», «применяла», «готова», «изучала»). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО "
                "использовать формы со скобками вроде «(а)» или слэшами («разрабатывал(а)», «готов/а»)."
            )
            exp_gender_note = " строго в женском роде"
        elif "муж" in gender:
            gender_rule = (
                "1. ПОЛ КАНДИДАТА — МУЖСКОЙ: Все глаголы, местоимения и формулировки ответов формулируйте СТРОГО в мужском роде "
                "(например: «разрабатывал», «настраивал», «применял», «готов», «изучал»). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО "
                "использовать формы со скобками вроде «(а)» или слэшами («разрабатывал(а)», «готов/а»)."
            )
            exp_gender_note = " строго в мужском роде"
        else:
            gender_rule = (
                "1. СТИЛЬ ОТВЕТОВ: Формулируйте ответы от первого лица в естественном стиле, "
                "категорически избегая скобок вроде «(а)» и слэшей («разрабатывал(а)», «готов/а»)."
            )
            exp_gender_note = ""

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
{gender_rule}
2. ПРЕЗУМПЦИЯ ОТСУТСТВИЯ ОПЫТА: Если конкретная технология, инструмент, архитектурный паттерн или библиотека (например, Row-Level Security / RLS, Data Lineage, Data Contracts, SDF, Parquet-пайплайны, конкретная БД) ПРЯМО НЕ УКАЗАНЫ в резюме кандидата или в FAQ — КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО утверждать о наличии коммерческого опыта или применения в проде!
   - В вопросах с вариантами выбора (single_choice / multi_choice): выбирайте вариант об отсутствии опыта («Не использовал», «Слышал в теории, но на практике не применял», «Нет опыта», «Не работал»).
   - В открытых вопросах: честно укажите, что в коммерческих проектах с этой конкретной технологией пока не сталкивались, но готовы быстро освоить.
3. Используйте ТОЛЬКО достоверную информацию из резюме и базовых предпочтений кандидата. Не выдумывайте факты, которых нет.
4. Для вопросов о локации/городе/гражданстве: используйте город и страну из резюме или профиля кандидата.
5. Для вопросов об IT-аккредитации, тестовом задании, формате работы: опирайтесь на базовые предпочтения кандидата. Если в профиле указано «не критична» или «готов выполнить тестовое», формулируйте вежливый и четкий ответ.
6. Для открытых технических/опытных вопросов: кратко (1-3 емких предложения) опишите реальный опыт кандидата с релевантными технологиями{exp_gender_note}.
7. Для вопросов с вариантами выбора (single_choice / multi_choice): выберите наиболее подходящий точный вариант из предложенных.
8. Флаг `requires_user_input`:
   - Установите `requires_user_input = False` (confidence 85-100%), если ответ однозначно следует из резюме, базы ответов кандидата (FAQ) или стандартных правил вежливости.
   - Установите `requires_user_input = True` (confidence < 85%), ТОЛЬКО если вопрос требует индивидуального личного решения кандидата, которого нет ни в резюме, ни в профиле (например: «Готовы ли вы выйти в офис в другом городе с понедельника?», «Какой размер опциона вас интересует?»).
9. Поле `all_confident`:
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

    def _call_openai_questions(
        self,
        prompt: str,
        target_model: str,
        temperature: float = 0.2,
        base_url: str = None,
        keys: List[str] = None,
        preset_name: str = None
    ) -> QuestionsAnalysisResult:
        """Запрос к OpenAI-совместимому API для ответов на вопросы с ротацией ключей."""
        effective_keys = keys if (keys is not None and len(keys) > 0) else self.openai_keys
        if not effective_keys:
            raise QuotaExceededError("Ключи OpenAI-совместимого API не настроены.")
        tried_keys = set()
        effective_name = preset_name or OPENAI_PROVIDER_PRESETS.get(self.openai_provider_preset, {}).get("name", self.openai_provider_preset)
        endpoint_url = f"{(base_url or self.openai_base_url).rstrip('/')}/chat/completions"
        if "openrouter.ai" in endpoint_url and (not target_model or target_model in DEAD_OPENROUTER_MODELS):
            target_model = "openrouter/free"

        for active_key in effective_keys:
            if not active_key or active_key in tried_keys:
                continue
            tried_keys.add(active_key)
            key_tag = f"...{active_key[-6:]}" if len(active_key) > 6 else active_key
            try:
                logger.info(f"Отправка запроса к {effective_name} [{target_model}, ключ {key_tag}] для генерации ответов на вопросы...")
                headers = {"Authorization": f"Bearer {active_key}", "Content-Type": "application/json"}
                if "openrouter.ai" in endpoint_url:
                    headers["HTTP-Referer"] = "https://github.com/andreimelneichuk/hh_job_applier"
                    headers["X-Title"] = "HH Job Applier"

                payload = {
                    "model": target_model,
                    "messages": [
                        {"role": "system", "content": "You are a professional career assistant answering job applicant screening questions in Russian. Always return valid JSON matching schema: {\"answers\": [{\"id\": str, \"question_text\": str, \"answer\": str, \"confidence\": int, \"requires_user_input\": bool, \"reasoning\": str}], \"all_confident\": bool}."},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": temperature
                }
                resp = requests.post(endpoint_url, headers=headers, json=payload, timeout=45)
                if resp.status_code == 200:
                    data = resp.json()
                    msg_obj = data["choices"][0]["message"]
                    content = msg_obj.get("content") or msg_obj.get("reasoning") or ""
                    LLMAnalyzer._openai_key_statuses[active_key] = {"status": "ok", "last_checked": time.time()}

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
                    logger.info(f"Ответы на вопросы от {effective_name} получены: {len(result.answers)} шт., all_confident={result.all_confident}")
                    return result
                elif resp.status_code == 429:
                    LLMAnalyzer._openai_key_statuses[active_key] = {"status": "error", "reason": "rate_limit_or_quota", "detail": resp.text, "last_checked": time.time()}
                    continue
                elif resp.status_code in (401, 403):
                    LLMAnalyzer._openai_key_statuses[active_key] = {"status": "error", "reason": "invalid_api_key", "detail": resp.text, "last_checked": time.time()}
                    continue
                else:
                    raise Exception(f"{effective_name} API error {resp.status_code}: {resp.text}")
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "rate limit" in err_msg.lower():
                    LLMAnalyzer._openai_key_statuses[active_key] = {"status": "error", "reason": "rate_limit_or_quota", "detail": err_msg, "last_checked": time.time()}
                    continue
                logger.warning(f"Ошибка {effective_name} при генерации ответов на вопросы: {e}")
                raise e
        raise QuotaExceededError(f"Все ключи {effective_name} API исчерпали квоту.")

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
                ans_text = user_faq.get("location_city") or ""
            elif any(w in q_lower for w in ["сумм", "зарплат", "доход", "оплат", "денег", "руб"]):
                ans_text = user_faq.get("salary_min") or ""
            elif any(w in q_lower for w in ["аккредит", "ит-аккредит", "it"]):
                ans_text = user_faq.get("it_accreditation") or "Нет, IT-аккредитация не критична"
            elif any(w in q_lower for w in ["тестов", "задани"]):
                ans_text = user_faq.get("test_task") or "Да, готов выполнить небольшое тестовое задание"
            elif any(w in q_lower for w in ["формат", "удален", "офис", "гибрид"]):
                ans_text = user_faq.get("work_format") or ""
            elif any(w in q_lower for w in ["оформлен", "ип", "самозанят", "тк", "гпх"]):
                ans_text = user_faq.get("employment_type") or "ТК РФ, ИП, самозанятость"
            else:
                ans_text = ""

            if not ans_text:
                ans_text = "Готов обсудить подробности на интервью."
                confidence = 50
                req_user = True
                reason = "Заполните ответ в разделе «Профиль» настроек"

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
        Учитывает индивидуальные настройки модели и температуры каждого настроенного провайдера.
        """
        if not questions:
            return QuestionsAnalysisResult(answers=[], all_confident=True)

        if not user_saved_answers:
            try:
                user_saved_answers = database.get_user_profile_answers()
            except Exception:
                user_saved_answers = []

        prompt = self._build_questions_prompt(resume_text, vacancy, questions, user_saved_answers)

        execution_providers = self._get_execution_providers()
        if not execution_providers:
            logger.warning("API ключи не настроены. Использование эвристического автоответа на вопросы.")
            return self._mock_questions_analysis(questions, user_saved_answers)

        for prov_cfg in execution_providers:
            p_id = prov_cfg["id"]
            protocol = prov_cfg["protocol"]
            target_model = prov_cfg.get("active_model")
            p_temp = prov_cfg.get("temperature", 0.2)
            p_keys = prov_cfg.get("api_keys", [])
            p_base_url = prov_cfg.get("base_url", "")
            p_name = prov_cfg.get("name", p_id)

            try:
                if protocol == "gemini":
                    return self._call_gemini_questions(prompt, target_model or "gemini-2.5-flash", temperature=p_temp)
                elif protocol == "mistral":
                    return self._call_mistral_questions(prompt, target_model or "open-mistral-nemo", temperature=p_temp)
                elif protocol == "openai":
                    return self._call_openai_questions(
                        prompt, target_model=target_model or "llama-3.3-70b-versatile",
                        temperature=p_temp, base_url=p_base_url, keys=p_keys or self.openai_keys, preset_name=p_name
                    )
            except Exception as e:
                logger.warning(f"Провайдер {p_name} вернул ошибку при ответах на вопросы ({e}). Переход к следующему...")
                continue

        logger.warning("Все провайдеры вернули ошибку при ответах на вопросы. Переход на эвристические ответы.")
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

        meta = self._extract_candidate_meta(final_resume_text, resumes)
        candidate_name = meta.get("first_name") or ""
        sign_str = f"«С уважением, {candidate_name}»" if candidate_name else "«С уважением»"

        return f"""Напишите живое персонализированное сопроводительное письмо от имени кандидата на hh.ru (3–5 предложений, до 500 символов).

РЕЗЮМЕ:
{final_resume_text}
{multi_info}

ВАКАНСИЯ:
{vacancy.get('title', 'Вакансия')} | {vacancy.get('company', '')}
З/П: {vacancy.get('salary', 'Не указана')} | Опыт: {vacancy.get('experience', 'Не указан')} | Формат: {vacancy.get('employment', '')}, {vacancy.get('schedule', '')}, {vacancy.get('location', '')}
Навыки: {skills_str}
Описание:
{vacancy.get('description', '')}

ПРАВИЛА:
- СТРОГИЙ ЗАПРЕТ: НЕ писать название позиции («Откликаюсь на позицию...»), НЕ использовать штампы («мой опыт решает задачи», «прошу рассмотреть», «с интересом ознакомился»), НЕ писать название компании в лоб.
- СТРУКТУРА:
  1. Вход сразу в суть и инженерный вызов проекта (например: «Здравствуйте! Обратил внимание на задачу по оптимизации пайплайнов и снижению latency: как раз недавно переводил сервис на асинхронный стек...»).
  2. Фактура и инструмент из РЕЗЮМЕ (1–2 предложения с подтвержденным кейсом под задачу компании).
  3. Партнерское приглашение к диалогу («Буду рад созвониться и обсудить технические решения. {sign_str}»).
- ПОДПИСЬ: СТРОГО {sign_str} (НЕ писать «Кандидат»).
- ТОН: уверенный диалог сильного инженера с нанимающим тимлидом, только реальные факты из резюме.

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

    def _call_openai_cover_letter(
        self,
        prompt: str,
        target_model: str,
        temperature: float = 0.2,
        base_url: str = None,
        keys: List[str] = None,
        preset_name: str = None
    ) -> str:
        """Запрос к OpenAI-совместимому сервису для генерации сопроводительного письма."""
        effective_keys = keys if (keys is not None and len(keys) > 0) else self.openai_keys
        if not effective_keys:
            raise QuotaExceededError("Ключи OpenAI-совместимого API не настроены.")
        tried_keys = set()
        effective_name = preset_name or OPENAI_PROVIDER_PRESETS.get(self.openai_provider_preset, {}).get("name", self.openai_provider_preset)
        endpoint_url = f"{(base_url or self.openai_base_url).rstrip('/')}/chat/completions"
        if "openrouter.ai" in endpoint_url and (not target_model or target_model in DEAD_OPENROUTER_MODELS):
            target_model = "openrouter/free"

        for active_key in effective_keys:
            if not active_key or active_key in tried_keys:
                continue
            tried_keys.add(active_key)
            key_tag = f"...{active_key[-6:]}" if len(active_key) > 6 else active_key
            try:
                logger.info(f"Отправка запроса к {effective_name} [{target_model}, ключ {key_tag}] для генерации сопроводительного письма...")
                headers = {"Authorization": f"Bearer {active_key}", "Content-Type": "application/json"}
                if "openrouter.ai" in endpoint_url:
                    headers["HTTP-Referer"] = "https://github.com/andreimelneichuk/hh_job_applier"
                    headers["X-Title"] = "HH Job Applier"

                payload = {
                    "model": target_model,
                    "messages": [
                        {"role": "system", "content": "You are a professional career consultant writing a personalized cover letter in Russian. Return valid JSON matching schema: {\"cover_letter\": str}."},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": temperature
                }
                resp = requests.post(endpoint_url, headers=headers, json=payload, timeout=45)
                if resp.status_code == 200:
                    data = resp.json()
                    msg_obj = data["choices"][0]["message"]
                    content = msg_obj.get("content") or msg_obj.get("reasoning") or ""
                    LLMAnalyzer._openai_key_statuses[active_key] = {"status": "ok", "last_checked": time.time()}

                    raw_content = content.strip()
                    if raw_content.startswith("```"):
                        lines = raw_content.splitlines()
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        raw_content = "\n".join(lines).strip()

                    try:
                        parsed = json.loads(raw_content)
                        if isinstance(parsed, dict) and "cover_letter" in parsed:
                            return parsed["cover_letter"].strip()
                        elif isinstance(parsed, dict):
                            return next(iter(parsed.values()), "").strip()
                        return str(parsed).strip()
                    except Exception:
                        return raw_content
                elif resp.status_code == 429:
                    logger.warning(f"Исчерпан лимит квоты {effective_name} на ключе {key_tag} (429). Переключаемся...")
                    LLMAnalyzer._openai_key_statuses[active_key] = {"status": "error", "reason": "rate_limit_or_quota", "detail": resp.text, "last_checked": time.time()}
                    continue
                elif resp.status_code in (401, 403):
                    logger.warning(f"Невалидный ключ {effective_name} {key_tag} ({resp.status_code}). Переключаемся...")
                    LLMAnalyzer._openai_key_statuses[active_key] = {"status": "error", "reason": "invalid_api_key", "detail": resp.text, "last_checked": time.time()}
                    continue
                else:
                    raise Exception(f"{effective_name} API error HTTP {resp.status_code}: {resp.text}")
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg:
                    continue
                logger.error(f"Ошибка {effective_name} при генерации письма: {e}")
                raise e
        raise QuotaExceededError(f"Все ключи {effective_name} исчерпали квоту.")

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
        Учитывает индивидуальные настройки модели и температуры каждого настроенного провайдера.
        Применяет настроенный постфикс (cover_letter_postfix).
        """
        if not vacancy:
            vacancy = {}

        if resumes and not resume_text:
            resume_text = resumes[0].get("text", "")

        prompt = self._build_cover_letter_prompt(resume_text, vacancy, resumes)

        letter = ""
        execution_providers = self._get_execution_providers()
        if not execution_providers:
            logger.warning("API ключи не настроены. Использование эвристического сопроводительного письма.")
            letter = self._mock_cover_letter(resume_text, vacancy)
        else:
            for prov_cfg in execution_providers:
                p_id = prov_cfg["id"]
                protocol = prov_cfg["protocol"]
                target_model = prov_cfg.get("active_model")
                p_temp = prov_cfg.get("temperature", 0.2)
                p_keys = prov_cfg.get("api_keys", [])
                p_base_url = prov_cfg.get("base_url", "")
                p_name = prov_cfg.get("name", p_id)

                try:
                    if protocol == "gemini":
                        letter = self._call_gemini_cover_letter(prompt, target_model or "gemini-2.5-flash", temperature=p_temp)
                    elif protocol == "mistral":
                        letter = self._call_mistral_cover_letter(prompt, target_model or "open-mistral-nemo", temperature=p_temp)
                    elif protocol == "openai":
                        letter = self._call_openai_cover_letter(
                            prompt, target_model=target_model or "llama-3.3-70b-versatile",
                            temperature=p_temp, base_url=p_base_url, keys=p_keys or self.openai_keys, preset_name=p_name
                        )
                    if letter and letter.strip():
                        break
                except Exception as e:
                    logger.warning(f"Провайдер {p_name} вернул ошибку при генерации письма ({e}). Переход к следующему...")
                    continue

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



