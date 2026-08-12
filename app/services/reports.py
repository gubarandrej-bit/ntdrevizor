from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Audit, AuditFile, CheckResult, Finding
from app.util import loads


def build_reports(db: Session, audit: Audit) -> dict[str, str]:
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    base = f"audit_{audit.id}_{stamp}"
    docx_path = settings.reports_dir / f"{base}.docx"
    xlsx_path = settings.reports_dir / f"{base}.xlsx"
    bov_path = settings.reports_dir / f"{base}_bov.xlsx"
    _write_docx(db, audit, docx_path)
    _write_xlsx(db, audit, xlsx_path)
    _write_bov(db, audit, bov_path)
    return {
        "docx": str(docx_path),
        "xlsx": str(xlsx_path),
        "bov": str(bov_path),
    }


def _meta(db: Session, audit: Audit):
    findings = (
        db.query(Finding).filter(Finding.audit_id == audit.id).order_by(Finding.id).all()
    )
    checks = (
        db.query(CheckResult).filter(CheckResult.audit_id == audit.id).order_by(CheckResult.id).all()
    )
    files = db.query(AuditFile).filter(AuditFile.audit_id == audit.id).all()
    crit = [f for f in findings if f.severity == "critical"]
    non = [f for f in findings if f.severity == "noncritical"]
    info = [f for f in findings if f.severity == "info"]
    return findings, checks, files, crit, non, info


def _write_docx(db: Session, audit: Audit, path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    findings, checks, files, crit, non, info = _meta(db, audit)
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(1.6)

    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    style._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")

    h = doc.add_heading("ОТЧЁТ О ПРОВЕРКЕ РАБОЧЕЙ ДОКУМЕНТАЦИИ", level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph("Система «Ревизор НТД»").alignment = WD_ALIGN_PARAGRAPH.CENTER

    p = doc.add_paragraph()
    p.add_run("Объект: ").bold = True
    p.add_run(audit.object_name or "не указан")
    p = doc.add_paragraph()
    p.add_run("Наименование проверки: ").bold = True
    p.add_run(audit.title)
    p = doc.add_paragraph()
    p.add_run("Системы: ").bold = True
    p.add_run(", ".join(loads(audit.systems_json, [])) or "не указаны")
    p = doc.add_paragraph()
    p.add_run("Режим / модели: ").bold = True
    p.add_run(f"{audit.mode}; {', '.join(loads(audit.models_json, [])) or 'без ИИ'}")
    p = doc.add_paragraph()
    p.add_run("Дата: ").bold = True
    p.add_run((audit.finished_at or audit.created_at).strftime("%d.%m.%Y %H:%M"))

    doc.add_heading("1. Выводы", level=1)
    if crit:
        doc.add_paragraph(
            f"Выявлено критических замечаний: {len(crit)}. "
            "Документация не может считаться соответствующей НТД без устранения этих замечаний "
            "либо предоставления недостающих исходных данных."
        )
    elif non:
        doc.add_paragraph(
            f"Критических замечаний нет. Некритических: {len(non)}. "
            "Требуется уточнение и устранение замечаний до выпуска в производство работ."
        )
    else:
        doc.add_paragraph(
            "По выполненным проверкам критических и некритических замечаний не зафиксировано. "
            "Это не означает проверку того, что не проводилось — см. раздел 4."
        )

    skipped = [c for c in checks if c.status == "skipped"]
    if skipped:
        doc.add_paragraph(
            f"Не проводилось проверок: {len(skipped)}. Причины указаны в разделе 4. "
            "По непроведённым проверкам выводы о соответствии не делались."
        )

    doc.add_heading("2. Критические замечания", level=1)
    if not crit:
        doc.add_paragraph("Критические замечания отсутствуют.")
    else:
        _docx_findings(doc, crit)

    doc.add_heading("3. Некритические замечания", level=1)
    if not non:
        doc.add_paragraph("Некритические замечания отсутствуют.")
    else:
        _docx_findings(doc, non)

    if info:
        doc.add_heading("3.1. Сведения (не являются замечаниями)", level=2)
        _docx_findings(doc, info)

    doc.add_heading("4. Перечень проверок", level=1)
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    hdr[0].text = "Код"
    hdr[1].text = "Проверка"
    hdr[2].text = "Статус"
    hdr[3].text = "Причина, если не проводилась"
    status_ru = {"done": "выполнена", "skipped": "не проводилась", "pending": "не начата", "error": "ошибка"}
    for c in checks:
        row = table.add_row().cells
        row[0].text = c.check_code
        row[1].text = c.title
        row[2].text = status_ru.get(c.status, c.status)
        row[3].text = c.reason or ""

    doc.add_heading("5. Исходные файлы", level=1)
    for f in files:
        doc.add_paragraph(
            f"{f.filename} — класс: {f.classified_as}; разбор: {f.parse_status}"
            + (f"; {f.parse_notes}" if f.parse_notes else ""),
            style="List Bullet",
        )

    doc.add_paragraph()
    note = doc.add_paragraph()
    note.add_run(
        "Правило отчёта: при отсутствии исходных данных сведения не выдумывались. "
        "Каждое замечание сопровождается ссылкой на пункт НТД, если он известен. "
        "Актуальность НТД — по локальному каталогу на дату проверки с попыткой сетевого зондирования источников."
    ).italic = True

    doc.save(path)


def _docx_findings(doc, items: list[Finding]) -> None:
    for i, f in enumerate(items, 1):
        refs = ", ".join(loads(f.ntd_refs, [])) or "пункт НТД не указан (не выдумывался)"
        p = doc.add_paragraph()
        run = p.add_run(f"{i}. {f.title}")
        run.bold = True
        doc.add_paragraph(f.description)
        doc.add_paragraph(f"НТД: {refs}")
        if f.evidence:
            doc.add_paragraph(f"Основание: {f.evidence}")
        if f.location:
            doc.add_paragraph(f"Файл: {f.location}")


def _write_xlsx(db: Session, audit: Audit, path: Path) -> None:
    findings, checks, files, crit, non, info = _meta(db, audit)
    wb = Workbook()

    ws = wb.active
    ws.title = "Сводка"
    _header_fill = PatternFill("solid", fgColor="1B2430")
    ws["A1"] = "Отчёт о проверке документации — Ревизор НТД"
    ws["A1"].font = Font(bold=True, size=14, color="F2E6C8")
    ws.merge_cells("A1:B1")
    rows = [
        ("Объект", audit.object_name or ""),
        ("Проверка", audit.title),
        ("Системы", ", ".join(loads(audit.systems_json, []))),
        ("Режим", audit.mode),
        ("Модели", ", ".join(loads(audit.models_json, []))),
        ("Дата", (audit.finished_at or audit.created_at).isoformat(sep=" ", timespec="minutes")),
        ("Критических", len(crit)),
        ("Некритических", len(non)),
        ("Сведений", len(info)),
        ("Проверок выполнено", sum(1 for c in checks if c.status == "done")),
        ("Проверок не проводилось", sum(1 for c in checks if c.status == "skipped")),
    ]
    for i, (k, v) in enumerate(rows, 3):
        ws[f"A{i}"] = k
        ws[f"B{i}"] = v
        ws[f"A{i}"].font = Font(bold=True, color="D4A054")
        ws[f"B{i}"].font = Font(color="E8EEF6")
    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 80
    ws.sheet_properties.tabColor = "D4A054"

    ws2 = wb.create_sheet("Замечания")
    headers = ["№", "Степень", "Проверка", "Заголовок", "Описание", "НТД", "Основание", "Файл"]
    for col, h in enumerate(headers, 1):
        cell = ws2.cell(1, col, h)
        cell.font = Font(bold=True, color="F2E6C8")
        cell.fill = _header_fill
    for i, f in enumerate(findings, 1):
        vals = [
            i,
            {"critical": "критическое", "noncritical": "некритическое", "info": "сведение"}.get(
                f.severity, f.severity
            ),
            f.check_code,
            f.title,
            f.description,
            "; ".join(loads(f.ntd_refs, [])),
            f.evidence,
            f.location,
        ]
        for col, v in enumerate(vals, 1):
            cell = ws2.cell(i + 1, col, v)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if f.severity == "critical":
                cell.fill = PatternFill("solid", fgColor="F8D0D0")
            elif f.severity == "noncritical":
                cell.fill = PatternFill("solid", fgColor="F8EBC0")
        ws2.row_dimensions[i + 1].height = 48
    widths = [6, 16, 22, 40, 60, 36, 36, 24]
    for i, w in enumerate(widths, 1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    ws3 = wb.create_sheet("Проверки")
    for col, h in enumerate(["Код", "Наименование", "Статус", "Причина непроведения"], 1):
        cell = ws3.cell(1, col, h)
        cell.font = Font(bold=True, color="F2E6C8")
        cell.fill = _header_fill
    ru = {"done": "выполнена", "skipped": "не проводилась", "pending": "не начата", "error": "ошибка"}
    for i, c in enumerate(checks, 1):
        ws3.cell(i + 1, 1, c.check_code)
        ws3.cell(i + 1, 2, c.title)
        ws3.cell(i + 1, 3, ru.get(c.status, c.status))
        ws3.cell(i + 1, 4, c.reason or "")
        if c.status == "skipped":
            for col in range(1, 5):
                ws3.cell(i + 1, col).fill = PatternFill("solid", fgColor="E6E6E6")
    for i, w in enumerate([24, 70, 20, 70], 1):
        ws3.column_dimensions[get_column_letter(i)].width = w

    ws4 = wb.create_sheet("Файлы")
    for col, h in enumerate(["Файл", "Класс", "Разбор", "Примечание", "Размер, байт"], 1):
        cell = ws4.cell(1, col, h)
        cell.font = Font(bold=True, color="F2E6C8")
        cell.fill = _header_fill
    for i, f in enumerate(files, 1):
        ws4.cell(i + 1, 1, f.filename)
        ws4.cell(i + 1, 2, f.classified_as)
        ws4.cell(i + 1, 3, f.parse_status)
        ws4.cell(i + 1, 4, f.parse_notes)
        ws4.cell(i + 1, 5, f.size)
    for i, w in enumerate([40, 22, 14, 70, 16], 1):
        ws4.column_dimensions[get_column_letter(i)].width = w

    wb.save(path)


def _write_bov(db: Session, audit: Audit, path: Path) -> None:
    """Ведомость объёмов — только из разобранных спецификации и журнала. Ничего не добавляется."""
    from collections import defaultdict

    files = db.query(AuditFile).filter(AuditFile.audit_id == audit.id).all()
    items = []
    cables = []
    sources = []
    for f in files:
        data = loads(f.extracted_json, {})
        if f.classified_as == "specification":
            items.extend(data.get("items") or data.get("equipment") or [])
            sources.append(f.filename)
        if f.classified_as == "cable_journal":
            cables.extend(data.get("cables") or data.get("items") or [])
            sources.append(f.filename)

    wb = Workbook()
    ws = wb.active
    ws.title = "ВОР"
    ws["A1"] = "Ведомость объёмов работ"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (
        "Сформирована только по разобранным строкам спецификации и кабельного журнала. "
        "Нормы расхода, коэффициенты и работы, которых нет во входных данных, не добавлялись."
    )
    ws.merge_cells("A2:G2")
    ws["A3"] = "Источники: " + (", ".join(sources) if sources else "нет разобранных спецификации/журнала")

    headers = ["№", "Наименование работ / ресурсов", "Ед.", "Кол-во", "Марка / тип", "Основание", "Примечание"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(5, col, h)
        cell.font = Font(bold=True, color="F2E6C8")
        cell.fill = PatternFill("solid", fgColor="1B2430")

    row_i = 6
    n = 1
    if not items and not cables:
        ws.cell(row_i, 1, "—")
        ws.merge_cells(start_row=row_i, start_column=2, end_row=row_i, end_column=7)
        ws.cell(row_i, 2, "Исходных данных для ведомости объёмов нет. Файл не выдумывался.")
    else:
        for it in items:
            name = it.get("name") or it.get("mark") or ""
            if not name:
                continue
            qty = it.get("qty") if it.get("qty") is not None else it.get("length")
            ws.cell(row_i, 1, n)
            ws.cell(row_i, 2, f"Поставка/монтаж: {name}")
            ws.cell(row_i, 3, it.get("unit") or ("м" if it.get("length") else "шт"))
            ws.cell(row_i, 4, qty if qty is not None else "нет данных")
            ws.cell(row_i, 5, it.get("mark") or it.get("type") or "")
            ws.cell(row_i, 6, "спецификация")
            ws.cell(row_i, 7, it.get("note") or "")
            row_i += 1
            n += 1
        # кабели журнала, которых могло не быть как отдельных монтажных строк
        by_mark: dict[str, float] = defaultdict(float)
        unit_mark: dict[str, str] = {}
        missing = 0
        for c in cables:
            key = (c.get("mark") or c.get("name") or "").strip()
            if not key:
                continue
            val = c.get("length") if c.get("length") is not None else c.get("qty")
            if val is None:
                missing += 1
                continue
            by_mark[key] += float(val)
            unit_mark[key] = "м"
        if by_mark:
            ws.cell(row_i, 2, "Кабельные трассы по журналу (сумма длин)")
            ws.cell(row_i, 2).font = Font(bold=True)
            row_i += 1
            for key, val in sorted(by_mark.items()):
                ws.cell(row_i, 1, n)
                ws.cell(row_i, 2, f"Прокладка кабеля {key}")
                ws.cell(row_i, 3, unit_mark[key])
                ws.cell(row_i, 4, round(val, 2))
                ws.cell(row_i, 5, key)
                ws.cell(row_i, 6, "кабельный журнал")
                row_i += 1
                n += 1
        if missing:
            ws.cell(row_i, 2, f"Строк журнала без длины (не вошли в сумму): {missing}")
            row_i += 1

    for i, w in enumerate([6, 55, 10, 14, 28, 22, 30], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    wb.save(path)
