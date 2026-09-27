"""Пользовательские облачные модели (OpenAI-совместимые endpoints).

Хранятся в data/custom_models.json (права 0600). Ключ API наружу не отдаётся —
только маска. id вида "custom:<slug>" — такой же model_id, как у встроенных
моделей: его можно выбирать в проверке (поле models) и он проходит через
ai.complete() наравне с gemini/groq/gigachat.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

_ID_PREFIX = "custom:"
_NAME_RE = re.compile(r"^[^<>\"'\\]{2,80}$")
_URL_RE = re.compile(r"^https?://[^\s]{4,300}$")


def store_path() -> Path:
    return settings.data_dir / "custom_models.json"


def load_custom_models() -> list[dict[str, Any]]:
    p = store_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("models") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("id", "").startswith(_ID_PREFIX) and it.get("base_url"):
            out.append(
                {
                    "id": str(it["id"]),
                    "name": str(it.get("name") or it["id"]),
                    "base_url": str(it.get("base_url") or ""),
                    "api_key": str(it.get("api_key") or ""),
                    "model": str(it.get("model") or ""),
                    "note": str(it.get("note") or ""),
                    "created_at": str(it.get("created_at") or ""),
                }
            )
    return out


def _save(models: list[dict[str, Any]]) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"version": 1, "models": models}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _slug(name: str, model: str) -> str:
    base = re.sub(r"[^a-z0-9._-]+", "-", f"{name} {model}".lower()).strip("-.")
    if len(base) < 2:
        base = "m" + hashlib.sha1(f"{name}|{model}".encode("utf-8")).hexdigest()[:6]
    return base[:48]


def find(models: list[dict[str, Any]], model_id: str) -> dict[str, Any] | None:
    for m in models:
        if m["id"] == model_id:
            return m
    return None


def public_view(m: dict[str, Any]) -> dict[str, Any]:
    key = m.get("api_key") or ""
    tail = key[-4:] if len(key) >= 8 else ("*" if key else "")
    return {
        "id": m["id"],
        "name": m["name"],
        "base_url": m["base_url"],
        "model": m["model"],
        "note": m.get("note", ""),
        "has_key": bool(key),
        "key_masked": (f"…{tail}" if tail else ""),
        "created_at": m.get("created_at", ""),
    }


def add(
    name: str, base_url: str, api_key: str, model: str, note: str = ""
) -> tuple[dict[str, Any] | None, str]:
    name = (name or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    model = (model or "").strip()
    if not _NAME_RE.match(name):
        return None, "Название: от 2 до 80 символов без < > \" ' \\."
    if not _URL_RE.match(base_url):
        return None, "Base URL должен начинаться с http:// или https://."
    if not model or len(model) > 120 or re.search(r"[<>\"'\\]", model):
        return None, "Имя модели у провайдера обязательно (до 120 символов)."
    models = load_custom_models()
    slug = _slug(name, model)
    mid = _ID_PREFIX + slug
    n = 2
    while find(models, mid):
        mid = _ID_PREFIX + f"{slug}-{n}"
        n += 1
    entry = {
        "id": mid,
        "name": name,
        "base_url": base_url,
        "api_key": (api_key or "").strip(),
        "model": model,
        "note": (note or "").strip()[:200],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    models.append(entry)
    _save(models)
    return entry, ""


def update(model_id: str, patch: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    models = load_custom_models()
    entry = find(models, model_id)
    if not entry:
        return None, "Модель не найдена."
    if patch.get("name") is not None:
        name = str(patch["name"]).strip()
        if not _NAME_RE.match(name):
            return None, "Название: от 2 до 80 символов без < > \" ' \\."
        entry["name"] = name
    if patch.get("base_url") is not None:
        url = str(patch["base_url"]).strip().rstrip("/")
        if not _URL_RE.match(url):
            return None, "Base URL должен начинаться с http:// или https://."
        entry["base_url"] = url
    if patch.get("model") is not None:
        model = str(patch["model"]).strip()
        if not model:
            return None, "Имя модели у провайдера обязательно."
        entry["model"] = model
    if patch.get("api_key") is not None:
        entry["api_key"] = str(patch["api_key"]).strip()
    if patch.get("note") is not None:
        entry["note"] = str(patch["note"]).strip()[:200]
    _save(models)
    return entry, ""


def remove(model_id: str) -> bool:
    models = load_custom_models()
    rest = [m for m in models if m["id"] != model_id]
    if len(rest) == len(models):
        return False
    _save(rest)
    return True


def describe_host(base_url: str) -> str:
    m = re.match(r"^https?://([^/]+)", base_url or "")
    return m.group(1) if m else (base_url or "?")
