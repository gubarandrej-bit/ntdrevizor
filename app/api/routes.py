from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import __version__
from app.config import settings
from app.db import get_db
from app.models import (
    ActionLog,
    Audit,
    AuditFile,
    CheckResult,
    DialogMessage,
    Finding,
    NtdDocument,
    Setting,
    User,
)
from app.security import (
    create_token,
    current_user,
    hash_password,
    require_admin,
    verify_password,
)
from app.services import ai as ai_svc
from app.services.engine import answer_dialog, log_dialog
from app.services.ntd import list_ntd, ntd_to_dict
from app.services.reports import build_reports
from app.util import dumps, loads, safe_filename

router = APIRouter(prefix="/api")


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    password: str = Field(min_length=8)


class UserIn(BaseModel):
    username: str
    password: str | None = None
    full_name: str = ""
    role: str = "engineer"
    is_active: bool = True


class NtdIn(BaseModel):
    code: str
    title: str
    doc_type: str = "ГОСТ"
    status: str = "check"
    in_force_from: str = ""
    replaced_by: str = ""
    source_url: str = ""
    applies_to: list[str] = []
    notes: str = ""
    clauses: list[dict[str, Any]] = []
    body_text: str = ""


class AuditIn(BaseModel):
    title: str
    object_name: str = ""
    systems: list[str] = []
    mode: str = "hybrid"
    models: list[str] = []


class DialogIn(BaseModel):
    text: str


class SettingsIn(BaseModel):
    values: dict[str, str]


def _user_out(u: User) -> dict[str, Any]:
    return {
        "id": u.id,
        "username": u.username,
        "full_name": u.full_name,
        "role": u.role,
        "is_active": u.is_active,
        "must_change_password": u.must_change_password,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login": u.last_login.isoformat() if u.last_login else None,
    }


def _log(db: Session, user: User | None, action: str, detail: str = ""):
    db.add(
        ActionLog(
            user_id=user.id if user else None,
            username=user.username if user else "",
            action=action,
            detail=detail,
        )
    )
    db.commit()


@router.get("/health")
def health():
    return {"ok": True, "name": settings.app_name, "version": __version__}


@router.post("/auth/login")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Учётная запись заблокирована")
    user.last_login = datetime.utcnow()
    db.commit()
    _log(db, user, "login")
    return {"token": create_token(user), "user": _user_out(user)}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return _user_out(user)


@router.post("/auth/password")
def change_own_password(
    payload: PasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    user.password_hash = hash_password(payload.password)
    user.must_change_password = False
    db.commit()
    return {"ok": True}


@router.get("/users")
def users_list(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [_user_out(u) for u in db.query(User).order_by(User.id).all()]


@router.post("/users")
def users_create(payload: UserIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Пользователь уже существует")
    if not payload.password:
        raise HTTPException(status_code=400, detail="Пароль обязателен")
    if payload.role not in {"admin", "engineer", "viewer"}:
        raise HTTPException(status_code=400, detail="Роль: admin, engineer, viewer")
    u = User(
        username=payload.username.strip(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=payload.role,
        is_active=payload.is_active,
        must_change_password=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    _log(db, admin, "user_create", u.username)
    return _user_out(u)


@router.patch("/users/{user_id}")
def users_update(
    user_id: int,
    payload: UserIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if payload.full_name:
        u.full_name = payload.full_name
    if payload.role in {"admin", "engineer", "viewer"}:
        u.role = payload.role
    u.is_active = payload.is_active
    if payload.password:
        u.password_hash = hash_password(payload.password)
        u.must_change_password = True
    db.commit()
    _log(db, admin, "user_update", u.username)
    return _user_out(u)


@router.post("/users/{user_id}/block")
def users_block(
    user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if u.id == admin.id:
        raise HTTPException(status_code=400, detail="Нельзя заблокировать собственную учётную запись")
    u.is_active = False
    db.commit()
    _log(db, admin, "user_block", u.username)
    return _user_out(u)


@router.post("/users/{user_id}/unblock")
def users_unblock(
    user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    u.is_active = True
    db.commit()
    return _user_out(u)


@router.delete("/users/{user_id}")
def users_delete(
    user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if u.id == admin.id:
        raise HTTPException(status_code=400, detail="Нельзя удалить собственную учётную запись")
    db.delete(u)
    db.commit()
    _log(db, admin, "user_delete", str(user_id))
    return {"ok": True}


@router.get("/ntd")
def ntd_list(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [ntd_to_dict(d) for d in list_ntd(db)]


@router.post("/ntd")
def ntd_create(payload: NtdIn, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.query(NtdDocument).filter(NtdDocument.code == payload.code).first():
        raise HTTPException(status_code=400, detail="Документ с таким шифром уже есть")
    d = NtdDocument(
        code=payload.code,
        title=payload.title,
        doc_type=payload.doc_type,
        status=payload.status,
        in_force_from=payload.in_force_from,
        replaced_by=payload.replaced_by,
        source_url=payload.source_url,
        applies_to=dumps(payload.applies_to),
        notes=payload.notes,
        clauses_json=dumps(payload.clauses),
        body_text=payload.body_text,
        last_checked=datetime.utcnow(),
        last_check_method="manual",
        last_check_note="Создан администратором",
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return ntd_to_dict(d)


@router.put("/ntd/{ntd_id}")
def ntd_update(
    ntd_id: int, payload: NtdIn, _: User = Depends(require_admin), db: Session = Depends(get_db)
):
    d = db.get(NtdDocument, ntd_id)
    if not d:
        raise HTTPException(status_code=404, detail="Документ не найден")
    d.code = payload.code
    d.title = payload.title
    d.doc_type = payload.doc_type
    d.status = payload.status
    d.in_force_from = payload.in_force_from
    d.replaced_by = payload.replaced_by
    d.source_url = payload.source_url
    d.applies_to = dumps(payload.applies_to)
    d.notes = payload.notes
    d.clauses_json = dumps(payload.clauses)
    d.body_text = payload.body_text
    d.last_check_method = "manual"
    d.last_check_note = "Изменён администратором"
    db.commit()
    return ntd_to_dict(d)


@router.delete("/ntd/{ntd_id}")
def ntd_delete(ntd_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    d = db.get(NtdDocument, ntd_id)
    if not d:
        raise HTTPException(status_code=404, detail="Документ не найден")
    db.delete(d)
    db.commit()
    return {"ok": True}


@router.post("/ntd/check-actuality")
def ntd_check(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.services.ntd import check_actuality

    result = check_actuality(db, [])
    _log(db, user, "ntd_actuality")
    return result


@router.get("/models")
def models(_: User = Depends(current_user)):
    return ai_svc.available_models()


@router.get("/settings")
def get_settings(_: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.query(Setting).all()
    return {r.key: r.value for r in rows}


@router.put("/settings")
def put_settings(
    payload: SettingsIn, _: User = Depends(require_admin), db: Session = Depends(get_db)
):
    for k, v in payload.values.items():
        row = db.get(Setting, k)
        if row:
            row.value = str(v)
        else:
            db.add(Setting(key=k, value=str(v)))
    db.commit()
    return {r.key: r.value for r in db.query(Setting).all()}


@router.get("/audits")
def audits_list(user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = db.query(Audit).order_by(Audit.id.desc())
    if user.role == "viewer":
        q = q.filter(Audit.created_by == user.id)
    return [_audit_brief(a) for a in q.limit(300).all()]


@router.post("/audits")
def audits_create(
    payload: AuditIn, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    if user.role == "viewer":
        raise HTTPException(status_code=403, detail="Просмотр без права создавать проверки")
    if payload.mode not in {"local", "cloud", "hybrid"}:
        raise HTTPException(status_code=400, detail="Режим: local, cloud, hybrid")
    a = Audit(
        title=payload.title.strip() or "Проверка без названия",
        object_name=payload.object_name.strip(),
        systems_json=dumps(payload.systems),
        mode=payload.mode,
        models_json=dumps(payload.models),
        status="draft",
        created_by=user.id,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    log_dialog(db, a.id, "system", f"Создана проверка «{a.title}». Загрузите файлы и запустите.")
    return _audit_full(db, a)


ALLOWED_EXT = {".xls", ".xlsx", ".xlsm", ".doc", ".docx", ".pdf", ".dwg", ".dxf", ".txt", ".csv"}


@router.post("/audits/{audit_id}/files")
async def audits_upload(
    audit_id: int,
    file: UploadFile = File(...),
    classified_as: str = Form("auto"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    audit = _get_audit(db, audit_id, user)
    if audit.status == "running":
        raise HTTPException(status_code=400, detail="Дождитесь окончания проверки")
    name = safe_filename(file.filename or "file")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Формат {ext} не принимается. Допустимы: xls, xlsx, doc, docx, pdf, dwg, dxf.",
        )
    dest_dir = settings.uploads_dir / str(audit.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid4().hex[:10]}_{name}"
    size = 0
    max_b = settings.upload_max_mb * 1024 * 1024
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_b:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"Файл больше {settings.upload_max_mb} МБ")
            out.write(chunk)
    rec = AuditFile(
        audit_id=audit.id,
        filename=name,
        stored_path=str(dest),
        ext=ext,
        classified_as="unknown",
        user_class="" if classified_as == "auto" else classified_as,
        size=size,
        parse_status="pending",
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    log_dialog(db, audit.id, "system", f"Загружен файл {name} ({size} байт).")
    return _file_out(rec)


@router.delete("/audits/{audit_id}/files/{file_id}")
def audits_file_delete(
    audit_id: int,
    file_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    audit = _get_audit(db, audit_id, user)
    rec = db.get(AuditFile, file_id)
    if not rec or rec.audit_id != audit.id:
        raise HTTPException(status_code=404, detail="Файл не найден")
    Path(rec.stored_path).unlink(missing_ok=True)
    db.delete(rec)
    db.commit()
    return {"ok": True}


@router.post("/audits/{audit_id}/start")
def audits_start(
    audit_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    from app.main import schedule_audit

    audit = _get_audit(db, audit_id, user)
    if user.role == "viewer":
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    if audit.status == "running":
        raise HTTPException(status_code=400, detail="Проверка уже выполняется")
    # очистить прошлые результаты при повторном запуске
    db.query(Finding).filter(Finding.audit_id == audit.id).delete()
    db.query(CheckResult).filter(CheckResult.audit_id == audit.id).delete()
    audit.status = "queued"
    audit.finished_at = None
    audit.error_text = ""
    db.commit()
    log_dialog(db, audit.id, "system", "Проверка поставлена в очередь.")
    schedule_audit(audit.id)
    return _audit_full(db, audit)


@router.get("/audits/{audit_id}")
def audits_get(audit_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _audit_full(db, _get_audit(db, audit_id, user))


@router.get("/audits/{audit_id}/dialog")
def audits_dialog(
    audit_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    audit = _get_audit(db, audit_id, user)
    rows = (
        db.query(DialogMessage)
        .filter(DialogMessage.audit_id == audit.id)
        .order_by(DialogMessage.id)
        .all()
    )
    return [
        {
            "id": m.id,
            "ts": m.ts.isoformat() if m.ts else None,
            "role": m.role,
            "text": m.text,
            "meta": loads(m.meta_json, {}),
        }
        for m in rows
    ]


@router.post("/audits/{audit_id}/dialog")
def audits_ask(
    audit_id: int,
    payload: DialogIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    audit = _get_audit(db, audit_id, user)
    log_dialog(db, audit.id, "user", payload.text)
    answer = answer_dialog(db, audit, payload.text)
    log_dialog(db, audit.id, "assistant", answer)
    return {"text": answer}


@router.get("/audits/{audit_id}/export/{kind}")
def audits_export(
    audit_id: int,
    kind: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    audit = _get_audit(db, audit_id, user)
    if audit.status not in {"done", "error"}:
        # разрешаем выгрузку и промежуточную, но лучше после
        pass
    paths = build_reports(db, audit)
    key = {"doc": "docx", "docx": "docx", "xls": "xlsx", "xlsx": "xlsx", "bov": "bov"}.get(kind)
    if not key:
        raise HTTPException(status_code=400, detail="Формат: doc, xls, bov")
    path = Path(paths[key])
    media = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "bov": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[key]
    filename = {
        "docx": f"otchet_{audit.id}.docx",
        "xlsx": f"otchet_{audit.id}.xlsx",
        "bov": f"vedomost_obemov_{audit.id}.xlsx",
    }[key]
    return FileResponse(path, media_type=media, filename=filename)


@router.delete("/audits/{audit_id}")
def audits_delete(
    audit_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    audit = _get_audit(db, audit_id, user)
    if user.role not in {"admin", "engineer"}:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    folder = settings.uploads_dir / str(audit.id)
    shutil.rmtree(folder, ignore_errors=True)
    db.delete(audit)
    db.commit()
    return {"ok": True}


def _get_audit(db: Session, audit_id: int, user: User) -> Audit:
    audit = db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Проверка не найдена")
    if user.role == "viewer" and audit.created_by != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа к этой проверке")
    return audit


def _file_out(f: AuditFile) -> dict[str, Any]:
    return {
        "id": f.id,
        "filename": f.filename,
        "ext": f.ext,
        "classified_as": f.classified_as,
        "user_class": f.user_class,
        "size": f.size,
        "parse_status": f.parse_status,
        "parse_notes": f.parse_notes,
    }


def _audit_brief(a: Audit) -> dict[str, Any]:
    return {
        "id": a.id,
        "title": a.title,
        "object_name": a.object_name,
        "systems": loads(a.systems_json, []),
        "mode": a.mode,
        "models": loads(a.models_json, []),
        "status": a.status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "finished_at": a.finished_at.isoformat() if a.finished_at else None,
        "summary": loads(a.summary_json, {}),
    }


def _audit_full(db: Session, a: Audit) -> dict[str, Any]:
    files = db.query(AuditFile).filter(AuditFile.audit_id == a.id).all()
    checks = db.query(CheckResult).filter(CheckResult.audit_id == a.id).order_by(CheckResult.id).all()
    findings = db.query(Finding).filter(Finding.audit_id == a.id).order_by(Finding.id).all()
    data = _audit_brief(a)
    data.update(
        {
            "error_text": a.error_text,
            "files": [_file_out(f) for f in files],
            "checks": [
                {
                    "code": c.check_code,
                    "title": c.title,
                    "status": c.status,
                    "reason": c.reason,
                }
                for c in checks
            ],
            "findings": [
                {
                    "id": f.id,
                    "check_code": f.check_code,
                    "severity": f.severity,
                    "title": f.title,
                    "description": f.description,
                    "ntd_refs": loads(f.ntd_refs, []),
                    "evidence": f.evidence,
                    "location": f.location,
                }
                for f in findings
            ],
        }
    )
    return data
