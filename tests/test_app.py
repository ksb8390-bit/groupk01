import re
from datetime import datetime

import pytest
from werkzeug.security import generate_password_hash

from hr.app import create_app
from hr.db import get_db

CLOCK = {"now": datetime(2026, 11, 10, 9, 5)}  # 화요일


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db"), "SECRET_KEY": "x",
                      "NOW": lambda: CLOCK["now"]})
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO departments(name) VALUES ('설계팀')")
        for login, role, hire in (("admin", "admin", "2020-01-01"), ("kim", "employee", "2024-03-01"),
                                  ("lee", "manager", "2021-05-01")):
            db.execute("INSERT INTO users(login_id,password_hash,name,role,department_id,hire_date,must_change_pw) "
                       "VALUES (?,?,?,?,1,?,0)", (login, generate_password_hash("pw123456"), login, role, hire))
        db.execute("INSERT INTO workplaces(name,lat,lon,radius_m) VALUES ('본사',33.4996,126.5312,200)")
        db.commit()
    return app


def login(client, who):
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/login", data={"login_id": who, "password": "pw123456", "csrf": token})
    assert r.status_code == 302
    return token


def test_requires_login(app):
    assert app.test_client().get("/").status_code == 302


def test_csrf_enforced(app):
    c = app.test_client()
    login(c, "kim")
    assert c.post("/punch", data={"action": "in"}).status_code == 400


def test_punch_flow_with_geofence(app):
    c = app.test_client()
    tok = login(c, "kim")
    c.post("/consent", data={"csrf": tok, "agree": "1"})
    CLOCK["now"] = datetime(2026, 11, 10, 9, 5)
    c.post("/punch", data={"csrf": tok, "action": "in", "work_type": "office", "lat": "33.4997", "lon": "126.5313"})
    CLOCK["now"] = datetime(2026, 11, 10, 18, 30)
    c.post("/punch", data={"csrf": tok, "action": "out", "lat": "33.4997", "lon": "126.5313"})
    with app.app_context():
        r = get_db().execute("SELECT * FROM attendance").fetchone()
        assert r["late_min"] == 5 and r["in_place"] == "본사" and r["check_out"].endswith("18:30:00")
    assert "8:25" in c.get("/").get_data(as_text=True)  # 9:05~18:30 - 1h


def test_require_location_blocks_office_outside(app):
    with app.app_context():
        get_db().execute("UPDATE settings SET value='1' WHERE key='require_location'")
        get_db().commit()
    c = app.test_client()
    tok = login(c, "kim")
    c.post("/consent", data={"csrf": tok, "agree": "1"})
    c.post("/punch", data={"csrf": tok, "action": "in", "work_type": "office", "lat": "33.30", "lon": "126.30"})
    c.post("/punch", data={"csrf": tok, "action": "in", "work_type": "field", "lat": "33.30", "lon": "126.30"})
    with app.app_context():
        rows = get_db().execute("SELECT work_type FROM attendance").fetchall()
        assert [r["work_type"] for r in rows] == ["field"]


def test_leave_request_approval_and_balance(app):
    c = app.test_client()
    tok = login(c, "kim")
    # 2026-11-16(월)~11-18(수) 3일
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "full",
                              "start_date": "2026-11-16", "end_date": "2026-11-18", "reason": "개인"})
    m = app.test_client()
    mt = login(m, "lee")
    with app.app_context():
        rid = get_db().execute("SELECT id, days FROM requests").fetchone()
        assert rid["days"] == 3
    r = m.post(f"/admin/requests/{rid['id']}/decide", data={"csrf": mt, "decision": "approved"})
    assert r.status_code == 302
    with app.app_context():
        from hr.app import leave_balance
        from flask import session  # noqa
        u = get_db().execute("SELECT * FROM users WHERE login_id='kim'").fetchone()
        b = leave_balance(u)
        assert b["granted"] == 15 and b["used"] == 3 and b["remain"] == 12


def test_insufficient_leave_rejected(app):
    c = app.test_client()
    tok = login(c, "kim")
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "full",
                              "start_date": "2026-11-16", "end_date": "2026-12-31"})
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) c FROM requests").fetchone()["c"] == 0


def test_employee_cannot_access_admin(app):
    c = app.test_client()
    login(c, "kim")
    assert c.get("/admin").status_code == 403
    assert c.get("/admin/users").status_code == 403


def test_correction_applies_on_approval(app):
    c = app.test_client()
    tok = login(c, "kim")
    c.post("/requests", data={"csrf": tok, "kind": "correction", "start_date": "2026-11-09",
                              "corr_in": "08:55", "corr_out": "18:10", "reason": "현장 직행"})
    a = app.test_client()
    at = login(a, "admin")
    with app.app_context():
        rid = get_db().execute("SELECT id FROM requests").fetchone()["id"]
    a.post(f"/admin/requests/{rid}/decide", data={"csrf": at, "decision": "approved"})
    with app.app_context():
        r = get_db().execute("SELECT * FROM attendance WHERE work_date='2026-11-09'").fetchone()
        assert r["check_in"] == "2026-11-09 08:55:00" and r["late_min"] == 0


def test_admin_pages_and_export(app):
    a = app.test_client()
    login(a, "admin")
    for url in ("/admin", "/admin/approvals", "/admin/attendance", "/admin/attendance?user=2",
                "/admin/users", "/admin/leave", "/admin/settings", "/admin/audit", "/notices",
                "/requests", "/my/attendance", "/manifest.webmanifest", "/sw.js"):
        assert a.get(url).status_code == 200, url
    r = a.get("/admin/export?month=2026-11")
    assert r.status_code == 200 and r.data[:2] == b"PK"


def test_create_user_forces_password_change(app):
    a = app.test_client()
    at = login(a, "admin")
    r = a.post("/admin/users", data={"csrf": at, "login_id": "park", "password": "temp1234", "name": "박",
                                     "role": "employee", "hire_date": "2026-11-01"}, follow_redirects=True)
    assert "park" in r.get_data(as_text=True)
    p = app.test_client()
    page = p.get("/login").get_data(as_text=True)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    p.post("/login", data={"login_id": "park", "password": "temp1234", "csrf": tok})
    assert "/password" in p.get("/").headers["Location"]
