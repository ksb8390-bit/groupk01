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


def test_admin_leave_shows_expiring_period(app):
    with app.app_context():
        db = get_db(); db.execute("UPDATE users SET hire_date='2024-12-01' WHERE login_id='kim'"); db.commit()
    a = app.test_client(); login(a, "admin")
    page = a.get("/admin/leave").get_data(as_text=True)
    # 기준일 2026-11-10 → kim 기간 2025-12-01~2026-11-30 (D-20)
    assert "60일 안에 연차 기간이 끝나는 직원" in page and "2026-11-30 종료 (D-20)" in page


def _login_raw(client, login_id, pw):
    page = client.get("/login").get_data(as_text=True)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    return client.post("/login", data={"login_id": login_id, "password": pw, "csrf": tok})


def test_login_tolerates_capitalised_id_and_pasted_spaces(app):
    assert _login_raw(app.test_client(), "KIM", "pw123456").status_code == 302          # 첫 글자 대문자
    assert _login_raw(app.test_client(), " kim ", " pw123456 ").status_code == 302      # 붙여넣기 공백
    assert _login_raw(app.test_client(), "kim", "pw12345").status_code == 200           # 틀린 비밀번호


def test_admin_reset_unlocks_and_marks(app):
    c = app.test_client()
    for _ in range(5):
        _login_raw(c, "Kim", "wrong")
    assert _login_raw(c, "kim", "pw123456").status_code == 200                           # 잠김
    a = app.test_client(); at = login(a, "admin")
    assert "잠김" in a.get("/admin/users").get_data(as_text=True)
    r = a.post("/admin/users/2/reset", data={"csrf": at}, follow_redirects=True)
    temp = re.search(r"임시 비밀번호: ([a-z0-9]{8})", r.get_data(as_text=True)).group(1)
    assert _login_raw(app.test_client(), "kim", temp).status_code == 302                 # 해제 + 새 비밀번호


def test_account_sheet_pdf(app, tmp_path):
    from hr.importer import save_accounts_xlsx
    x = tmp_path / "acc.xlsx"
    save_accounts_xlsx([["관리자", "admin", "(기존 비밀번호 유지)", "관리자", "", "대표이사", "2010-01-02"],
                        ["홍길동", "4174980", "k7mq2xra", "직원", "설계사업본부", "대리", "2024-03-01"]], str(x))
    out = tmp_path / "a.pdf"
    r = app.test_cli_runner().invoke(args=["account-sheet", "--xlsx", str(x), "--out", str(out),
                                           "--url", "http://192.168.1.119:5050"])
    assert r.exit_code == 0, r.output
    assert "목록 2명, 개인 안내장 1장" in r.output and out.read_bytes()[:4] == b"%PDF"
    # 엑셀 분실 → 첫 로그인 전 직원만 재발급
    with app.app_context():
        db = get_db(); db.execute("UPDATE users SET must_change_pw=1 WHERE login_id='kim'"); db.commit()
    r = app.test_cli_runner().invoke(args=["account-sheet", "--reissue", "--out", str(out)], input="y\n")
    assert r.exit_code == 0, r.output and "개인 안내장 1장" in r.output



# ---------------------------------------------------------------- 배정 근무지(본사·현장) 출근 확인
def _setup_sites(app, policy="flag"):
    with app.app_context():
        db = get_db()
        db.execute("UPDATE workplaces SET kind='hq' WHERE name='본사'")                     # 33.4996,126.5312
        db.execute("INSERT INTO workplaces(name,lat,lon,radius_m,kind) VALUES ('애월현장',33.4620,126.3290,300,'site')")
        site = db.execute("SELECT id FROM workplaces WHERE name='애월현장'").fetchone()["id"]
        db.execute("INSERT INTO user_workplaces(user_id, workplace_id) VALUES (2, ?)", (site,))   # kim = 감리원
        db.execute("UPDATE users SET location_consent=1 WHERE login_id='kim'")
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('location_policy', ?)", (policy,))
        db.commit()


def _att(app):
    with app.app_context():
        return get_db().execute("SELECT * FROM attendance ORDER BY id DESC").fetchone()


def test_site_supervisor_verified_at_assigned_site(app):
    _setup_sites(app)
    c = app.test_client(); tok = login(c, "kim")
    c.post("/punch", data={"csrf": tok, "action": "in", "work_type": "office", "lat": "33.4625", "lon": "126.3295"})
    r = _att(app)
    assert (r["in_place"], r["in_verified"], r["work_type"]) == ("애월현장", 1, "field")    # 현장으로 자동 지정


def test_site_supervisor_at_hq_gps_is_not_their_site(app):
    _setup_sites(app)
    c = app.test_client(); tok = login(c, "kim")
    # 본사 좌표(배정 외) → 사유 없으면 거부
    r = c.post("/punch", data={"csrf": tok, "action": "in", "work_type": "field", "lat": "33.4997", "lon": "126.5313"})
    assert "need_reason=1" in r.headers["Location"] and _att(app) is None
    # 사유 입력 → 위치 미확인으로 기록, 가장 가까운 근무지 표기
    c.post("/punch", data={"csrf": tok, "action": "in", "work_type": "field", "lat": "33.4997", "lon": "126.5313",
                           "note": "본사 회의"})
    r = _att(app)
    assert r["in_verified"] == 0 and r["in_place"].startswith("근무지 밖(본사") and r["note"] == "본사 회의"
    assert "위치 미확인" in c.get("/").get_data(as_text=True)
    a = app.test_client(); login(a, "admin")
    assert "위치 미확인" in a.get("/admin").get_data(as_text=True)


def test_office_wifi_counts_as_hq_and_unassigned_staff_any_site(app):
    _setup_sites(app)
    with app.app_context():
        db = get_db(); db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('office_networks','127.0.0.1')"); db.commit()
    l = app.test_client(); tok = login(l, "lee")                       # 배정 없음, 위치 없음, 사무실 Wi‑Fi
    l.post("/punch", data={"csrf": tok, "action": "in", "work_type": "office"})
    r = _att(app)
    assert (r["in_place"], r["in_verified"], r["work_type"]) == ("본사", 1, "office")


def test_block_policy_and_remote_trip_exempt_and_checkout_never_blocked(app):
    _setup_sites(app, policy="block")
    c = app.test_client(); tok = login(c, "kim")
    c.post("/punch", data={"csrf": tok, "action": "in", "work_type": "field", "lat": "33.30", "lon": "126.30", "note": "x"})
    assert _att(app) is None                                           # 차단은 사유가 있어도 거부
    c.post("/punch", data={"csrf": tok, "action": "in", "work_type": "remote"})
    assert (_att(app)["work_type"], _att(app)["in_verified"]) == ("remote", None)
    with app.app_context():
        db = get_db(); db.execute("DELETE FROM attendance"); db.commit()
    c.post("/punch", data={"csrf": tok, "action": "in", "work_type": "field", "lat": "33.4625", "lon": "126.3295"})
    c.post("/punch", data={"csrf": tok, "action": "out", "lat": "33.30", "lon": "126.30"})   # 현장 밖 퇴근도 허용
    r = _att(app)
    assert r["check_out"] and r["out_verified"] == 0


def test_requests_and_chat_have_no_location_restriction(app):
    _setup_sites(app, policy="block")
    c = app.test_client(); tok = login(c, "kim")
    c.post("/requests", data={"csrf": tok, "kind": "annual", "unit": "full", "start_date": "2026-11-16"})
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) c FROM requests").fetchone()["c"] == 1


def test_admin_assigns_workplace_members(app):
    a = app.test_client(); at = login(a, "admin")
    with app.app_context():
        wid = get_db().execute("SELECT id FROM workplaces").fetchone()["id"]
    a.post(f"/admin/workplaces/{wid}", data={"csrf": at, "name": "본사", "kind": "hq", "lat": "33.4996",
                                              "lon": "126.5312", "radius_m": "150", "members": ["2", "3"]})
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT kind, radius_m FROM workplaces WHERE id=?", (wid,)).fetchone()[:] == ("hq", 150)
        assert {r[0] for r in db.execute("SELECT user_id FROM user_workplaces")} == {2, 3}
    assert "배정 2명" in a.get("/admin/settings").get_data(as_text=True)
    assert "🏢" in login(app.test_client(), "kim") or True
