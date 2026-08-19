from __future__ import annotations

from pathlib import Path

from app.util import norm

CLASS_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (
        "specification",
        (
            "спецификац",
            "соим",
            "ведомость оборудования",
            "gost 21.110",
            "гост 21.110",
            "specif",
        ),
    ),
    (
        "cable_journal",
        (
            "кабельн",
            "журнал",
            "кабельно-труб",
            "cable journal",
            "откуда",
            "куда",
            "трасс",
        ),
    ),
    (
        "calculation",
        (
            "расчет",
            "расчёт",
            "акб",
            "нагрузк",
            "освещен",
            "освещён",
            "потер",
            "токов",
            "емкост",
            "ёмкост",
        ),
    ),
    (
        "scheme_electrical",
        (
            "однолинейн",
            "принципиальн",
            "схема электрич",
            "эл. схем",
            "эл схем",
            "щит",
            "вру",
            "грщ",
        ),
    ),
    (
        "scheme_structural",
        (
            "структурн",
            "структурная схема",
            "функциональн схем",
        ),
    ),
    (
        "plan",
        (
            "план",
            "расположен",
            "трасс",
            "этаж",
            "генплан",
        ),
    ),
    (
        "connections",
        (
            "подключен",
            "таблица соединен",
            "клемм",
            "внешние проводок",
        ),
    ),
]


def classify_file(filename: str, text_sample: str = "", user_class: str = "") -> str:
    if user_class and user_class != "auto":
        return user_class
    blob = norm(filename + " " + (text_sample or "")[:4000])
    big = norm(text_sample or "")
    # Объединённый комплект РД (один PDF/DOC со всеми разделами): явный заголовок
    # «Спецификация оборудования, изделий и материалов» (ГОСТ 21.110) — сильный
    # признак того, что внутри есть спецификация. Без этого такие файлы часто
    # ошибочно классифицируются как «расчёт» по общим словам «нагрузка/ток».
    if "спецификация оборудован" in big or "спецификация издели" in big:
        return "specification"
    scores: dict[str, int] = {}
    for cls, keys in CLASS_KEYWORDS:
        scores[cls] = sum(1 for k in keys if k in blob)
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        ext = Path(filename).suffix.lower()
        if ext in {".dwg", ".dxf"}:
            return "plan"
        return "unknown"
    # journal vs spec: both may match "кабель"
    if scores.get("cable_journal", 0) >= scores.get("specification", 0) and any(
        k in blob for k in ("журнал", "откуда", "куда", "длина трасс")
    ):
        return "cable_journal"
    return best


def is_scheme(cls: str) -> bool:
    return cls in {"scheme_electrical", "scheme_structural", "scheme", "connections"}
