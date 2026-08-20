from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def loads(text: str, default: Any = None) -> Any:
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {} if default is None else default


def norm(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = text.replace("×", "x").replace("х", "x").replace("Х", "x")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def compact_mark(text: str) -> str:
    t = norm(text)
    t = t.replace(" ", "")
    t = t.replace("(", "").replace(")", "")
    t = t.replace("-", "").replace("_", "")
    return t


CABLE_MARK_RE = re.compile(
    r"(?i)\b("
    r"ввг(?:нг)?(?:\(a\))?(?:-?fr)?(?:-?ls)?(?:-?ltx)?(?:-?hf)?"
    r"|вббшв(?:нг)?(?:\(a\))?(?:-?ls)?"
    r"|ввгнг(?:\(a\))?-?fr(?:ls|hf|lsltx)?"
    r"|кг(?:-?хл)?"
    r"|пугв|пув|пвс|шввп"
    r"|кпс(?:в|э)(?:нг)?(?:\(a\))?(?:-?fr)?(?:-?ls)?"
    r"|ксвв|кспв|кспэ"
    r"|ftp|utp|sftp"
    r"|окн|окг|дпс"
    r")[\w\(\)\-]*",
)

SECTION_RE = re.compile(
    r"(?i)(\d+(?:[.,]\d+)?)\s*[xх×]\s*(\d+(?:[.,]\d+)?)\s*(?:мм2|мм²)?"
)
SECTION_SIMPLE_RE = re.compile(r"(?i)(\d+(?:[.,]\d+)?)\s*(?:мм2|мм²)")
NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace(",", ".")
    text = text.replace("м", "").replace("шт", "").replace("А", "").replace("а", "")
    m = NUMBER_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_section(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    raw = str(text)
    m = SECTION_RE.search(raw.replace(" ", ""))
    if m:
        cores = parse_float(m.group(1))
        mm2 = parse_float(m.group(2))
        # 3x2.5 vs 2.5x3 — cores are usually integer 1-5, section is 0.5-240
        if cores and mm2:
            if cores <= 5 and mm2 >= 0.5:
                return {"cores": int(cores), "mm2": mm2, "raw": m.group(0)}
            if mm2 <= 5 and cores >= 0.5:
                return {"cores": int(mm2), "mm2": cores, "raw": m.group(0)}
    m2 = SECTION_SIMPLE_RE.search(raw)
    if m2:
        mm2 = parse_float(m2.group(1))
        if mm2:
            return {"cores": None, "mm2": mm2, "raw": m2.group(0)}
    return None


def detect_metal(text: str) -> str | None:
    t = norm(text)
    if any(x in t for x in ("аввг", "авбб", "алюминий", "алюм", " а ")):
        if "аввг" in t or "авбб" in t or "алюминий" in t or "алюм" in t:
            return "Al"
    if any(x in t for x in ("ввг", "вбб", "пугв", "медь", "медн", "кпс")):
        return "Cu"
    return None


def is_fire_resistant_mark(mark: str) -> bool:
    t = compact_mark(mark)
    return "fr" in t


def has_ls(mark: str) -> bool:
    t = compact_mark(mark)
    return "ls" in t or "hf" in t


_CABLE_BRAND_TOKENS = (
    "ввг", "вббшв", "кввг", "квббшв", "кпс", "кспв", "кссв", "кпв", "кпбп",
    "тпп", "мкэш", "мкш", "шввп", "пугв", "пвс", "сип", "аввг", "апв",
    "нрг", "спббшв", "ftp", "utp", "витая пара", "вок", "волс",
    "окнг", "окгм", "frls", "frhf",
)

# существительные «кабель/провод» в разных падежах (но не прилагательные)
_CABLE_WORD_RE = re.compile(
    r"\b(?:кабел[ььяюе]|кабел[её]й|кабел[её]м|провод[ауомеы]?|провод[её]й)\b"
)

# фразы, где «кабель» — часть описания материала/работы, а не изделие
_CABLE_WEAK_EXCLUDE = (
    "прокладк",        # прокладка кабеля, для прокладки кабеля
    "ниже кабель",     # сигнальная лента «не копать, ниже кабель»
    "не копать",
    "в комплекте",     # «кабель в комплекте поставки» — не отдельная позиция
    "под кабель",
    "кабель-канал",
    "кабель канал",
)


def looks_like_cable(name: str) -> bool:
    t = norm(name)
    if not t:
        return False
    # 1) марка кабельной продукции — сильный признак (ВВГ, КПС, UTP, ВОК, FRLS…)
    if any(b in t for b in _CABLE_BRAND_TOKENS):
        return True
    # 2) слово «кабель/провод» как существительное. Прилагательные
    #    («кабельная канализация», «кабельные трассы», «кабельный журнал»)
    #    сюда не попадают. Дополнительно отсекаем материалы/работы для монтажа,
    #    где «кабель» — лишь часть описания, а не изделие.
    if _CABLE_WORD_RE.search(t):
        if any(x in t for x in _CABLE_WEAK_EXCLUDE):
            return False
        return True
    return False


def safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^\w.\-() +а-яА-ЯёЁ]+", "_", name, flags=re.UNICODE)
    return name[:180] or "file"


def truncate(text: str, n: int = 4000) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    return text[: n - 20] + "\n…[обрезано]…"
