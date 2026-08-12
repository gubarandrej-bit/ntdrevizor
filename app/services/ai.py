from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import ROOT_DIR, settings
from app.util import truncate

PromptRules = (ROOT_DIR / "data" / "prompt_rules.md")


def load_rules() -> str:
    path = PromptRules
    if not path.exists():
        path = settings.data_dir / "prompt_rules.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return (
        "Если не хватает исходных данных — ничего не придумывай, запроси недостающее. "
        "Непроведённые проверки указывай с причиной. Ответ — как есть."
    )


def available_models() -> dict[str, Any]:
    local: list[dict[str, Any]] = []
    cloud: list[dict[str, Any]] = []

    # Ollama
    ollama_models, ollama_err = _ollama_tags()
    if ollama_models:
        for name in ollama_models:
            local.append(
                {
                    "id": f"ollama:{name}",
                    "provider": "ollama",
                    "name": name,
                    "place": "local",
                    "ready": True,
                    "note": "Локально через Ollama",
                }
            )
    else:
        local.append(
            {
                "id": "ollama:",
                "provider": "ollama",
                "name": "Ollama",
                "place": "local",
                "ready": False,
                "note": ollama_err or "Ollama не запущена",
            }
        )

    gguf = settings.local_gguf_path
    gguf_ready = bool(gguf and Path(gguf).exists())
    local.append(
        {
            "id": "llamacpp:local",
            "provider": "llamacpp",
            "name": Path(gguf).name if gguf else "llama.cpp GGUF",
            "place": "local",
            "ready": gguf_ready,
            "note": "Файл GGUF найден" if gguf_ready else "Укажите LOCAL_GGUF_PATH. На FX-8120 — модель 1–1.5B Q4, сборка без AVX2.",
        }
    )

    def add_cloud(mid: str, provider: str, name: str, ready: bool, note: str):
        cloud.append(
            {
                "id": mid,
                "provider": provider,
                "name": name,
                "place": "cloud",
                "ready": ready,
                "note": note,
            }
        )

    add_cloud(
        "gemini:gemini-2.0-flash",
        "gemini",
        "Gemini 2.0 Flash (бесплатный тариф Google AI Studio)",
        bool(settings.gemini_api_key),
        "Ключ: GEMINI_API_KEY. Мультимодальность, хороший русский.",
    )
    add_cloud(
        "gemini:gemini-2.5-flash",
        "gemini",
        "Gemini 2.5 Flash (если доступен в проекте)",
        bool(settings.gemini_api_key),
        "Тот же ключ GEMINI_API_KEY.",
    )
    add_cloud(
        "groq:llama-3.3-70b-versatile",
        "groq",
        "Groq Llama 3.3 70B",
        bool(settings.groq_api_key),
        "Ключ: GROQ_API_KEY. Быстрый бесплатный тариф.",
    )
    add_cloud(
        "groq:llama-3.1-8b-instant",
        "groq",
        "Groq Llama 3.1 8B Instant",
        bool(settings.groq_api_key),
        "Ключ: GROQ_API_KEY.",
    )
    add_cloud(
        "openrouter:meta-llama/llama-3.3-70b-instruct:free",
        "openrouter",
        "OpenRouter Llama 3.3 70B :free",
        bool(settings.openrouter_api_key),
        "Ключ: OPENROUTER_API_KEY. Лимиты дневные.",
    )
    add_cloud(
        "openrouter:google/gemini-2.0-flash-exp:free",
        "openrouter",
        "OpenRouter Gemini Flash :free",
        bool(settings.openrouter_api_key),
        "Ключ: OPENROUTER_API_KEY.",
    )
    add_cloud(
        "gigachat:GigaChat",
        "gigachat",
        "GigaChat Lite (Сбер, серверы РФ)",
        bool(settings.gigachat_credentials),
        "GIGACHAT_CREDENTIALS = Authorization Key. Бесплатный лимит PERS.",
    )
    add_cloud(
        "gigachat:GigaChat-Pro",
        "gigachat",
        "GigaChat Pro (если доступен на ключе)",
        bool(settings.gigachat_credentials),
        "Тот же ключ GIGACHAT_CREDENTIALS.",
    )
    add_cloud(
        "yandex:yandexgpt-lite",
        "yandex",
        "YandexGPT Lite",
        bool(settings.yandex_api_key and settings.yandex_folder_id),
        "YANDEX_API_KEY + YANDEX_FOLDER_ID.",
    )
    add_cloud(
        "openai:compat",
        "openai",
        settings.openai_compat_model or "OpenAI-совместимый endpoint",
        bool(settings.openai_compat_base_url),
        "OPENAI_COMPAT_BASE_URL / KEY / MODEL.",
    )

    return {
        "local": local,
        "cloud": cloud,
        "recommended": {
            "local": "На AMD FX-8120 без GPU: Ollama qwen2.5:1.5b или GGUF 1.5B Q4_K_M. "
            "HD 7950 3 ГБ (GCN 1.0) современными бэкендами LLM не поддерживается.",
            "cloud": "Для русского инженерного текста предпочтительны GigaChat и Gemini Flash. "
            "Groq — скорость. OpenRouter — запасной бесплатный пул.",
            "hybrid": "Детерминированные сверки всегда локально. ИИ — для схем и текстовых расчётов. "
            "Если облако недоступно, соответствующие проверки помечаются как не проведённые.",
        },
    }


def _ollama_tags() -> tuple[list[str], str]:
    try:
        r = httpx.get(settings.ollama_base_url.rstrip("/") + "/api/tags", timeout=2.5)
        r.raise_for_status()
        models = [m.get("name") for m in r.json().get("models", []) if m.get("name")]
        if not models:
            return [], "Ollama запущена, модели не установлены"
        return models, ""
    except Exception as exc:
        return [], f"Ollama недоступна ({exc.__class__.__name__})"


def complete(
    model_id: str,
    user_prompt: str,
    system_prompt: str | None = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Синхронный вызов модели. Ничего не выдумывает при ошибке — возвращает error."""
    system_prompt = system_prompt or load_rules()
    if not model_id or ":" not in model_id:
        return {"ok": False, "error": "Модель не выбрана.", "text": ""}
    provider, name = model_id.split(":", 1)
    started = time.time()
    try:
        if provider == "ollama":
            text = _ollama_chat(name, system_prompt, user_prompt, timeout)
        elif provider == "llamacpp":
            text = _llamacpp_chat(system_prompt, user_prompt)
        elif provider == "gemini":
            text = _gemini_chat(name, system_prompt, user_prompt, timeout)
        elif provider == "groq":
            text = _openai_chat(
                "https://api.groq.com/openai/v1",
                settings.groq_api_key,
                name,
                system_prompt,
                user_prompt,
                timeout,
            )
        elif provider == "openrouter":
            text = _openai_chat(
                "https://openrouter.ai/api/v1",
                settings.openrouter_api_key,
                name,
                system_prompt,
                user_prompt,
                timeout,
                extra_headers={
                    "HTTP-Referer": "http://localhost:8080",
                    "X-Title": "NTD-Revizor",
                },
            )
        elif provider == "gigachat":
            text = _gigachat_chat(name, system_prompt, user_prompt, timeout)
        elif provider == "yandex":
            text = _yandex_chat(name, system_prompt, user_prompt, timeout)
        elif provider == "openai":
            if not settings.openai_compat_base_url:
                return {"ok": False, "error": "OPENAI_COMPAT_BASE_URL не задан.", "text": ""}
            text = _openai_chat(
                settings.openai_compat_base_url.rstrip("/"),
                settings.openai_compat_api_key,
                settings.openai_compat_model or name,
                system_prompt,
                user_prompt,
                timeout,
            )
        else:
            return {"ok": False, "error": f"Неизвестный провайдер: {provider}", "text": ""}
        return {
            "ok": True,
            "text": text,
            "model": model_id,
            "elapsed_s": round(time.time() - started, 2),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{provider}: {exc}",
            "text": "",
            "model": model_id,
            "elapsed_s": round(time.time() - started, 2),
        }


def parse_json_response(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re_strip_fence(text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"findings": data}
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def re_strip_fence(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _ollama_chat(model: str, system: str, user: str, timeout: float) -> str:
    if not model:
        raise RuntimeError("Не указана модель Ollama")
    r = httpx.post(
        settings.ollama_base_url.rstrip("/") + "/api/chat",
        json={
            "model": model,
            "stream": False,
            "options": {"temperature": 0.1},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("message", {}).get("content") or ""


def _llamacpp_chat(system: str, user: str) -> str:
    path = settings.local_gguf_path
    if not path or not Path(path).exists():
        raise RuntimeError("Файл GGUF не найден (LOCAL_GGUF_PATH)")
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise RuntimeError(
            "llama-cpp-python не установлен. См. scripts/install-local-llm.sh"
        ) from exc
    llm = Llama(
        model_path=path,
        n_ctx=settings.local_gguf_n_ctx,
        n_threads=settings.local_gguf_n_threads,
        verbose=False,
    )
    out = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        max_tokens=800,
    )
    return out["choices"][0]["message"]["content"]


def _gemini_chat(model: str, system: str, user: str, timeout: float) -> str:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY не задан")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    r = httpx.post(
        url,
        params={"key": settings.gemini_api_key},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
        },
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    data = r.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _openai_chat(
    base: str,
    key: str,
    model: str,
    system: str,
    user: str,
    timeout: float,
    extra_headers: dict | None = None,
) -> str:
    if not key and "openrouter" in base:
        raise RuntimeError("OPENROUTER_API_KEY не задан")
    if not key and "groq" in base:
        raise RuntimeError("GROQ_API_KEY не задан")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if extra_headers:
        headers.update(extra_headers)
    url = base.rstrip("/") + "/chat/completions"
    r = httpx.post(
        url,
        headers=headers,
        json={
            "model": model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    return r.json()["choices"][0]["message"]["content"]


_giga_token: dict[str, Any] = {"value": "", "exp": 0.0}


def _gigachat_chat(model: str, system: str, user: str, timeout: float) -> str:
    if not settings.gigachat_credentials:
        raise RuntimeError("GIGACHAT_CREDENTIALS не задан")
    token = _gigachat_token()
    r = httpx.post(
        "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "model": model or "GigaChat",
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=timeout,
        verify=False,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    return r.json()["choices"][0]["message"]["content"]


def _gigachat_token() -> str:
    now = time.time()
    if _giga_token["value"] and _giga_token["exp"] > now + 30:
        return _giga_token["value"]
    r = httpx.post(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        headers={
            "Authorization": f"Basic {settings.gigachat_credentials}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={"scope": settings.gigachat_scope},
        timeout=20,
        verify=False,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"OAuth GigaChat HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    _giga_token["value"] = data["access_token"]
    _giga_token["exp"] = now + float(data.get("expires_at") or 1500) / (
        1000 if data.get("expires_at", 0) > 10_000_000 else 1
    )
    if data.get("expires_at", 0) > 10_000_000:
        _giga_token["exp"] = data["expires_at"] / 1000.0
    else:
        _giga_token["exp"] = now + 1400
    return _giga_token["value"]


def _yandex_chat(model: str, system: str, user: str, timeout: float) -> str:
    if not settings.yandex_api_key or not settings.yandex_folder_id:
        raise RuntimeError("YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы")
    uri = f"gpt://{settings.yandex_folder_id}/{model}/latest"
    r = httpx.post(
        "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
        headers={
            "Authorization": f"Api-Key {settings.yandex_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "modelUri": uri,
            "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": "1500"},
            "messages": [
                {"role": "system", "text": system},
                {"role": "user", "text": user},
            ],
        },
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    alts = r.json().get("result", {}).get("alternatives", [])
    if not alts:
        return ""
    return alts[0].get("message", {}).get("text", "")


def pick_model(mode: str, selected: list[str]) -> tuple[str | None, str]:
    """Возвращает (model_id, reason). Не подменяет модель молча."""
    catalog = available_models()
    ready = {
        m["id"]: m
        for m in catalog["local"] + catalog["cloud"]
        if m.get("ready") and m.get("id") and not m["id"].endswith(":")
    }
    if mode == "local":
        pool = [s for s in selected if s.startswith(("ollama:", "llamacpp:"))]
    elif mode == "cloud":
        pool = [s for s in selected if not s.startswith(("ollama:", "llamacpp:"))]
    else:
        pool = list(selected)

    for mid in pool:
        if mid in ready:
            return mid, ""
    # ничего не подставляем
    if not selected:
        return None, "Модель ИИ не выбрана."
    return None, (
        "Выбранные модели недоступны в текущем режиме "
        f"«{mode}». Нет ключей API или локальная модель не установлена. "
        f"Выбрано: {', '.join(selected)}."
    )
