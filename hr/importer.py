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
    # 대표이사(관리자) 행이 하나뿐이고 기존 관리자 계정도 하나뿐이면 같은 사람으로 보고 기존 계정을 갱신
    # (앱 설치 때 만든 관리자 계정과 시프티 이름이 달라 중복 생성되는 것을 방지)
    new_admins = [r for r in rows if r["action"] == "create" and r["role"] == "admin"]
    matched = {r.get("user_id") for r in rows if r["action"] == "update"}
    admins = [u for u in existing.values() if u["role"] == "admin" and u["active"] and u["id"] not in matched]
    if len(new_admins) == 1 and len(admins) == 1:
        r, u = new_admins[0], admins[0]
        taken.discard(r["login_id"])
        r.update(action="update", user_id=u["id"], login_id=u["login_id"], rename_from=u["name"])
    return rows


def apply_rows(rows: list[dict], actor_id) -> tuple[list[list], int, int]:
    """include=True 행을 DB에 반영하고 (계정 목록, 신규 수, 갱신 수) 반환."""
    db = get_db()
    dept_ids = {d["name"]: d["id"] for d in db.execute("SELECT * FROM departments").fetchall()}
    accounts, created, updated = [], 0, 0
    for r in rows:
        if not r.get("include", True):
            continue
        if r["dept"] and r["dept"] not in dept_ids:
            dept_ids[r["dept"]] = db.execute("INSERT INTO departments(name) VALUES (?)", (r["dept"],)).lastrowid
        did = dept_ids.get(r["dept"]) if r["dept"] else None
        active = 0 if r["leave_date"] else 1
        if r["action"] == "update":
            cur = db.execute("SELECT role FROM users WHERE id=?", (r["user_id"],)).fetchone()
            role = "admin" if cur and cur["role"] == "admin" else r["role"]
            db.execute("UPDATE users SET name=?, department_id=?, position=?, hire_date=?, role=?, "
                       "phone=COALESCE(?, phone) WHERE id=?",
                       (r["name"], did, r["position"], r["hire_date"], role, r["phone"], r["user_id"]))
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
    audit(actor_id, "import_employees", f"신규 {created} / 갱신 {updated}")
    db.commit()
    return accounts, created, updated


def save_accounts_xlsx(accounts: list[list], path: str):
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
    wb.save(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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

    accounts, created, updated = apply_rows(rows, me["id"])
    os.remove(path)
    save_accounts_xlsx(accounts, _token_path(token, "-accounts.xlsx"))
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


# ================================================================ 휴가 가져오기 (시프티)
SHIFTEE_NOTE = "시프티 이관"


def _cells(row):
    return [str(c).strip() if c is not None else "" for c in row]


def detect_leave_file(data: bytes) -> str | None:
    """'report'(직원별 휴가 현황) / 'records'(휴가 사용 내역) / None"""
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
    for r in list(ws.iter_rows(values_only=True))[:5]:
        c = _cells(r)
        if "남은 휴가 일수" in c and "휴가 그룹" in c:
            return "report"
        if "차감 일수" in c and "휴가 유형" in c:
            return "records"
    return None


def _table(data: bytes, must: str):
    import openpyxl
    rows = list(openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
                .iter_rows(values_only=True))
    for i, r in enumerate(rows[:5]):
        c = _cells(r)
        if must in c:
            idx = {name: j for j, name in enumerate(c) if name}
            return idx, [r for r in rows[i + 1:] if r and r[0]]
    raise ValueError(f"'{must}' 열을 찾지 못했습니다.")


def parse_leave_report(data: bytes) -> dict:
    """이름 → 연차휴가 {total, used, remain, period}"""
    idx, rows = _table(data, "남은 휴가 일수")
    out = {}
    for r in rows:
        g = lambda k: r[idx[k]] if idx.get(k) is not None and idx[k] < len(r) else None  # noqa: E731
        if str(g("휴가 그룹") or "").strip() != "연차휴가":
            continue
        out[str(g("이름")).strip()] = {"total": float(g("총 휴가 일수") or 0), "used": float(g("사용한 휴가 일수") or 0),
                                     "remain": float(g("남은 휴가 일수") or 0), "period": str(g("산정 기간") or "")}
    return out


def _minutes(v) -> int:
    s = str(v or "")
    m = re.match(r"(?:(\d+) days?, )?(\d+):(\d{2})(?::\d{2})?$", s)
    if not m:
        return 0
    return int(m[1] or 0) * 1440 + int(m[2]) * 60 + int(m[3])


def _kind(group: str, typ: str) -> str | None:
    if group.startswith("연차"):
        return "annual"
    if group.startswith("경조"):
        return "family"
    if group.startswith("무급"):
        return "sick" if "병가" in typ else "unpaid"
    if group.startswith("포상"):
        return "reward"
    if group.startswith("출산"):
        return "maternity"
    if group.startswith("공가"):
        return "official"
    if "대체" in group or "보상" in group:
        return "comp"
    return None


def parse_leave_records(data: bytes) -> tuple[list[dict], list[str]]:
    from . import rules
    idx, rows = _table(data, "차감 일수")
    out, warns = [], []
    for n, r in enumerate(rows, start=2):
        g = lambda k: r[idx[k]] if idx.get(k) is not None and idx[k] < len(r) else None  # noqa: E731
        name, group, typ = (str(g(k) or "").strip() for k in ("직원", "휴가 그룹", "휴가 유형"))
        kind = _kind(group, typ)
        st, et = str(g("시작 시간") or ""), str(g("종료 시간") or "")
        try:
            sd, ed = date.fromisoformat(st[:10]), date.fromisoformat(et[:10])
            days = float(g("차감 일수") or 0)
        except ValueError:
            warns.append(f"{n}행 {name}: 날짜·일수 형식 오류 — 건너뜀")
            continue
        if not kind:
            warns.append(f"{n}행 {name}: 알 수 없는 휴가 그룹 '{group}' — 건너뜀")
            continue
        t0, t1 = st[11:16], et[11:16]
        rec = {"name": name, "kind": kind, "type": typ, "start_date": sd.isoformat(), "end_date": ed.isoformat(),
               "start_time": None, "end_time": None, "minutes": 0, "days": days,
               "reason": (str(g("사유") or "").strip() or typ)[:300]}
        if t0 == "00:00" and t1 == "23:59":
            rec["unit"] = "full"
            if days == 0 and kind != "annual":
                rec["days"] = rules.leave_days(sd, ed, "full")
        elif "반차" in typ and abs(days - 0.5) < 1e-9:
            rec["unit"] = "am" if t0 < "12:00" else "pm"
        else:
            rec.update(unit="hours", start_time=t0, end_time=t1, minutes=_minutes(g("유급 시간")))
        if typ and typ not in rec["reason"]:
            rec["reason"] = f"[{typ}] {rec['reason']}"
        out.append(rec)
    return out, warns


def import_leaves(records: list[dict] | None, report: dict | None, actor_id) -> dict:
    """휴가 내역 등록(중복 건너뜀) + 연차 잔여를 시프티 값에 맞춤. 여러 번 실행해도 결과 동일."""
    from .app import leave_balance, now
    db = get_db()
    users = {u["name"]: u for u in db.execute("SELECT * FROM users").fetchall()}
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    res = {"added": 0, "dup": 0, "no_user": set(), "adjusted": [], "matched": 0}
    for r in records or []:
        u = users.get(r["name"])
        if not u:
            res["no_user"].add(r["name"])
            continue
        if db.execute("SELECT 1 FROM requests WHERE user_id=? AND kind=? AND start_date=? AND end_date=? "
                      "AND COALESCE(start_time,'')=? AND unit=? AND decision_note=?",
                      (u["id"], r["kind"], r["start_date"], r["end_date"], r["start_time"] or "", r["unit"],
                       SHIFTEE_NOTE)).fetchone():
            res["dup"] += 1
            continue
        db.execute("INSERT INTO requests(user_id,kind,unit,start_date,end_date,days,minutes,start_time,end_time,reason,"
                   "status,decided_at,decision_note,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,'approved',?,?,?)",
                   (u["id"], r["kind"], r["unit"], r["start_date"], r["end_date"], r["days"], r["minutes"],
                    r["start_time"], r["end_time"], r["reason"], stamp, SHIFTEE_NOTE, stamp))
        res["added"] += 1
    for name, rep in (report or {}).items():
        u = users.get(name)
        if not u:
            res["no_user"].add(name)
            continue
        db.execute("DELETE FROM leave_adjustments WHERE user_id=? AND reason LIKE ?", (u["id"], SHIFTEE_NOTE + "%"))
        bal = leave_balance(u)
        diff = round(rep["remain"] - bal["remain"], 4)
        if abs(diff) > 1e-9:
            db.execute("INSERT INTO leave_adjustments(user_id,period_start,days,reason,created_by,created_at) "
                       "VALUES (?,?,?,?,?,?)",
                       (u["id"], bal["period_start"].isoformat(), diff,
                        f"{SHIFTEE_NOTE}: {now():%Y-%m-%d} 기준 시프티 잔여 {rep['remain']:g}일에 맞춤 "
                        f"(시프티 산정기간 {rep['period']}, 총 {rep['total']:g} / 사용 {rep['used']:g})", actor_id, stamp))
            res["adjusted"].append((name, bal["remain"], rep["remain"], diff))
        else:
            res["matched"] += 1
    audit(actor_id, "import_leaves", f"내역 {res['added']}건, 잔여조정 {len(res['adjusted'])}명")
    db.commit()
    res["no_user"] = sorted(res["no_user"])
    return res


@bp.route("/leaves", methods=["POST"])
@role_required("admin")
def leaves():
    me = current_user()
    records = report = None
    warns = []
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        data = f.read()
        try:
            kind = detect_leave_file(data)
            if kind == "report":
                report = parse_leave_report(data)
            elif kind == "records":
                records, w = parse_leave_records(data)
                warns += w
            else:
                warns.append(f"{f.filename}: 시프티 휴가 파일 형식이 아닙니다.")
        except Exception as e:  # noqa: BLE001
            warns.append(f"{f.filename}: 읽기 실패 ({e})")
    if records is None and report is None:
        for w in warns:
            flash(w)
        flash("휴가 현황(EMPLOYEE-LEAVE-REPORT) 또는 휴가 내역(LEAVES) 파일을 선택하세요.")
        return redirect(url_for("imp.index"))
    res = import_leaves(records, report, me["id"])
    return render_template("import.html", step="leaves_done", res=res, warns=warns)


def register(app):
    app.register_blueprint(bp)

    import click

    @app.cli.command("import-employees")
    @click.argument("xlsx", type=click.Path(exists=True, dir_okay=False))
    @click.option("--out", default=None, help="임시 비밀번호 엑셀 저장 위치 (기본: 바탕화면)")
    @click.option("--include-admin", is_flag=True, help="대표이사(관리자) 행도 새 계정으로 만듦")
    @click.option("--yes", is_flag=True, help="확인 질문 없이 바로 적용")
    def import_employees_cmd(xlsx, out, include_admin, yes):
        """시프티 직원 목록 엑셀을 가져와 계정을 만듭니다."""
        with open(xlsx, "rb") as fp:
            rows, warns = parse_workbook(fp.read())
        rows = plan(rows)
        db = get_db()
        has_admin = db.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone() is not None
        for r in rows:
            r["include"] = True
            if r["action"] == "create" and r["role"] == "admin" and has_admin and not include_admin:
                r["include"] = False
        for w in warns:
            click.echo("경고: " + w)
        click.echo(f"{'처리':4} {'이름':6} {'아이디':10} {'권한':5} {'부서':14} 직위")
        for r in rows:
            mark = ("신규" if r["action"] == "create" else "갱신") if r["include"] else "제외"
            click.echo(f"{mark:4} {r['name']:6} {r['login_id']:10} {ROLE_LABELS[r['role']]:5} "
                       f"{(r['dept'] or '-'):14} {r['position'] or ''}")
        for r in rows:
            if r.get("rename_from") and r["rename_from"] != r["name"]:
                click.echo(f"※ {r['name']}(대표이사) → 기존 관리자 계정({r['login_id']})에 연결합니다. "
                           f"계정 이름 '{r['rename_from']}' → '{r['name']}', 비밀번호는 그대로입니다.")
        skipped = [r for r in rows if not r["include"]]
        if skipped:
            click.echo(f"※ 관리자 {len(skipped)}명은 기존 관리자 계정과 구분이 안 돼 제외했습니다(--include-admin 으로 포함).")
        if not yes and not click.confirm(f"{sum(1 for r in rows if r['include'])}명을 반영할까요?", default=True):
            click.echo("취소했습니다.")
            return
        admin = db.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        accounts, created, updated = apply_rows(rows, admin["id"] if admin else None)
        if out is None:
            desk = os.path.join(os.path.expanduser("~"), "Desktop")
            out = os.path.join(desk if os.path.isdir(desk) else os.getcwd(), "직원계정_임시비밀번호.xlsx")
        save_accounts_xlsx(accounts, out)
        click.echo(f"완료: 신규 {created}명, 갱신 {updated}명")
        click.echo(f"임시 비밀번호 파일: {out}  (직원 전달 후 삭제하세요)")

    @app.cli.command("import-leaves")
    @click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
    def import_leaves_cmd(files):
        """시프티 휴가 현황(EMPLOYEE-LEAVE-REPORT)·휴가 내역(LEAVES) 엑셀을 가져옵니다."""
        records = report = None
        for path in files:
            with open(path, "rb") as fp:
                data = fp.read()
            kind = detect_leave_file(data)
            if kind == "report":
                report = parse_leave_report(data)
                click.echo(f"휴가 현황: {os.path.basename(path)} — 연차 {len(report)}명")
            elif kind == "records":
                records, warns = parse_leave_records(data)
                click.echo(f"휴가 내역: {os.path.basename(path)} — {len(records)}건")
                for w in warns:
                    click.echo("경고: " + w)
            else:
                click.echo(f"건너뜀(형식 아님): {os.path.basename(path)}")
        if records is None and report is None:
            raise click.ClickException("시프티 휴가 파일이 없습니다.")
        admin = get_db().execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        res = import_leaves(records, report, admin["id"] if admin else None)
        click.echo(f"휴가 내역 등록 {res['added']}건 (이미 있어 건너뜀 {res['dup']}건)")
        click.echo(f"연차 잔여: 시프티와 일치 {res['matched']}명, 조정 {len(res['adjusted'])}명")
        for name, before, after, diff in res["adjusted"]:
            click.echo(f"  {name}: 앱 계산 {before:g}일 → 시프티 잔여 {after:g}일 (조정 {diff:+g})")
        if res["no_user"]:
            click.echo("앱에 없는 직원(먼저 import-employees 실행): " + ", ".join(res["no_user"]))
