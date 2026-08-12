from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import NtdDocument
from app.util import dumps, loads


def list_ntd(db: Session) -> list[NtdDocument]:
    return db.query(NtdDocument).order_by(NtdDocument.doc_type, NtdDocument.code).all()


def ntd_to_dict(doc: NtdDocument) -> dict[str, Any]:
    return {
        "id": doc.id,
        "code": doc.code,
        "title": doc.title,
        "doc_type": doc.doc_type,
        "status": doc.status,
        "in_force_from": doc.in_force_from,
        "replaced_by": doc.replaced_by,
        "source_url": doc.source_url,
        "applies_to": loads(doc.applies_to, []),
        "notes": doc.notes,
        "clauses": loads(doc.clauses_json, []),
        "body_text": doc.body_text,
        "last_checked": doc.last_checked.isoformat() if doc.last_checked else None,
        "last_check_method": doc.last_check_method,
        "last_check_note": doc.last_check_note,
    }


def relevant_ntd(db: Session, systems: list[str]) -> list[NtdDocument]:
    docs = list_ntd(db)
    if not systems:
        return docs
    out = []
    for d in docs:
        applies = loads(d.applies_to, [])
        if not applies or any(s in applies for s in systems):
            out.append(d)
    return out


def check_actuality(db: Session, systems: list[str] | None = None) -> dict[str, Any]:
    docs = relevant_ntd(db, systems or [])
    results = []
    online_ok = False
    online_note = ""
    if settings.ntd_online_check:
        online_ok, online_note = _probe_online()

    for doc in docs:
        note_parts = []
        method = "local"
        if doc.status == "replaced":
            note_parts.append(f"Заменён на {doc.replaced_by or 'не указан'}.")
        elif doc.status == "cancelled":
            note_parts.append("Документ отменён.")
        elif doc.status == "partial":
            note_parts.append(doc.notes or "Применяется частично.")
        elif doc.status == "check":
            note_parts.append("Статус требует подтверждения. Онлайн-проверка не дала однозначного ответа." if not online_ok else "Статус в каталоге — «проверить».")
        else:
            note_parts.append("По локальному каталогу — действует.")

        if doc.in_force_from:
            note_parts.append(f"Дата введения: {doc.in_force_from}.")

        if online_ok:
            method = "local+online-probe"
            note_parts.append(online_note)
        else:
            note_parts.append(
                "Онлайн-подтверждение не выполнено"
                + (f" ({online_note})" if online_note else " (сеть недоступна или проверка отключена)")
                + ". Использован локальный каталог."
            )

        doc.last_checked = datetime.utcnow()
        doc.last_check_method = method
        doc.last_check_note = " ".join(note_parts)
        results.append(
            {
                "code": doc.code,
                "title": doc.title,
                "status": doc.status,
                "replaced_by": doc.replaced_by,
                "note": doc.last_check_note,
            }
        )
    db.commit()
    return {
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "online": online_ok,
        "online_note": online_note,
        "documents": results,
    }


def _probe_online() -> tuple[bool, str]:
    """Проверяет доступность официальных источников, не выдумывая статусы документов."""
    urls = [
        "http://pravo.gov.ru/",
        "https://www.consultant.ru/",
    ]
    ok_any = False
    notes = []
    try:
        with httpx.Client(timeout=settings.ntd_online_timeout, follow_redirects=True) as client:
            for url in urls:
                try:
                    r = client.get(url, headers={"User-Agent": "NTD-Revizor/1.0"})
                    if r.status_code < 400:
                        ok_any = True
                        notes.append(f"{url} доступен (HTTP {r.status_code}).")
                    else:
                        notes.append(f"{url} HTTP {r.status_code}.")
                except Exception as exc:
                    notes.append(f"{url}: {exc.__class__.__name__}")
    except Exception as exc:
        return False, f"Сетевая проверка не удалась: {exc}"
    if not ok_any:
        return False, "Официальные источники недоступны. " + " ".join(notes)
    return True, (
        "Сеть доступна, но автоматический разбор карточки каждого НТД с правовых порталов "
        "не выполняется (нет стабильного открытого API). Статусы — из локальной базы. "
        + " ".join(notes)
    )


def clauses_for_prompt(db: Session, systems: list[str], limit_chars: int = 9000) -> str:
    parts = []
    size = 0
    for doc in relevant_ntd(db, systems):
        if doc.status in {"cancelled"}:
            continue
        block = [f"## {doc.code} — {doc.title} [{doc.status}]"]
        if doc.notes:
            block.append(doc.notes)
        for cl in loads(doc.clauses_json, []):
            block.append(f"- {cl.get('ref', '')}: {cl.get('text', '')}")
        if doc.body_text:
            block.append(doc.body_text[:2500])
        text = "\n".join(block)
        if size + len(text) > limit_chars:
            break
        parts.append(text)
        size += len(text)
    return "\n\n".join(parts)


def find_outdated_refs(text: str, db: Session) -> list[dict[str, str]]:
    if not text:
        return []
    hits = []
    replaced = db.query(NtdDocument).filter(NtdDocument.status == "replaced").all()
    for doc in replaced:
        # точное вхождение кода
        if re.search(re.escape(doc.code), text, flags=re.IGNORECASE):
            hits.append(
                {
                    "found": doc.code,
                    "replaced_by": doc.replaced_by,
                    "title": doc.title,
                }
            )
    # устаревшие типы СОУЭ
    if re.search(r"соуэ\s*[1-5]\s*тип", text, flags=re.I) or re.search(
        r"тип[а-я]*\s*[1-5]\s*соуэ", text, flags=re.I
    ):
        hits.append(
            {
                "found": "СОУЭ N-го типа",
                "replaced_by": "СП 3.13130.2026 (способы оповещения, без типов 1–5)",
                "title": "Устаревшая классификация СОУЭ",
            }
        )
    return hits
