"""직원 가져오기(시프티 엑셀) · 사무실 Wi‑Fi(공인 IP) 출근 인증 테스트."""
import io
import re
from datetime import datetime

import openpyxl

from tests.test_app import CLOCK, app, login  # noqa: F401
from hr.db import get_db


def shiftee_xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["사원번호", "직원", "입사일", "퇴사일", "본조직", "본직무", "근로정보명", "적용 시점",
               "소정근로규칙 - 단위기간 시작일자", "최대근로규칙 - 단위기간 시작일자", None])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


SAMPLE = [
    ["474505", "홍대표", "2008-12-01", None, "대표이사", "대표이사", "선택근무제", "2023-01-01", "", ""],
    ["82688", "이본부", "2012-03-02", None, "건설사업관리본부", "본부장", "선택근무제", "2023-01-01", "", ""],
    [None, "최대리", "2025-07-01", None, "설계사업본부", "대리", "선택근무제", "2025-07-01", "", ""],
    ["4174980", "박퇴사", "2020-01-01", "2026-06-30", "설계사업본부", "과장", "선택근무제", "2023-01-01", "", ""],
    ["9", "kim", "2024-03-01", None, "설계사업본부", "과장", "선택근무제", "2023-01-01", "", ""],   # 기존 계정과 이름 같음
]


def _preview(client, tok, rows=SAMPLE):
    r = client.post("/admin/import/preview", data={"csrf": tok, "file": (shiftee_xlsx(rows), "SHIFTEE-EMPLOYEES.xlsx")},
                    content_type="multipart/form-data")
    html = r.get_data(as_text=True)
    return html, re.search(r'name="token" value="([0-9a-f]+)"', html).group(1)


def _apply_all(client, tok, html, token, overrides=None):
    n = len(re.findall(r'name="inc_\d+"', html))
    form = {"csrf": tok, "token": token}
    for i in range(n):
        form[f"inc_{i}"] = "1"
        form[f"role_{i}"] = re.search(rf'name="role_{i}".*?<option value="(\w+)" selected', html, re.S).group(1)
        m = re.search(rf'name="login_{i}" value="([^"]*)"', html)
        if m:
            form[f"login_{i}"] = m.group(1)
    form.update(overrides or {})
    return client.post("/admin/import/apply", data=form)


def test_import_shiftee_employees(app):
    a = app.test_client(); tok = login(a, "admin")
    html, token = _preview(a, tok)
    assert html.count(">신규<") == 4 and html.count(">갱신<") == 1
    r = _apply_all(a, tok, html, token)
    assert "신규 계정 <b>4명</b>" in r.get_data(as_text=True)
    with app.app_context():
        db = get_db()
        u = {r["name"]: r for r in db.execute(
            "SELECT u.*, d.name AS dept FROM users u LEFT JOIN departments d ON d.id=u.department_id").fetchall()}
    assert u["홍대표"]["role"] == "admin" and u["홍대표"]["dept"] is None and u["홍대표"]["login_id"] == "474505"
    assert u["이본부"]["role"] == "manager" and u["이본부"]["dept"] == "건설사업관리본부"
    assert u["최대리"]["login_id"].startswith("gk") and u["최대리"]["must_change_pw"] == 1
    assert u["박퇴사"]["active"] == 0
    assert u["kim"]["login_id"] == "kim" and u["kim"]["position"] == "과장" and u["kim"]["hire_date"] == "2024-03-01"
    # 임시 비밀번호 파일: 1회만
    dl = a.get(f"/admin/import/accounts/{token}")
    ws = openpyxl.load_workbook(io.BytesIO(dl.data)).active
    accts = {r[0]: r for r in ws.iter_rows(min_row=2, values_only=True) if r and r[1]}
    assert len(accts["최대리"][2]) == 8 and accts["kim"][2] == "(기존 비밀번호 유지)"
    assert a.get(f"/admin/import/accounts/{token}").status_code == 302
    # 새 계정으로 로그인 → 비밀번호 변경 강제
    c = app.test_client()
    t = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
    c.post("/login", data={"login_id": accts["최대리"][1], "password": accts["최대리"][2], "csrf": t})
    assert "/password" in c.get("/").headers["Location"]


def test_import_rejects_duplicate_login_and_keeps_preview(app):
    a = app.test_client(); tok = login(a, "admin")
    html, token = _preview(a, tok)
    r = _apply_all(a, tok, html, token, {"login_0": "kim"})        # 이미 있는 아이디
    assert "중복" in r.get_data(as_text=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 3


def test_import_bad_file_and_permission(app):
    a = app.test_client(); tok = login(a, "admin")
    r = a.post("/admin/import/preview", data={"csrf": tok, "file": (io.BytesIO(b"not excel"), "x.xlsx")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "파일을 읽지 못했습니다" in r.get_data(as_text=True)
    k = app.test_client(); login(k, "kim")
    assert k.get("/admin/import").status_code == 403


def test_office_network_checkin(app):
    with app.app_context():
        db = get_db(); db.execute("UPDATE settings SET value='1' WHERE key='require_location'"); db.commit()
    k = app.test_client(); tok = login(k, "kim")
    CLOCK["now"] = datetime(2026, 11, 10, 8, 50)
    # 사무실 네트워크 미등록 + GPS 없음 → 사무실 출근 거부
    k.post("/punch", data={"csrf": tok, "action": "in", "work_type": "office"})
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) c FROM attendance").fetchone()["c"] == 0
    # 관리자가 현재 주소(테스트 127.0.0.1)를 사무실로 등록
    a = app.test_client(); at = login(a, "admin")
    a.post("/admin/settings", data={"csrf": at, "section": "office_network", "office_networks": "",
                                    "office_network_name": "그룹케이 Wi‑Fi", "add_current": "1"})
    assert "등록된 사무실 네트워크" in a.get("/admin/settings").get_data(as_text=True)
    k.post("/punch", data={"csrf": tok, "action": "in", "work_type": "office"})
    with app.app_context():
        r = get_db().execute("SELECT * FROM attendance").fetchone()
    assert r["in_place"] == "그룹케이 Wi‑Fi" and r["in_ip"] == "127.0.0.1"
    CLOCK["now"] = datetime(2026, 11, 10, 9, 5)


def test_office_network_cidr_and_spoofing(app):
    a = app.test_client(); at = login(a, "admin")
    r = a.post("/admin/settings", data={"csrf": at, "section": "office_network", "office_networks": "abc"},
               follow_redirects=True)
    assert "IP 주소 형식" in r.get_data(as_text=True)
    a.post("/admin/settings", data={"csrf": at, "section": "office_network", "office_networks": "211.1.2.0/24"})
    from hr.app import client_ip, in_office_network
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "8.8.8.8"}, headers={"X-Forwarded-For": "211.1.2.9"}):
        assert client_ip() == "8.8.8.8" and not in_office_network(client_ip())    # 외부에서 헤더 위조 무시
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}, headers={"X-Forwarded-For": "211.1.2.9"}):
        assert client_ip() == "211.1.2.9" and in_office_network(client_ip())      # 서버 내 프록시(Caddy) 경유
