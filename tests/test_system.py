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


if __name__ == "__main__":
    test_parsers_and_checks()
    test_api()
    print("ВСЕ ПРОВЕРКИ РЕПОЗИТОРИЯ ПРОЙДЕНЫ")
