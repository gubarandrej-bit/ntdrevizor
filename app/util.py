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


# Гомоглифы: CAD-экспорты пишут марки вперемешку латиницей и кириллицей
# («24 OB» и «24 ОВ», «MC» и «МС», «0L-IY» и «ОЛ-И») — сравнение марок
# должно от этого не ломаться. Направление одно: кириллица → латиница; обе
# стороны проходят нормализацию одинаково, разные марки между собой не сливаются.
_CONFUSABLES = str.maketrans(
    {
        "а": "a", "в": "b", "е": "e", "о": "o", "р": "p", "с": "c", "к": "k",
        "м": "m", "и": "i", "й": "j", "л": "l", "у": "y", "т": "t",
    }
)
# Цифра «0» вместо буквы «О» и «1» вместо «L/l» — типичные подмены в PDF,
# экспортированных из CAD (шрифты без корректного Unicode-маппинга).
# Сворачивается только цифра, ПРИЛИПАВШАЯ к буквенному хвосту марки (не к «x»:
# иначе развалится токен сечения «1x2x0,75»; не перед запятой/точкой — цифры
# сечения остаются цифрами). Обе стороны сравнения проходят фолдинг одинаково.
_MARK_OCR_RE = re.compile(r"(?i)([01])(?=[a-wyz])")


def mark_ocr_fold(text: str) -> str:
    """OCR/CAD-фолдинг марки: «0L-IY» → «oli», «4x2x0,52» — без изменений."""
    def _sub(m: re.Match) -> str:
        return "o" if m.group(1) == "0" else "l"

    t = _MARK_OCR_RE.sub(_sub, text)
    t = t.replace("iy", "i")  # «И» в экспорте иногда распадается в «IY»
    return t


def compact_mark(text: str) -> str:
    t = norm(text)
    t = t.replace(" ", "")
    t = t.replace("(", "").replace(")", "")
    t = t.replace("-", "").replace("_", "")
    t = t.translate(_CONFUSABLES)
    return t


def fold_mark(text: str) -> str:
    """Канонический облик марки для сверок: compact_mark + OCR-фолдинг."""
    return mark_ocr_fold(compact_mark(text))


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
# Симметричные cables: «1x2x0,75» — 1 пара × 2 жилы × 0,75 мм²
SECTION_PAIR_RE = re.compile(
    r"(?i)(\d+)\s*[xх×*]\s*(\d+)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)\s*(?:мм2|мм²)?"
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
    # Симметричные кабели связи: «1x2x0,75» = 1 пара × 2 жилы × 0,75 мм².
    # Без этого правила парсер брал «1x2» и выдавал mm2=2.0 — число 0,75
    # терялось («подменяются символы»).
    mp = SECTION_PAIR_RE.search(raw.replace(" ", ""))
    if mp:
        p, c, s = int(mp.group(1)), int(mp.group(2)), parse_float(mp.group(3))
        if s is not None and 0.35 <= s <= 6.0 and 1 <= p <= 61 and 2 <= c <= 4:
            return {
                "cores": p * c,
                "pairs": p,
                "conductors": c,
                "mm2": s,
                "raw": mp.group(0),
            }
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
    "с вилк",          # «шнур сетевой с вилкой» — аксессуар, не трассируется в журнале
)


def looks_like_cable(name: str) -> bool:
    t = norm(name)
    if not t:
        return False
    # 0) позиции оборудования/монтажа, которые начинаются с этих существительных
    #    (после возможного номера позиции «6.», «12. ») — это не кабели: крепления,
    #    устройства, муфты, коробки, шкафы и т.п., даже если внутри описания есть
    #    слово «кабель» или марка NMF….
    head = re.sub(r"^\d+(?:[.)]\s*|\.\s*)", "", t).strip()
    if head.startswith(("креплен", "устройств", "муфт", "коробк", "шкаф", "щит",
                        "кронштейн", "консол", "полк", "панел", "органайзер", "заглушк")):
        return False
    # 0.1) готовые шнуры с вилкой — аксессуары, а не кабель трассы
    if "с вилк" in t:
        return False
    # 1) марка кабельной продукции — сильный признак (ВВГ, КПС, UTP, ВОК, FRLS…)
    if any(b in t for b in _CABLE_BRAND_TOKENS):
        return True
    # 1.1) монтажный симметричный кабель «МС 4х2х0,52» (в CAD-экспортах марка
    #      пишется и латиницей «MC», и вперемешку): признак — «мс/mc» + сечение
    if re.search(r"\b[мm][сc]\b\s*\d+\s*[xх×]\s*\d", t):
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
