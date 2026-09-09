from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from rapidfuzz import fuzz

from app.config import ROOT_DIR, settings
from app.util import (
    compact_mark,
    detect_metal,
    has_ls,
    is_fire_resistant_mark,
    loads,
    looks_like_cable,
    norm,
    parse_float,
    parse_section,
)

TABLES = None


def engineering_tables() -> dict[str, Any]:
    global TABLES
    if TABLES is None:
        import json

        path = ROOT_DIR / "data" / "engineering_tables.json"
        if not path.exists():
            path = settings.data_dir / "engineering_tables.json"
        TABLES = json.loads(path.read_text(encoding="utf-8"))
    return TABLES


def finding(
    severity: str,
    title: str,
    description: str,
    ntd_refs: list[str],
    evidence: str = "",
    location: str = "",
) -> dict[str, Any]:
    return {
        "severity": severity,
        "title": title,
        "description": description,
        "ntd_refs": ntd_refs,
        "evidence": evidence,
        "location": location,
    }


def collect_by_class(parsed_files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in parsed_files:
        groups[f.get("classified_as") or "unknown"].append(f)
    return groups


def all_items(files: list[dict[str, Any]], key: str = "items") -> list[dict[str, Any]]:
    out = []
    for f in files:
        extracted = f.get("extracted") or {}
        for it in extracted.get(key) or []:
            row = dict(it)
            row["_file"] = f.get("filename")
            out.append(row)
    return out


def all_text(files: list[dict[str, Any]]) -> str:
    return "\n".join((f.get("extracted") or {}).get("text") or "" for f in files)


# ----------  комплектность ----------

REQUIRED_BY_SYSTEM = {
    "ES": ["specification", "scheme_electrical"],
    "EO": ["specification", "scheme_electrical"],
    "PS": ["specification", "scheme_structural"],
    "SOUE": ["specification", "scheme_structural"],
    "PT": ["specification"],
    "SKS": ["specification"],
    "LVS": ["specification"],
    "VOLS": ["specification"],
    "CCTV": ["specification", "scheme_structural"],
    "SKUD": ["specification", "scheme_structural"],
    "OS": ["specification"],
    "ASU": ["specification"],
    "ASUTP": ["specification"],
}


# Класс документа может быть не отдельным файлом, а разделом внутри объединённого
# PDF/DOC. Тогда класс «засчитываем» по явным фразам в тексте файлов.
_TEXT_CLASS_EVIDENCE: dict[str, tuple[str, ...]] = {
    "specification": ("спецификация оборудован", "спецификация издели"),
    "scheme_electrical": ("схема электрич", "однолинейн", "принципиальн"),
    "scheme_structural": ("схема структурн", "структурная схема", "функциональная схема"),
    "cable_journal": ("кабельный журнал", "журнал кабел"),
    "plan": ("план расположен", "план прокладк", "план трасс"),
    "calculation": ("расчетн", "расчётн"),
}


def _files_text_match(files: list[dict[str, Any]], *phrases: str) -> bool:
    """Есть ли хотя бы одна из фраз в тексте загруженных файлов."""
    hay = " ".join(((f.get("extracted") or {}).get("text") or "").lower() for f in files)
    if not hay.strip():
        return False
    return any(p in hay for p in phrases)


def check_completeness(files: list[dict[str, Any]], systems: list[str]) -> dict[str, Any]:
    present = {f.get("classified_as") for f in files}
    names = [f.get("filename") for f in files]
    findings = []
    if not files:
        findings.append(
            finding(
                "critical",
                "Не загружены исходные файлы",
                "Проверка невозможна: комплект документации не передан.",
                ["ГОСТ Р 21.101-2020", "ГОСТ 21.110-2013"],
            )
        )
        return {"status": "done", "reason": "", "findings": findings}

    # учитываем разделы внутри объединённых PDF/DOC как наличие класса
    effective = set(present)
    for cls, phrases in _TEXT_CLASS_EVIDENCE.items():
        if cls not in effective and _files_text_match(files, *phrases):
            effective.add(cls)

    has_spec = "specification" in effective

    missing_global = []
    if not has_spec:
        missing_global.append("спецификация оборудования, изделий и материалов")
    for sys in systems:
        need = REQUIRED_BY_SYSTEM.get(sys, ["specification"])
        absent = [x for x in need if x not in effective and not _has_alt(x, effective)]
        if absent:
            findings.append(
                finding(
                    "critical",
                    f"Неполный комплект для системы {sys}",
                    "Отсутствуют документы: "
                    + ", ".join(_cls_name(a) for a in absent)
                    + ". Без них часть проверок выполнена не будет.",
                    ["ГОСТ Р 21.101-2020", "ГОСТ 21.613-2014 п. 4.2–4.3"]
                    if sys in {"ES", "EO"}
                    else ["ГОСТ Р 21.101-2020", "ГОСТ 21.110-2013"],
                    evidence="Загружено: " + ", ".join(names),
                )
            )
    if not has_spec:
        findings.append(
            finding(
                "critical",
                "Нет спецификации",
                "Спецификация по ГОСТ 21.110 не идентифицирована среди загруженных файлов.",
                ["ГОСТ 21.110-2013"],
                evidence="Файлы: " + ", ".join(names),
            )
        )
    # устаревшие ссылки
    return {"status": "done", "reason": "", "findings": findings, "present": sorted(present)}


def _has_alt(need: str, present: set) -> bool:
    if need == "scheme_electrical":
        return "scheme_electrical" in present or "scheme" in present
    if need == "scheme_structural":
        return "scheme_structural" in present or "scheme" in present
    return need in present


def _cls_name(code: str) -> str:
    return {
        "specification": "спецификация",
        "cable_journal": "кабельный журнал",
        "scheme_electrical": "электрическая схема",
        "scheme_structural": "структурная схема",
        "plan": "план трасс/оборудования",
        "calculation": "расчёты",
        "connections": "таблица подключений",
    }.get(code, code)


# ---------- спецификация ↔ журнал ----------

def check_spec_journal_names(spec_items: list[dict], journal: list[dict]) -> dict[str, Any]:
    if not spec_items:
        return _skip("Спецификация не разобрана или в ней нет строк оборудования/кабелей.")
    if not journal:
        return _skip("Кабельный журнал не разобран или не загружен.")

    spec_cables = _spec_cable_positions(spec_items)
    if not spec_cables:
        return {
            "status": "done",
            "reason": "",
            "findings": [
                finding(
                    "info",
                    "В спецификации не выделены кабельные позиции",
                    "Автоматически не удалось отнести строки спецификации к кабелям (нет слов «кабель/провод» и типичных марок). Сверка наименований с журналом не выполнялась по этим строкам.",
                    ["ГОСТ 21.110-2013"],
                )
            ],
        }

    findings = []
    spec_keys = {_cable_key(i): i for i in spec_cables}
    jour_keys = [_cable_key(i) for i in journal]

    for jk, j in zip(jour_keys, journal):
        if not jk or jk == "|":
            findings.append(
                finding(
                    "noncritical",
                    "Строка журнала без марки/наименования",
                    "Невозможно сопоставить строку журнала со спецификацией: пустая марка и наименование.",
                    ["ГОСТ 21.613-2014"],
                    evidence=str(j.get("pos") or j.get("from") or ""),
                    location=j.get("_file", ""),
                )
            )
            continue
        if jk in spec_keys or _fuzzy_in(jk, spec_keys):
            continue
        findings.append(
            finding(
                "critical",
                "Марка из журнала отсутствует в спецификации",
                f"Позиция журнала «{j.get('name') or ''} {j.get('mark') or ''}» "
                f"({j.get('from') or '?'} → {j.get('to') or '?'}) не найдена в спецификации.",
                ["ГОСТ 21.110-2013", "ГОСТ 21.613-2014"],
                evidence=f"ключ сверки: {jk}",
                location=j.get("_file", ""),
            )
        )

    jour_set = set(jour_keys)
    for sk, s in spec_keys.items():
        if sk in jour_set or _fuzzy_in(sk, {k: True for k in jour_set}):
            continue
        # кабель в спецификации может быть «итогом» без построчного журнала — некритично, если журнал в принципе есть
        findings.append(
            finding(
                "noncritical",
                "Кабель спецификации не встретился в журнале",
                f"Позиция спецификации «{s.get('name') or ''} {s.get('mark') or ''}» не сопоставлена ни с одной строкой журнала.",
                ["ГОСТ 21.110-2013"],
                evidence=f"ключ сверки: {sk}",
                location=s.get("_file", ""),
            )
        )
    return {"status": "done", "reason": "", "findings": findings}


def check_spec_journal_qty(spec_items: list[dict], journal: list[dict], tol_pct: float) -> dict[str, Any]:
    if not spec_items:
        return _skip("Спецификация не разобрана — количества сверить нельзя.")
    if not journal:
        return _skip("Кабельный журнал не разобран — количества сверить нельзя.")
    spec_cables = _spec_cable_positions(spec_items)
    if not spec_cables:
        return _skip("В спецификации не выделены кабельные позиции с количеством/длиной.")

    spec_sum: dict[str, float] = defaultdict(float)
    spec_has: dict[str, bool] = {}
    for i in spec_cables:
        key = _cable_key(i)
        qty = i.get("length") if i.get("length") is not None else i.get("qty")
        spec_has[key] = qty is not None
        if qty is not None:
            spec_sum[key] += float(qty)

    jour_sum: dict[str, float] = defaultdict(float)
    jour_has: dict[str, bool] = {}
    # итоговые значения блока «Итого» (суммы по маркам с запасом) авторитетны
    # для сравнения со спецификацией; детальные строки трасс — только если
    # итогов нет (например, в объединённых PDF без блока «Итого»).
    total_by_key: dict[str, float] = {}
    for i in journal:
        if i.get("is_total"):
            key = _cable_key(i)
            qty = i.get("length") if i.get("length") is not None else i.get("qty")
            if qty is not None:
                total_by_key[key] = float(qty)
    for i in journal:
        key = _cable_key(i)
        qty = i.get("length") if i.get("length") is not None else i.get("qty")
        jour_has[key] = jour_has.get(key, False) or qty is not None
        if qty is not None:
            jour_sum[key] += float(qty)
    for key, val in total_by_key.items():
        jour_sum[key] = val
        jour_has[key] = True

    findings = []
    keys = set(spec_sum) | set(jour_sum) | set(spec_has) | set(jour_has)
    for key in sorted(keys):
        if not spec_has.get(key, False):
            findings.append(
                finding(
                    "noncritical",
                    "Нет количества в спецификации",
                    f"Для «{key}» в спецификации не указано количество/длина. Сравнение с журналом не выполнено.",
                    ["ГОСТ 21.110-2013"],
                )
            )
            continue
        if not jour_has.get(key, False):
            findings.append(
                finding(
                    "noncritical",
                    "Нет длины в журнале",
                    f"Для «{key}» в журнале нет числовой длины. Сравнение количеств не выполнено.",
                    ["ГОСТ 21.613-2014"],
                )
            )
            continue
        s, j = spec_sum.get(key, 0.0), jour_sum.get(key, 0.0)
        if s == 0 and j == 0:
            continue
        delta = abs(s - j)
        base = max(s, j, 1e-9)
        pct = 100.0 * delta / base
        if pct > tol_pct:
            findings.append(
                finding(
                    "critical" if pct > max(tol_pct, 15) else "noncritical",
                    "Расхождение длины спецификация/журнал",
                    f"«{key}»: спецификация {s:g} м, журнал (сумма) {j:g} м, расхождение {pct:.1f}% "
                    f"(допуск настройки {tol_pct:g}%).",
                    ["ГОСТ 21.110-2013", "ГОСТ 21.613-2014"],
                    evidence=f"spec={s}, journal={j}",
                )
            )
    return {"status": "done", "reason": "", "findings": findings}


def _is_cable_item(i: dict) -> bool:
    return looks_like_cable(" ".join(str(i.get(k) or "") for k in ("name", "mark", "type", "manufacturer")))


def _is_material_line(i: dict) -> bool:
    """Строка спецификации — материальная позиция (есть количество/длина).

    Отсекает строки оглавления и заголовков («План … кабельных трасс»),
    которые содержат слово «кабель», но не являются позициями спецификации.
    """
    return i.get("qty") is not None or i.get("length") is not None


def _cable_brand(i: dict) -> str:
    """Марка кабеля: предпочитаем заводскую марку, если она выглядит как кабель.

    Если позиция — кабель по наименованию («Кабель …», «Провод …»), марка в поле
    mark/manufacturer приоритетна даже если она не входит в список типовых марок
    (например NMF-4XE… — волоконно-оптический кабель NIKOMAX).
    """
    name = str(i.get("name") or "").strip()
    by_name = looks_like_cable(name)
    for k in ("manufacturer", "mark", "type"):
        v = str(i.get(k) or "").strip()
        if v and (looks_like_cable(v) or by_name):
            return _strip_vendor(v)
    return _strip_vendor(str(i.get("mark") or i.get("name") or "").strip())


def _strip_section_from_mark(mark: str) -> str:
    """Убирает полный токен сечения из марки: '3x2.5', '3х2,5', '1x2x0,75',
    '2x2x0,78' (и любые цепочки NxNx…) и суффикс «ТУ …», чтобы журнал
    и спецификация сходились."""
    t = compact_mark(mark)
    # полный токен: одна или несколько групп «число x число» подряд
    t = re.sub(r"\d+(?:[.,]\d+)?(?:[xх×]\d+(?:[.,]\d+)?)+", "", t)
    t = re.sub(r"\d+(?:[.,]\d+)?мм2?", "", t)
    t = re.sub(r"ту\d+[-.\w]*", "", t)
    return t


def _cable_key(i: dict) -> str:
    brand = _cable_brand(i)
    mark = _strip_section_from_mark(brand) if brand else ""
    name = _strip_section_from_mark(i.get("name") or "") if not brand else ""
    # Сечение ищем в марке/производителе/типе. Наименование не подставляем в
    # разбор сечения: номер позиции («7. Кабель …») склеивается с «3х2,5» после
    # удаления пробелов и ломает разбор («3х2,57»).
    parsed = i.get("section")
    if not parsed:
        parsed = parse_section(" ".join(str(i.get(k) or "") for k in ("mark", "manufacturer", "type")))
    if not parsed:
        parsed = parse_section(str(i.get("name") or ""))
    sec = ""
    if parsed and parsed.get("mm2"):
        cores = parsed.get("cores")
        sec = f"{cores}x{parsed['mm2']}" if cores else str(parsed["mm2"])
    left = mark or name
    return f"{left}|{sec}"


def _fuzzy_in(key: str, mapping: dict) -> bool:
    if key in mapping:
        return True
    a = key.split("|")[0]
    for other in mapping:
        b = other.split("|")[0]
        if a and b and fuzz.ratio(a, b) >= 92:
            # сечение если есть у обоих — должно совпасть
            sa = key.split("|")[1] if "|" in key else ""
            sb = other.split("|")[1] if "|" in other else ""
            if not sa or not sb or sa == sb:
                return True
    return False


# ---------- составные позиции (ОКЛ) и строки-продолжения ----------

# Префиксы производителей, которые в спецификации пишут перед маркой
# («СПЕЦЛАН SF/UTP …»), а в кабельном журнале опускают. Убираем только
# префикс, за которым идёт разделитель, чтобы не трогать саму марку.
_VENDOR_PREFIXES = (
    "спецлан", "спецкаблайн", "спецкабель", "камкабель", "инкаб",
    "москабель", "энергокабель", "сегмент", "людиновокабель",
)

# «- кабель МАРКА … - 18 м;» — длина вложения в хвосте строки
_COMPOSITE_LEN_RE = re.compile(r"[-–—]\s*(\d+(?:[.,]\d+)?)\s*м(?:\.?\s*п)?\s*[;.,]?\s*$")

# Начало новой позиции спецификации внутри наименования: «1.», «3.1», «12)»
_POSITION_LEAD_RE = re.compile(r"^\s*\d+(?:[.,]\d+)*\s*[.)]\s+")


def _strip_vendor(mark: str) -> str:
    """Убирает префикс производителя из марки кабеля («СПЕЦЛАН SF/UTP…» → «SF/UTP…»)."""
    t = str(mark or "").strip()
    low = t.lower()
    for v in _VENDOR_PREFIXES:
        if low.startswith(v) and len(t) > len(v) and t[len(v)] in " /-":
            t = t[len(v):].lstrip(" /-")
            break
    return t


def _merged_spec_rows(spec_items: list[dict]) -> list[dict]:
    """Сливает строки-продолжения спецификации с родительской позицией.

    В CAD-экспортах длинное описание позиции разбито на несколько строк
    таблицы: «Кабель … СПЕЦЛАН SF/UTP Cat5e ZH» + «нг(А)-HF 4x2x0,52».
    Строка считается продолжением, только если:
      * у предыдущей строки заполнен номер позиции (иначе в документах, где
        номер позиции живёт внутри наименования, строки склеивать нельзя);
      * у самой строки номер позиции пуст, она не начинается с «-» (это уже
        вложение составной позиции ОКЛ) и не начинает новую позицию
        («N.», «N.N», «N)» в начале наименования).
    """
    merged: list[dict] = []
    for it in spec_items:
        pos = str(it.get("pos") or "").strip()
        name = re.sub(r"\s+", " ", str(it.get("name") or "")).strip()
        if (
            merged
            and not pos
            and name
            and merged[-1].get("pos")
            and not name.startswith(("-", "–", "—"))
            and not _POSITION_LEAD_RE.match(name)
            and it.get("sheet") == merged[-1].get("sheet")
        ):
            prev = merged[-1]
            prev["name"] = (str(prev.get("name") or "") + " " + name).strip()
            m = str(it.get("mark") or "").strip()
            if m:
                prev["mark"] = (str(prev.get("mark") or "") + " " + m).strip()
            if not prev.get("section"):
                prev["section"] = parse_section(
                    " ".join(str(prev.get(k) or "") for k in ("mark", "name", "type"))
                )
            continue
        merged.append(dict(it))
    return merged


def _composite_cable_item(i: dict) -> dict | None:
    """Вложение составной позиции (ОКЛ): «- кабель МАРКА … - N м;».

    Возвращает материальную кабельную позицию с длиной из хвоста строки
    либо None, если строка не является вложенным кабелем с длиной.
    """
    name = re.sub(r"\s+", " ", str(i.get("name") or "")).strip()
    if not name or not re.match(r"^[-–—]\s+", name):
        return None
    if "кабеленесущ" in name.lower():
        return None
    if not looks_like_cable(name):
        return None
    m = _COMPOSITE_LEN_RE.search(name)
    if not m:
        return None
    length = parse_float(m.group(1))
    if length is None:
        return None
    clean = _COMPOSITE_LEN_RE.sub("", name).rstrip(" ;,.-–—").strip()
    mark = clean
    mm = re.match(r"^[-–—]\s*(?:кабел[ььяюея]?|провод[ауомеы]?)\s+", clean, re.IGNORECASE)
    if mm:
        mark = clean[mm.end():].strip()
    mark = _strip_vendor(mark)
    out = dict(i)
    out["name"] = clean
    out["mark"] = mark
    out["type"] = mark
    out["manufacturer"] = ""
    out["length"] = length
    out["qty"] = None
    out["unit"] = "м"
    out["_composite"] = True
    if not out.get("section"):
        out["section"] = parse_section(" ".join(str(out.get(k) or "") for k in ("mark", "name")))
    return out


def _spec_cable_positions(spec_items: list[dict]) -> list[dict]:
    """Кабельные позиции спецификации для сверки с журналом: обычные
    материальные строки (с учётом строк-продолжений) + вложенные кабели
    составных позиций ОКЛ («- кабель … - N м»)."""
    merged = _merged_spec_rows(spec_items)
    out: list[dict] = []
    for i in merged:
        if _is_cable_item(i) and _is_material_line(i):
            out.append(i)
        extra = _composite_cable_item(i)
        if extra:
            out.append(extra)
    return out


# ---------- схемы ↔ спецификация ----------

# Двухбуквенные обозначения по ГОСТ 2.702/2.710 — реальные устройства.
# Одиночные буквы (A, D, K, U, B…) не включаем: это метки цепей/выводов
# и перевёрнутый текст, они дают ложные «не найдено в спецификации».
EQUIP_TOKEN_RE = re.compile(
    r"\b(?:QF|QS|QA|KM|KK|HL|EL|XS|XT|SG|BK|SA|SB|FU|TV|TA|CT|PT|PA|PV|WH)"
    r"\s*-?\s*\d+[A-Za-zА-Яа-я0-9.\-]*\b"
)

# Обозначения с префиксом устройства по ГОСТ 2.710: «-U1», «-A2», «-KM1».
# Дефис может быть отдельным текстовым объектом (в CAD-экспортах — на своей
# строке), поэтому допускаем пробелы/перевод строки после «-».
PREFIXED_TOKEN_RE = re.compile(
    r"(?<![\w-])-\s*[A-ZА-Я]{1,2}\s*\d+(?:[.\-]\d+)*\b",
    re.UNICODE,
)

# Кириллические разъёмы/клеммы по ГОСТ 2.710 (ХР — розетка, ХТ — клемма,
# ХS/ХА — соединитель/колодка, ХW/ХВ — вилка/ввод).
CYR_TOKEN_RE = re.compile(r"\b(?:ХР|ХТ|ХS|ХА|ХW|ХВ)\s*\d+\b")

# Проектные обозначения цепей/шкафов/листов вида «PS2.2.SC6», «PS2.RU6»,
# «ШПС2.1». Это не обязательно позиции спецификации — помечаем отдельно.
DOTTED_TOKEN_RE = re.compile(r"\b[A-ZА-Я]{1,3}\d+(?:[.\-][A-ZА-Я]{0,3}\d+){1,3}\b")

# Перекрёстные ссылки: «см. ГРК-177-2020-060-АОВ», «см. лист 4».
XREF_RE = re.compile(
    r"\bсм\.\s*(?:лист(?:а|у|е)?\s*)?([A-ZА-Я]{1,6}[-. ]?\d[\w.\-/А-Яа-я]*|\d+[\w./\-А-Яа-я]*)",
    re.I,
)

# Линия RS-485 и её полярности (A/B/GND).
RS485_RE = re.compile(r"rs-?\s?485", re.I)


def _all_designation_tokens(text: str) -> set[str]:
    """Все распознанные обозначения устройств/клемм/цепей на схемах."""
    toks = set(EQUIP_TOKEN_RE.findall(text or ""))
    toks.update(PREFIXED_TOKEN_RE.findall(text or ""))
    toks.update(CYR_TOKEN_RE.findall(text or ""))
    toks.update(DOTTED_TOKEN_RE.findall(text or ""))
    return {re.sub(r"\s+", "", t).lstrip("-") for t in toks if t}


def _equipment_tokens(text: str) -> set[str]:
    """Обозначения оборудования/клемм (без цепей DOTTED) — то, что обязано
    быть в спецификации. Ссылки на документы («- СП 484…», «- ТУ …») и
    шифры листов отсеиваются."""
    toks = set(EQUIP_TOKEN_RE.findall(text or ""))
    toks.update(PREFIXED_TOKEN_RE.findall(text or ""))
    toks.update(CYR_TOKEN_RE.findall(text or ""))
    cleaned = set()
    for t in toks:
        c = re.sub(r"\s+", "", t).lstrip("-")
        if not c:
            continue
        up = c.upper()
        if up.startswith(("СП", "ТУ", "ГОСТ", "НПБ", "СПДС")):
            continue
        cleaned.add(c)
    return cleaned


def _scheme_text_facts(text: str) -> list[str]:
    """Структурированные факты из текста схемы (для ИИ и детерминированных проверок).

    Возвращает краткие строки: обозначения, линии RS-485, перекрёстные ссылки,
    указания напряжения. Это «выжимка», которую модель получает вместе с текстом
    листов — так проверка идёт по фактам, а не по потоку слов.
    """
    facts: list[str] = []
    toks = sorted(_all_designation_tokens(text))
    if toks:
        facts.append(
            "Обозначения устройств/клемм: " + ", ".join(toks[:80]) + (" …" if len(toks) > 80 else "")
        )
    low = (text or "").lower()
    if RS485_RE.search(text or ""):
        near = []
        for m in RS485_RE.finditer(text or ""):
            ctx = text[max(0, m.start() - 40): m.end() + 60].replace("\n", " ")
            near.append(ctx.strip()[:90])
        facts.append("Линии RS-485: " + " | ".join(dict.fromkeys(near))[:600])
    xrefs = sorted(set(XREF_RE.findall(text or "")))
    if xrefs:
        facts.append("Перекрёстные ссылки (см. …): " + ", ".join(xrefs[:30]))
    volts = sorted(set(re.findall(r"\b(?:24|12|220|48|36)\s*В(?:\b|,)", text or "")))
    if volts:
        facts.append("Указания напряжений: " + ", ".join(volts[:10]))
    return facts


def scheme_facts(scheme_files: list[dict]) -> str:
    """Структурированная сводка по файлам схем (для промпта ИИ)."""
    parts = []
    for f in scheme_files:
        ext = f.get("extracted") or {}
        facts = _scheme_text_facts(ext.get("text") or "")
        if facts:
            parts.append(f"### {f.get('filename')}\n" + "\n".join(facts))
    return "\n\n".join(parts)



def _brand_no_section(i: dict) -> str:
    """Марка кабеля без сечения (для сопоставления типа по строкам)."""
    brand = _cable_brand(i)
    if brand:
        return _strip_section_from_mark(brand)
    return _strip_section_from_mark(i.get("name") or "")


def _item_section(i: dict) -> str:
    """Сечение из записи: «3x2.5» / «1x2.0» и т.п.; '' если не указано."""
    parsed = i.get("section") or parse_section(
        " ".join(str(i.get(k) or "") for k in ("mark", "manufacturer", "name", "type"))
    )
    if parsed and parsed.get("mm2"):
        cores = parsed.get("cores")
        return f"{cores}x{parsed['mm2']}" if cores else str(parsed["mm2"])
    return ""


def check_spec_journal_section(spec_items: list[dict], journal: list[dict]) -> dict[str, Any]:
    """Сверка типа (марки) и сечения кабеля между спецификацией и журналом."""
    if not spec_items:
        return _skip("Спецификация не разобрана — тип/сечение сверить нельзя.")
    if not journal:
        return _skip("Кабельный журнал не разобран — тип/сечение сверить нельзя.")
    spec_cables = _spec_cable_positions(spec_items)
    if not spec_cables:
        return _skip("В спецификации не выделены кабельные позиции с количеством/длиной.")

    spec_sec: dict[str, set] = defaultdict(set)
    spec_type: dict[str, set] = defaultdict(set)
    for i in spec_cables:
        b = _brand_no_section(i)
        if not b:
            continue
        s = _item_section(i)
        if s:
            spec_sec[b].add(s)
        t = (i.get("type") or "").strip()
        if t:
            spec_type[b].add(t)

    jour_sec: dict[str, set] = defaultdict(set)
    jour_type: dict[str, set] = defaultdict(set)
    for i in journal:
        b = _brand_no_section(i)
        if not b:
            continue
        s = _item_section(i)
        if s:
            jour_sec[b].add(s)
        t = (i.get("type") or "").strip()
        if t:
            jour_type[b].add(t)

    findings = []
    common = set(spec_sec) & set(jour_sec)
    for b in sorted(common):
        ss, js = spec_sec[b], jour_sec[b]
        if ss and js and ss != js:
            findings.append(
                finding(
                    "critical",
                    "Сечение кабеля в журнале не совпадает со спецификацией",
                    f"«{b}»: спецификация {', '.join(sorted(ss))}, журнал {', '.join(sorted(js))}. "
                    f"Сечение должно совпадать — расхождение влияет на допустимый ток и автоматы защиты.",
                    ["ГОСТ 21.110-2013", "ГОСТ 21.613-2014"],
                    evidence=f"spec={sorted(ss)}, journal={sorted(js)}",
                )
            )
        elif ss and not js:
            findings.append(
                finding(
                    "noncritical",
                    "В журнале не указано сечение кабеля",
                    f"«{b}»: в спецификации сечение {', '.join(sorted(ss))}, в журнале не указано.",
                    ["ГОСТ 21.613-2014"],
                )
            )
        elif not ss and js:
            findings.append(
                finding(
                    "noncritical",
                    "В спецификации не указано сечение кабеля",
                    f"«{b}»: в журнале сечение {', '.join(sorted(js))}, в спецификации не указано.",
                    ["ГОСТ 21.110-2013"],
                )
            )
    # тип/марка: сравниваем поле «тип», когда оно заполнено в обоих документах
    for b in sorted(set(spec_type) & set(jour_type)):
        st, jt = spec_type[b], jour_type[b]
        if st and jt and not (st & jt):
            findings.append(
                finding(
                    "noncritical",
                    "Тип кабеля в журнале отличается от спецификации",
                    f"«{b}»: тип в спецификации {', '.join(sorted(st))}, в журнале {', '.join(sorted(jt))}.",
                    ["ГОСТ 21.110-2013", "ГОСТ 21.613-2014"],
                    evidence=f"spec_type={sorted(st)}, journal_type={sorted(jt)}",
                )
            )
    return {"status": "done", "reason": "", "findings": findings}


def check_scheme_vs_spec(spec_items: list[dict], scheme_files: list[dict]) -> dict[str, Any]:
    if not spec_items:
        return _skip("Спецификация не разобрана — сверять оборудование со схем не с чем.")
    if not scheme_files:
        return _skip("Электрические/структурные схемы не загружены либо не распознаны.")

    extracted_ok = any((f.get("extracted") or {}).get("ok") for f in scheme_files)
    if not extracted_ok:
        reasons = "; ".join(
            (f.get("extracted") or {}).get("error") or f.get("parse_notes") or f.get("filename", "")
            for f in scheme_files
        )
        return _skip(f"Схемы не удалось разобрать. {reasons}")

    text = all_text(scheme_files)
    geom_texts = []
    blocks = []
    for f in scheme_files:
        ext = f.get("extracted") or {}
        geom_texts.extend(t.get("text", "") for t in ext.get("texts_geom") or [])
        blocks.extend(e.get("name", "") for e in ext.get("equipment") or [])
    blob = "\n".join([text, *geom_texts, *blocks])
    tokens = _equipment_tokens(blob)
    circuit_tokens = set(DOTTED_TOKEN_RE.findall(blob))
    if not tokens and not circuit_tokens and not blob.strip():
        return _skip("Из схем не извлечены текст и обозначения оборудования.")

    spec_blob = " ".join(
        " ".join(str(i.get(k) or "") for k in ("pos", "name", "mark", "type", "note"))
        for i in spec_items
    )
    spec_norm = norm(spec_blob)
    findings = []
    unmatched = []
    for tok in sorted(tokens):
        t = compact_mark(tok)
        if t and t in compact_mark(spec_blob):
            continue
        # мягкий поиск
        if tok.lower() in spec_norm or compact_mark(tok) in compact_mark(spec_norm):
            continue
        unmatched.append(tok)

    if tokens and unmatched:
        show = unmatched[:40]
        findings.append(
            finding(
                "critical" if len(unmatched) > 40 else "noncritical",
                "Обозначения на схемах не найдены в спецификации",
                "Следующие позиционные обозначения извлечены со схем и не сопоставлены со спецификацией: "
                + ", ".join(show)
                + ("…" if len(unmatched) > 40 else "")
                + ". Возможны сокращения/иной шифр — требуется ручная проверка.",
                ["ГОСТ 2.702-2011", "ГОСТ 21.110-2013"],
                evidence=f"извлечено обозначений: {len(tokens)}, не сопоставлено: {len(unmatched)}",
            )
        )
    elif not tokens:
        findings.append(
            finding(
                "info",
                "На схемах не распознаны позиционные обозначения",
                "Автоматически не найдены типовые обозначения (QF, HL, SG и т.п.). "
                "Сверка оборудования выполнена только по текстовым совпадениям наименований — их недостаточно для вывода о полном соответствии.",
                ["ГОСТ 2.702-2011"],
            )
        )

    # ---------- цепи/шкафы с проектными обозначениями ----------
    if circuit_tokens:
        findings.append(
            finding(
                "info",
                "Цепи и шкафы с проектными обозначениями",
                "На схемах распознано цепей/шкафов/линий с проектными обозначениями: "
                + ", ".join(sorted(circuit_tokens)[:30])
                + (", …" if len(circuit_tokens) > 30 else "")
                + ". Это не позиции спецификации; проверить, что каждая цепь соответствует "
                "кабелю журнала/спецификации (марка, сечение) и листу подключения.",
                ["ГОСТ 2.709-2019"],
                evidence=f"обозначений цепей: {len(circuit_tokens)}",
            )
        )

    # ---------- перекрёстные ссылки ----------
    xrefs = sorted(set(XREF_RE.findall(blob)))
    if xrefs:
        findings.append(
            finding(
                "info",
                "Перекрёстные ссылки на другие документы/листы",
                "На схемах есть ссылки: "
                + ", ".join(f"«см. {x}»" for x in xrefs[:20])
                + (", …" if len(xrefs) > 20 else "")
                + ". Проверить наличие этих документов/листов в комплекте и сходимость обозначений.",
                ["ГОСТ 2.701-2008"],
                evidence=f"ссылок: {len(xrefs)}",
            )
        )

    # ---------- целостность линий RS-485 (полярность) ----------
    if RS485_RE.search(blob):
        low = blob.lower()
        has_a = bool(re.search(r"rs-?485[^\n]{0,40}[\s(]a\b|[\s(]a\b[^\n]{0,40}rs-?485", blob, re.I))
        has_b = bool(re.search(r"rs-?485[^\n]{0,40}[\s(]b\b|[\s(]b\b[^\n]{0,40}rs-?485", blob, re.I))
        if has_a != has_b:
            findings.append(
                finding(
                    "noncritical",
                    "Линия RS-485: неполная полярность",
                    "На схемах упоминается RS-485, но полярности A и B "
                    + ("не обе указаны рядом с линией." if has_a or has_b else "не распознаны.")
                    + " Для корректного подключения интерфейса должны быть обозначены оба провода (A/B) и общий (GND).",
                    ["ГОСТ 2.702-2011"],
                    evidence=f"A={'есть' if has_a else 'нет'}, B={'есть' if has_b else 'нет'}",
                )
            )
        else:
            findings.append(
                finding(
                    "info",
                    "Линия RS-485",
                    "На схемах распознаны линии RS-485 с полярностями A/B — "
                    "проверить соответствие клеммам приборов и марке кабеля (экранированная витая пара).",
                    ["ГОСТ 2.702-2011"],
                    evidence=f"A={'есть' if has_a else 'нет'}, B={'есть' if has_b else 'нет'}",
                )
            )
    return {"status": "done", "reason": "", "findings": findings}


# ---------- топология подключений (схемы) ----------

_CHAIN_RE = re.compile(r"\b(?:PS|CD|QS|QD)\d+(?:[.\-][A-ZА-Я]{0,4}\d+)*\b")
_TERM_RE = re.compile(r"\bXT\d+(?:\.\d+)?\b|\bХ[ТРSАWВ]\d+\b|\bХР\d+\b", re.I)


def _scheme_lines_of(scheme_files: list[dict]) -> list[str]:
    lines: list[str] = []
    for f in scheme_files:
        ext = f.get("extracted") or {}
        for ln in ext.get("scheme_lines") or []:
            lines.append(re.sub(r"\s+", " ", str(ln)).strip())
    return lines


def check_scheme_topology(spec_items: list[dict], scheme_files: list[dict]) -> dict[str, Any]:
    """Топология схем подключения: пары «устройство → клемма/цепь» и целостность
    цепей (наличие второго конца). Только по визуальным строкам схем; если пару
    определить нельзя — помечается причиной, ничего не выдумывается."""
    lines = _scheme_lines_of(scheme_files)
    if not lines:
        return _skip(
            "Топология подключений не извлечена: на схемах нет распознанных строк "
            "(нужен векторный PDF с листами схем либо DXF/DWG)."
        )

    # марки оборудования спецификации (для привязки «устройство» в строке)
    spec_marks: dict[str, str] = {}
    for i in spec_items:
        if _is_cable_item(i):
            continue
        for k in ("mark", "type"):
            v = str(i.get(k) or "").strip()
            if v and len(v) >= 3:
                spec_marks.setdefault(compact_mark(v), re.sub(r"\s+", " ", v))
        # короткие типовые обозначения оборудования
        name = str(i.get("name") or "").strip()
        for m in re.findall(r"(?:С2000[\w./\-]*|МПН|УК-ВК[\w./\-]*|РИП[\w.\-]*|ШПС[\w.\-]*|БЗЛ|БКИ|КДЛ|БРШС[\w\-]*|QF\d+|SB\d+)", name, re.I):
            spec_marks.setdefault(compact_mark(m), m)

    pairs: list[str] = []
    chain_counts: dict[str, int] = defaultdict(int)
    subs_by_base: dict[str, set] = defaultdict(set)
    cable_marks_on_scheme = 0
    for ln in lines:
        up = ln.upper()
        # цепи и их базы (второй конец)
        for tok in _CHAIN_RE.findall(up):
            t = re.sub(r"\s+", "", tok)
            chain_counts[t] += 1
            base = re.sub(r"[.\-]\d+$", "", t)
            if base != t:
                subs_by_base[base].add(t)
        # кабельные марки на схемах
        if re.search(r"ВВГ|КПС|КСБГ|КСБК|КСВВ|КУНР|ПУГВ|UTP|FTP|NMF|ОКЛ", up):
            cable_marks_on_scheme += 1
        # пары устройство → клемма/цепь
        devs = [spec_marks[c] for c in spec_marks if c in compact_mark(ln) and c]
        terms = set(_TERM_RE.findall(ln))
        chains = set(_CHAIN_RE.findall(ln))
        if devs and (terms or chains):
            d = devs[0]
            targets = sorted({re.sub(r"\s+", "", t) for t in terms})[:2]
            if not targets and chains:
                targets = sorted({re.sub(r"\s+", "", t) for t in chains})[:2]
            for tgt in targets:
                pairs.append(f"{d} → {tgt}")

    findings: list[dict] = []

    # факты (info)
    uniq_pairs: list[str] = []
    for p in pairs:
        if p not in uniq_pairs:
            uniq_pairs.append(p)
    findings.append(
        finding(
            "info",
            "Топология подключений распознана",
            f"Строк подключений: {len(lines)}; пар «устройство → клемма/цепь»: {len(uniq_pairs)}. "
            + ("Примеры: " + "; ".join(uniq_pairs[:6]) + (", …" if len(uniq_pairs) > 6 else "") if uniq_pairs else "Пары не определены — проверить привязку вручную."),
            ["ГОСТ 2.702-2011", "ГОСТ 2.709-2019"],
            evidence=f"строк={len(lines)}, пар={len(uniq_pairs)}",
        )
    )

    # целостность цепей: второй конец.
    # Цепь считается полной, если: есть «базовый» токен (PS1.RS1) либо
    # не менее двух суб-концов с общей базой (PS1.RS1.1 и PS1.RS1.2).
    bare_tokens = {t for t in chain_counts if re.sub(r"[.\-]\d+$", "", t) == t}
    dangling: list[str] = []
    for base, subs in sorted(subs_by_base.items()):
        if base in bare_tokens:
            continue
        if len(subs) >= 2:
            continue
        dangling.append(f"{sorted(subs)[0]} (нет второго конца {base})")
    for b in sorted(bare_tokens):
        if chain_counts.get(b, 0) == 1 and b not in subs_by_base:
            dangling.append(f"{b} (встречается один раз)")
    if dangling:
        findings.append(
            finding(
                "noncritical",
                "Цепи схем без второго конца",
                "Следующие цепи/линии встречаются только с одной стороны: " + ", ".join(dangling[:15])
                + ("…" if len(dangling) > 15 else "")
                + ". Проверить, где их второй конец (возможно, в смежном комплекте документации).",
                ["ГОСТ 2.702-2011"],
                evidence=f"цепей без второго конца: {len(dangling)}",
            )
        )

    if cable_marks_on_scheme == 0:
        findings.append(
            finding(
                "info",
                "Марки кабелей на схемах подключений не подписаны",
                "В строках схем подключений не распознаны марки кабелей (ВВГ/КПС/КСБГ/UTP и т.п.). "
                "Привязку «кабель → клемма» по схемам выполнить нельзя — сверяйте с кабельным журналом вручную.",
                ["ГОСТ 2.702-2011"],
            )
        )

    return {"status": "done", "reason": "", "findings": findings}


# ---------- длины на планах ----------

_SCALE_NOTE_RE = re.compile(r"\b(?:масштаб|м)\s*1\s*[:：]\s*(\d+)\b", re.I)


def _journal_total(journal: list[dict]) -> tuple[float, int, int]:
    """(сумма длин, строк с длиной, строк без длины) по журналу."""
    total = 0.0
    n = 0
    missing = 0
    for row in journal:
        val = row.get("length") if row.get("length") is not None else row.get("qty")
        if val is None:
            missing += 1
            continue
        total += float(val)
        n += 1
    return total, n, missing


def _check_pdf_plan_lengths(journal: list[dict], pdf_plans: list[dict]) -> dict[str, Any]:
    """Измерение трасс на PDF-планах по цветным линиям (без выдумывания метров)."""
    jour_total, jour_n, _ = _journal_total(journal)
    if jour_n == 0:
        return _skip("В журнале нет ни одной числовой длины.")

    findings = []
    for f in pdf_plans:
        ext = f.get("extracted") or {}
        lens = ext.get("lengths") or []
        cable = [x for x in lens if x.get("likely_cable")]
        if not cable:
            findings.append(
                finding(
                    "info",
                    "На плане не выделены цветные трассы",
                    f"{f.get('filename')}: цветных линий (трасс) не найдено — измерение невозможно.",
                    ["ГОСТ 21.613-2014"],
                )
            )
            continue
        measured = sum(float(x.get("length") or 0) for x in cable)
        n_lines = len(cable)
        m = _SCALE_NOTE_RE.search(ext.get("text") or "")
        if m:
            scale = int(m.group(1))
            if scale > 0:
                # 1 pt = 1/72 дюйма = 25.4/72 мм; при масштабе 1:N 1 м = (1000/N) мм = (1000/N)*72/25.4 pt
                meters = measured * scale / 2834.6
                diff_pct = (meters - jour_total) / jour_total * 100 if jour_total else 0.0
                findings.append(
                    finding(
                        "noncritical" if abs(diff_pct) > 10 else "info",
                        "Измерение трасс на PDF-плане (ориентировочно)",
                        f"{f.get('filename')}: цветных линий трасс {n_lines} шт, сумма {measured:.0f} pt ≈ {meters:.1f} м (масштаб по надписи 1:{scale}). "
                        f"Сумма длин журнала {jour_total:.1f} м — расхождение {diff_pct:+.1f}%. "
                        f"Оценка по цвету линий без слоёв — требуется проверка по DXF/DWG.",
                        ["ГОСТ 21.613-2014"],
                        evidence=f"measured_pt={measured:.0f}; scale=1:{scale}",
                    )
                )
                continue
        findings.append(
            finding(
                "info",
                "Измерение трасс на PDF-плане: масштаб не указан",
                f"{f.get('filename')}: сумма цветных линий трасс {measured:.0f} ед. чертежа по {n_lines} линиям. "
                f"Масштаб листа не указан — перевод в метры не выполняется, данные не выдумываются. "
                f"Сумма журнала {jour_total:.1f} м. Для численного сравнения нужен DXF/DWG либо надпись масштаба на листе.",
                ["ГОСТ 21.613-2014"],
                evidence=f"measured_pt={measured:.0f}",
            )
        )
    if not findings:
        return _skip("Не удалось измерить трассы на PDF-планах.")
    return {"status": "done", "reason": "", "findings": findings}


def check_plan_lengths(journal: list[dict], plan_files: list[dict], tol_pct: float) -> dict[str, Any]:
    if not journal:
        return _skip("Кабельный журнал отсутствует — сравнивать длины трасс не с чем.")
    if not plan_files:
        return _skip("Планы трасс не загружены (нужен DXF/DWG или план с измеримыми линиями).")

    pdf_plans = [f for f in plan_files if (f.get("extracted") or {}).get("kind") == "pdf"]
    vector_plans = [f for f in plan_files if (f.get("extracted") or {}).get("kind") != "pdf"]

    findings: list[dict[str, Any]] = []
    if pdf_plans:
        findings.extend((_check_pdf_plan_lengths(journal, pdf_plans)).get("findings", []))

    usable = []
    errors = []
    for f in vector_plans:
        ext = f.get("extracted") or {}
        if not ext.get("ok"):
            errors.append(f"{f.get('filename')}: {ext.get('error') or 'не разобран'}")
            continue
        lens = [x for x in ext.get("lengths") or [] if x.get("likely_cable")]
        if not lens:
            # если есть любые полилинии — берём, но пометим
            lens = ext.get("lengths") or []
            if not lens:
                errors.append(f"{f.get('filename')}: в чертеже нет измеримых линий/полилиний.")
                continue
        usable.append((f, lens))

    if not usable and not pdf_plans:
        return _skip(
            "Не удалось измерить трассы на планах. "
            + (" ".join(errors) if errors else "Нет DXF с линиями на кабельных слоях.")
        )

    if errors:
        findings.append(
            finding(
                "info",
                "Часть планов не измерена",
                " ".join(errors),
                ["ГОСТ 21.613-2014"],
            )
        )

    plan_total = 0.0
    cable_only = True
    for _f, lens in usable:
        for x in lens:
            plan_total += float(x.get("length") or 0)
            if not x.get("likely_cable"):
                cable_only = False

    jour_total, jour_n, missing_len = _journal_total(journal)

    if jour_n == 0:
        return {"status": "done", "reason": "", "findings": findings}

    # единицы DXF неизвестны — если числа отличаются на порядки, не делаем вывод
    ratio = (jour_total / plan_total) if plan_total else None
    if plan_total <= 0:
        if findings:
            return {"status": "done", "reason": "", "findings": findings}
        return _skip("Суммарная длина линий на плане равна нулю.")

    if ratio is not None and (ratio > 50 or ratio < 0.02):
        findings.append(
            finding(
                "info",
                "Единицы измерения плана не подтверждены",
                f"Сумма линий на плане {plan_total:.1f} (единицы DXF), сумма журнала {jour_total:.1f} м. "
                f"Отношение {ratio:.3g}. Без указания единиц чертежа ($INSUNITS) численное сравнение не выполняется — данные не выдумываются.",
                ["ГОСТ 21.613-2014"],
                evidence=f"plan={plan_total}, journal={jour_total}",
            )
        )
        return {"status": "done", "reason": "", "findings": findings}

    if not cable_only:
        findings.append(
            finding(
                "info",
                "На плане измерены не только кабельные слои",
                "Имена слоёв не содержат явных признаков кабельных трасс. Сравнение суммарное и носит ориентировочный характер.",
                ["ГОСТ 21.613-2014"],
            )
        )

    if jour_total + 1e-6 < plan_total:
        pct = 100.0 * (plan_total - jour_total) / plan_total
        findings.append(
            finding(
                "critical",
                "Длина в журнале меньше геометрической длины на плане",
                f"Сумма журнала {jour_total:.1f} м < сумма трасс на плане {plan_total:.1f} "
                f"(расхождение {pct:.1f}%). Запас длины в журнале не подставлялся.",
                ["ГОСТ 21.613-2014"],
                evidence=f"journal={jour_total}, plan={plan_total}",
            )
        )
    else:
        pct = 100.0 * (jour_total - plan_total) / plan_total if plan_total else 0
        if pct > tol_pct:
            findings.append(
                finding(
                    "noncritical",
                    "Длина журнала существенно больше плана",
                    f"Журнал {jour_total:.1f} м, план {plan_total:.1f}, запас {pct:.1f}% "
                    f"(порог пояснения {tol_pct:g}%). Нормативного единого процента запаса нет — требуется обоснование (спуски, запас, вертикали).",
                    ["ГОСТ 21.613-2014"],
                )
            )

    if missing_len:
        findings.append(
            finding(
                "noncritical",
                "В журнале есть строки без длины",
                f"Строк без числовой длины: {missing_len}. Они не вошли в сумму.",
                ["ГОСТ 21.613-2014"],
            )
        )
    return {"status": "done", "reason": "", "findings": findings}


# ---------- марка кабеля ----------

SPZ_SYSTEMS = {"PS", "SOUE", "PT"}


def check_cable_mark(items: list[dict], systems: list[str], full_text: str) -> dict[str, Any]:
    cables = [i for i in items if _is_cable_item(i)]
    if not cables:
        return _skip("Нет распознанных кабельных позиций (спецификация/журнал).")
    findings = []
    seen: set[tuple[str, str]] = set()

    def add(fnd: dict[str, Any]) -> None:
        ev = compact_mark(fnd.get("evidence") or fnd.get("description")[:80])
        ev = re.sub(r"\d.*", "", ev) or ev
        key = (fnd["title"], ev[:24])
        if key in seen:
            return
        seen.add(key)
        findings.append(fnd)

    need_fr = any(s in SPZ_SYSTEMS for s in systems)
    for c in cables:
        mark = " ".join(str(c.get(k) or "") for k in ("mark", "type", "name"))
        laying = norm(c.get("laying") or "")
        if need_fr and not is_fire_resistant_mark(mark):
            # не все кабели комплекта — СПЗ; если в имени есть питание щита/освещение — всё равно для выбранных систем PS/SOUE/PT требуем внимание
            if _looks_spz_cable(c, full_text):
                add(
                    finding(
                        "critical",
                        "Кабель СПЗ без индекса огнестойкости FR",
                        f"«{c.get('name') or ''} {c.get('mark') or ''}» применяется в комплекте систем противопожарной защиты, индекс FR не обнаружен. "
                        "Исключения СП 6.13130.2025 в проекте не подтверждены — не зачитываются.",
                        ["СП 6.13130.2025", "ГОСТ 31565-2012", "ГОСТ Р 53316", "ФЗ-123 ст. 82"],
                        location=c.get("_file", ""),
                        evidence=mark,
                    )
                )
        if any(s in systems for s in ("ES", "EO", "PS", "SOUE", "SKUD", "CCTV", "OS")):
            if re.search(r"\bввг\b", norm(mark)) and "нг" not in compact_mark(mark):
                add(
                    finding(
                        "critical",
                        "Кабель без исполнения «нг» в здании",
                        f"Марка «{mark}» не содержит индекса нг. Для прокладки в зданиях требуется кабельное изделие, не распространяющее горение (ГОСТ 31565).",
                        ["ГОСТ 31565-2012", "СП 6.13130.2025"],
                        location=c.get("_file", ""),
                        evidence=mark,
                    )
                )
        if laying and any(k in laying for k in ("земл", "транш", "грунт")):
            if not re.search(r"бб|вбб|брон|вбш", compact_mark(mark) + laying):
                if "труб" not in laying and "канал" not in laying:
                    add(
                        finding(
                            "critical",
                            "Небронированный кабель в земле без трубы/канала",
                            f"«{mark}», способ: «{c.get('laying')}». По ПУЭ гл. 2.3 кабели в земле, как правило, бронированные либо в трубах. Иное должно быть обосновано проектом — обоснования в данных нет.",
                            ["ПУЭ-7 гл. 2.3"],
                            location=c.get("_file", ""),
                            evidence=mark,
                        )
                    )
        obj_public = bool(re.search(r"обществен|школ|больниц|торгов|офис", norm(full_text)))
        if obj_public and "нг" in compact_mark(mark) and not has_ls(mark) and "hf" not in compact_mark(mark):
            add(
                finding(
                    "noncritical",
                    "Для общественного здания нет индекса LS/HF",
                    f"«{mark}»: в общественных зданиях обычно требуется низкое дымо- и газовыделение (нг-LS / нг-HF) по ГОСТ 31565. "
                    "Класс функциональной пожарной опасности помещений в комплекте не подтверждён — замечание некритическое.",
                    ["ГОСТ 31565-2012 табл. 2"],
                    location=c.get("_file", ""),
                    evidence=mark,
                )
            )

    # информационная рекомендация по маркам (из справочника типовых марок)
    catalog = engineering_tables().get("cable_catalog", {}).get("by_system", {})
    if need_fr:
        fr_missing = [c for c in cables if not is_fire_resistant_mark(
            " ".join(str(c.get(k) or "") for k in ("mark", "type", "name"))
        ) and _looks_spz_cable(c, full_text)]
        if fr_missing:
            rec = catalog.get("PS") or catalog.get("SOUE")
            if rec:
                add(
                    finding(
                        "info",
                        "Рекомендуемые марки кабелей для систем ПС/СОУЭ",
                        f"Для линий СПЗ, сохраняющих работоспособность в пожаре, применяются огнестойкие кабели. "
                        f"Типовые марки ({rec.get('label', '')}): {', '.join(rec.get('recommended', []))}. "
                        f"Конкретная марка определяется проектом и ТУ — рекомендация справочная.",
                        ["СП 6.13130.2025", "ГОСТ 31565-2012"],
                        evidence="cable_catalog",
                    )
                )
    return {"status": "done", "reason": "", "findings": findings}


def _looks_spz_cable(c: dict, full_text: str) -> bool:
    blob = norm(" ".join(str(c.get(k) or "") for k in ("name", "mark", "note", "from", "to")))
    keys = ("пож", "соуэ", "апс", "спс", "ппу", "ппкп", "оповещ", "пожаротуш", "дымоуд", "спз")
    return any(k in blob for k in keys)


def _is_detector(i: dict) -> bool:
    """Пожарный извещатель (дымовой/тепловой/ручной/комбинированный)."""
    blob = norm(" ".join(str(i.get(k) or "") for k in ("name", "mark", "type")))
    if not blob:
        return False
    if "извещател" in blob:
        return True
    if re.search(r"\bип\s*2\d\d|\bипр\b|\bипт\b|дип-", blob):
        return True
    return False


def _detector_kind(i: dict) -> str:
    blob = norm(" ".join(str(i.get(k) or "") for k in ("name", "mark", "type")))
    if "ручн" in blob or "ипр" in blob:
        return "manual"
    if "теплов" in blob or "ипт" in blob or re.search(r"\bип\s*1\d\d", blob):
        return "heat"
    if "комбинир" in blob:
        return "combined"
    if "дым" in blob or re.search(r"\bип\s*212\b|дип-", blob):
        return "smoke"
    return "unknown"


_EXPL_STOP = {"стадия", "формат", "спс", "план", "разрез", "лист", "листов", "изм", "согласовано", "подп", "дата", "№док", "взам", "инв"}
_EXPL_CAT_RE = re.compile(r"^(?:-|—|[А-ЯA-Z]{1,2}\d{0,2})$")


def _parse_explication_rooms(text: str) -> list[dict]:
    """Помещения из таблиц «Экспликация помещений» (номер | наименование | площадь).

    В CAD-экспортах таблица разбирается по строкам текста: чистый номер →
    наименование (одна или несколько строк) → площадь (число) → категория.
    Шум планов (оси, размеры, номера без наименования) отсеивается. Дубли
    (экспликация повторяется на нескольких листах) убираются.
    """
    rooms: list[dict] = []
    for seg in re.split(r"(?=--- страница \d+ ---\n)", text or ""):
        m = re.match(r"--- страница (\d+) ---\n", seg)
        if not m:
            continue
        body = seg[m.end():]
        if "экспликац" not in body.lower():
            continue
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        start = next((i for i, ln in enumerate(lines) if re.fullmatch(r"\d{1,3}", ln)), None)
        if start is None:
            continue
        i = start
        while i < len(lines):
            tok = lines[i]
            if re.fullmatch(r"\d{1,3}", tok):
                num = int(tok)
                i += 1
                name_parts: list[str] = []
                while i < len(lines):
                    t = lines[i]
                    if re.fullmatch(r"\d{1,5}(?:[.,]\d{1,3})?", t):
                        break
                    if t.lower() in _EXPL_STOP:
                        break
                    name_parts.append(t)
                    i += 1
                if i >= len(lines) or not re.fullmatch(r"\d{1,5}(?:[.,]\d{1,3})?", lines[i]):
                    break
                area = parse_float(lines[i])
                i += 1
                cat = ""
                if i < len(lines) and _EXPL_CAT_RE.match(lines[i]):
                    cat = lines[i]
                    i += 1
                name = " ".join(name_parts)
                if name and re.search(r"[А-Яа-яA-Za-z]{2,}", name) and area and 1 <= area <= 2000:
                    rooms.append({"num": num, "name": name, "area": area, "cat": cat})
            else:
                if tok.lower() in _EXPL_STOP:
                    break
                i += 1
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in rooms:
        k = (r["num"], r["name"], round(r["area"], 2))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def check_detector_spacing(spec_items: list[dict], full_text: str, systems: list[str] | None = None) -> dict[str, Any]:
    """Расстановка пожарных извещателей по СП 484.1311500.2020.

    Считает извещатели по типам (информационно) и, если есть размерные данные
    помещений (площади в тексте либо таблицы «Экспликация помещений»), сверяет
    требуемое минимальное число дымовых извещателей с фактическим по табл. А.1.
    Сверка по площади выполняется только для системы ПС (для СКУД/ОС «извещатели»
    — охранные, не пожарные). Тепловые ИП не обязательны в каждом помещении —
    по ним только сводка. Без размерных данных проверка не проводится — данные
    не выдумываются.
    """
    is_ps = not systems or "PS" in systems
    detectors = [i for i in spec_items if _is_detector(i) and _is_material_line(i)]
    if not detectors:
        return _skip("В спецификации не найдены пожарные извещатели.")

    tab = engineering_tables().get("sp484_detectors", {})
    smoke_rows = tab.get("smoke", [])
    heat_rows = tab.get("heat", [])

    kinds: dict[str, int] = defaultdict(int)
    for i in detectors:
        k = _detector_kind(i)
        qty = i.get("qty") if i.get("qty") is not None else (i.get("length") or 0)
        kinds[k] += int(qty or 0)

    findings = [
        finding(
            "info",
            "Извещатели в спецификации (по типам)",
            "Сводка: "
            + "; ".join(f"{_detector_label(k)} — {v} шт." for k, v in sorted(kinds.items()))
            + ".",
            ["СП 484.1311500.2020"],
        )
    ]

    heights = [parse_float(x) for x in re.findall(r"[вВ]ысот[а-я]*\s*(?:помещ[а-я]*)?\s*[-—]?\s*(\d+(?:[.,]\d+)?)", full_text)]
    heights = [h for h in heights if h and 1 < h < 30]
    h = max(heights) if heights else 3.0

    rooms = _parse_explication_rooms(full_text)
    if rooms:
        smoke_norm = next((r for r in smoke_rows if float(r["height"].split()[1].replace(",", ".")) >= h), None)
        smoke_area = float(smoke_norm["area_m2"]) if smoke_norm else 85.0
        import math as _math

        total_area = sum(float(r["area"]) for r in rooms)
        have_smoke = kinds.get("smoke", 0) + kinds.get("unknown", 0)
        need_smoke = sum(max(1, _math.ceil(r["area"] / smoke_area)) for r in rooms)
        big = [r for r in rooms if _math.ceil(r["area"] / smoke_area) > 1]
        findings.append(
            finding(
                "info",
                "Помещения по экспликации",
                f"Распознано {len(rooms)} помещений, суммарная площадь {total_area:.0f} м² "
                f"(высота до {h:g} м). Требуется не менее {need_smoke} дымовых ИП "
                f"(мин. 1 на помещение, табл. А.1)."
                + (f" Помещения с более чем одним ИП: " + ", ".join(f"«{r['name']}» {r['area']:g} м²" for r in big) + "." if big else ""),
                ["СП 484.1311500.2020 табл. А.1"],
                evidence=f"rooms={len(rooms)}, area={total_area:.0f}, need={need_smoke}",
            )
        )
        if is_ps and have_smoke > 0 and have_smoke < need_smoke:
            findings.append(
                finding(
                    "critical",
                    "Извещателей меньше требуемого по площади",
                    f"По экспликации требуется не менее {need_smoke} дымовых ИП, в спецификации {have_smoke}. "
                    f"Оценка по площади (мин. 1 ИП на помещение) и не заменяет план расстановки.",
                    ["СП 484.1311500.2020 табл. А.1"],
                    evidence=f"need={need_smoke}, have={have_smoke}",
                )
            )
        elif is_ps and have_smoke > 0:
            findings.append(
                finding(
                    "info",
                    "Дымовых извещателей достаточно по площади",
                    f"Требуется не менее {need_smoke} дымовых ИП, в спецификации {have_smoke}.",
                    ["СП 484.1311500.2020 табл. А.1"],
                    evidence=f"need={need_smoke}, have={have_smoke}",
                )
            )
        elif not is_ps and have_smoke > 0:
            findings.append(
                finding(
                    "info",
                    "Сверка по площади не выполнялась (система не ПС)",
                    "Извещатели найдены, но выбрана система, отличная от пожарной сигнализации. "
                    "Для охранных извещателей норматив расстановки иной — сверка по СП 484 табл. А.1 не выполняется.",
                    ["СП 484.1311500.2020"],
                )
            )
        if kinds.get("heat", 0) > 0:
            findings.append(
                finding(
                    "info",
                    "Тепловые извещатели (сводка)",
                    f"Тепловых ИП в спецификации: {kinds.get('heat', 0)} шт. Норматив их количества "
                    "зависит от назначения помещений (техпроцессы, взрывоопасные зоны) и по площади "
                    "всех помещений не нормируется — требуется проверка по проекту.",
                    ["СП 484.1311500.2020 табл. А.2"],
                )
            )
        return {"status": "done", "reason": "", "findings": findings}

    # запасной путь: площади, выписанные прямо в тексте («… 250 м² …»)
    areas = [parse_float(x) for x in re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:м\s*2|м²|кв\.?\s*м)", full_text)]
    areas = [a for a in areas if a and 1 < a < 100000]
    if not areas:
        return {
            "status": "skipped",
            "reason": (
                "Нет размерных данных помещений (ни таблиц «Экспликация помещений», "
                "ни площадей в тексте). Расстановка извещателей по СП 484 табл. А.1/А.2 "
                "не проверяется — данные не выдумываются."
            ),
            "findings": findings,
        }

    smoke_norm = next((r for r in smoke_rows if float(r["height"].split()[1].replace(",", ".")) >= h), None)
    total_area = sum(areas)
    have_smoke = kinds.get("smoke", 0) + kinds.get("unknown", 0)
    if is_ps and smoke_norm and have_smoke > 0:
        need = total_area / float(smoke_norm["area_m2"])
        if have_smoke < need:
            findings.append(
                finding(
                    "critical",
                    "Извещателей меньше требуемого по площади",
                    f"Суммарная площадь помещений {total_area:.0f} м², высота до {h:g} м → требуется не менее {need:.0f} дымовых ИП (табл. А.1), в спецификации {have_smoke}. "
                    f"Оценка суммарная и не заменяет план расстановки.",
                    ["СП 484.1311500.2020 табл. А.1"],
                    evidence=f"area={total_area:.0f}, need={need:.0f}, have={have_smoke}",
                )
            )
    return {"status": "done", "reason": "", "findings": findings}




def _detector_label(kind: str) -> str:
    return {
        "smoke": "дымовые",
        "heat": "тепловые",
        "manual": "ручные",
        "combined": "комбинированные",
        "unknown": "тип не определён",
    }.get(kind, kind)


# ---------- зоны контроля и отказоустойчивость (СП 484 Изм. № 1) ----------

def check_spz_zones(full_text: str, systems: list[str]) -> dict[str, Any]:
    """Зоны контроля пожарной сигнализации (ЗКСПС) — СП 484 Изм. № 1.

    Изменение № 1 (приказ МЧС России от 27.03.2025 № 252, в силу с 01.09.2025)
    ввело понятие «зона контроля системы пожарной сигнализации» (ЗКСПС):
    площадь одной ЗКСПС ≤ 2000 м², не более 5 смежных помещений на одном этаже;
    единичная неисправность линии связи/питания не должна приводить к потере
    связи с ИП более чем одной ЗКСПС (п. 6.3.4).
    """
    if not any(s in SPZ_SYSTEMS for s in systems):
        return _skip("Системы противопожарной защиты не выбраны.")
    low = norm(full_text)
    findings = []
    if "зкспс" in low or "зона контроля" in low:
        findings.append(
            finding(
                "info",
                "Зоны контроля (ЗКСПС) упомянуты в документации",
                "Обнаружены термины ЗКСПС/«зона контроля». Требование СП 484 п. 6.3 (в ред. Изм. № 1): площадь одной ЗКСПС ≤ 2000 м², не более 5 смежных помещений на одном этаже.",
                ["СП 484.1311500.2020 п. 6.3"],
            )
        )
    else:
        findings.append(
            finding(
                "noncritical",
                "Не выделены зоны контроля пожарной сигнализации (ЗКСПС)",
                "Термины «ЗКСПС»/«зона контроля» в документации не найдены. По СП 484 п. 6.3 (в ред. Изм. № 1 от 27.03.2025 № 252, в силу с 01.09.2025) объект должен быть разделён на зоны контроля. Для проектов, разработанных до 01.09.2025, требуется актуализация.",
                ["СП 484.1311500.2020 п. 6.3"],
                evidence="зкспс не найдено",
            )
        )
    if "единичн" in low and "неисправн" in low:
        findings.append(
            finding(
                "info",
                "Отказоустойчивость при единичной неисправности упомянута",
                "Требование Изм. № 1 к СП 484: единичная неисправность линии связи или электропитания СПС не должна приводить к потере связи с ИП более чем одной ЗКСПС.",
                ["СП 484.1311500.2020 п. 6.3.4"],
            )
        )
    else:
        findings.append(
            finding(
                "noncritical",
                "Отказоустойчивость при единичной неисправности не подтверждена",
                "Упоминания «единичная неисправность» в документации не найдено. По СП 484 п. 6.3.4 (Изм. № 1) единичная неисправность линии связи/электропитания не должна приводить к потере связи с ИП более чем одной ЗКСПС.",
                ["СП 484.1311500.2020 п. 6.3.4"],
            )
        )
    return {"status": "done", "reason": "", "findings": findings}


def check_redundancy(full_text: str, systems: list[str]) -> dict[str, Any]:
    """Резервирование линий электропитания и линий связи СПС.

    Требование: резервирование линий питания и связи (ФЗ-123 ст. 82; СП 484,
    усилено Изм. № 1). Проверяется по текстовым упоминаниям; схемы в PDF —
    векторная графика, автоматически не разбираются, поэтому отсутствие
    упоминаний в тексте даёт некритическое замечание «проверить по схемам».
    """
    if not any(s in SPZ_SYSTEMS for s in systems):
        return _skip("Системы противопожарной защиты не выбраны.")
    low = norm(full_text)
    kw = ("резервиров", "кольц", "дублиров", "два источник", "двух независим", "авр")
    found = [k for k in kw if k in low]
    if found:
        return {
            "status": "done",
            "reason": "",
            "findings": [
                finding(
                    "info",
                    "Резервирование линий упомянуто в документации",
                    f"Найдены признаки резервирования: {', '.join(found)}. Требование: резервирование линий электропитания и линий связи СПС.",
                    ["СП 484.1311500.2020", "ФЗ-123 ст. 82"],
                )
            ],
        }
    return {
        "status": "done",
        "reason": "",
        "findings": [
            finding(
                "noncritical",
                "Резервирование линий питания/связи не подтверждено в тексте",
                "В тексте документации не найдены упоминания резервирования («кольцевая», «резервированная», «два источника», АВР). СП 484 требует резервирование линий электропитания и линий связи СПС. Проверить по схемам (векторная графика PDF автоматически не разбирается).",
                ["СП 484.1311500.2020", "ФЗ-123 ст. 82"],
            )
        ],
    }


def check_laying_params(full_text: str, journal: list[dict]) -> dict[str, Any]:
    """Параметры прокладки кабелей: радиус изгиба и высота (СП 76.13330.2016).

    Радиус изгиба многожильных кабелей с пластмассовой изоляцией до 3 кВ —
    не менее 7,5 наружных диаметров (7,5D), для огнестойких марок (FRLS) —
    до 8–10D. Открытая прокладка — на высоте не менее 2 м от пола.
    """
    if not journal:
        return _skip("Кабельный журнал не разобран — параметры прокладки сверять не с чем.")
    low = norm(full_text)
    findings = []
    if "изгиб" in low:
        findings.append(
            finding(
                "info",
                "Радиус изгиба упомянут в документации",
                "СП 76.13330.2016 (изм. № 1): радиус изгиба многожильных кабелей с пластмассовой изоляцией до 3 кВ — не менее 7,5 наружных диаметров (7,5D); для огнестойких марок (FRLS) — до 8–10D.",
                ["СП 76.13330.2016"],
            )
        )
    else:
        findings.append(
            finding(
                "noncritical",
                "Минимальный радиус изгиба кабелей не указан",
                "В документации не найден радиус изгиба. По СП 76.13330.2016 (изм. № 1) для многожильных кабелей с пластмассовой изоляцией до 3 кВ — не менее 7,5D; для огнестойких (FRLS) — до 8–10D.",
                ["СП 76.13330.2016"],
            )
        )
    if "высота прокладк" in low or re.search(r"высот[а-я]*\s*(?:не менее|от|≥|более)\s*(?:2|3)\s*м", low):
        findings.append(
            finding(
                "info",
                "Высота прокладки указана в документации",
                "Открытая прокладка кабелей — на высоте не менее 2 м от уровня пола; ниже — только с механической защитой.",
                ["СП 76.13330.2016"],
            )
        )
    else:
        findings.append(
            finding(
                "noncritical",
                "Высота прокладки кабелей не подтверждена",
                "Явных отметок высоты прокладки (≥ 2 м от пола для открытых трасс) не найдено. Проверить по планам прокладки.",
                ["СП 76.13330.2016"],
            )
        )
    return {"status": "done", "reason": "", "findings": findings}


# ---------- сечение ----------

def check_cable_section(items: list[dict], calc_text: str) -> dict[str, Any]:
    candidates = []
    for i in items:
        if not (_is_cable_item(i) or i.get("current") or i.get("power")):
            continue
        sec = i.get("section") or parse_section(" ".join(str(i.get(k) or "") for k in ("mark", "name", "type")))
        if not sec or not sec.get("mm2"):
            continue
        candidates.append((i, sec))
    if not candidates:
        return _skip("Нет кабелей с распознанным сечением и/или нет нагрузок (I, P) в тех же строках.")

    tables = engineering_tables()
    findings = []
    checked = 0
    for i, sec in candidates:
        current = i.get("current")
        power = i.get("power")
        voltage = i.get("voltage")
        cosphi = i.get("cos")
        if current is None and power is None:
            continue
        if current is None and power is not None:
            if voltage is None:
                findings.append(
                    finding(
                        "info",
                        "Ток не рассчитан — нет напряжения",
                        f"Для «{i.get('name') or i.get('mark')}» есть мощность {power:g}, но нет U. "
                        "Ток не вычислялся (напряжение не выдумывается).",
                        ["ПУЭ-7 табл. 1.3.6"],
                        location=i.get("_file", ""),
                    )
                )
                continue
            phases = 3 if (sec.get("cores") or 0) >= 4 or (voltage and voltage >= 360) else 1
            if phases == 3:
                current = power * (1000.0 if power < 500 else 1.0) / (math.sqrt(3) * voltage * (cosphi or 1.0))
            else:
                current = power * (1000.0 if power < 500 else 1.0) / (voltage * (cosphi or 1.0))
            if cosphi is None:
                findings.append(
                    finding(
                        "info",
                        "Ток посчитан при cosφ = 1",
                        f"«{i.get('name') or i.get('mark')}»: cosφ в данных нет, принят 1.0 только для пересчёта P→I и это отмечено. Полный ток может быть выше.",
                        ["ПУЭ-7 п. 1.3.10"],
                        location=i.get("_file", ""),
                    )
                )
        if current is None:
            continue
        metal = detect_metal(" ".join(str(i.get(k) or "") for k in ("mark", "name", "type"))) or "Cu"
        laying = norm(i.get("laying") or "")
        in_ground = any(k in laying for k in ("земл", "транш", "грунт"))
        cores = sec.get("cores") or 3
        col = _ampacity_column(cores, in_ground)
        table = tables["pue_1_3_6_copper"] if metal == "Cu" else tables["pue_1_3_7_aluminum"]
        mm2 = _nearest_section(sec["mm2"], table["rows"])
        if mm2 is None:
            findings.append(
                finding(
                    "info",
                    "Сечения нет в таблице ПУЭ 1.3.6/1.3.7",
                    f"Сечение {sec['mm2']} мм² не найдено в таблице. Проверка Iдоп не выполнена.",
                    [table["source"]],
                    location=i.get("_file", ""),
                )
            )
            continue
        row = table["rows"][mm2]
        idx = table["columns"].index(col)
        i_dop = row[idx]
        if i_dop is None:
            findings.append(
                finding(
                    "info",
                    "В таблице ПУЭ нет значения для данного сочетания",
                    f"{metal} {mm2} мм², колонка {col}.",
                    [table["source"]],
                )
            )
            continue
        checked += 1
        if current > i_dop + 1e-6:
            findings.append(
                finding(
                    "critical",
                    "Сечение меньше допустимого по току",
                    f"«{i.get('name') or ''} {i.get('mark') or ''}»: Iрасч={current:.2f} А > Iдоп={i_dop} А "
                    f"({metal}, {mm2} мм², {col}, без поправочных коэффициентов — условия среды не заданы).",
                    [table["source"], "ПУЭ-7 п. 1.3.10"],
                    location=i.get("_file", ""),
                    evidence=f"I={current:.2f} A, Iдоп={i_dop} A",
                )
            )
        # потеря напряжения, если есть длина
        length = i.get("length")
        if length and voltage:
            rho = tables["resistivity_20c"]["Cu" if metal == "Cu" else "Al"]
            r = rho * float(length) / float(sec["mm2"])
            if (sec.get("cores") or 2) >= 3 and (voltage or 0) >= 360:
                du = math.sqrt(3) * current * r * 100.0 / voltage
            else:
                du = 2.0 * current * r * 100.0 / voltage
            findings.append(
                finding(
                    "info",
                    "Оценка потери напряжения (только R20, без X)",
                    f"«{i.get('name') or i.get('mark')}»: L={length:g} м, ΔU≈{du:.2f}% "
                    f"(ρ20={rho}, реактивность не задана и не учитывалась). Норматив ΔU не применялся — тип здания/сети не подтверждён.",
                    ["ПУЭ-7", "СП 256.1325800.2016 разд. 7"],
                    location=i.get("_file", ""),
                )
            )
    if checked == 0 and not findings:
        return _skip(
            "У кабелей с сечением нет сопоставимых нагрузок (ток или мощность+напряжение в той же строке). "
            "Нагрузки из других листов не переносились автоматически."
        )
    return {"status": "done", "reason": "", "findings": findings}


def _ampacity_column(cores: int, in_ground: bool) -> str:
    if cores <= 1:
        return "single_ground" if in_ground else "single_air"
    if cores == 2:
        return "two_ground" if in_ground else "two_air"
    return "three_ground" if in_ground else "three_air"


def _nearest_section(mm2: float, rows: dict) -> str | None:
    key = str(int(mm2)) if float(mm2).is_integer() else str(mm2)
    if key in rows:
        return key
    # 1.5 vs 1,5
    for k in rows:
        if abs(float(k) - float(mm2)) < 1e-6:
            return k
    return None


# ---------- автоматы ----------

BREAKER_RE = re.compile(
    r"(?i)(?:ва-?47|iek|abb|schneider|автомат|выключатель)[^\n]{0,40}?(\d{1,3})\s*а"
)
BREAKER_SIMPLE = re.compile(r"(?i)\b(c|d|b)?\s*(6|10|16|20|25|32|40|50|63|80|100|125|160)\s*а\b")


def check_protection(items: list[dict], text: str) -> dict[str, Any]:
    breakers = []
    for i in items:
        blob = " ".join(str(i.get(k) or "") for k in ("name", "mark", "type", "note"))
        if not re.search(r"автомат|выключатель|ва-?47|qf", norm(blob)):
            continue
        cur = i.get("current")
        if cur is None:
            m = re.search(r"(\d{1,3})\s*а\b", norm(blob))
            if m:
                cur = float(m.group(1))
        if cur is not None:
            breakers.append({**i, "in": cur})
    if not breakers:
        # попробуем из текста
        for m in BREAKER_SIMPLE.finditer(text or ""):
            breakers.append({"name": m.group(0), "in": float(m.group(2)), "_file": "текст"})
    cables = [i for i in items if _is_cable_item(i) and (i.get("section") or parse_section(str(i.get("mark") or "")))]
    if not breakers:
        return _skip("Аппараты защиты с номинальным током не распознаны.")
    if not cables:
        return _skip("Кабели с сечением не распознаны — согласовать с автоматами нельзя.")

    # без явной связи QF↔кабель не выдумываем пары, кроме случая одна линия
    findings = []
    if len(breakers) == 1 and len(cables) == 1:
        findings.extend(_coord_pair(breakers[0], cables[0]))
        return {"status": "done", "reason": "", "findings": findings}

    # сопоставление по from/to / pos
    used = set()
    for b in breakers:
        target = None
        keys = [b.get("to"), b.get("from"), b.get("pos"), b.get("name")]
        for c in cables:
            blob = " ".join(str(c.get(k) or "") for k in ("from", "to", "pos", "name", "mark"))
            if any(k and str(k) in blob for k in keys if k):
                target = c
                break
        if target:
            used.add(id(target))
            findings.extend(_coord_pair(b, target))
    if not findings:
        return _skip(
            "Нет явной связи между автоматами и кабелями (разные строки, нет «откуда/куда»). "
            "Пары не назначались автоматически."
        )
    return {"status": "done", "reason": "", "findings": findings}


def _coord_pair(breaker: dict, cable: dict) -> list[dict]:
    sec = cable.get("section") or parse_section(" ".join(str(cable.get(k) or "") for k in ("mark", "name")))
    if not sec:
        return []
    metal = detect_metal(" ".join(str(cable.get(k) or "") for k in ("mark", "name"))) or "Cu"
    tables = engineering_tables()
    table = tables["pue_1_3_6_copper"] if metal == "Cu" else tables["pue_1_3_7_aluminum"]
    mm2 = _nearest_section(sec["mm2"], table["rows"])
    if not mm2:
        return []
    laying = norm(cable.get("laying") or "")
    col = _ampacity_column(sec.get("cores") or 3, any(k in laying for k in ("земл", "транш")))
    i_dop = table["rows"][mm2][table["columns"].index(col)]
    if i_dop is None:
        return []
    inom = float(breaker.get("in"))
    if inom > i_dop + 1e-6:
        return [
            finding(
                "critical",
                "Номинал автомата выше Iдоп кабеля",
                f"{breaker.get('name') or breaker.get('mark')} Iн={inom:g} А, кабель {cable.get('mark') or cable.get('name')} "
                f"{mm2} мм² Iдоп={i_dop} А ({col}).",
                ["ПУЭ-7 п. 3.1.10", "ПУЭ-7 п. 3.1.11", table["source"]],
                evidence=f"Iн={inom}, Iдоп={i_dop}",
            )
        ]
    return []


# ---------- способ прокладки ----------

def check_laying(journal: list[dict]) -> dict[str, Any]:
    if not journal:
        return _skip("Кабельный журнал не загружен — способы прокладки неизвестны.")
    findings = []
    empty = 0
    for row in journal:
        laying = (row.get("laying") or "").strip()
        if not laying:
            empty += 1
            continue
    if empty == len(journal):
        return _skip("В журнале ни у одной строки не заполнен способ прокладки.")
    if empty:
        findings.append(
            finding(
                "noncritical",
                "Не у всех кабелей указан способ прокладки",
                f"Строк без способа прокладки: {empty} из {len(journal)}. Для них проверка марки vs способ не выполнялась.",
                ["ПУЭ-7 гл. 2.1", "ГОСТ 21.613-2014"],
            )
        )
    # конкретные конфликты марки и способа — в check_cable_mark
    return {"status": "done", "reason": "", "findings": findings}


# ---------- питание ----------

def check_power_source(items: list[dict], text: str) -> dict[str, Any]:
    loads = [i for i in items if i.get("power") or i.get("current")]
    sources = [
        i
        for i in items
        if re.search(r"ибп|ибэ|бп |источник|трансформатор|упс|psu|акб|блок питания", norm(i.get("name") or "" + " " + (i.get("mark") or "")))
    ]
    # вытащим числа из расчёта
    calc_nums = _extract_calc_numbers(text)
    if not loads and not calc_nums.get("power") and not calc_nums.get("current"):
        return _skip(
            "Нет нагрузок (P или I) в таблицах и нет распознанных величин в расчёте источника питания."
        )

    findings = []
    sum_p = sum(float(i["power"]) for i in loads if i.get("power"))
    sum_i = sum(float(i["current"]) for i in loads if i.get("current") and not i.get("power"))
    if sum_p:
        findings.append(
            finding(
                "info",
                "Сумма мощностей по разобранным строкам",
                f"ΣP = {sum_p:g} (единица как в исходных ячейках, без перевода Вт/кВт, если размерность не указана). "
                "Коэффициент спроса не применялся — в данных его нет.",
                ["ПУЭ-7", "СП 256.1325800.2016"],
                evidence=f"строк с P: {sum(1 for i in loads if i.get('power'))}",
            )
        )
    # сравнить с номиналом источника, если он есть
    src_p = None
    src_name = ""
    for s in sources:
        blob = " ".join(str(s.get(k) or "") for k in ("name", "mark", "type", "note"))
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*квт", blob, flags=re.I)
        if m:
            src_p = parse_float(m.group(1))
            src_name = blob
            break
        if s.get("power"):
            src_p = float(s["power"])
            src_name = blob
            break
    if src_p is None and calc_nums.get("source_power"):
        src_p = calc_nums["source_power"]
        src_name = "из текста расчёта"
    if src_p is not None and sum_p:
        # осторожно с единицами
        sp, lp = src_p, sum_p
        if lp > 50 and sp < 50:
            lp = lp / 1000.0  # вероятно Вт vs кВт
        if lp > sp * 1.01:
            findings.append(
                finding(
                    "critical",
                    "Сумма нагрузок выше номинала источника",
                    f"ΣP≈{lp:g} > Pист={sp:g} ({src_name}). Коэффициенты спроса не вводились.",
                    ["ПУЭ-7", "СП 6.13130.2025"],
                    evidence=f"sum={lp}, src={sp}",
                )
            )
    elif sources and not sum_p:
        findings.append(
            finding(
                "info",
                "Источник указан, нагрузки не суммированы",
                "В спецификации найден источник питания, но у потребителей нет мощности/тока — проверить номинал нельзя.",
                ["СП 6.13130.2025 прил. Б"],
            )
        )
    return {"status": "done", "reason": "", "findings": findings}


def _extract_calc_numbers(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    if not text:
        return out
    m = re.search(r"P\s*(?:ист|ист\.|источника)?\s*=\s*(\d+(?:[.,]\d+)?)", text, flags=re.I)
    if m:
        out["source_power"] = parse_float(m.group(1))  # type: ignore
    return {k: v for k, v in out.items() if v is not None}


# ---------- АКБ ----------

def check_battery(items: list[dict], text: str, systems: list[str]) -> dict[str, Any]:
    bat_items = [
        i
        for i in items
        if re.search(r"акб|аккумул|батаре", norm((i.get("name") or "") + " " + (i.get("mark") or "")))
    ]
    has_calc = bool(text and re.search(r"акб|емкост|ёмкост|а·ч|а\s*\*\s*ч|ач\b", norm(text)))
    if not bat_items and not has_calc:
        return _skip("Нет позиций АКБ в спецификации и нет текста расчёта аккумуляторов.")

    findings = []
    if any(s in SPZ_SYSTEMS for s in systems) and not has_calc:
        findings.append(
            finding(
                "critical",
                "Нет расчёта ёмкости АКБ для СПЗ",
                "Для систем противопожарной защиты расчёт ёмкости АКБ обязателен (СП 6.13130.2025 приложение Б — обязательное). Файл расчёта не предоставлен либо в нём нет ёмкости.",
                ["СП 6.13130.2025 прил. Б"],
            )
        )

    # попытка проверить арифметику, если в тексте есть формула и числа
    if has_calc:
        findings.extend(_review_battery_arithmetic(text, systems))
        if re.search(r"прил(?:ожение)?\s*а", text, flags=re.I) and any(s in SPZ_SYSTEMS for s in systems):
            findings.append(
                finding(
                    "noncritical",
                    "Ссылка на приложение А СП 6 вместо Б",
                    "В СП 6.13130.2025 расчёт ёмкости АКБ перенесён в обязательное приложение Б (ранее А было рекомендуемым).",
                    ["СП 6.13130.2025 прил. Б"],
                )
            )
    # ёмкость из спецификации vs расчёт
    spec_ah = None
    for b in bat_items:
        blob = " ".join(str(b.get(k) or "") for k in ("name", "mark", "type", "note"))
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*а\s*[·\*]?\s*ч", blob, flags=re.I)
        if m:
            spec_ah = parse_float(m.group(1))
            break
        if b.get("qty") and re.search(r"ач|а·ч", norm(blob)):
            spec_ah = b.get("qty")
    m = re.search(r"(?:C|ёмкость|емкость)\s*=\s*(\d+(?:[.,]\d+)?)", text or "", flags=re.I)
    calc_ah = parse_float(m.group(1)) if m else None
    if spec_ah and calc_ah and spec_ah + 1e-6 < calc_ah:
        findings.append(
            finding(
                "critical",
                "Ёмкость АКБ в спецификации меньше расчётной",
                f"В спецификации {spec_ah:g} А·ч, в расчёте C={calc_ah:g} А·ч.",
                ["СП 6.13130.2025 прил. Б", "ГОСТ 21.110-2013"],
                evidence=f"spec={spec_ah}, calc={calc_ah}",
            )
        )
    return {"status": "done", "reason": "", "findings": findings}


def _review_battery_arithmetic(text: str, systems: list[str]) -> list[dict]:
    """Проверяет только явно выписанные равенства вида a+b=c, a*b=c. Коэффициенты не подставляет."""
    findings = []
    # равенства
    for m in re.finditer(
        r"(?<![\d.,+\-*/x×])(\d+(?:[.,]\d+)?)\s*([+*x×/])\s*(\d+(?:[.,]\d+)?)\s*=\s*(\d+(?:[.,]\d+)?)(?!\s*[-+*/])",
        text,
    ):
        a, op, b, c = (parse_float(m.group(i)) for i in range(1, 5))
        if None in (a, b, c):
            continue
        if op in {"*", "x", "×"}:
            expect = a * b  # type: ignore
        elif op == "/":
            expect = a / b if b else None  # type: ignore
        else:
            expect = a + b  # type: ignore
        if expect is None:
            continue
        if abs(expect - c) > max(0.05 * abs(expect), 0.05):  # type: ignore
            findings.append(
                finding(
                    "critical",
                    "Арифметическая ошибка в расчёте АКБ/питания",
                    f"В тексте: {m.group(0)}. Ожидается {expect:g}.",
                    ["СП 6.13130.2025 прил. Б"],
                    evidence=m.group(0),
                )
            )
    if any(s in SPZ_SYSTEMS for s in systems):
        if not re.search(r"24\s*ч", text) and not re.search(r"дежурн", norm(text)):
            findings.append(
                finding(
                    "noncritical",
                    "В расчёте АКБ не видно времени дежурного режима 24 ч",
                    "Типовое требование для СПЗ — 24 ч дежурного режима. В тексте это не найдено. "
                    "Если проектом обосновано иное время — приложите обоснование.",
                    ["СП 6.13130.2025 прил. Б", "СП 6.13130.2025 табл. 6.2"],
                )
            )
    return findings


# ---------- категория электроснабжения СПЗ ----------

def check_spz_category(text: str, systems: list[str]) -> dict[str, Any]:
    if not any(s in SPZ_SYSTEMS for s in systems):
        return _skip("Системы противопожарной защиты не выбраны.")
    if not text.strip():
        return _skip("Нет текстовых данных схем/расчётов для поиска категории электроснабжения.")
    t = norm(text)
    if re.search(r"i\s*категор|1\s*категор|перв(ая|ой)\s*категор", t):
        return {
            "status": "done",
            "reason": "",
            "findings": [
                finding(
                    "info",
                    "В документации указана I категория электроснабжения",
                    "Найдено указание на I категорию. Соответствие конкретной схеме питания (два независимых источника) автоматически по растру не подтверждалось.",
                    ["СП 6.13130.2025", "ПУЭ-7 гл. 1.2"],
                )
            ],
        }
    if re.search(r"ii\s*категор|2\s*категор|iii\s*категор|3\s*категор", t):
        return {
            "status": "done",
            "reason": "",
            "findings": [
                finding(
                    "critical",
                    "Для СПЗ указана не I категория",
                    "В тексте найдена II/III категория. По СП 6.13130.2025 электроприёмники СПЗ, перечисленные в таблице 6.1, относятся к I категории. Проверьте, не попали ли эти приёмники под пониженную категорию.",
                    ["СП 6.13130.2025 табл. 6.1", "ПУЭ-7 гл. 1.2"],
                )
            ],
        }
    return {
        "status": "done",
        "reason": "",
        "findings": [
            finding(
                "noncritical",
                "Категория электроснабжения СПЗ в комплекте не найдена",
                "Явного указания категории надёжности электроснабжения СПЗ в разобранных текстах нет.",
                ["СП 6.13130.2025 табл. 6.1"],
            )
        ],
    }


# ---------- совместимость оборудования ----------

def _eq_blob(i: dict) -> str:
    return norm(" ".join(str(i.get(k) or "") for k in ("name", "mark", "type", "note")))


def _eq_compact(i: dict) -> str:
    return compact_mark(" ".join(str(i.get(k) or "") for k in ("name", "mark", "type", "note")))


def _eq_has(i: dict, tokens: tuple[str, ...]) -> bool:
    b = _eq_blob(i)
    return any(t in b for t in tokens)


def _mark_of(i: dict) -> str:
    m = re.sub(r"\s+", " ", (i.get("mark") or i.get("type") or "").strip()).strip()
    if m:
        return m[:40]
    return re.sub(r"\s+", " ", (i.get("name") or "").strip())[:40]


def _marks_of(lst: list[dict], n: int = 5) -> list[str]:
    """Уникальные марки позиций (без повторов) для компактного перечисления."""
    out: list[str] = []
    for i in lst:
        m = _mark_of(i)
        if m and m not in out:
            out.append(m)
        if len(out) >= n:
            break
    return out


def _eq_volt(i: dict) -> int | None:
    """Напряжение источника питания из марки: РИП-12, ШПС-24, ИВЭПР-24 и т.п."""
    c = _eq_compact(i)
    m = re.search(r"(?:рип|шпс|ивэпр|бп|скб|аир|апс)(\d{2})", c)
    if m:
        return int(m.group(1))
    return None


def _bat_volt(i: dict) -> int | None:
    """Напряжение АКБ из марки: «АБ 1217С» → 12 В, «12В» → 12 В."""
    c = _eq_compact(i)
    m = re.search(r"аб(\d{2})", c)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{2})в(?:\b|$)", c)
    if m:
        return int(m.group(1))
    return None


def _opov_volt(i: dict) -> int | None:
    """Напряжение оповещателя из марки: КРИСТАЛЛ-24, Маяк-24-ЗМ1 → 24 В."""
    b = _eq_blob(i)
    if not any(t in b for t in ("оповещател", "маяк", "кристалл", "табло")):
        return None
    c = _eq_compact(i)
    m = re.search(r"(?:кристалл|маяк|оповещател|табло)(\d{2})", c)
    if m:
        return int(m.group(1))
    return None


def _rip_load_facts(text: str) -> dict[str, Any]:
    """Извлекает итоги нагрузки из таблицы токов («Итого … мА») и марку РИП.

    Таблица вида «Наименование | Кол | Ток (Реж.„Дежур."), мА | Ток всего, мА |
    Ток (Реж.„Пожар"), мА | Ток всего, мА | … Итого: 130». Обрабатываются только
    страницы с заголовком «Реж. „Дежур."»; никаких значений не подставляется —
    только то, что явно написано в тексте.
    """
    out: dict[str, Any] = {"found": False, "totals": [], "rip": None}
    if not text:
        return out
    totals: list[float] = []
    rip_mark: str | None = None
    for seg in re.split(r"--- страница \d+ ---\n", text):
        low = seg.lower()
        # только листы таблиц токов: заголовок «Реж."Дежур."»
        if not re.search(r"реж\W{0,12}дежур", low):
            continue
        if "ма" not in low and "мa" not in low:
            continue
        for m in re.finditer(r"итого", low):
            tail = seg[m.end(): m.end() + 60]
            for n in re.findall(r"\d+(?:[.,]\d+)?", tail)[:2]:
                v = parse_float(n)
                if v is not None:
                    totals.append(v)
        if rip_mark is None:
            rm = re.search(r"рип[- ]?\d{1,2}\s*исп\.?\s*[\w\d\-]{0,8}", seg, re.I)
            if rm:
                rip_mark = re.sub(r"\s+", " ", rm.group(0)).strip(" .,-")
    if totals:
        out["found"] = True
        out["totals"] = [round(v, 1) for v in totals]
    if rip_mark:
        out["rip"] = rip_mark
    return out


def check_equipment_compat(spec_items: list[dict], text: str = "") -> dict[str, Any]:
    """Совместимость оборудования спецификации: протоколы (адресность),
    искробезопасные цепи, напряжения (АКБ↔РИП, оповещатели↔источник),
    комплектность АКБ для источников 24 В, интерфейсы (RS-485/Ethernet/ДПЛС),
    нагрузка РИП по таблице токов, экосистема производителя. Только по фактам
    из спецификации/текста; если пару определить нельзя — помечается причиной,
    ничего не выдумывается."""
    eq = [i for i in spec_items if i.get("qty") is not None and not _is_cable_item(i)]
    if not eq:
        return _skip("В спецификации нет позиций оборудования с количеством — совместимость проверять не с чем.")

    compat = engineering_tables().get("equipment_compat") or {}
    izv_tokens = ("извещател", "датчик", "дип-", "ипр-", "ип 212", "ип 535", "ипт-")
    ex_device_tokens = tuple(compat.get("ex_device_tokens") or ())
    ex_barrier_tokens = tuple(compat.get("ex_barrier_tokens") or ())
    addr_tokens = tuple(compat.get("addr_tokens") or ())
    nonaddr_tokens = tuple(compat.get("nonaddr_tokens") or ())
    addr_panel_tokens = tuple(compat.get("addr_panel_tokens") or ())
    eco_list = compat.get("ecosystems") or []

    detectors = [i for i in eq if _eq_has(i, izv_tokens)]
    ex_dev = [i for i in detectors if _eq_has(i, ex_device_tokens) and not _eq_has(i, ex_barrier_tokens)]
    ex_barrier = [i for i in eq if _eq_has(i, ex_barrier_tokens)]
    addr_ip = [i for i in detectors if _eq_has(i, addr_tokens) and not _eq_has(i, nonaddr_tokens)]
    nonaddr_ip = [i for i in detectors if _eq_has(i, nonaddr_tokens)]
    addr_panel = [i for i in eq if _eq_has(i, addr_panel_tokens)]
    extenders = [i for i in eq if _eq_has(i, ("с2000-ар", "расширител"))]

    rips = [i for i in eq if _eq_has(i, ("рип", "источник бесперебойн", "ибп", "резервн"))]
    bats = [i for i in eq if _eq_has(i, ("акб", "аккумул", "батаре"))]
    opov = [i for i in eq if _eq_has(i, ("оповещател", "маяк", "кристалл", "табло"))]

    findings: list[dict] = []

    def qty_of(lst: list[dict]) -> float:
        return sum(float(i.get("qty") or 0) for i in lst)

    # 1) искробезопасные цепи
    if ex_dev:
        if not ex_barrier:
            findings.append(
                finding(
                    "critical",
                    "Искробезопасные извещатели без барьеров искрозащиты",
                    f"В спецификации есть искробезопасные устройства ({', '.join(_marks_of(ex_dev))}), "
                    "но блок искробезопасных цепей/барьер (С2000-БРШС-Ex, барьер искрозащиты) не найден. "
                    "Подключение искробезопасного извещателя без барьера во взрывоопасной зоне недопустимо.",
                    ["ГОСТ 31610.11-2014 (IEC 60079-11)", "СП 484.1311500.2020"],
                    evidence=f"искробезопасных устройств: {qty_of(ex_dev):g}; барьеров: 0",
                )
            )
        else:
            findings.append(
                finding(
                    "info",
                    "Искробезопасные цепи обеспечены барьерами",
                    f"Искробезопасные устройства ({', '.join(_marks_of(ex_dev))}) сопряжены с "
                    f"барьерами ({', '.join(_marks_of(ex_barrier))}) — совместимо.",
                    ["ГОСТ 31610.11-2014 (IEC 60079-11)"],
                )
            )

    # 2) адресные извещатели ↔ адресный прибор
    if addr_ip:
        if not addr_panel:
            findings.append(
                finding(
                    "critical",
                    "Адресные извещатели без адресного прибора",
                    f"В спецификации {qty_of(addr_ip):g} адресных извещателей "
                    f"({', '.join(_marks_of(addr_ip))}), но адресный ППКП/контроллер "
                    "(например, «Сириус», С2000-КДЛ) не найден. Адресные извещатели работают только с адресным прибором.",
                    ["ГОСТ Р 53325-2012", "СП 484.1311500.2020"],
                    evidence=f"адресных извещателей: {qty_of(addr_ip):g}",
                )
            )
        else:
            findings.append(
                finding(
                    "info",
                    "Адресные извещатели имеют адресный прибор",
                    f"Адресные извещатели ({', '.join(_marks_of(addr_ip))}) сопряжены с "
                    f"адресным прибором ({', '.join(_marks_of(addr_panel))}) — совместимо.",
                    ["СП 484.1311500.2020"],
                )
            )

    # 3) неадресные извещатели — нужен расширитель/неадресный вход
    if nonaddr_ip and not extenders:
        findings.append(
            finding(
                "noncritical",
                "Неадресные извещатели: вход подключения не найден",
                f"В спецификации {qty_of(nonaddr_ip):g} неадресных извещателей, но расширители адресные "
                "(С2000-АР1/АР2) не найдены. Проверить схему включения неадресных извещателей.",
                ["СП 484.1311500.2020"],
                evidence=f"неадресных извещателей: {qty_of(nonaddr_ip):g}",
            )
        )
    elif nonaddr_ip:
        findings.append(
            finding(
                "info",
                "Неадресные извещатели подключены через расширители",
                f"Неадресные извещатели ({', '.join(_marks_of(nonaddr_ip))}) и "
                f"расширители ({', '.join(_marks_of(extenders))}) — совместимо.",
                ["СП 484.1311500.2020"],
            )
        )

    # 4) напряжение АКБ ↔ РИП/ИБП
    if rips and bats:
        rip_volts = {v for v in (_eq_volt(i) for i in rips) if v}
        bat_volts = {v for v in (_bat_volt(i) for i in bats) if v}
        if rip_volts and bat_volts:
            if 24 in rip_volts and 24 not in bat_volts and 12 in bat_volts:
                findings.append(
                    finding(
                        "noncritical",
                        "РИП 24 В с АКБ 12 В",
                        "В спецификации источник 24 В, а АКБ — только 12 В. Для питания 24 В требуются "
                        "две АКБ по 12 В последовательно (или АКБ 24 В). Проверить комплектность и схему включения.",
                        ["ГОСТ Р 53325-2012", "СП 6.13130.2025 прил. Б"],
                        evidence=f"источники: {sorted(rip_volts)} В; АКБ: {sorted(bat_volts)} В",
                    )
                )
            elif rip_volts & bat_volts:
                findings.append(
                    finding(
                        "info",
                        "Напряжение АКБ соответствует источнику",
                        f"Источники ({', '.join(_marks_of(rips))}) и АКБ "
                        f"({', '.join(_marks_of(bats))}) — {sorted(rip_volts & bat_volts)} В, совместимо.",
                        ["ГОСТ Р 53325-2012"],
                    )
                )
            else:
                findings.append(
                    finding(
                        "noncritical",
                        "Напряжение АКБ и источника не согласовано",
                        f"Источники {sorted(rip_volts)} В, АКБ {sorted(bat_volts)} В — совпадения нет. "
                        "Проверить напряжение питания приборов и АКБ.",
                        ["ГОСТ Р 53325-2012"],
                        evidence=f"источники: {sorted(rip_volts)} В; АКБ: {sorted(bat_volts)} В",
                    )
                )
        else:
            findings.append(
                finding(
                    "info",
                    "Напряжение АКБ/источника из марок не определено",
                    "В спецификации есть РИП/ИБП и АКБ, но напряжение из марок не извлечено. Проверить совместимость вручную.",
                    ["ГОСТ Р 53325-2012"],
                )
            )

    # 5) напряжение оповещателей ↔ источник
    opov_volts = {v for v in (_opov_volt(i) for i in opov) if v}
    if opov_volts:
        src_volts = {v for v in (_eq_volt(i) for i in eq) if v}
        missing = [v for v in sorted(opov_volts) if v not in src_volts]
        if missing:
            findings.append(
                finding(
                    "noncritical",
                    "Источник питания оповещателей не найден",
                    f"Оповещатели {sorted(opov_volts)} В, но явного источника питания такого напряжения "
                    f"в спецификации не найдено (найдены источники: {sorted(src_volts) or '—'}). Проверить схему питания оповещателей.",
                    ["СП 3.13130.2026", "ГОСТ Р 53325-2012"],
                    evidence=f"оповещатели: {sorted(opov_volts)} В; источники: {sorted(src_volts) or '—'} В",
                )
            )
        else:
            findings.append(
                finding(
                    "info",
                    "Оповещатели обеспечены источником нужного напряжения",
                    f"Оповещатели {sorted(opov_volts)} В, источник {sorted(src_volts)} В — совместимо.",
                    ["СП 3.13130.2026"],
                )
            )

    # 6) экосистема производителя
    eco_map: dict[str, list[str]] = {}
    for i in eq:
        b = _eq_blob(i)
        for e in eco_list:
            if any(t in b for t in e.get("tokens") or []):
                eco_map.setdefault(e["name"], []).append(_mark_of(i))
                break
    if len(eco_map) == 1:
        name, marks = next(iter(eco_map.items()))
        findings.append(
            finding(
                "info",
                f"Оборудование одной линейки: {name}",
                f"Распознано {len(marks)} позиций линейки «{name}» — протокольная совместимость подтверждена по составу. "
                "Позиции без распознанной линейки не учитывались.",
                ["ГОСТ Р 53325-2012"],
                evidence=", ".join(marks[:10]) + ("…" if len(marks) > 10 else ""),
            )
        )
    elif len(eco_map) > 1:
        findings.append(
            finding(
                "noncritical",
                "Оборудование разных линеек производителей",
                "В спецификации позиции разных линеек: " + ", ".join(f"«{k}» ({len(v)} поз.)" for k, v in eco_map.items())
                + ". Проверить протокольную совместимость и интерфейсы связи (шлюзы).",
                ["ГОСТ Р 53325-2012"],
            )
        )

    # 7) комплектность АКБ: источник 24 В требует две АКБ по 12 В
    src24 = [i for i in eq if _eq_volt(i) == 24]
    bat12 = [i for i in bats if _bat_volt(i) == 12]
    if src24 and bat12:
        need = 2 * sum(int(i.get("qty") or 0) for i in src24)
        have = sum(int(i.get("qty") or 0) for i in bat12)
        if have < need:
            findings.append(
                finding(
                    "noncritical",
                    "Недостаточно АКБ 12 В для источников 24 В",
                    f"Источники 24 В: {', '.join(_marks_of(src24))} (нужно 2×АКБ 12 В на каждый, т.е. {need} шт.), "
                    f"в спецификации АКБ 12 В — {have} шт. Проверить комплектность и последовательное включение.",
                    ["ГОСТ Р 53325-2012", "СП 6.13130.2025 прил. Б"],
                    evidence=f"нужно={need}, в спецификации={have}",
                )
            )
        else:
            findings.append(
                finding(
                    "info",
                    "Комплектность АКБ для источников 24 В соблюдена",
                    f"Источники 24 В: {', '.join(_marks_of(src24))}; АКБ 12 В: {have} шт. — "
                    f"достаточно для последовательного включения (по 2 на источник).",
                    ["ГОСТ Р 53325-2012"],
                    evidence=f"нужно={need}, в спецификации={have}",
                )
            )

    # 8) интерфейсы: кабель ↔ устройства
    iface = compat.get("interface_tokens") or {}
    cable_items = [i for i in spec_items if _is_cable_item(i)]
    cable_blob = norm(" ".join(
        " ".join(str(i.get(k) or "") for k in ("name", "mark", "type", "manufacturer"))
        for i in cable_items
    ))
    eq_blob = norm(" ".join(
        " ".join(str(i.get(k) or "") for k in ("name", "mark", "type", "note"))
        for i in eq
    ))

    def _has(tokens, blob):
        return any(t in blob for t in tokens)

    rs_cable = _has(iface.get("rs485_cable") or [], cable_blob)
    rs_dev = _has(iface.get("rs485_device") or [], eq_blob)
    eth_cable = _has(iface.get("eth_cable") or [], cable_blob)
    eth_dev = _has(iface.get("eth_device") or [], eq_blob)
    dpls_dev = _has(iface.get("dpls_device") or [], eq_blob)
    two_wire = _has(iface.get("two_wire_cable") or [], cable_blob)

    if rs_cable and rs_dev:
        findings.append(finding("info", "Линия RS-485 обеспечена кабелем и устройствами",
            "Есть кабель RS-485 (экранированная витая пара) и устройства с интерфейсом RS-485 — совместимо.",
            ["ГОСТ 2.702-2011"]))
    elif rs_cable and not rs_dev:
        findings.append(finding("noncritical", "Кабель RS-485 без устройств с RS-485",
            "В спецификации есть кабель RS-485, но устройств с этим интерфейсом не распознано. Проверить назначение кабеля.",
            ["ГОСТ 2.702-2011"]))
    elif rs_dev and not rs_cable:
        findings.append(finding("noncritical", "Устройства RS-485 без кабеля RS-485",
            "В спецификации есть устройства с RS-485, но кабеля RS-485 (экранированная витая пара, КСБГ/КСБК) не найдено.",
            ["ГОСТ 2.702-2011"]))

    if eth_cable and eth_dev:
        findings.append(finding("info", "Линия Ethernet обеспечена кабелем и устройствами",
            "Есть кабель Ethernet (витая пара SF/UTP и т.п.) и устройства с сетевым интерфейсом — совместимо.",
            ["ГОСТ 2.702-2011"]))
    elif eth_cable and not eth_dev:
        findings.append(finding("noncritical", "Кабель Ethernet без сетевых устройств",
            "В спецификации есть кабель Ethernet, но сетевых устройств (сервер/АРМ/коммутатор/камера) не распознано.",
            ["ГОСТ 2.702-2011"]))
    elif eth_dev and not eth_cable:
        findings.append(finding("noncritical", "Сетевые устройства без кабеля Ethernet",
            "В спецификации есть сетевые устройства, но кабеля Ethernet (витая пара) не найдено — "
            "возможно, используется оптический кабель (ВОЛС). Проверить среду передачи.",
            ["ГОСТ 2.702-2011"]))

    if dpls_dev and not two_wire:
        findings.append(finding("noncritical", "ДПЛС без двухпроводного кабеля",
            "Есть контроллер/линия ДПЛС, но кабеля для двухпроводной линии связи (КПСЭнг… 1x2) не найдено.",
            ["СП 484.1311500.2020"]))
    elif dpls_dev and two_wire:
        findings.append(finding("info", "ДПЛС обеспечена двухпроводным кабелем",
            "Контроллер ДПЛС и двухпроводный кабель присутствуют — совместимо.",
            ["СП 484.1311500.2020"]))

    # 9) нагрузка РИП по таблице токов (дежурный/пожарный режим)
    if text:
        load = _rip_load_facts(text)
        if load.get("found"):
            totals = load["totals"]
            rip_mark = load.get("rip") or "РИП"
            rip_catalog = (compat.get("rip_catalog") or {}).get("models") or {}
            key = norm(rip_mark)
            known = None
            for mk, val in rip_catalog.items():
                if mk in key or key in mk:
                    known = val
                    break
            if known is not None and totals:
                over = [t for t in totals if t > known]
                if over:
                    findings.append(
                        finding(
                            "critical",
                            "Нагрузка превышает номинал РИП",
                            f"Итоги таблицы токов: {', '.join(f'{t:g} мА' for t in totals)}; номинал {rip_mark} = {known} мА. "
                            "Превышение номинала недопустимо — нужен более мощный источник или пересчёт нагрузки.",
                            ["СП 6.13130.2025 прил. Б", "ГОСТ Р 53325-2012"],
                            evidence=f"итого={totals}, номинал={known}",
                        )
                    )
                else:
                    findings.append(
                        finding(
                            "info",
                            "Нагрузка в пределах номинала РИП",
                            f"Итоги таблицы токов: {', '.join(f'{t:g} мА' for t in totals)}; номинал {rip_mark} = {known} мА.",
                            ["СП 6.13130.2025 прил. Б"],
                            evidence=f"итого={totals}, номинал={known}",
                        )
                    )
            else:
                shown = ", ".join(f"{t:g}" for t in totals[:8]) + ("…" if len(totals) > 8 else "")
                findings.append(
                    finding(
                        "info",
                        "Таблица токов найдена, номинал РИП не внесён в каталог",
                        f"Итоги нагрузки из таблиц токов (дежурный/пожарный режимы, мА): {shown or 'не извлечены'}. "
                        f"Номинал «{rip_mark}» в каталоге отсутствует — проверка на превышение не выполнялась "
                        "(значение не выдумывается); сверьте с паспортом источника.",
                        ["СП 6.13130.2025 прил. Б", "ГОСТ Р 53325-2012"],
                        evidence=f"итого={totals}, rip={rip_mark}",
                    )
                )

    if not findings:
        return {
            "status": "done",
            "reason": "",
            "findings": [
                finding(
                    "info",
                    "Детерминированные пары совместимости не найдены",
                    "Состав оборудования спецификации не дал распознаваемых пар (адресные/неадресные извещатели, "
                    "искробезопасные цепи, РИП-АКБ, оповещатели, известные линейки производителей). "
                    "Совместимость требует ручной проверки.",
                    ["ГОСТ Р 53325-2012"],
                )
            ],
        }
    return {"status": "done", "reason": "", "findings": findings}


def check_outdated_ntd_refs(text: str, hits: list[dict]) -> dict[str, Any]:
    findings = []
    for h in hits:
        findings.append(
            finding(
                "critical",
                f"Ссылка на недействующий документ: {h['found']}",
                f"{h.get('title') or ''}. Действующая замена: {h.get('replaced_by') or 'см. каталог НТД'}.",
                [h.get("replaced_by") or h["found"]],
                evidence=h["found"],
            )
        )
    return {"status": "done", "reason": "", "findings": findings}


def _skip(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason, "findings": []}
