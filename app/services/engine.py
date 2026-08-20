from __future__ import annotations

import json
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import ROOT_DIR, settings
from app.models import Audit, AuditFile, CheckResult, DialogMessage, Finding, Setting
from app.services import ai as ai_svc
from app.services.checks import (
    _is_cable_item,
    all_items,
    all_text,
    check_battery,
    check_cable_mark,
    check_cable_section,
    check_completeness,
    check_laying,
    check_outdated_ntd_refs,
    check_plan_lengths,
    check_power_source,
    check_protection,
    check_scheme_vs_spec,
    check_spec_journal_names,
    check_spec_journal_qty,
    check_spz_category,
)
from app.services.classify import classify_file
from app.services.ntd import check_actuality, clauses_for_prompt, find_outdated_refs
from app.services.parsers import parse_file
from app.util import dumps, loads, truncate


Listener = Callable[[str, dict[str, Any]], None]


def emit(listener: Listener | None, text: str, **meta):
    if listener:
        listener(text, meta)


def log_dialog(db: Session, audit_id: int, role: str, text: str, meta: dict | None = None):
    db.add(
        DialogMessage(
            audit_id=audit_id, role=role, text=text, meta_json=dumps(meta or {})
        )
    )
    db.commit()


def run_audit(db: Session, audit_id: int, listener: Listener | None = None) -> None:
    audit = db.get(Audit, audit_id)
    if not audit:
        return
    audit.status = "running"
    audit.error_text = ""
    db.commit()

    def talk(text: str, **meta):
        log_dialog(db, audit_id, "system", text, meta)
        emit(listener, text, **meta)

    try:
        _run(db, audit, talk)
        audit = db.get(Audit, audit_id)
        audit.status = "done"
        audit.finished_at = datetime.utcnow()
        audit.summary_json = dumps(_summary(db, audit))
        db.commit()
        talk("Проверка завершена. Сформированы замечания и отчёт.")
    except Exception as exc:
        db.rollback()
        audit = db.get(Audit, audit_id)
        if audit:
            audit.status = "error"
            audit.error_text = f"{exc}\n{traceback.format_exc()}"
            audit.finished_at = datetime.utcnow()
            db.commit()
        talk(f"Сбой выполнения: {exc}. Никакие недостающие выводы не подставлялись.")


def _run(db: Session, audit: Audit, talk: Callable) -> None:
    systems = loads(audit.systems_json, [])
    mode = audit.mode
    models = loads(audit.models_json, [])
    talk(f"Старт проверки «{audit.title}». Режим: {mode}. Системы: {', '.join(systems) or 'не заданы'}.")

    # перечень проверок
    catalog = _check_catalog()
    _ensure_check_rows(db, audit, catalog)
    talk("Перечень проверок сформирован. Далее — только по наличию исходных данных.")

    files = list(audit.files)
    parsed: list[dict[str, Any]] = []
    for f in files:
        talk(f"Разбор файла: {f.filename}")
        path = Path(f.stored_path)
        extracted = parse_file(path) if path.exists() else {
            "ok": False,
            "error": "Файл отсутствует на диске",
            "text": "",
        }
        sample = extracted.get("text") or ""
        cls = classify_file(f.filename, sample, f.user_class or "auto")
        f.classified_as = cls
        f.parse_status = "ok" if extracted.get("ok") else "error"
        f.parse_notes = extracted.get("error") or extracted.get("notes") or ""
        f.extracted_json = dumps(_light_extracted(extracted))
        db.commit()
        parsed.append(
            {
                "id": f.id,
                "filename": f.filename,
                "classified_as": cls,
                "parse_notes": f.parse_notes,
                "extracted": extracted,
            }
        )
        if extracted.get("ok"):
            talk(f"  тип: {cls}; {extracted.get('notes') or 'разбор выполнен'}")
        else:
            talk(f"  не разобран ({cls}): {extracted.get('error')}")

    spec_files = [p for p in parsed if p["classified_as"] == "specification"]
    journal_files = [p for p in parsed if p["classified_as"] == "cable_journal"]
    scheme_files = [
        p
        for p in parsed
        if p["classified_as"] in {"scheme_electrical", "scheme_structural", "scheme", "connections"}
    ]
    plan_files = [p for p in parsed if p["classified_as"] == "plan"]
    calc_files = [p for p in parsed if p["classified_as"] == "calculation"]

    # Схемы и планы могут быть разделами объединённого PDF/DOC (такой файл
    # классифицируется как specification). Распознаём их по маркерам в тексте
    # страниц и передаём в соответствующие проверки «нарезанный» текст.
    if not scheme_files:
        scheme_files = [
            {**p, "classified_as": "scheme", "extracted": _slice_pages(p.get("extracted") or {}, _SCHEME_PAGE_MARKERS)}
            for p in parsed
            if _text_has_any((p.get("extracted") or {}).get("text") or "", _SCHEME_PAGE_MARKERS)
        ]
    if not plan_files:
        plan_files = [
            {**p, "classified_as": "plan", "extracted": _slice_pages(p.get("extracted") or {}, _PLAN_PAGE_MARKERS)}
            for p in parsed
            if _text_has_any((p.get("extracted") or {}).get("text") or "", _PLAN_PAGE_MARKERS)
        ]

    spec_items = all_items(spec_files) or all_items(
        [p for p in parsed if p["classified_as"] == "unknown"], "items"
    )
    # если спецификация не классифицировалась, но есть items — используем все items
    if not spec_items:
        spec_items = all_items(parsed, "items")
    full_text = all_text(parsed)
    journal_items = all_items(journal_files, "cables") or all_items(journal_files, "items")
    # кабельный журнал может быть разделом внутри объединённого PDF/DOC
    # (такой файл классифицируется как specification). Тогда берём строки с
    # длиной/направлением из всех файлов как записи журнала.
    if not journal_items:
        low = full_text.lower()
        if any(k in low for k in ("кабельный журнал", "направление кабеля", "потребность кабелей", "монтажная единица")):
            # строки с длиной/направлением — это трассы кабельного журнала
            journal_items = [
                i
                for i in all_items(parsed, "cables")
                if i.get("length") is not None or i.get("from") or i.get("to")
            ]
            if not journal_items:
                journal_items = [
                    i
                    for i in all_items(parsed, "items")
                    if (i.get("length") is not None or i.get("from") or i.get("to"))
                    and _is_cable_item(i)
                ]
    all_cables = journal_items + [i for i in spec_items if i]
    calc_text = all_text(calc_files) or full_text

    tol_qty = float(_setting(db, "qty_tolerance_pct", "5") or 5)
    tol_len = float(_setting(db, "length_tolerance_pct", "10") or 10)

    # 1 актуальность НТД
    talk("Проверка актуальности НТД…")
    actual = check_actuality(db, systems)
    act_findings = []
    replaced_in_catalog = [d["code"] for d in actual["documents"] if d["status"] == "replaced"]
    if replaced_in_catalog:
        act_findings.append(
            {
                "severity": "info",
                "title": "В каталоге хранятся заменённые редакции (справочно)",
                "description": (
                    "Это не замечание к проекту. Заменённые документы оставлены, чтобы ловить устаревшие ссылки. "
                    + ", ".join(replaced_in_catalog)
                ),
                "ntd_refs": replaced_in_catalog,
                "evidence": "",
                "location": "",
            }
        )
    for d in actual["documents"]:
        if d["status"] == "check":
            act_findings.append(
                {
                    "severity": "info",
                    "title": f"Статус {d['code']} не подтверждён онлайн",
                    "description": d["note"],
                    "ntd_refs": [d["code"]],
                    "evidence": "",
                    "location": "",
                }
            )
    if not actual["online"]:
        act_findings.append(
            {
                "severity": "info",
                "title": "Онлайн-проверка НТД не подтвердила карточки документов",
                "description": actual.get("online_note")
                or "Сеть недоступна. Использован только локальный каталог.",
                "ntd_refs": [],
                "evidence": "",
                "location": "",
            }
        )
    _store(db, audit, "NTD_ACTUALITY", "done", "", act_findings)
    talk(f"  документов в выборке: {len(actual['documents'])}; онлайн: {actual['online']}")

    outdated = find_outdated_refs(full_text, db)
    if outdated:
        talk(f"  в комплекте найдены ссылки на недействующие НТД: {len(outdated)}")

    # 2 комплектность
    talk("Комплектность документации…")
    r = check_completeness(parsed, systems)
    _store(db, audit, "DOC_COMPLETENESS", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "DOC_COMPLETENESS", r)

    # сверки
    talk("Сверка кабельного журнала и спецификации (наименования)…")
    r = check_spec_journal_names(spec_items, journal_items)
    _store(db, audit, "SPEC_VS_JOURNAL_NAMES", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "SPEC_VS_JOURNAL_NAMES", r)

    talk("Сверка количеств/длин журнала и спецификации…")
    r = check_spec_journal_qty(spec_items, journal_items, tol_qty)
    _store(db, audit, "SPEC_VS_JOURNAL_QTY", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "SPEC_VS_JOURNAL_QTY", r)

    talk("Сверка оборудования на схемах со спецификацией…")
    r = check_scheme_vs_spec(spec_items, scheme_files)
    _store(db, audit, "SCHEME_VS_SPEC", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "SCHEME_VS_SPEC", r)

    talk("Сравнение длин трасс на планах с журналом…")
    r = check_plan_lengths(journal_items, plan_files, tol_len)
    _store(db, audit, "PLAN_LENGTH_VS_JOURNAL", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "PLAN_LENGTH_VS_JOURNAL", r)

    talk("Проверка марок кабелей…")
    r = check_cable_mark(all_cables, systems, full_text)
    extra = check_outdated_ntd_refs(full_text, outdated)
    r["findings"] = list(r.get("findings") or []) + list(extra.get("findings") or [])
    _store(db, audit, "CABLE_MARK", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "CABLE_MARK", r)

    talk("Проверка сечений по нагрузке и ПУЭ…")
    r = check_cable_section(spec_items + journal_items, calc_text)
    _store(db, audit, "CABLE_SECTION", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "CABLE_SECTION", r)

    talk("Согласование аппаратов защиты и сечений…")
    r = check_protection(spec_items + journal_items, full_text)
    _store(db, audit, "PROTECTION_COORD", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "PROTECTION_COORD", r)

    talk("Способы прокладки…")
    r = check_laying(journal_items)
    _store(db, audit, "LAYING_METHOD", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "LAYING_METHOD", r)

    talk("Огнестойкость линий СПЗ…")
    if any(s in {"PS", "SOUE", "PT"} for s in systems):
        # не дублируем CABLE_MARK: оставляем только FR, если они уже есть в текущих findings
        existing = {
            (f.title, f.evidence)
            for f in db.query(Finding).filter(Finding.audit_id == audit.id, Finding.check_code == "CABLE_MARK")
        }
        r = check_cable_mark(all_cables, [s for s in systems if s in {"PS", "SOUE", "PT"}], full_text)
        fr = [
            f
            for f in r.get("findings", [])
            if "FR" in f.get("title", "") or "огнест" in f.get("title", "").lower()
        ]
        # если уже зафиксировано в CABLE_MARK — не плодим копии
        fr = [f for f in fr if (f.get("title"), f.get("evidence")) not in existing]
        _store(db, audit, "SPZ_FIRE_RESIST", "done", "", fr)
        talk(f"  отдельных замечаний FR (сверх проверки марок): {len(fr)}")
    else:
        _store(db, audit, "SPZ_FIRE_RESIST", "skipped", "Системы противопожарной защиты не выбраны.", [])
        talk("  не проводилась: системы СПЗ не выбраны.")

    talk("Расчёт источников питания…")
    r = check_power_source(spec_items + journal_items, calc_text)
    _store(db, audit, "POWER_SOURCE", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "POWER_SOURCE", r)

    talk("Подбор аккумуляторов…")
    r = check_battery(spec_items, calc_text, systems)
    _store(db, audit, "BATTERY", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "BATTERY", r)

    talk("Категория электроснабжения СПЗ…")
    r = check_spz_category(full_text, systems)
    _store(db, audit, "SPZ_POWER_CATEGORY", r["status"], r.get("reason", ""), r.get("findings", []))
    _announce(talk, "SPZ_POWER_CATEGORY", r)

    # ИИ-проверки
    model_id, why = ai_svc.pick_model(mode, models)
    ai_targets = [
        ("ATTACHED_CALCS", "прилагаемые расчёты", calc_files, "calculation"),
        ("ELEC_SCHEME", "электрические схемы", [p for p in scheme_files if p["classified_as"] in {"scheme_electrical", "scheme", "connections"}], "scheme_electrical"),
        ("STRUCT_SCHEME", "структурные схемы", [p for p in scheme_files if p["classified_as"] in {"scheme_structural", "scheme"}], "scheme_structural"),
        ("CONNECTIONS", "электрические подключения", scheme_files + [p for p in parsed if p["classified_as"] == "connections"], "connections"),
    ]
    if model_id:
        talk(f"ИИ-модель: {model_id}. Правила: не выдумывать, запрашивать недостающее, фиксировать непроведённое.")
    else:
        talk(f"ИИ недоступен: {why} Проверки схем/подключений/качественного разбора расчётов будут помечены как не проведённые.")

    ntd_ctx = clauses_for_prompt(db, systems)
    for code, title, subset, kind in ai_targets:
        talk(f"ИИ-проверка: {title}…")
        if not subset and code != "CONNECTIONS":
            _store(db, audit, code, "skipped", f"Нет файлов класса «{title}».", [])
            talk(f"  не проводилась: нет исходных файлов ({title}).")
            continue
        if code == "CONNECTIONS" and not subset:
            _store(db, audit, code, "skipped", "Нет схем и таблиц подключений.", [])
            talk("  не проводилась: нет схем/таблиц подключений.")
            continue
        if not model_id:
            _store(db, audit, code, "skipped", why, [])
            talk(f"  не проводилась: {why}")
            continue
        # если файлы не разобраны
        texts = []
        for p in subset:
            ext = p.get("extracted") or {}
            if ext.get("ok") and ext.get("text"):
                texts.append(f"### {p['filename']}\n{truncate(ext['text'], 6000)}")
            else:
                texts.append(f"### {p['filename']}\nНЕ РАЗОБРАН: {ext.get('error') or p.get('parse_notes')}")
        if all("НЕ РАЗОБРАН" in t and len(t) < 400 for t in texts):
            _store(db, audit, code, "skipped", "Файлы не разобраны, текст для модели отсутствует.", [])
            talk("  не проводилась: файлы не разобраны.")
            continue
        prompt = _ai_prompt(audit, systems, title, kind, "\n\n".join(texts), ntd_ctx)
        result = ai_svc.complete(model_id, prompt)
        if not result.get("ok"):
            _store(db, audit, code, "skipped", f"Модель не ответила: {result.get('error')}", [])
            talk(f"  не проводилась: {result.get('error')}")
            continue
        parsed_json = ai_svc.parse_json_response(result["text"])
        findings = []
        if parsed_json:
            for fnd in parsed_json.get("findings") or []:
                findings.append(
                    {
                        "severity": fnd.get("severity") or "noncritical",
                        "title": fnd.get("title") or "Замечание ИИ",
                        "description": fnd.get("description") or "",
                        "ntd_refs": fnd.get("ntd_refs") or [],
                        "evidence": fnd.get("evidence") or "",
                        "location": "",
                    }
                )
            for sk in parsed_json.get("skipped") or []:
                findings.append(
                    {
                        "severity": "info",
                        "title": f"ИИ не провёл: {sk.get('check') or 'фрагмент'}",
                        "description": sk.get("reason") or "Причина не указана моделью.",
                        "ntd_refs": [],
                        "evidence": "",
                        "location": "",
                    }
                )
            for q in parsed_json.get("questions") or []:
                findings.append(
                    {
                        "severity": "info",
                        "title": "Запрошены недостающие данные",
                        "description": str(q),
                        "ntd_refs": [],
                        "evidence": "",
                        "location": "",
                    }
                )
                talk(f"  запрос данных: {q}")
        else:
            findings.append(
                {
                    "severity": "info",
                    "title": "Ответ модели без структурированных замечаний",
                    "description": truncate(result["text"], 2500),
                    "ntd_refs": [],
                    "evidence": "Модель не вернула JSON. Текст сохранён как есть, выводы не домысливались.",
                    "location": "",
                }
            )
        _store(db, audit, code, "done", "", findings)
        talk(f"  модель {model_id}, замечаний/сообщений: {len(findings)}")


def _ai_prompt(audit: Audit, systems: list[str], title: str, kind: str, payload: str, ntd_ctx: str) -> str:
    return (
        f"Объект: {audit.object_name or 'не указан'}\n"
        f"Проект: {audit.title}\n"
        f"Системы: {', '.join(systems)}\n"
        f"Задача: {title} (тип {kind}).\n\n"
        "Работай ТОЛЬКО по приведённому фрагменту. Не дополняй типовыми решениями.\n"
        "Если данных мало — findings пустой, questions заполнен, skipped с причиной.\n\n"
        "Известные пункты НТД (неполный конспект, не выдумывай другие номера пунктов):\n"
        f"{ntd_ctx}\n\n"
        "Фрагмент документации:\n"
        f"{payload}\n"
    )


def _announce(talk, code: str, result: dict):
    if result.get("status") == "skipped":
        talk(f"  не проводилась: {result.get('reason')}")
    else:
        n = len(result.get("findings") or [])
        talk(f"  выполнена, записей: {n}")


def _store(db: Session, audit: Audit, code: str, status: str, reason: str, findings: list[dict]):
    row = (
        db.query(CheckResult)
        .filter(CheckResult.audit_id == audit.id, CheckResult.check_code == code)
        .first()
    )
    now = datetime.utcnow()
    if row:
        row.status = status
        row.reason = reason or ""
        row.details_json = dumps({"findings_count": len(findings)})
        row.finished_at = now
        if row.started_at is None:
            row.started_at = now
    for fnd in findings:
        db.add(
            Finding(
                audit_id=audit.id,
                check_code=code,
                severity=fnd.get("severity") or "noncritical",
                title=fnd.get("title") or "",
                description=fnd.get("description") or "",
                ntd_refs=dumps(fnd.get("ntd_refs") or []),
                evidence=fnd.get("evidence") or "",
                location=fnd.get("location") or "",
            )
        )
    db.commit()


def _ensure_check_rows(db: Session, audit: Audit, catalog: dict):
    existing = {c.check_code for c in audit.checks}
    for item in catalog.get("checks", []):
        if item["code"] in existing:
            continue
        db.add(
            CheckResult(
                audit_id=audit.id,
                check_code=item["code"],
                title=item["title"],
                status="pending",
                started_at=datetime.utcnow(),
            )
        )
    db.commit()


def _check_catalog() -> dict:
    path = ROOT_DIR / "data" / "check_catalog.json"
    if not path.exists():
        path = settings.data_dir / "check_catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _setting(db: Session, key: str, default: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else default


_SCHEME_PAGE_MARKERS = (
    "схема структурн", "схема электрич", "схема принципиальн",
    "схема функциональн", "схема подключен",
)
_PLAN_PAGE_MARKERS = (
    "план расположен", "план прокладк", "план трасс",
)


def _text_has_any(text: str, markers: tuple[str, ...]) -> bool:
    low = (text or "").lower()
    return any(m in low for m in markers)


def _slice_pages(extracted: dict, markers: tuple[str, ...]) -> dict:
    """Оставляет в extracted только текст страниц, содержащих маркеры.

    Используется, когда схема/план — раздел объединённого PDF/DOC: проверкам
    передаётся текст именно этих страниц, а не всего документа (иначе
    спецификация «подтверждала бы» сама себя).
    """
    data = dict(extracted)
    text = data.get("text") or ""
    parts = [
        block for block in re.split(r"(?=--- страница \d+ ---\n)", text)
        if _text_has_any(block, markers)
    ]
    data["text"] = "\n".join(parts).strip()
    data["tables"] = []
    data["items"] = []
    data["cables"] = []
    data["equipment"] = []
    data["texts_geom"] = []
    # lengths (геометрия планов) сохраняем — они уже отфильтрованы по страницам-планам
    return data


def _light_extracted(extracted: dict) -> dict:
    # не храним гигантские geom
    data = dict(extracted)
    if len(data.get("texts_geom") or []) > 200:
        data["texts_geom"] = data["texts_geom"][:200]
    if len(data.get("text") or "") > 80_000:
        data["text"] = data["text"][:80_000]
    return data


def _summary(db: Session, audit: Audit) -> dict[str, Any]:
    findings = db.query(Finding).filter(Finding.audit_id == audit.id).all()
    checks = db.query(CheckResult).filter(CheckResult.audit_id == audit.id).all()
    return {
        "critical": sum(1 for f in findings if f.severity == "critical"),
        "noncritical": sum(1 for f in findings if f.severity == "noncritical"),
        "info": sum(1 for f in findings if f.severity == "info"),
        "done_checks": sum(1 for c in checks if c.status == "done"),
        "skipped_checks": sum(1 for c in checks if c.status == "skipped"),
        "total_checks": len(checks),
    }


def answer_dialog(db: Session, audit: Audit, question: str) -> str:
    """Ответ в диалоге: сначала по фактам проверки, ИИ — только если выбран и доступен."""
    findings = db.query(Finding).filter(Finding.audit_id == audit.id).all()
    checks = db.query(CheckResult).filter(CheckResult.audit_id == audit.id).all()
    facts = []
    facts.append(f"Статус проверки: {audit.status}")
    facts.append(
        "Проверки: "
        + "; ".join(
            f"{c.check_code}={c.status}" + (f" ({c.reason})" if c.status == "skipped" else "")
            for c in checks
        )
    )
    if findings:
        facts.append("Замечания:")
        for f in findings[:40]:
            refs = ", ".join(loads(f.ntd_refs, []))
            facts.append(f"- [{f.severity}] {f.title}. {f.description} НТД: {refs}")
    else:
        facts.append("Замечаний нет либо проверка ещё не выполнялась.")
    fact_text = "\n".join(facts)
    models = loads(audit.models_json, [])
    model_id, why = ai_svc.pick_model(audit.mode, models)
    if not model_id:
        return (
            fact_text
            + "\n\nОтвет сформирован только по результатам уже выполненных проверок. "
            + (why or "ИИ не используется.")
        )
    prompt = (
        "Ответь на вопрос пользователя СТРОГО по фактам проверки. "
        "Ничего не додумывай. Если факта нет — скажи, что данных нет.\n\n"
        f"Факты:\n{truncate(fact_text, 8000)}\n\nВопрос: {question}"
    )
    result = ai_svc.complete(model_id, prompt)
    if not result.get("ok"):
        return fact_text + f"\n\nМодель недоступна ({result.get('error')}). Выше — только факты."
    return result["text"]
