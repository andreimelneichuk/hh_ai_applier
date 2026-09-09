import os
import time
import requests
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException
from src.core.config import Config
from src.db import database
from src.clients.browser import HHBrowserClient
from src.clients.llm import LLMAnalyzer, OPENAI_PROVIDER_PRESETS, UNIFIED_PROVIDERS, DEAD_OPENROUTER_MODELS
from src.api.state import (
    SearchSettings, SystemSettingsPayload, UserProfileAnswerPayload,
    ModelSyncPayload, ModelSelectPayload, ProviderConfigPayload, CustomProviderCreatePayload,
    ProviderProbePayload
)

try:
    from google import genai
except ImportError:
    genai = None

router = APIRouter(tags=["Settings"])

@router.get("/api/settings")
def get_settings():
    """Возвращает текущие настройки поиска из базы данных."""
    queries_str = database.get_config_value("search_queries")
    if queries_str is not None:
        queries = [q.strip() for q in queries_str.split(",") if q.strip()]
    else:
        queries = [q.strip() for q in Config.SEARCH_QUERIES if q.strip()] if Config.SEARCH_QUERIES else []
        
    area_id = database.get_config_value("search_area") or Config.SEARCH_AREA
    
    threshold_str = database.get_config_value("match_threshold")
    threshold = int(threshold_str) if threshold_str else Config.MATCH_THRESHOLD
    
    resume_id = database.get_config_value("resume_id") or Config.HH_RESUME_ID
    if resume_id.startswith("your_"):
        resume_id = ""
        
    dry_run_str = database.get_config_value("dry_run")
    if dry_run_str:
        dry_run = dry_run_str.lower() in ("true", "1", "yes")
    else:
        dry_run = Config.DRY_RUN
        
    raw_gemini = database.get_config_value("gemini_api_keys")
    if raw_gemini is None:
        gem_cfg = database.get_provider_config("gemini")
        if gem_cfg and gem_cfg.get("api_keys"):
            raw_gemini = ",".join(gem_cfg["api_keys"])
        else:
            raw_gemini = os.getenv("GEMINI_API_KEYS", "") or Config.GEMINI_API_KEY
    gemini_api_keys = ",".join(database._parse_keys_field(raw_gemini))

    gem_cfg = database.get_provider_config("gemini")
    gemini_model = (gem_cfg.get("active_model") if gem_cfg else None) or database.get_config_value("gemini_model") or Config.GEMINI_MODEL or "gemini-3.6-flash"

    raw_mistral = database.get_config_value("mistral_api_keys")
    if raw_mistral is None:
        mis_cfg = database.get_provider_config("mistral")
        if mis_cfg and mis_cfg.get("api_keys"):
            raw_mistral = ",".join(mis_cfg["api_keys"])
        else:
            raw_mistral = os.getenv("MISTRAL_API_KEYS", "") or Config.MISTRAL_API_KEY
    mistral_api_keys = ",".join(database._parse_keys_field(raw_mistral))

    mis_cfg = database.get_provider_config("mistral")
    mistral_model = (mis_cfg.get("active_model") if mis_cfg else None) or database.get_config_value("mistral_model") or Config.MISTRAL_MODEL or "open-mistral-nemo"

    openai_provider_preset = database.get_config_value("openai_provider_preset") or Config.OPENAI_PROVIDER_PRESET or "groq"
    preset_info = OPENAI_PROVIDER_PRESETS.get(openai_provider_preset, {})
    oa_cfg = database.get_provider_config(openai_provider_preset)

    raw_openai = database.get_config_value("openai_api_keys")
    if raw_openai is None:
        if oa_cfg and oa_cfg.get("api_keys"):
            raw_openai = ",".join(oa_cfg["api_keys"])
        else:
            raw_openai = os.getenv("OPENAI_API_KEYS", "") or Config.OPENAI_API_KEY
    openai_api_keys = ",".join(database._parse_keys_field(raw_openai))

    openai_base_url = (oa_cfg.get("base_url") if oa_cfg else None) or database.get_config_value("openai_base_url") or Config.OPENAI_BASE_URL or preset_info.get("base_url", "https://api.groq.com/openai/v1")
    openai_model = (oa_cfg.get("active_model") if oa_cfg else None) or database.get_config_value("openai_model") or Config.OPENAI_MODEL or preset_info.get("default_model", "llama-3.3-70b-versatile")

    stop_condition = database.get_config_value("stop_condition") or "both"
    limit_apps_str = database.get_config_value("limit_applications")
    try:
        limit_applications = int(limit_apps_str) if limit_apps_str is not None else 10
    except ValueError:
        limit_applications = 10

    limit_proc_str = database.get_config_value("limit_processed")
    try:
        limit_processed = int(limit_proc_str) if limit_proc_str is not None else 20
    except ValueError:
        limit_processed = 20

    return {
        "queries": queries,
        "area_id": area_id,
        "threshold": threshold,
        "resume_id": resume_id,
        "dry_run": dry_run,
        "gemini_api_keys": gemini_api_keys,
        "gemini_model": gemini_model,
        "mistral_api_keys": mistral_api_keys,
        "mistral_model": mistral_model,
        "openai_api_keys": openai_api_keys,
        "openai_provider_preset": openai_provider_preset,
        "openai_base_url": openai_base_url,
        "openai_model": openai_model,
        "stop_condition": stop_condition,
        "limit_applications": limit_applications,
        "limit_processed": limit_processed
    }

@router.post("/api/settings")
def save_settings(settings: SearchSettings):
    """Сохраняет настройки в базу данных."""
    database.set_config_value("search_queries", ",".join(settings.queries))
    database.set_config_value("search_area", settings.area_id)
    database.set_config_value("match_threshold", str(settings.threshold))
    database.set_config_value("resume_id", settings.resume_id)
    database.set_config_value("dry_run", str(settings.dry_run))
    if settings.gemini_model:
        database.set_config_value("gemini_model", settings.gemini_model)
        try:
            database.save_provider_config("gemini", {"active_model": settings.gemini_model})
        except Exception:
            pass
    if settings.gemini_api_keys is not None:
        database.set_config_value("gemini_api_keys", settings.gemini_api_keys)
        try:
            database.save_provider_config("gemini", {"api_keys": database._parse_keys_field(settings.gemini_api_keys)})
        except Exception:
            pass
    if settings.mistral_model:
        database.set_config_value("mistral_model", settings.mistral_model)
        try:
            database.save_provider_config("mistral", {"active_model": settings.mistral_model})
        except Exception:
            pass
    if settings.mistral_api_keys is not None:
        database.set_config_value("mistral_api_keys", settings.mistral_api_keys)
        try:
            database.save_provider_config("mistral", {"api_keys": database._parse_keys_field(settings.mistral_api_keys)})
        except Exception:
            pass
    if settings.openai_provider_preset:
        database.set_config_value("openai_provider_preset", settings.openai_provider_preset)
    if settings.openai_api_keys is not None:
        database.set_config_value("openai_api_keys", settings.openai_api_keys)
        try:
            preset = settings.openai_provider_preset or database.get_config_value("openai_provider_preset") or "groq"
            database.save_provider_config(preset, {"api_keys": database._parse_keys_field(settings.openai_api_keys)})
        except Exception:
            pass
    if settings.openai_base_url:
        database.set_config_value("openai_base_url", settings.openai_base_url)
    if settings.openai_model:
        database.set_config_value("openai_model", settings.openai_model)
    if settings.stop_condition is not None:
        database.set_config_value("stop_condition", settings.stop_condition)
    if settings.limit_applications is not None:
        database.set_config_value("limit_applications", str(settings.limit_applications))
    if settings.limit_processed is not None:
        database.set_config_value("limit_processed", str(settings.limit_processed))
        
    # Сбрасываем кэш проверки LLM чтобы перепроверить новые ключи
    LLMAnalyzer._initialized_keys = False
    return {"status": "ok"}

@router.get("/api/system-settings")
def get_system_settings():
    """Возвращает системные настройки LLM, стратегию и редактируемый промпт."""
    all_settings = database.get_all_system_settings()
    gemini_model = database.get_config_value("gemini_model") or Config.GEMINI_MODEL or "gemini-3.6-flash"
    mistral_model = database.get_config_value("mistral_model") or Config.MISTRAL_MODEL or "open-mistral-nemo"
    
    openai_provider_preset = database.get_config_value("openai_provider_preset") or Config.OPENAI_PROVIDER_PRESET or "groq"
    preset_info = OPENAI_PROVIDER_PRESETS.get(openai_provider_preset, {})
    oa_cfg = database.get_provider_config(openai_provider_preset)
    openai_base_url = (oa_cfg.get("base_url") if oa_cfg else None) or database.get_config_value("openai_base_url") or Config.OPENAI_BASE_URL or preset_info.get("base_url", "https://api.groq.com/openai/v1")
    openai_model = (oa_cfg.get("active_model") if oa_cfg else None) or database.get_config_value("openai_model") or Config.OPENAI_MODEL or preset_info.get("default_model", "llama-3.3-70b-versatile")
    if oa_cfg and "api_keys" in oa_cfg:
        openai_api_keys = ",".join(oa_cfg["api_keys"])
    else:
        raw_openai = database.get_config_value("openai_api_keys")
        if raw_openai is None:
            raw_openai = os.getenv("OPENAI_API_KEYS", "") or Config.OPENAI_API_KEY
        openai_api_keys = raw_openai or ""
    if openai_api_keys.startswith("your_"):
        openai_api_keys = ""

    fallback_str = all_settings.get("fallback_enabled", "true")
    fallback_bool = fallback_str.lower() in ("true", "1", "yes") if fallback_str else True
    
    try:
        temp_float = float(all_settings.get("temperature", "0.2"))
    except (ValueError, TypeError):
        temp_float = 0.2

    return {
        "system_prompt": all_settings.get("system_prompt", database.DEFAULT_SYSTEM_PROMPT),
        "default_system_prompt": database.DEFAULT_SYSTEM_PROMPT,
        "cover_letter_postfix": all_settings.get("cover_letter_postfix", ""),
        "primary_provider": all_settings.get("primary_provider", "gemini"),
        "fallback_enabled": fallback_bool,
        "temperature": temp_float,
        "gemini_model": gemini_model,
        "mistral_model": mistral_model,
        "openai_provider_preset": openai_provider_preset,
        "openai_base_url": openai_base_url,
        "openai_model": openai_model,
        "openai_api_keys": openai_api_keys,
        "openai_presets": list(OPENAI_PROVIDER_PRESETS.values()),
        "providers": list(UNIFIED_PROVIDERS.values())
    }

@router.get("/api/providers")
def get_all_providers():
    """Возвращает единый список всех провайдеров из базы данных с актуальными статусами."""
    providers_list = database.get_all_providers_config()
    # Обогащаем метаданными из пресетов и операционным статусом
    for item in providers_list:
        preset = UNIFIED_PROVIDERS.get(item["id"], {})
        if preset:
            if not item.get("description"):
                item["description"] = preset.get("description", "")
            if not item.get("get_key_url"):
                item["get_key_url"] = preset.get("get_key_url", "")
            item["badge"] = preset.get("badge", "")
            item["icon"] = preset.get("icon", "⚡")
            item["key_prefix_hint"] = preset.get("key_prefix_hint", "")

        status_info = LLMAnalyzer.get_provider_status(item["id"], item.get("api_keys", []), item.get("is_enabled", True))
        item["status_info"] = status_info
        item["operational_status"] = status_info["status"]
        item["status_label"] = status_info["label"]
        item["status_color"] = status_info["color"]
        if item["id"] == "openrouter" and (not item.get("active_model") or item.get("active_model") in DEAD_OPENROUTER_MODELS):
            item["active_model"] = "openrouter/free"
    return {"providers": providers_list}

@router.get("/api/providers/{provider_id}")
def get_provider_detail(provider_id: str):
    """Возвращает детальные настройки конкретного провайдера и список доступных моделей."""
    cfg = database.get_provider_config(provider_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Provider not found")
    
    preset = UNIFIED_PROVIDERS.get(cfg["id"], {})
    if preset:
        if not cfg.get("description"):
            cfg["description"] = preset.get("description", "")
        if not cfg.get("get_key_url"):
            cfg["get_key_url"] = preset.get("get_key_url", "")
        cfg["badge"] = preset.get("badge", "")
        cfg["icon"] = preset.get("icon", "⚡")
        cfg["key_prefix_hint"] = preset.get("key_prefix_hint", "")

    if cfg["id"] == "openrouter" and (not cfg.get("active_model") or cfg.get("active_model") in DEAD_OPENROUTER_MODELS):
        cfg["active_model"] = "openrouter/free"

    # Загружаем сохраненные модели из БД
    models = database.get_provider_models(cfg["id"])
    if cfg["id"] == "openrouter":
        models = [m for m in models if (m.get("model_id") or m.get("id")) not in DEAD_OPENROUTER_MODELS]
    if not models and preset and preset.get("models"):
        models = preset["models"]
    cfg["models"] = models

    status_info = LLMAnalyzer.get_provider_status(cfg["id"], cfg.get("api_keys", []), cfg.get("is_enabled", True))
    cfg["status_info"] = status_info
    cfg["operational_status"] = status_info["status"]
    cfg["status_label"] = status_info["label"]
    cfg["status_color"] = status_info["color"]

    key_statuses = LLMAnalyzer.get_key_statuses()
    cfg["keys_detail"] = [
        {
            "key": database.mask_api_key(k),
            "raw_key": k,
            "status": key_statuses.get(k, {}).get("status", "unknown"),
            "reason": key_statuses.get(k, {}).get("reason", ""),
            "detail": key_statuses.get(k, {}).get("detail", ""),
            "latency_ms": key_statuses.get(k, {}).get("latency_ms", 0)
        }
        for k in cfg.get("api_keys", [])
    ]

    res = dict(cfg)
    res["provider"] = dict(cfg)
    res["models"] = models
    return res

@router.post("/api/providers/custom")
def add_custom_provider(payload: CustomProviderCreatePayload):
    """Создает новый пользовательский OpenAI-совместимый провайдер."""
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    created = database.create_custom_provider(data)
    LLMAnalyzer._initialized_keys = False
    return {"status": "ok", "provider": created}

@router.delete("/api/providers/custom/{provider_id}")
def remove_custom_provider(provider_id: str):
    """Удаляет пользовательский провайдер."""
    try:
        ok = database.delete_custom_provider(provider_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Provider not found")
    LLMAnalyzer._initialized_keys = False
    return {"status": "ok"}

@router.post("/api/providers/probe-all")
def probe_all_providers():
    """Тестирует работоспособность всех настроенных провайдеров и возвращает их обновленные статусы."""
    providers_list = database.get_all_providers_config()
    for p in providers_list:
        keys = p.get("api_keys", [])
        if keys and p.get("is_enabled", True):
            try:
                probe_provider_keys(p["id"])
            except Exception:
                pass
    return get_all_providers()

@router.post("/api/providers/{provider_id}")
def update_provider_settings(provider_id: str, payload: ProviderConfigPayload):
    """Обновляет настройки провайдера (ключи, модель, температуру, base_url, активность)."""
    data = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else payload.dict(exclude_unset=True)
    old_cfg = database.get_provider_config(provider_id)
    try:
        updated = database.save_provider_config(provider_id, data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    
    # Очищаем кэш статусов для ключей, которых больше нет в пуле
    if old_cfg and "api_keys" in data:
        old_keys = set(old_cfg.get("api_keys", []))
        new_keys = set(updated.get("api_keys", []))
        removed_keys = old_keys - new_keys
        for rk in removed_keys:
            LLMAnalyzer.remove_key_status(rk)

    # Сбрасываем кэш проверки LLM
    LLMAnalyzer._initialized_keys = False
    return {"status": "ok", "provider": updated}

@router.post("/api/providers/{provider_id}/probe")
def probe_provider_keys(provider_id: str, payload: Optional[ProviderProbePayload] = None):
    """Тестирует работоспособность ключей конкретного провайдера или одного указанного ключа."""
    cfg = database.get_provider_config(provider_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Provider not found")

    target_key = payload.api_key.strip() if (payload and payload.api_key) else None

    protocol = cfg.get("protocol", "openai")
    active_model = cfg.get("active_model", "")
    base_url = cfg.get("base_url", "")

    def _test_single_key(k: str) -> tuple[str, str, str, int]:
        t0 = time.time()
        status = "ok"
        reason = ""
        detail = ""
        clean_k = database._clean_key(k)
        try:
            if protocol == "gemini":
                model_name = active_model or "gemini-2.5-flash"
                if genai:
                    try:
                        client = genai.Client(api_key=clean_k)
                        client.models.generate_content(model=model_name, contents="Hi")
                    except Exception as ge:
                        err_s = str(ge).lower()
                        status = "error"
                        reason = "rate_limit_or_quota" if ("429" in err_s or "resource_exhausted" in err_s) else ("invalid_api_key" if any(x in err_s for x in ("401", "403", "api_key_invalid", "permission_denied")) else "api_error")
                        detail = str(ge)[:200]
                else:
                    r = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={clean_k}",
                        json={"contents": [{"parts": [{"text": "Hi"}]}]},
                        timeout=10
                    )
                    if r.status_code != 200:
                        status = "error"
                        reason = "rate_limit_or_quota" if r.status_code == 429 else ("invalid_api_key" if r.status_code in (401, 403) else f"http_{r.status_code}")
                        detail = r.text[:200]
            elif protocol == "mistral":
                url = f"{(base_url or 'https://api.mistral.ai/v1').rstrip('/')}/chat/completions"
                r = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {clean_k}", "Content-Type": "application/json"},
                    json={"model": active_model or "open-mistral-nemo", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
                    timeout=10
                )
                if r.status_code != 200:
                    status = "error"
                    reason = "rate_limit_or_quota" if r.status_code == 429 else ("invalid_api_key" if r.status_code in (401, 403) else f"http_{r.status_code}")
                    detail = r.text[:200]
            else: # openai
                url = f"{(base_url or 'https://api.groq.com/openai/v1').rstrip('/')}/chat/completions"
                headers = {"Authorization": f"Bearer {clean_k}", "Content-Type": "application/json"}
                if "openrouter.ai" in url:
                    headers["HTTP-Referer"] = "https://github.com/andreimelneichuk/hh_job_applier"
                    headers["X-Title"] = "HH Job Applier"
                test_model = active_model or "gpt-4o-mini"
                if "openrouter.ai" in url and (not active_model or active_model in DEAD_OPENROUTER_MODELS):
                    test_model = "openrouter/free"
                r = requests.post(
                    url,
                    headers=headers,
                    json={"model": test_model, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
                    timeout=20
                )
                if r.status_code != 200:
                    # Если на OpenRouter выбранная модель устарела или платная, пробуем fallback на openrouter/free
                    if "openrouter.ai" in url and test_model != "openrouter/free":
                        try:
                            r_fallback = requests.post(
                                url,
                                headers=headers,
                                json={"model": "openrouter/free", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
                                timeout=20
                            )
                            if r_fallback.status_code == 200:
                                r = r_fallback
                                test_model = "openrouter/free"
                                try:
                                    database.save_provider_config("openrouter", {"active_model": "openrouter/free"})
                                except Exception:
                                    pass
                        except Exception:
                            pass

                if r.status_code != 200:
                    status = "error"
                    reason = "rate_limit_or_quota" if r.status_code == 429 else ("invalid_api_key" if r.status_code in (401, 403) else f"http_{r.status_code}")
                    detail = r.text[:200]
        except Exception as e:
            status = "error"
            reason = "network_error"
            detail = str(e)[:200]

        latency_ms = max(1, int((time.time() - t0) * 1000))
        LLMAnalyzer.record_key_status(clean_k, status, reason=reason, detail=detail, provider_id=provider_id, latency_ms=latency_ms)
        return status, reason, detail, latency_ms

    if target_key:
        st, reas, det, lat = _test_single_key(target_key)
        is_ok = (st == "ok")
        prov_status = LLMAnalyzer.get_provider_status(provider_id, cfg.get("api_keys", [target_key]), cfg.get("is_enabled", True))
        return {
            "status": "ok" if is_ok else "error",
            "success": is_ok,
            "latency_ms": lat,
            "provider": provider_id,
            "key": database.mask_api_key(target_key),
            "available": 1 if is_ok else 0,
            "total": 1,
            "error": (det or reas) if not is_ok else None,
            "reason": reas,
            "detail": det,
            "status_info": prov_status,
            "keys": [{
                "key": database.mask_api_key(target_key),
                "raw_key": target_key,
                "status": st,
                "reason": reas,
                "detail": det,
                "latency_ms": lat
            }]
        }

    keys = cfg.get("api_keys", [])
    if not keys:
        prov_status = LLMAnalyzer.get_provider_status(provider_id, [], cfg.get("is_enabled", True))
        return {
            "status": "no_keys",
            "success": False,
            "provider": provider_id,
            "available": 0,
            "total": 0,
            "status_info": prov_status,
            "keys": []
        }

    key_statuses = []
    available = 0
    total_lat = 0
    for k in keys:
        st, reas, det, lat = _test_single_key(k)
        if st == "ok":
            available += 1
        total_lat += lat
        key_statuses.append({
            "key": database.mask_api_key(k),
            "raw_key": k,
            "status": st,
            "reason": reas,
            "detail": det,
            "latency_ms": lat
        })

    avg_lat = int(total_lat / len(keys)) if keys else 0
    prov_status = LLMAnalyzer.get_provider_status(provider_id, keys, cfg.get("is_enabled", True))
    return {
        "status": "ok" if available > 0 else "error",
        "success": available > 0,
        "latency_ms": avg_lat,
        "provider": provider_id,
        "available": available,
        "total": len(keys),
        "status_info": prov_status,
        "keys": key_statuses
    }

@router.post("/api/models/sync")
def sync_models(payload: ModelSyncPayload):
    """Синхронизирует актуальный список моделей напрямую из API провайдера и сохраняет в БД."""
    analyzer = LLMAnalyzer()
    target_prov = payload.provider or "all"
    synced = analyzer.sync_provider_models(provider=target_prov)
    return {
        "status": "ok",
        "provider": target_prov,
        "synced_count": len(synced),
        "total": len(synced),
        "synced": {target_prov: len(synced)},
        "models": synced
    }

@router.post("/api/models/select")
def select_active_model(payload: ModelSelectPayload):
    """Фиксирует выбранную модель для провайдера в базе данных."""
    database.set_active_provider_model(payload.provider, payload.model_id)
    if payload.provider == "gemini":
        database.set_config_value("gemini_model", payload.model_id)
    elif payload.provider == "mistral":
        database.set_config_value("mistral_model", payload.model_id)
    else:
        database.set_config_value("openai_model", payload.model_id)
    return {
        "status": "ok",
        "provider": payload.provider,
        "model_id": payload.model_id,
        "active_model": payload.model_id
    }

@router.post("/api/system-settings")
def save_system_settings(payload: SystemSettingsPayload):
    """Сохраняет измененные системные настройки LLM."""
    if payload.system_prompt is not None:
        database.set_system_setting("system_prompt", payload.system_prompt)
    if payload.cover_letter_postfix is not None:
        database.set_system_setting("cover_letter_postfix", payload.cover_letter_postfix)
    if payload.primary_provider is not None:
        database.set_system_setting("primary_provider", payload.primary_provider)
    if payload.fallback_enabled is not None:
        database.set_system_setting("fallback_enabled", str(payload.fallback_enabled).lower())
    if payload.temperature is not None:
        database.set_system_setting("temperature", str(payload.temperature))
    if payload.gemini_model:
        database.set_config_value("gemini_model", payload.gemini_model)
        database.set_active_provider_model("gemini", payload.gemini_model)
    if payload.mistral_model:
        database.set_config_value("mistral_model", payload.mistral_model)
        database.set_active_provider_model("mistral", payload.mistral_model)
    if payload.openai_provider_preset:
        database.set_config_value("openai_provider_preset", payload.openai_provider_preset)
    if payload.openai_base_url:
        database.set_config_value("openai_base_url", payload.openai_base_url)
    if payload.openai_model:
        database.set_config_value("openai_model", payload.openai_model)
        if payload.openai_provider_preset:
            database.set_active_provider_model(payload.openai_provider_preset, payload.openai_model)
    if payload.openai_api_keys is not None:
        database.set_config_value("openai_api_keys", payload.openai_api_keys)
        
    # Сбрасываем кэш проверки LLM чтобы перепроверить новые настройки/ключи
    LLMAnalyzer._initialized_keys = False
    return {"status": "ok"}

@router.get("/api/openai-presets")
def get_openai_presets():
    """Возвращает готовые пресеты бесплатных провайдеров OpenAI API со ссылками на получение ключей."""
    return {"presets": list(OPENAI_PROVIDER_PRESETS.values())}

@router.post("/api/system-settings/reset-prompt")
def reset_system_prompt():
    """Сбрасывает системный промпт к дефолтному заводскому виду."""
    default_prompt = database.reset_system_prompt_to_default()
    return {"status": "ok", "system_prompt": default_prompt}

@router.get("/api/user-profile-answers")
def get_user_profile_answers():
    """Возвращает список сохраненных ответов пользователя на частые вопросы работодателей."""
    answers = database.get_user_profile_answers()
    return {"answers": answers}

@router.post("/api/user-profile-answers")
def save_user_profile_answer(payload: UserProfileAnswerPayload):
    """Сохраняет или обновляет ответ в профиле пользователя."""
    database.set_user_profile_answer(payload.key, payload.question_hint, payload.answer)
    return {"status": "ok"}

@router.delete("/api/user-profile-answers/{key}")
def delete_user_profile_answer(key: str):
    """Удаляет сохраненный ответ из профиля пользователя."""
    database.delete_user_profile_answer(key)
    return {"status": "ok"}

@router.get("/api/models")
def get_available_models(provider: str = "all", free_only: bool = False, catalog: bool = False):
    """Возвращает список доступных моделей для Gemini, Mistral и OpenAI пресетов."""
    if catalog or provider == "catalog":
        target = None if provider in ("all", "catalog") else provider
        models = database.get_provider_models(target, is_free_only=free_only)
        if not models:
            analyzer = LLMAnalyzer()
            analyzer.get_available_models(provider="all")
            models = database.get_provider_models(target, is_free_only=free_only)
        return {"catalog": models, "models": models}

    analyzer = LLMAnalyzer()
    models = analyzer.get_available_models(provider=provider, free_only=free_only)
    if isinstance(models, list):
        return {"models": models}
    return models

@router.get("/api/model-status")
def get_model_status(probe: bool = False):
    """Возвращает статус доступности LLM (Gemini, Mistral, OpenAI/Groq) и подробный статус ключей."""
    analyzer = LLMAnalyzer()
    res = analyzer.check_availability(force_probe=probe)
    all_keys = []
    if "gemini" in res and "keys" in res["gemini"]:
        all_keys.extend(res["gemini"]["keys"])
    if "mistral" in res and "keys" in res["mistral"]:
        all_keys.extend(res["mistral"]["keys"])
    if "openai" in res and "keys" in res["openai"]:
        all_keys.extend(res["openai"]["keys"])
    res["keys"] = all_keys
    return res

@router.get("/api/resumes")
def get_resumes():
    """Возвращает список резюме со страницы пользователя."""
    hh_client = HHBrowserClient()
    try:
        resumes = hh_client.get_my_resumes()
    finally:
        hh_client.stop()
    return {"resumes": resumes}
