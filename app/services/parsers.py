from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.util import (
    looks_like_cable,
    parse_float,
    parse_section,
    truncate,
)

MAX_TEXT = 200_000
MAX_ROWS = 8_000


def parse_file(path: Path) -> dict[str, Any]:
    ext = path.suffix.lower()
    if ext in {".xls", ".xlsx", ".xlsm"}:
        return parse_spreadsheet(path)
    if ext in {".doc", ".docx"}:
        return parse_document(path)
    if ext == ".pdf":
        return parse_pdf(path)
    if ext == ".dxf":
        return parse_dxf(path)
    if ext == ".dwg":
        return parse_dwg(path)
    if ext in {".txt", ".csv"}:
        return parse_text(path)
    return {
        "ok": False,
        "kind": "unknown",
        "error": f"Формат {ext or '(без расширения)'} не поддерживается. Допустимы: xls, xlsx, doc, docx, pdf, dwg, dxf.",
        "text": "",
        "tables": [],
        "items": [],
        "cables": [],
        "equipment": [],
        "lengths": [],
        "texts_geom": [],
    }


def parse_text(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if "\x00" in text[:200]:
        text = raw.decode("cp1251", errors="replace")
    return _base(ok=True, kind="text", text=text[:MAX_TEXT])


def parse_spreadsheet(path: Path) -> dict[str, Any]:
    try:
        import openpyxl
    except ImportError as exc:
        return _base(ok=False, kind="xls", error=f"openpyxl недоступен: {exc}")

    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        return _base(ok=False, kind="xls", error=f"Не удалось открыть книгу: {exc}")

    tables: list[dict[str, Any]] = []
    text_parts: list[str] = [f"Листы: {', '.join(wb.sheetnames)}"]
    items: list[dict[str, Any]] = []
    cables: list[dict[str, Any]] = []

    try:
        for sheet in wb.worksheets:
            rows = []
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i >= MAX_ROWS:
                    break
                values = [_cell(c) for c in row]
                if any(v != "" for v in values):
                    rows.append(values)
            if not rows:
                continue
            header_idx = _header_row(rows)
            headers = rows[header_idx] if header_idx is not None else []
            mapped = _map_headers(headers)
            records = []
            start = (header_idx + 1) if header_idx is not None else 0
            for row in rows[start:]:
                rec = {}
                if mapped:
                    for key, idx in mapped.items():
                        if idx < len(row):
                            rec[key] = row[idx]
                rec["_raw"] = row
                records.append(rec)
            tables.append(
                {
                    "sheet": sheet.title,
                    "headers": headers,
                    "mapped": mapped,
                    "records": records[:MAX_ROWS],
                }
            )
            text_parts.append(f"\n=== Лист {sheet.title} ===")
            for row in rows[:80]:
                text_parts.append(" | ".join(str(x) for x in row if x != ""))

            for rec in records:
                item = _record_to_item(rec, sheet.title)
                if not item:
                    continue
                items.append(item)
                if looks_like_cable(
                    " ".join(
                        str(item.get(k, ""))
                        for k in ("name", "mark", "type", "note")
                    )
                ) or rec.get("from") or rec.get("to") or rec.get("length"):
                    cables.append(item)
    finally:
        wb.close()

    return _base(
        ok=True,
        kind="xls",
        text="\n".join(text_parts)[:MAX_TEXT],
        tables=tables,
        items=items,
        cables=cables,
        equipment=[i for i in items if not looks_like_cable(i.get("name", "") + " " + i.get("mark", ""))],
    )


def parse_document(path: Path) -> dict[str, Any]:
    ext = path.suffix.lower()
    if ext == ".doc":
        converted = _convert_with_libreoffice(path, "docx")
        if converted is None:
            # last resort: strings
            raw = path.read_bytes()
            text = raw.decode("cp1251", errors="ignore")
            text = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in text)
            text = "\n".join(line.strip() for line in text.splitlines() if len(line.strip()) > 3)
            if len(text) < 40:
                return _base(
                    ok=False,
                    kind="doc",
                    error="Формат .doc (OLE) не разобран: LibreOffice/soffice не найден. Сохраните файл как .docx.",
                )
            return _base(
                ok=True,
                kind="doc",
                text=text[:MAX_TEXT],
                notes="Текст извлечён грубо из .doc без LibreOffice. Таблицы не разобраны.",
            )
        path = converted

    try:
        import docx
    except ImportError as exc:
        return _base(ok=False, kind="docx", error=f"python-docx недоступен: {exc}")

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        return _base(ok=False, kind="docx", error=f"Не удалось открыть документ: {exc}")

    paras = [p.text.strip() for p in document.paragraphs if p.text and p.text.strip()]
    tables: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    cables: list[dict[str, Any]] = []
    for ti, table in enumerate(document.tables):
        rows = []
        for row in table.rows:
            rows.append([c.text.strip() for c in row.cells])
        if not rows:
            continue
        header_idx = _header_row(rows)
        headers = rows[header_idx] if header_idx is not None else rows[0]
        mapped = _map_headers(headers)
        records = []
        start = (header_idx + 1) if header_idx is not None else 1
        for row in rows[start:]:
            rec = {}
            for key, idx in mapped.items():
                if idx < len(row):
                    rec[key] = row[idx]
            rec["_raw"] = row
            records.append(rec)
            item = _record_to_item(rec, f"таблица {ti + 1}")
            if item:
                items.append(item)
                if looks_like_cable(item.get("name", "") + " " + item.get("mark", "")):
                    cables.append(item)
        tables.append({"sheet": f"table_{ti + 1}", "headers": headers, "mapped": mapped, "records": records})

    return _base(
        ok=True,
        kind="docx",
        text="\n".join(paras)[:MAX_TEXT],
        tables=tables,
        items=items,
        cables=cables,
        equipment=[i for i in items if not looks_like_cable(i.get("name", "") + i.get("mark", ""))],
    )


def parse_pdf(path: Path) -> dict[str, Any]:
    text_parts: list[str] = []
    tables: list[dict[str, Any]] = []
    notes = []
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None  # type: ignore

    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(path)) as pdf:
                for i, page in enumerate(pdf.pages[:80]):
                    t = page.extract_text() or ""
                    if t.strip():
                        text_parts.append(f"--- страница {i + 1} ---\n{t}")
                    try:
                        extracted = page.extract_tables() or []
                    except Exception:
                        extracted = []
                    for ti, table in enumerate(extracted):
                        if not table:
                            continue
                        rows = [[_cell(c) for c in row] for row in table if row]
                        if not rows:
                            continue
                        headers = rows[0]
                        mapped = _map_headers(headers)
                        records = []
                        for row in rows[1:]:
                            rec = {k: row[idx] if idx < len(row) else "" for k, idx in mapped.items()}
                            rec["_raw"] = row
                            records.append(rec)
                        tables.append(
                            {
                                "sheet": f"p{i + 1}_t{ti + 1}",
                                "headers": headers,
                                "mapped": mapped,
                                "records": records,
                            }
                        )
        except Exception as exc:
            notes.append(f"pdfplumber: {exc}")

    if not text_parts:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            for i, page in enumerate(reader.pages[:80]):
                t = page.extract_text() or ""
                if t.strip():
                    text_parts.append(f"--- страница {i + 1} ---\n{t}")
        except Exception as exc:
            notes.append(f"pypdf: {exc}")

    text = "\n".join(text_parts).strip()
    if len(text) < 40:
        ocr_text, ocr_note = _ocr_pdf(path)
        notes.append(ocr_note)
        if ocr_text:
            text = ocr_text
        else:
            return _base(
                ok=False,
                kind="pdf",
                error="Из PDF не извлечён текст (вероятно сканированный чертёж). "
                + (ocr_note or "OCR недоступен."),
                notes="; ".join(notes),
            )

    items: list[dict[str, Any]] = []
    cables: list[dict[str, Any]] = []
    for table in tables:
        for rec in table.get("records", []):
            item = _record_to_item(rec, table.get("sheet", ""))
            if item:
                items.append(item)
                if looks_like_cable(item.get("name", "") + " " + item.get("mark", "")):
                    cables.append(item)

    return _base(
        ok=True,
        kind="pdf",
        text=text[:MAX_TEXT],
        tables=tables,
        items=items,
        cables=cables,
        notes="; ".join(notes),
    )


def parse_dxf(path: Path) -> dict[str, Any]:
    try:
        import ezdxf
        from ezdxf import recover
    except ImportError as exc:
        return _base(ok=False, kind="dxf", error=f"ezdxf недоступен: {exc}")

    try:
        doc, _auditor = recover.readfile(str(path))
    except Exception:
        try:
            doc = ezdxf.readfile(str(path))
        except Exception as exc:
            return _base(ok=False, kind="dxf", error=f"Не удалось прочитать DXF: {exc}")

    return _from_dxf_doc(doc, kind="dxf")


def parse_dwg(path: Path) -> dict[str, Any]:
    # 1) ODA File Converter via ezdxf addon
    try:
        from ezdxf.addons import odafc

        doc = odafc.readfile(str(path))
        result = _from_dxf_doc(doc, kind="dwg")
        result["notes"] = "DWG преобразован через ODA File Converter."
        return result
    except Exception as exc:
        oda_err = str(exc)

    # 2) LibreDWG dwg2dxf
    dwg2dxf = shutil.which("dwg2dxf") or shutil.which("dwgread")
    if dwg2dxf:
        tmp = Path(tempfile.mkdtemp(prefix="dwg_"))
        try:
            out = tmp / (path.stem + ".dxf")
            cmd = [dwg2dxf, str(path), "-o", str(out)] if "dwg2dxf" in dwg2dxf else [dwg2dxf, "-O", "DXF", "-o", str(out), str(path)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if out.exists() and out.stat().st_size > 0:
                result = parse_dxf(out)
                result["kind"] = "dwg"
                result["notes"] = f"DWG преобразован через {Path(dwg2dxf).name}."
                return result
            lib_err = proc.stderr or proc.stdout or "пустой результат"
        except Exception as exc:
            lib_err = str(exc)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        lib_err = "dwg2dxf/dwgread не установлены"

    return _base(
        ok=False,
        kind="dwg",
        error=(
            "Формат DWG — закрытый. Чтение возможно только после преобразования в DXF. "
            f"ODA File Converter: {oda_err}. LibreDWG: {lib_err}. "
            "Установите ODA File Converter или сохраните чертежи в DXF."
        ),
    )


def _from_dxf_doc(doc, kind: str) -> dict[str, Any]:
    msp = doc.modelspace()
    texts: list[str] = []
    texts_geom: list[dict[str, Any]] = []
    equipment: list[dict[str, Any]] = []
    lengths: list[dict[str, Any]] = []

    def add_text(value: str, x: float = 0, y: float = 0, layer: str = ""):
        value = (value or "").strip()
        if not value:
            return
        texts.append(value)
        texts_geom.append({"text": value, "x": x, "y": y, "layer": layer})

    try:
        query_entities = list(msp)
    except Exception:
        query_entities = []

    for e in query_entities:
        dxftype = e.dxftype()
        layer = getattr(getattr(e, "dxf", None), "layer", "") or ""
        try:
            if dxftype == "TEXT":
                ins = e.dxf.insert
                add_text(e.dxf.text, float(ins[0]), float(ins[1]), layer)
            elif dxftype == "MTEXT":
                ins = e.dxf.insert
                add_text(e.text, float(ins[0]), float(ins[1]), layer)
            elif dxftype == "ATTRIB":
                ins = e.dxf.insert
                add_text(f"{e.dxf.tag}={e.dxf.text}", float(ins[0]), float(ins[1]), layer)
            elif dxftype == "INSERT":
                name = e.dxf.name
                ins = e.dxf.insert
                attrs = []
                try:
                    for attrib in e.attribs:
                        attrs.append(f"{attrib.dxf.tag}={attrib.dxf.text}")
                        add_text(attrib.dxf.text, float(ins[0]), float(ins[1]), layer)
                except Exception:
                    pass
                equipment.append(
                    {
                        "block": name,
                        "x": float(ins[0]),
                        "y": float(ins[1]),
                        "layer": layer,
                        "attrs": attrs,
                        "name": name,
                    }
                )
            elif dxftype in {"LWPOLYLINE", "POLYLINE", "LINE", "SPLINE"}:
                length = _entity_length(e)
                if length and length > 0.01:
                    lengths.append(
                        {
                            "layer": layer,
                            "type": dxftype,
                            "length": round(float(length), 3),
                            "likely_cable": _layer_is_cable(layer),
                        }
                    )
        except Exception:
            continue

    # paperspace texts
    try:
        for layout in doc.layouts:
            if layout.name.lower() == "model":
                continue
            for e in layout:
                if e.dxftype() in {"TEXT", "MTEXT"}:
                    try:
                        texts.append(e.dxf.text if e.dxftype() == "TEXT" else e.text)
                    except Exception:
                        pass
    except Exception:
        pass

    return _base(
        ok=True,
        kind=kind,
        text="\n".join(texts)[:MAX_TEXT],
        equipment=equipment,
        lengths=lengths,
        texts_geom=texts_geom[:5000],
        notes=f"Слоёв: {len(doc.layers) if hasattr(doc, 'layers') else '?'}; текстов: {len(texts)}; блоков: {len(equipment)}; линий: {len(lengths)}",
    )


def _entity_length(e) -> float | None:
    try:
        if e.dxftype() == "LINE":
            start, end = e.dxf.start, e.dxf.end
            dx = float(end[0]) - float(start[0])
            dy = float(end[1]) - float(start[1])
            dz = float(end[2]) - float(start[2]) if len(end) > 2 else 0.0
            return (dx * dx + dy * dy + dz * dz) ** 0.5
        if e.dxftype() == "LWPOLYLINE":
            return float(e.length())
        if e.dxftype() == "POLYLINE":
            if hasattr(e, "length"):
                return float(e.length())
            pts = list(e.points())
            total = 0.0
            for a, b in zip(pts, pts[1:]):
                total += ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
            return total
        if hasattr(e, "length"):
            return float(e.length())
    except Exception:
        return None
    return None


def _layer_is_cable(layer: str) -> bool:
    t = (layer or "").lower()
    keys = ("каб", "кабель", "cable", "трсс", "трасс", "эл ", "эом", "сс", "слабот", "пс", "соуэ", "скс")
    return any(k in t for k in keys)


def _ocr_pdf(path: Path) -> tuple[str, str]:
    from app.config import settings

    if not settings.ocr_enabled:
        return "", "OCR отключён в настройках."
    if not shutil.which("tesseract"):
        return "", "Tesseract OCR не установлен (пакет tesseract-ocr-rus)."
    try:
        from pdf2image import convert_from_path
    except ImportError:
        # fallback via pdftoppm
        if not shutil.which("pdftoppm"):
            return "", "Для OCR сканов нужны pdf2image или poppler (pdftoppm)."
        return _ocr_via_pdftoppm(path)
    try:
        import pytesseract
    except ImportError:
        pytesseract = None  # type: ignore
    try:
        pages = convert_from_path(str(path), dpi=150, first_page=1, last_page=8)
    except Exception as exc:
        return "", f"Растеризация PDF не удалась: {exc}"
    if pytesseract is None:
        return "", "Пакет pytesseract не установлен. OCR не выполнялся."
    parts = []
    for i, img in enumerate(pages):
        try:
            parts.append(f"--- OCR стр. {i + 1} ---\n" + pytesseract.image_to_string(img, lang="rus+eng"))
        except Exception as exc:
            parts.append(f"OCR стр. {i + 1}: {exc}")
    text = "\n".join(parts).strip()
    return text, "Текст получен OCR (возможны ошибки распознавания)." if text else "OCR не дал текста."


def _ocr_via_pdftoppm(path: Path) -> tuple[str, str]:
    tmp = Path(tempfile.mkdtemp(prefix="ocr_"))
    try:
        subprocess.run(
            ["pdftoppm", "-png", "-r", "150", "-f", "1", "-l", "8", str(path), str(tmp / "p")],
            check=False,
            timeout=90,
            capture_output=True,
        )
        parts = []
        for img in sorted(tmp.glob("p*.png")):
            proc = subprocess.run(
                ["tesseract", str(img), "stdout", "-l", "rus+eng"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.stdout.strip():
                parts.append(proc.stdout)
        text = "\n".join(parts).strip()
        return text, "OCR через pdftoppm+tesseract." if text else "OCR не дал текста."
    except Exception as exc:
        return "", f"OCR pdftoppm: {exc}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _convert_with_libreoffice(path: Path, target: str) -> Path | None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="lo_"))
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", target, "--outdir", str(tmp), str(path)],
            check=False,
            timeout=90,
            capture_output=True,
        )
        found = list(tmp.glob(f"*.{target}"))
        return found[0] if found else None
    except Exception:
        return None


HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "pos": ("поз", "№", "n", "nn", "п/п", "код", "position"),
    "name": ("наименование", "наименов", "название", "name", "оборудован", "наим"),
    "type": ("тип", "type", "исполнение"),
    "mark": ("марка", "тип, марка", "тип марка", "обознач", "марка кабеля", "кабель"),
    "section": ("сечен", "сечение", "жил", "мм2", "мм²", "q,", "qxn"),
    "unit": ("ед", "единиц", "изм", "unit"),
    "qty": ("кол", "колич", "qty", "количество", "число"),
    "length": ("длин", "l,", "трасс", "метр"),
    "from": ("откуда", "начало", "от куда", "из", "start", "питающ"),
    "to": ("куда", "конец", "к ", "finish", "потребит", "назначен"),
    "laying": ("способ", "проклад", "уклад", "лоток", "труба", "транше"),
    "power": ("мощн", "p,", "квт", "вт", "kw"),
    "current": ("ток", "iном", "а,", "current"),
    "voltage": ("напр", "uном", "вольт", "voltage"),
    "cos": ("cos", "коэфф мощности", "cosφ", "cosf"),
    "note": ("примеч", "примечание", "note"),
    "manufacturer": ("завод", "изготов", "произв"),
}


def _header_row(rows: list[list[Any]]) -> int | None:
    best_i, best_s = None, 0
    for i, row in enumerate(rows[:12]):
        mapped = _map_headers(row)
        if len(mapped) > best_s:
            best_s, best_i = len(mapped), i
    return best_i if best_s >= 2 else None


def _map_headers(headers: list[Any]) -> dict[str, int]:
    mapped: dict[str, int] = {}
    for idx, raw in enumerate(headers):
        h = str(raw or "").strip().lower().replace("ё", "е")
        if not h:
            continue
        for key, aliases in HEADER_ALIASES.items():
            if key in mapped:
                continue
            if any(a in h for a in aliases):
                mapped[key] = idx
                break
    return mapped


def _record_to_item(rec: dict[str, Any], sheet: str) -> dict[str, Any] | None:
    name = str(rec.get("name") or "").strip()
    mark = str(rec.get("mark") or rec.get("type") or "").strip()
    if not name and not mark:
        raw = rec.get("_raw") or []
        joined = " ".join(str(x) for x in raw if x not in (None, ""))
        if len(joined) < 3:
            return None
        name = joined[:200]
    section = parse_section(" ".join(str(rec.get(k) or "") for k in ("section", "mark", "name", "type")))
    item = {
        "pos": str(rec.get("pos") or "").strip(),
        "name": name,
        "type": str(rec.get("type") or "").strip(),
        "mark": mark,
        "section": section,
        "unit": str(rec.get("unit") or "").strip(),
        "qty": parse_float(rec.get("qty")),
        "length": parse_float(rec.get("length")),
        "from": str(rec.get("from") or "").strip(),
        "to": str(rec.get("to") or "").strip(),
        "laying": str(rec.get("laying") or "").strip(),
        "power": parse_float(rec.get("power")),
        "current": parse_float(rec.get("current")),
        "voltage": parse_float(rec.get("voltage")),
        "cos": parse_float(rec.get("cos")),
        "note": str(rec.get("note") or "").strip(),
        "sheet": sheet,
    }
    if item["length"] is None and item["unit"] in {"м", "м.", "м.п", "м.п.", "п.м", "пм"} and item["qty"]:
        item["length"] = item["qty"]
    return item


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _base(**kwargs) -> dict[str, Any]:
    out = {
        "ok": False,
        "kind": "",
        "error": "",
        "notes": "",
        "text": "",
        "tables": [],
        "items": [],
        "cables": [],
        "equipment": [],
        "lengths": [],
        "texts_geom": [],
    }
    out.update(kwargs)
    if out.get("text"):
        out["text"] = truncate(out["text"], MAX_TEXT)
    return out
