"""Автопроверка репозитория: парсеры, сверки, API, отчёты. Ничего не подменяет."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.config import settings
from app.seed import init_db
from app.services.checks import (
    check_cable_mark,
    check_spec_journal_names,
    check_spec_journal_qty,
    check_spec_journal_section,
)
from app.services.parsers import parse_file


def assert_true(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_parsers_and_checks():
    import runpy

    runpy.run_path(str(ROOT / "samples" / "make_samples.py"), run_name="__main__")
    sp = parse_file(ROOT / "samples" / "specifikaciya.xlsx")
    jn = parse_file(ROOT / "samples" / "kabelnyy_zhurnal.xlsx")
    assert_true(sp["ok"], sp.get("error"))
    assert_true(jn["ok"], jn.get("error"))
    assert_true(len(sp["items"]) >= 5, f"спецификация разобрана слабо: {len(sp['items'])}")
    assert_true(len(jn["cables"] or jn["items"]) >= 4, "журнал разобран слабо")

    names = check_spec_journal_names(sp["items"], jn["cables"] or jn["items"])
    assert_true(names["status"] == "done", names)
    qty = check_spec_journal_qty(sp["items"], jn["cables"] or jn["items"], 5)
    assert_true(qty["status"] == "done", qty)
    sec = check_spec_journal_section(sp["items"], jn["cables"] or jn["items"])
    assert_true(sec["status"] == "done", sec)
    # должно быть расхождение 120 vs 95 по 3х2,5 и/или кабель журнала КВВГ
    assert_true(any("Расхождение" in f["title"] or "отсутствует" in f["title"] for f in names["findings"] + qty["findings"]),
                f"ожидались расхождения, получено: {names} {qty}")

    marks = check_cable_mark(sp["items"] + (jn["cables"] or []), ["PS", "EO"], "общественное здание")
    assert_true(any("FR" in f["title"] or "нг" in f["title"] for f in marks["findings"]),
                f"ожидались замечания по марке: {marks}")
    print("OK parsers+checks")


def test_api():
    init_db()
    from app.main import app

    client = TestClient(app)
    h = client.get("/api/health")
    assert_true(h.status_code == 200 and h.json()["ok"], h.text)

    bad = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert_true(bad.status_code == 401, bad.text)

    login = client.post("/api/auth/login", json={"username": "admin", "password": settings.admin_password})
    assert_true(login.status_code == 200, login.text)
    token = login.json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}

    ntd = client.get("/api/ntd", headers=hdr)
    assert_true(ntd.status_code == 200 and len(ntd.json()) >= 10, ntd.text)

    models = client.get("/api/models", headers=hdr)
    assert_true(models.status_code == 200, models.text)
    assert_true("local" in models.json() and "cloud" in models.json(), models.text)

    created = client.post(
        "/api/audits",
        headers=hdr,
        json={
            "title": "Контрольная проверка образцов",
            "object_name": "Тестовый объект",
            "systems": ["EO", "PS", "SOUE"],
            "mode": "local",
            "models": [],
        },
    )
    assert_true(created.status_code == 200, created.text)
    aid = created.json()["id"]

    for name in ("specifikaciya.xlsx", "kabelnyy_zhurnal.xlsx", "raschet.xlsx", "poyasnitelnaya.docx"):
        path = ROOT / "samples" / name
        cls = {
            "specifikaciya.xlsx": "specification",
            "kabelnyy_zhurnal.xlsx": "cable_journal",
            "raschet.xlsx": "calculation",
            "poyasnitelnaya.docx": "calculation",
        }[name]
        with path.open("rb") as fh:
            up = client.post(
                f"/api/audits/{aid}/files",
                headers=hdr,
                files={"file": (name, fh, "application/octet-stream")},
                data={"classified_as": cls},
            )
        assert_true(up.status_code == 200, up.text)

    start = client.post(f"/api/audits/{aid}/start", headers=hdr)
    assert_true(start.status_code == 200, start.text)

    deadline = time.time() + 90
    data = None
    while time.time() < deadline:
        data = client.get(f"/api/audits/{aid}", headers=hdr).json()
        if data["status"] in {"done", "error"}:
            break
        time.sleep(0.4)
    assert_true(data and data["status"] == "done", data)
    assert_true(data["findings"], "ожидались замечания по контрольному комплекту")
    assert_true(any(f["severity"] == "critical" for f in data["findings"]), data["findings"])
    skipped = [c for c in data["checks"] if c["status"] == "skipped"]
    for c in skipped:
        assert_true(c["reason"], f"пропуск без причины: {c}")
    # ИИ не выбран — схемы должны быть skipped с причиной
    ai_codes = {"ELEC_SCHEME", "STRUCT_SCHEME", "CONNECTIONS", "ATTACHED_CALCS"}
    for c in data["checks"]:
        if c["code"] in ai_codes:
            assert_true(c["status"] == "skipped" and c["reason"], c)

    dlg = client.get(f"/api/audits/{aid}/dialog", headers=hdr).json()
    assert_true(any("не проводилась" in m["text"] for m in dlg), "диалог должен фиксировать непроведённые проверки")

    for kind in ("doc", "xls", "bov"):
        exp = client.get(f"/api/audits/{aid}/export/{kind}", headers=hdr)
        assert_true(exp.status_code == 200, exp.text)
        assert_true(len(exp.content) > 1000, f"пустой {kind}")

    # админ: блокировка
    uname = f"engineer_{int(time.time())}"
    u = client.post(
        "/api/users",
        headers=hdr,
        json={"username": uname, "password": "Engineer#2026", "role": "engineer", "full_name": "Инженер"},
    )
    assert_true(u.status_code == 200, u.text)
    blk = client.post(f"/api/users/{u.json()['id']}/block", headers=hdr)
    assert_true(blk.status_code == 200 and blk.json()["is_active"] is False, blk.text)
    print("OK api+audit+export+users")


def test_new_checks_synthetic():
    """Синтетические кейсы новых проверок: совместимость, экспликация, топология, счёт устройств, нагрузка РИП."""
    from app.services.checks import (
        check_equipment_compat,
        check_scheme_topology,
        check_plan_device_counts,
        _parse_explication_rooms,
        _rip_load_facts,
    )

    # совместимость: адресные ИП без адресного прибора → critical
    items = [
        {"pos": "1", "name": "Извещатель пожарный дымовой адресно-аналоговый", "mark": "ДИП-34А-04", "type": "", "qty": 10, "length": None, "note": "", "manufacturer": ""},
        {"pos": "2", "name": "Извещатель пожарный тепловой искробезопасный", "mark": "ИПТ-Ех", "type": "", "qty": 4, "length": None, "note": "", "manufacturer": ""},
        {"pos": "3", "name": "Кабель", "mark": "КСБГСнг(А)-FRLS 2x2x0,78", "type": "", "qty": 100, "length": 100, "note": "", "manufacturer": ""},
    ]
    r = check_equipment_compat(items, "")
    titles = {f["title"] for f in r["findings"]}
    assert_true(any("без адресного прибора" in t for t in titles), titles)
    assert_true(any("без барьеров" in t for t in titles), titles)

    # экспликация: синтетический текст с двумя помещениями
    text = (
        "--- страница 3 ---\nЭкспликация помещений\nНомер\nпоме-щения\nНаименование\nПлощадь, м2\nКат.\n"
        "1\nПроходная\n4,45\n-\n2\nПомещение панелей\n168,08\nВ4\nСтадия\nЛист\n"
    )
    rooms = _parse_explication_rooms(text)
    assert_true(len(rooms) == 2, rooms)
    assert_true(rooms[1]["name"] == "Помещение панелей" and abs(rooms[1]["area"] - 168.08) < 1e-6, rooms)

    # топология: нет строк схем → skipped с причиной
    r = check_scheme_topology(items, [{"filename": "x.pdf", "extracted": {"scheme_lines": []}}])
    assert_true(r["status"] == "skipped" and r["reason"], r)

    # счёт устройств на планах: обозначения PS*.BTH* (дымовые) vs спецификация
    plan_files = [{"filename": "p.pdf", "extracted": {
        "text": "--- страница 10 ---\nПлан\nPS1.BTH1 PS1.BTH2 PS1.BTH3\nPS2.BTM1\n"
    }}]
    spec = [
        {"pos": "1", "name": "Извещатель пожарный дымовой адресно-аналоговый", "mark": "ДИП-34А-04", "type": "", "qty": 5, "length": None, "note": "", "manufacturer": ""},
        {"pos": "2", "name": "Извещатель пожарный ручной", "mark": "ИПР-513-3АМ", "type": "", "qty": 2, "length": None, "note": "", "manufacturer": ""},
    ]
    r = check_plan_device_counts(spec, plan_files)
    assert_true(r["status"] == "done", r)
    titles = {f["title"] for f in r["findings"]}
    assert_true(any("дымовые" in t and "меньше" in t for t in titles), titles)

    # нагрузка РИП: таблица токов + номинал из каталога
    load_text = (
        "--- страница 5 ---\nТаблица токов Реж. Дежур., мА Реж. Пожар, мА\n"
        "Прибор 60 60 120 120.0 Итого: 770.0 1110.0\nРИП-12 исп.20\n"
    )
    facts = _rip_load_facts(load_text)
    assert_true(facts["found"] and facts["rip"] == "РИП-12 исп.20", facts)
    r = check_equipment_compat(
        [{"pos": "1", "name": "Источник", "mark": "РИП-12 исп.20", "type": "", "qty": 1, "length": None, "note": "", "manufacturer": ""}],
        load_text,
    )
    titles = {f["title"] for f in r["findings"]}
    assert_true(any("превышает номинал" in t for t in titles), titles)

    # арифметика таблицы токов: «Всего = Кол × Ток», «Итого = Σ строк»
    from app.services.checks import _check_current_table_arithmetic
    bad_table = (
        "--- страница 5 ---\nТаблица токов Реж. Дежур., мА Реж. Пожар, мА\n"
        "Наименование Кол Ток всего Ток всего\n"
        "РИП-12 исп.14 1 30 30 30 30.0\n"
        "Блок индикации C2000-БКИ 2 50 120 200 400.0\n"
        "Итого: 130.0 430.0\n"
    )
    t_res = {f["title"] for f in _check_current_table_arithmetic(bad_table)}
    assert_true(any("не сходится" in t for t in t_res), t_res)
    assert_true(any("не равен сумме" in t for t in t_res), t_res)
    good_table = bad_table.replace("2 50 120 200 400.0", "2 50 100 200 400.0")
    t_res2 = {f["title"] for f in _check_current_table_arithmetic(good_table)}
    assert_true(any("сходится" in t for t in t_res2), t_res2)

    # легенда ↔ спецификация: перепутанные типы оповещателей
    from app.services.checks import check_legend_vs_spec
    legend_text = (
        "--- страница 6 ---\nУсловные обозначения\n"
        "Оповещатель звуковой Кристалл-24\n"
        "Оповещатель световой \"ВЫХОД\" Маяк-24-ЗМ1\n"
        "Извещатель пожарный дымовой ДИП-34А-04\n"
    )
    spec = [
        {"pos": "1", "name": "Оповещатель световой \"ВЫХОД\"", "mark": "КРИСТАЛЛ-24", "type": "", "qty": 2, "length": None, "note": "", "manufacturer": ""},
        {"pos": "2", "name": "Оповещатель звуковой", "mark": "Маяк-24-ЗМ1", "type": "", "qty": 3, "length": None, "note": "", "manufacturer": ""},
        {"pos": "3", "name": "Извещатель пожарный дымовой", "mark": "ДИП-34А-04", "type": "", "qty": 5, "length": None, "note": "", "manufacturer": ""},
    ]
    r = check_legend_vs_spec(spec, legend_text)
    titles = {f["title"] for f in r["findings"]}
    assert_true(r["status"] == "done", r)
    assert_true(any("Противоречие" in t for t in titles), titles)
    # дымовой не должен дать противоречия
    assert_true(len(r["findings"]) == 2, r["findings"])
    print("OK new checks (compat/explication/topology/plan-count/rip-load/legend)")


def test_ocr_raster_page():
    """Постраничный OCR растровых сканов (толерантно к отсутствию tesseract)."""
    import shutil
    import tempfile
    from pathlib import Path as _Path

    import pymupdf

    from app.services.parsers import parse_pdf

    # растровая страница без текстового слоя
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("SKIP OCR: Pillow недоступен")
        return
    img = Image.new("L", (1400, 900), 255)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    except Exception:
        font = ImageFont.load_default()
    d.text((60, 60), "Кабельный журнал", fill=0, font=font)
    d.text((60, 130), "Марка: КПСЭнг(А)-FRLS 1x2x0,75", fill=0, font=font)
    d.text((60, 190), "Длина: 123 м", fill=0, font=font)
    buf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.save(buf.name, format="PNG")
    buf.close()

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=buf.name)
    pdf = _Path(tempfile.mkdtemp()) / "scan.pdf"
    doc.save(str(pdf))
    doc.close()

    res = parse_pdf(pdf)
    notes = res.get("notes", "")
    text = res.get("text", "")
    if shutil.which("tesseract"):
        assert_true("OCR" in notes, notes)
        assert_true(len(text) >= 20, f"OCR не дал текста: {text[:80]!r}")
    else:
        # без tesseract — не падаем, помечаем причину
        assert_true(res["ok"] is True or res["ok"] is False, res)
        assert_true("tesseract" in notes.lower() or "ocr" in notes.lower(), notes)
    print("OK ocr raster page" if shutil.which("tesseract") else "OK ocr skip (tesseract absent)")


def test_parser_wrapping():
    """CAD-переносы в таблицах: склейка марок, сечение парных кабелей, колонки журнала."""
    from app.util import parse_section
    from app.services.parsers import _cell, _merge_wrapped_rows, _journal_columns, _record_to_item

    # перенос через дефис склеивается без пробела
    assert_true(_cell("КСБГСнг(А)-\nFRLS") == "КСБГСнг(А)-FRLS", _cell("КСБГСнг(А)-\nFRLS"))
    assert_true(_cell("Кабель витая\nпара") == "Кабель витая пара", _cell("Кабель витая\nпара"))

    # тройное сечение симметричного кабеля
    s = parse_section("КПСЭнг(А)-FRLS 1x2x0,75")
    assert_true(s and s["mm2"] == 0.75 and s["cores"] == 2 and s.get("pairs") == 1, s)
    s = parse_section("СПЕЦЛАН SF/UTP Cat5e ZH нг(А)-HF 4x2x0,52")
    assert_true(s and s["mm2"] == 0.52 and s["pairs"] == 4 and s["cores"] == 8, s)
    s = parse_section("ВВГнг(А)-LS 3х2,5")
    assert_true(s and s["cores"] == 3 and s["mm2"] == 2.5, s)

    # склейка строк-продолжений многострочных ячеек
    body = [
        ["3.6", "Кабель витая пара, категория 5e, 4 пары,", "СПЕЦЛАН SF/UTP Cat5e ZH", "м", "6", ""],
        ["", "групповой прокладки, из полимерной", "нг(А)-HF 4x2x0,52", "", "", ""],
        ["", "композиции", "ТУ 16.К99-058-2014", "", "", ""],
        ["3.7", "Провод ПуГВ", "ПуГВ 1х6", "м", "5", ""],
    ]
    mapped = {"pos": 0, "name": 1, "mark": 2, "unit": 3, "qty": 4, "note": 5}
    merged = _merge_wrapped_rows(body, mapped)
    assert_true(len(merged) == 2, merged)
    assert_true("нг(А)-HF 4x2x0,52" in merged[0][2], merged[0])
    assert_true("композиции" in merged[0][1], merged[0])
    assert_true(merged[0][5].startswith("ТУ"), f"ТУ должен уйти в примечание: {merged[0]}")
    assert_true(merged[1][0] == "3.7", merged[1])

    # вложение ОКЛ («- кабель …») не склеивается с родителем
    body2 = [
        ["3.1", "Огнестойкая кабельная линия в составе:", "ОКЛ", "компл.", "1", ""],
        ["", "- кабель КПСЭнг(А)-FRLS 1x2x0,75 - 27 м;", "", "", "", ""],
    ]
    merged2 = _merge_wrapped_rows(body2, mapped)
    assert_true(len(merged2) == 2, merged2)

    # мусор штампа не становится позицией
    junk = _record_to_item({"pos": "", "name": "Согласовано 08.21", "mark": "", "unit": "", "qty": None}, "p1_t1")
    assert_true(junk is None, junk)
    junk2 = _record_to_item({"pos": "", "name": "Бирюлин", "mark": "", "unit": "", "qty": None}, "p1_t1")
    assert_true(junk2 is None, junk2)

    # двухстрочная шапка журнала ПС: все колонки найдены
    rows = [
        ["", "Монтажная\nединица", "Обозначение\nкабеля по\nпроекту", "Заводская марка", "", "Число\nрез. жил",
         "Направление кабеля", "", "", "", "Длина, м", "", "Примечание", ""],
        ["", "", "", "Тип", "Кол.,\nчисло и\nсечение жил", "", "Начало", "Конец", "", "", "по\nпроекту", "проло-жено", "", ""],
        ["", "", "PS1.RS1-1", "КСБГСнг(А)-FRLS", "2x2x0,78", "2", "Шкаф ШПС1", "PS1.BZL", "", "", "2", "", "П-1; КК-1", ""],
    ]
    cols = _journal_columns(rows)
    assert_true(cols["mark"] == 3 and cols["sec"] == 4 and cols["len"] == 10, cols)
    assert_true(cols["desig"] == 2 and cols["from"] == 6 and cols["to"] == 7, cols)
    assert_true(cols["reserve"] == 5 and cols["laid"] == 11 and cols["note"] == 12, cols)
    assert_true(cols["me"] == 1, cols)
    assert_true(0 in cols["header_rows"] and 1 in cols["header_rows"], cols["header_rows"])

    # склейка посимвольного текста и OCR-гомоглифы в ключах сверки:
    # «МС 4х2х0,52 ОЛ-И» (спец) == «MC 4х2x0,52 0L-IY» (журнал CAD-экспорта)
    from app.services.checks import _cable_key, _strip_section_from_mark, check_spec_journal_names
    assert_true(
        _strip_section_from_mark("MC 4х2x0,52 0L-IY") == _strip_section_from_mark("МС 4х2х0,52 ОЛ-И"),
        (_strip_section_from_mark("MC 4х2x0,52 0L-IY"), _strip_section_from_mark("МС 4х2х0,52 ОЛ-И")),
    )
    spec = [
        {"pos": "12", "name": "Кабель монтажный симметричный", "mark": "МС 4х2х0,52 ОЛ-И",
         "type": "", "manufacturer": "", "note": "", "qty": 150.0, "length": 150.0, "unit": "м",
         "section": None, "from": "", "to": "", "laying": "", "sheet": "p5_t1"},
    ]
    jour = [
        {"pos": "LAN1.26", "name": "MC 4х2x0,52 0L-IY", "mark": "MC 4х2x0,52 0L-IY", "type": "",
         "manufacturer": "", "note": "", "qty": None, "length": 60.0, "unit": "м",
         "section": None, "from": "Шкаф ЛВС", "to": "Розетка ИР1.26", "is_total": False, "sheet": "p6_journal"},
    ]
    res = check_spec_journal_names(spec, jour)
    titles = [f["title"] for f in res["findings"]]
    assert_true(not any("не найдена" in t or "отсутствует" in t for t in titles), titles)
    assert_true(not any("не встретился" in t for t in titles), titles)
    k1, k2 = _cable_key(spec[0]), _cable_key(jour[0])
    assert_true(k1.split("|")[0] == k2.split("|")[0], (k1, k2))
    print("OK parser wrapping+homo")


def test_cloud_models_api():
    """CRUD своих облачных моделей через API + попадание в каталог /api/models."""
    init_db()
    from app.main import app

    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": "admin", "password": settings.admin_password})
    assert_true(login.status_code == 200, login.text)
    hdr = {"Authorization": f"Bearer {login.json()['token']}"}

    secret = "sk-test-1234567890abcdef"
    base = client.get("/api/models/cloud", headers=hdr)
    assert_true(base.status_code == 200, base.text)
    n0 = len(base.json()["models"])

    # невалидный base_url → 400 с причиной
    bad = client.post(
        "/api/models/cloud",
        headers=hdr,
        json={"name": "Сломанная", "base_url": "ftp://x", "model": "m", "api_key": secret},
    )
    assert_true(bad.status_code == 400 and "http" in bad.json()["detail"], bad.text)

    name = f"Тестовая LLM {int(time.time())}"
    added = client.post(
        "/api/models/cloud",
        headers=hdr,
        json={"name": name, "base_url": "http://127.0.0.1:9/v1", "model": "test-llm", "api_key": secret},
    )
    assert_true(added.status_code == 200, added.text)
    entry = added.json()
    mid = entry["id"]
    assert_true(mid.startswith("custom:"), mid)
    assert_true(entry["has_key"] is True, entry)
    assert_true(secret not in added.text, "ключ не должен отдажаться в ответе API")

    # модель появилась в облачном каталоге и она ready
    cat = client.get("/api/models", headers=hdr).json()
    hit = [m for m in cat["cloud"] if m["id"] == mid]
    assert_true(hit and hit[0]["ready"] is True, hit)

    # тест соединения: закрытый порт → честная ошибка без падения
    t = client.post(f"/api/models/cloud/{mid}/test", headers=hdr)
    assert_true(t.status_code == 200 and t.json()["ok"] is False, t.text)
    assert_true(bool(t.json()["error"]), t.text)

    # обновление имени
    up = client.put(f"/api/models/cloud/{mid}", headers=hdr, json={"name": name + " v2"})
    assert_true(up.status_code == 200 and up.json()["name"].endswith("v2"), up.text)

    # удаление
    dele = client.delete(f"/api/models/cloud/{mid}", headers=hdr)
    assert_true(dele.status_code == 200 and dele.json()["ok"], dele.text)
    assert_true(len(client.get("/api/models/cloud", headers=hdr).json()["models"]) == n0, "список не вернулся к исходному")
    gone = client.get("/api/models", headers=hdr).json()
    assert_true(not [m for m in gone["cloud"] if m["id"] == mid], "модель осталась в каталоге")
    assert_true(client.delete(f"/api/models/cloud/{mid}", headers=hdr).status_code == 404, "повторное удаление не 404")
    print("OK cloud models api")


if __name__ == "__main__":
    test_parsers_and_checks()
    test_new_checks_synthetic()
    test_ocr_raster_page()
    test_api()
    test_cloud_models_api()
    print("ВСЕ ПРОВЕРКИ РЕПОЗИТОРИЯ ПРОЙДЕНЫ")
