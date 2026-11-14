"""실사용 시나리오·경계 상황 테스트."""
import re
from datetime import datetime
import pytest
from tests.test_app import CLOCK, _approve_all, app, login  # noqa: F401  (fixture 재사용)
from hr.db import get_db

def cnt(app, sql="SELECT COUNT(*) c FROM requests"):
    with app.app_context():
        return get_db().execute(sql).fetchone()["c"]

def test_overnight_checkout(app):
    c = app.test_client(); tok = login(c, "kim")
    CLOCK["now"] = datetime(2026, 11, 10, 9, 0); c.post("/punch", data={"csrf": tok, "action": "in"})
    CLOCK["now"] = datetime(2026, 11, 11, 2, 30); c.post("/punch", data={"csrf": tok, "action": "out"})
    with app.app_context():
        r = get_db().execute("SELECT * FROM attendance WHERE work_date='2026-11-10'").fetchone()
    assert r["check_out"] == "2026-11-11 02:30:00"

def test_duplicate_leave_blocked(app):
    c = app.test_client(); tok = login(c, "kim")
    for _ in range(2):
        c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "full", "start_date": "2026-11-16"})
    assert cnt(app) == 1

def test_half_day_on_weekend_blocked(app):
    c = app.test_client(); tok = login(c, "kim")
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "am", "start_date": "2026-11-14"})
    assert cnt(app) == 0

@pytest.mark.parametrize("data", [
    {"kind": "overtime", "start_date": "2026-11-10", "hours": "abc"},
    {"kind": "annual", "start_date": "bad"},
    {"kind": "correction", "start_date": "2026-11-09", "corr_in": "18:00", "corr_out": "09:00"},
])
def test_bad_request_input_no_500(app, data):
    c = app.test_client(); tok = login(c, "kim")
    r = c.post("/requests", data={"csrf": tok, **data})
    assert r.status_code < 500
    assert cnt(app) == 0

def test_correction_future_blocked(app):
    c = app.test_client(); tok = login(c, "kim")
    c.post("/requests", data={"csrf": tok, "kind": "correction", "start_date": "2026-12-01", "corr_in": "09:00"})
    assert cnt(app) == 0

@pytest.mark.parametrize("url", ["/my/attendance?month=2026-13", "/admin/attendance?month=xx", "/admin/export?month=2026-00"])
def test_bad_month_no_500(app, url):
    a = app.test_client(); login(a, "admin")
    assert a.get(url).status_code < 500

@pytest.mark.parametrize("data", [
    {"section": "company", "late_grace_min": "abc", "work_start": "09:00", "work_end": "18:00", "break_min": "60", "company_name": "G"},
    {"section": "company", "late_grace_min": "10", "work_start": "9시", "work_end": "18:00", "break_min": "60", "company_name": "G"},
    {"section": "workplace_add", "name": "x", "lat": "abc", "lon": "1"},
    {"section": "holiday_add", "day": "2026-02-30", "name": "x"},
])
def test_bad_settings_no_500_and_app_still_works(app, data):
    a = app.test_client(); at = login(a, "admin")
    r = a.post("/admin/settings", data={"csrf": at, **data})
    assert r.status_code < 500
    assert a.get("/").status_code == 200 and a.get("/admin").status_code == 200

def test_admin_edit_out_before_in_rejected(app):  # 익일 체크 없이는 거부
    a = app.test_client(); at = login(a, "admin")
    a.post("/admin/attendance/edit", data={"csrf": at, "user_id": 2, "work_date": "2026-11-09",
                                           "check_in": "18:00", "check_out": "09:00", "note": "x"})
    assert cnt(app, "SELECT COUNT(*) c FROM attendance") == 0

def test_admin_cannot_lock_self_out(app):
    a = app.test_client(); at = login(a, "admin")
    a.post("/admin/users", data={"csrf": at, "id": 1, "name": "admin", "role": "employee",
                                 "hire_date": "2020-01-01", "active": "0"})
    with app.app_context():
        u = get_db().execute("SELECT role, active FROM users WHERE id=1").fetchone()
    assert (u["role"], u["active"]) == ("admin", 1)

def test_manager_without_dept_cannot_see_admin(app):
    with app.app_context():
        db = get_db(); db.execute("UPDATE users SET department_id=NULL WHERE login_id IN ('lee','admin')"); db.commit()
    m = app.test_client(); login(m, "lee")
    page = m.get("/admin").get_data(as_text=True)
    assert ">admin<" not in page

def test_login_bruteforce_lockout(app):
    c = app.test_client()
    page = c.get("/login").get_data(as_text=True)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    for _ in range(6):
        c.post("/login", data={"login_id": "kim", "password": "wrong", "csrf": tok})
    r = c.post("/login", data={"login_id": "kim", "password": "pw123456", "csrf": tok})
    assert r.status_code == 200  # 잠금 → 로그인 안 됨(리다이렉트 아님)

def test_double_punch_race_no_500(app):
    c = app.test_client(); tok = login(c, "kim")
    with app.app_context():
        db = get_db(); db.execute("INSERT INTO attendance(user_id,work_date) VALUES (2,'2026-11-10')"); db.commit()
    CLOCK["now"] = datetime(2026, 11, 10, 9, 0)
    assert c.post("/punch", data={"csrf": tok, "action": "in"}).status_code < 500


def test_overnight_banner_and_cap(app):
    c = app.test_client(); tok = login(c, "kim")
    CLOCK["now"] = datetime(2026, 11, 10, 9, 0); c.post("/punch", data={"csrf": tok, "action": "in"})
    CLOCK["now"] = datetime(2026, 11, 11, 1, 0)
    assert "전날 근무 지금 퇴근 처리" in c.get("/").get_data(as_text=True)
    # 20시간 초과(다음 날 아침) → 퇴근 처리 거부, 정정 안내
    CLOCK["now"] = datetime(2026, 11, 11, 9, 30)
    c.post("/punch", data={"csrf": tok, "action": "out"})
    with app.app_context():
        assert get_db().execute("SELECT check_out FROM attendance").fetchone()["check_out"] is None
    CLOCK["now"] = datetime(2026, 11, 10, 9, 5)


def test_admin_overnight_edit_with_next_day(app):
    a = app.test_client(); at = login(a, "admin")
    a.post("/admin/attendance/edit", data={"csrf": at, "user_id": 2, "work_date": "2026-11-09",
                                           "check_in": "09:00", "check_out": "02:30", "out_next_day": "on", "note": "마감"})
    with app.app_context():
        r = get_db().execute("SELECT * FROM attendance").fetchone()
    assert r["check_out"] == "2026-11-10 02:30:00"
    page = a.get("/admin/attendance?user=2&month=2026-11").get_data(as_text=True)
    assert "16:30" in page  # 17.5h - 1h 휴게


def test_am_and_pm_half_same_day_allowed(app):
    c = app.test_client(); tok = login(c, "kim")
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "am", "start_date": "2026-11-16"})
    c.post("/requests", data={"csrf": tok, "kind": "comp", "unit": "pm", "start_date": "2026-11-16"})  # 잔여 없음 → 거부
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "pm", "start_date": "2026-11-16"})
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "am", "start_date": "2026-11-16"})  # 중복
    assert cnt(app) == 2


def test_cancel_approved_future_leave_restores_balance(app):
    from hr.app import leave_balance
    c = app.test_client(); tok = login(c, "kim")
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "full", "start_date": "2026-11-16"})
    _approve_all(app)
    with app.app_context():
        rid = get_db().execute("SELECT id FROM requests").fetchone()["id"]
    c.post(f"/requests/{rid}/cancel", data={"csrf": tok})
    with app.app_context():
        u = get_db().execute("SELECT * FROM users WHERE login_id='kim'").fetchone()
        assert leave_balance(u)["used"] == 0


def test_manager_cannot_approve_other_department(app):
    with app.app_context():
        db = get_db(); db.execute("INSERT INTO departments(name) VALUES ('CM팀')")
        db.execute("UPDATE users SET department_id=2 WHERE login_id='lee'"); db.commit()
    c = app.test_client(); tok = login(c, "kim")
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "full", "start_date": "2026-11-16"})
    m = app.test_client(); mt = login(m, "lee")
    with app.app_context():
        rid = get_db().execute("SELECT id FROM requests").fetchone()["id"]
    assert m.post(f"/admin/requests/{rid}/decide", data={"csrf": mt, "decision": "approved"}).status_code == 403
    assert "kim" not in m.get("/admin").get_data(as_text=True)


def test_manager_cannot_approve_own_request(app):
    m = app.test_client(); mt = login(m, "lee")
    m.post("/requests", data={"csrf": mt, "kind": "annual", "unit": "full", "start_date": "2026-11-16"})
    with app.app_context():
        rid = get_db().execute("SELECT id FROM requests").fetchone()["id"]
    assert m.post(f"/admin/requests/{rid}/decide", data={"csrf": mt, "decision": "approved"}).status_code == 403


def test_first_year_employee_cannot_borrow_future_leave(app):
    with app.app_context():
        db = get_db(); db.execute("UPDATE users SET hire_date='2026-09-01' WHERE login_id='kim'"); db.commit()
    c = app.test_client(); tok = login(c, "kim")
    # 11/10 기준 2개월 경과 → 2일 발생. 3일 신청은 거부, 2일은 허용
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "full", "start_date": "2026-11-16", "end_date": "2026-11-18"})
    assert cnt(app) == 0
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "full", "start_date": "2026-11-16", "end_date": "2026-11-17"})
    assert cnt(app) == 1


def test_deactivated_user_session_dropped(app):
    c = app.test_client(); login(c, "kim")
    with app.app_context():
        db = get_db(); db.execute("UPDATE users SET active=0 WHERE login_id='kim'"); db.commit()
    assert "/login" in c.get("/").headers["Location"]


def test_admin_can_view_former_employee_records(app):
    with app.app_context():
        db = get_db(); db.execute("UPDATE users SET active=0 WHERE login_id='kim'"); db.commit()
    a = app.test_client(); login(a, "admin")
    assert a.get("/admin/attendance?user=2").status_code == 200


def test_no_500_on_any_page_for_each_role(app):
    urls = ["/", "/my/attendance", "/requests", "/notices", "/password", "/admin", "/admin/approvals",
            "/admin/approvals?status=approved", "/admin/attendance", "/admin/attendance?user=2", "/admin/users",
            "/admin/users?edit=2", "/admin/leave", "/admin/settings", "/admin/audit", "/admin/export"]
    for who in ("admin", "lee", "kim"):
        c = app.test_client(); login(c, who)
        for u in urls:
            assert c.get(u).status_code in (200, 403), (who, u)
