import os
import re
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

class Config:
    HH_ACCESS_TOKEN = os.getenv("HH_ACCESS_TOKEN", "").strip()
    HH_RESUME_ID = os.getenv("HH_RESUME_ID", "").strip()
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    
    # Поддержка нескольких ключей через запятую или пробелы/переносы
    keys_env = os.getenv("GEMINI_API_KEYS", "") or GEMINI_API_KEY
    GEMINI_API_KEYS = [k.strip() for k in re.split(r'[,\n;]+', keys_env) if k.strip() and "your_gemini_api_key" not in k.lower()]
    
    # Mistral AI (резервный / дополнительный провайдер)
    MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
    mistral_keys_env = os.getenv("MISTRAL_API_KEYS", "") or MISTRAL_API_KEY
    MISTRAL_API_KEYS = [k.strip() for k in re.split(r'[,\n;]+', mistral_keys_env) if k.strip() and "your_mistral_api_key" not in k.lower()]
    MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest").strip()
    
    HH_CLIENT_ID = os.getenv("HH_CLIENT_ID", "").strip()
    HH_CLIENT_SECRET = os.getenv("HH_CLIENT_SECRET", "").strip()
    
    # Парсим поисковые запросы из строки (по умолчанию пусто, для рекомендаций)
    queries_str = os.getenv("SEARCH_QUERIES", "")
    SEARCH_QUERIES = [q.strip() for q in queries_str.split(",") if q.strip()]
    
    SEARCH_AREA = os.getenv("SEARCH_AREA", "113").strip()
    
    try:
        MATCH_THRESHOLD = int(os.getenv("MATCH_THRESHOLD", "75"))
    except ValueError:
        MATCH_THRESHOLD = 75
        
    DRY_RUN = os.getenv("DRY_RUN", "True").lower() in ("true", "1", "yes")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
    
    # Обязательный заголовок User-Agent для hh.ru
    # Согласно требованиям hh.ru API: НазваниеПриложения/Версия (контактный_email)
    USER_AGENT = "HH-AI-Applier/1.0 (andreimelneichuk@yandex.ru)"
    
    @classmethod
    def validate(cls):
        """Проверяет критические переменные конфигурации."""
        warnings = []
        has_gemini = bool(cls.GEMINI_API_KEYS or (cls.GEMINI_API_KEY and "your_gemini_api_key" not in cls.GEMINI_API_KEY.lower()))
        has_mistral = bool(cls.MISTRAL_API_KEYS or (cls.MISTRAL_API_KEY and "your_mistral_api_key" not in cls.MISTRAL_API_KEY.lower()))
        if not has_gemini and not has_mistral:
            warnings.append("Внимание: Ни GEMINI_API_KEY, ни MISTRAL_API_KEY не установлены! Анализ LLM не будет работать.")
        return warnings
