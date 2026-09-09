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


if __name__ == "__main__":
    test_parsers_and_checks()
    test_new_checks_synthetic()
    test_api()
    print("ВСЕ ПРОВЕРКИ РЕПОЗИТОРИЯ ПРОЙДЕНЫ")
