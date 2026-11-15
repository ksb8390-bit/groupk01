# -*- coding: utf-8 -*-
"""직원 명부 가져오기 (시프티 '직원 목록' 엑셀 또는 자체 양식).

흐름: 파일 업로드 → 미리보기(아이디·권한 수정 가능) → 적용 → 임시 비밀번호 엑셀 1회 내려받기
- 같은 이름의 기존 계정이 있으면 새로 만들지 않고 부서·직위·입사일만 갱신
- 퇴사일이 있으면 비활성 계정으로 등록
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
from datetime import date, datetime

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   send_file, url_for)

from .app import ROLE_LABELS, audit, current_user, hash_pw, role_required
from .db import get_db

bp = Blueprint("imp", __name__, url_prefix="/admin/import")

ALIASES = {
    "name": ("직원", "이름", "성명", "직원명"),
    "emp_no": ("사원번호", "아이디", "로그인아이디"),
    "hire": ("입사일", "입사일자"),
    "leave": ("퇴사일", "퇴사일자"),
    "dept": ("본조직", "부서", "조직", "소속"),
    "position": ("본직무", "직위", "직책", "직급"),
    "role": ("권한",),
    "phone": ("연락처", "휴대폰", "전화번호", "휴대전화"),
}
ROLE_FROM_TEXT = {"관리자": "admin", "admin": "admin", "부서장": "manager", "manager": "manager",
                  "직원": "employee", "employee": "employee"}
NO_DEPT_ORGS = {"대표이사", "대표"}            # 대표이사를 조직명으로 쓴 경우 부서 없음 처리
MANAGER_POSITIONS = {"본부장", "부서장", "팀장", "실장"}
ADMIN_POSITIONS = {"대표이사", "대표"}
LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")
PW_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"   # 헷갈리는 문자(0,o,1,l,i) 제외


def _dir() -> str:
    d = os.path.join(current_app.instance_path, "imports")
    os.makedirs(d, exist_ok=True)
    return d


def _token_path(token: str, suffix: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        abort(404)
    return os.path.join(_dir(), token + suffix)


def _date(v) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip().replace(".", "-").replace("/", "-")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if not m:
        raise ValueError(s)
    return date(int(m[1]), int(m[2]), int(m[3])).isoformat()


def parse_workbook(data: bytes) -> tuple[list[dict], list[str]]:
    """엑셀에서 직원 행을 읽어 표준 형태로 반환. (rows, warnings)"""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    all_rows = list(ws.iter_rows(values_only=True))
    head_idx, cols = None, {}
    for i, r in enumerate(all_rows[:10]):
        cells = [str(c).strip() if c is not None else "" for c in r]
        found = {key: cells.index(a) for key, names in ALIASES.items() for a in names if a in cells}
        if "name" in found and "hire" in found:
            head_idx, cols = i, found
            break
    if head_idx is None:
        raise ValueError("머리글에서 '직원(이름)'과 '입사일' 열을 찾지 못했습니다.")
    out, warns = [], []
    for n, r in enumerate(all_rows[head_idx + 1:], start=head_idx + 2):
        get = lambda k: (r[cols[k]] if k in cols and cols[k] < len(r) else None)  # noqa: E731
        name = str(get("name") or "").strip()
        if not name:
            continue
        try:
            hire = _date(get("hire"))
            left = _date(get("leave"))
        except ValueError as e:
            warns.append(f"{n}행 {name}: 날짜 형식 오류({e}) — 건너뜀")
            continue
        if not hire:
            warns.append(f"{n}행 {name}: 입사일 없음 — 건너뜀")
            continue
        dept = str(get("dept") or "").strip() or None
        pos = str(get("position") or "").strip() or None
        if dept in NO_DEPT_ORGS:
            dept = None
        role_txt = str(get("role") or "").strip().lower()
        role = ROLE_FROM_TEXT.get(role_txt) or ("admin" if pos in ADMIN_POSITIONS else
                                                "manager" if pos in MANAGER_POSITIONS else "employee")
        emp_no = str(get("emp_no") or "").strip()
        out.append({"name": name, "emp_no": emp_no, "hire_date": hire, "leave_date": left, "dept": dept,
                    "position": pos, "role": role, "phone": str(get("phone") or "").strip() or None})
    return out, warns


def plan(rows: list[dict]) -> list[dict]:
    """기존 계정과 대조해 신규/갱신 결정, 기본 아이디 제안."""
    db = get_db()
    existing = {u["name"]: u for u in db.execute("SELECT * FROM users").fetchall()}
    taken = {u["login_id"] for u in existing.values()}
    seq = 1
    for r in rows:
        u = existing.get(r["name"])
        if u:
            r["action"], r["user_id"], r["login_id"] = "update", u["id"], u["login_id"]
            if u["role"] == "admin":
                r["role"] = "admin"           # 기존 관리자는 권한 유지
            continue
        r["action"] = "create"
        cand = r["emp_no"] if LOGIN_RE.match(r["emp_no"] or "") else ""
        while not cand or cand in taken:
            cand = f"gk{seq:02d}"
            seq += 1
        r["login_id"] = cand
        taken.add(cand)
    return rows


@bp.route("", methods=["GET"])
@role_required("admin")
def index():
    return render_template("import.html", step="upload")


@bp.route("/preview", methods=["POST"])
@role_required("admin")
def preview():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("엑셀(.xlsx) 파일을 선택하세요.")
        return redirect(url_for("imp.index"))
    try:
        rows, warns = parse_workbook(f.read())
    except Exception as e:  # noqa: BLE001 — 사용자 파일 오류는 안내만
        flash(f"파일을 읽지 못했습니다: {e}")
        return redirect(url_for("imp.index"))
    if not rows:
        flash("가져올 직원 행이 없습니다.")
        return redirect(url_for("imp.index"))
    rows = plan(rows)
    token = secrets.token_hex(16)
    with open(_token_path(token, ".json"), "w", encoding="utf-8") as fp:
        json.dump(rows, fp, ensure_ascii=False)
    depts = sorted({r["dept"] for r in rows if r["dept"]})
    return render_template("import.html", step="preview", rows=rows, warns=warns, token=token,
                           depts=depts, ROLES=ROLE_LABELS)


@bp.route("/apply", methods=["POST"])
@role_required("admin")
def apply():
    me = current_user()
    token = request.form.get("token", "")
    path = _token_path(token, ".json")
    if not os.path.exists(path):
        flash("미리보기가 만료되었습니다. 파일을 다시 올려 주세요.")
        return redirect(url_for("imp.index"))
    with open(path, encoding="utf-8") as fp:
        rows = json.load(fp)
    f = request.form
    db = get_db()
    taken = {u["login_id"]: u["id"] for u in db.execute("SELECT id, login_id FROM users").fetchall()}
    seen, errors = set(), []
    for i, r in enumerate(rows):
        r["include"] = f.get(f"inc_{i}") == "1"
        r["role"] = f.get(f"role_{i}") if f.get(f"role_{i}") in ROLE_LABELS else r["role"]
        if r["action"] == "create":
            r["login_id"] = (f.get(f"login_{i}") or "").strip()
        if not r["include"]:
            continue
        lid = r["login_id"]
        if r["action"] == "create":
            if not LOGIN_RE.match(lid):
                errors.append(f"{r['name']}: 아이디는 영문·숫자·._- 3~30자")
            elif lid in taken or lid in seen:
                errors.append(f"{r['name']}: 아이디 '{lid}' 중복")
        seen.add(lid)
    if errors:
        for e in errors:
            flash(e)
        depts = sorted({r["dept"] for r in rows if r["dept"]})
        return render_template("import.html", step="preview", rows=rows, warns=[], token=token,
                               depts=depts, ROLES=ROLE_LABELS)

    dept_ids = {d["name"]: d["id"] for d in db.execute("SELECT * FROM departments").fetchall()}
    accounts, created, updated = [], 0, 0
    for r in rows:
        if not r["include"]:
            continue
        if r["dept"] and r["dept"] not in dept_ids:
            dept_ids[r["dept"]] = db.execute("INSERT INTO departments(name) VALUES (?)", (r["dept"],)).lastrowid
        did = dept_ids.get(r["dept"]) if r["dept"] else None
        active = 0 if r["leave_date"] else 1
        if r["action"] == "update":
            cur = db.execute("SELECT role FROM users WHERE id=?", (r["user_id"],)).fetchone()
            role = "admin" if cur and cur["role"] == "admin" else r["role"]
            db.execute("UPDATE users SET department_id=?, position=?, hire_date=?, role=?, "
                       "phone=COALESCE(?, phone) WHERE id=?",
                       (did, r["position"], r["hire_date"], role, r["phone"], r["user_id"]))
            updated += 1
            accounts.append([r["name"], r["login_id"], "(기존 비밀번호 유지)", ROLE_LABELS[role], r["dept"] or "",
                             r["position"] or "", r["hire_date"]])
        else:
            pw = "".join(secrets.choice(PW_CHARS) for _ in range(8))
            db.execute("INSERT INTO users(login_id,password_hash,name,role,department_id,position,phone,hire_date,"
                       "active,must_change_pw) VALUES (?,?,?,?,?,?,?,?,?,1)",
                       (r["login_id"], hash_pw(pw), r["name"], r["role"], did, r["position"], r["phone"],
                        r["hire_date"], active))
            created += 1
            accounts.append([r["name"], r["login_id"], pw, ROLE_LABELS[r["role"]], r["dept"] or "",
                             r["position"] or "", r["hire_date"]])
    audit(me["id"], "import_employees", f"신규 {created} / 갱신 {updated}")
    db.commit()
    os.remove(path)

    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "계정"
    ws.append(["이름", "아이디", "임시 비밀번호", "권한", "부서", "직위", "입사일"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
    for a in accounts:
        ws.append(a)
    for col, w in zip("ABCDEFG", (10, 14, 16, 10, 18, 10, 12)):
        ws.column_dimensions[col].width = w
    ws.append([])
    ws.append(["※ 첫 로그인 시 비밀번호 변경이 강제됩니다. 전달 후 이 파일은 삭제하세요."])
    wb.save(_token_path(token, "-accounts.xlsx"))
    return render_template("import.html", step="done", token=token, created=created, updated=updated)


@bp.route("/accounts/<token>")
@role_required("admin")
def accounts(token):
    path = _token_path(token, "-accounts.xlsx")
    if not os.path.exists(path):
        flash("계정 파일은 한 번만 내려받을 수 있습니다. 비밀번호를 잊은 직원은 '직원' 메뉴에서 초기화하세요.")
        return redirect(url_for("admin_users"))
    with open(path, "rb") as fp:
        data = fp.read()
    os.remove(path)                            # 임시 비밀번호 파일은 1회만 제공
    return send_file(io.BytesIO(data), as_attachment=True, download_name="직원계정_임시비밀번호.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def register(app):
    app.register_blueprint(bp)
