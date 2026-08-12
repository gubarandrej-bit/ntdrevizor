from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import ROOT_DIR, settings
from app.db import Base, engine, session_scope
from app.models import NtdDocument, Setting, User
from app.security import hash_password
from app.util import dumps


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with session_scope() as db:
        _seed_admin(db)
        _seed_ntd(db)
        _seed_settings(db)


def _seed_admin(db: Session) -> None:
    if db.query(User).filter(User.username == settings.admin_username).first():
        return
    db.add(
        User(
            username=settings.admin_username,
            password_hash=hash_password(settings.admin_password),
            full_name=settings.admin_full_name,
            role="admin",
            is_active=True,
            must_change_password=True,
        )
    )


def _seed_ntd(db: Session) -> None:
    path = ROOT_DIR / "data" / "ntd_catalog.json"
    if not path.exists():
        path = settings.data_dir / "ntd_catalog.json"
    if not path.exists():
        return
    catalog = json.loads(path.read_text(encoding="utf-8"))
    for item in catalog.get("documents", []):
        existing = db.query(NtdDocument).filter(NtdDocument.code == item["code"]).first()
        payload = dict(
            title=item.get("title", ""),
            doc_type=item.get("doc_type", ""),
            status=item.get("status", "check"),
            in_force_from=item.get("in_force_from", ""),
            replaced_by=item.get("replaced_by", ""),
            source_url=item.get("source_url", ""),
            applies_to=dumps(item.get("applies_to", [])),
            notes=item.get("notes", ""),
            clauses_json=dumps(item.get("clauses", [])),
            last_checked=datetime.utcnow(),
            last_check_method="seed",
            last_check_note=f"Загрузка каталога на {catalog.get('as_of', '')}",
        )
        if existing:
            # не затираем вручную отредактированный полный текст
            for key, value in payload.items():
                if key == "notes" and existing.body_text:
                    continue
                setattr(existing, key, value)
        else:
            db.add(NtdDocument(code=item["code"], **payload))


def _seed_settings(db: Session) -> None:
    defaults = {
        "company_name": "",
        "default_mode": "hybrid",
        "qty_tolerance_pct": "5",
        "length_tolerance_pct": "10",
        "ai_temperature": "0.1",
        "ai_max_tokens": "1800",
    }
    for key, value in defaults.items():
        if db.get(Setting, key) is None:
            db.add(Setting(key=key, value=value))


def catalog_as_of() -> str:
    path = ROOT_DIR / "data" / "ntd_catalog.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")).get("as_of", "")
    return ""


if __name__ == "__main__":
    settings.ensure_dirs()
    init_db()
    print("База инициализирована:", settings.db_path)
