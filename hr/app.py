# -*- coding: utf-8 -*-
"""GroupK 근태관리 앱 (출퇴근·연차·전자결재·근로시간 관리).

실행:
    flask --app hr.app init-db
    flask --app hr.app create-admin
    flask --app hr.app run --host 0.0.0.0        (개발용)
    gunicorn -w 2 -b 0.0.0.0:8000 "hr.app:create_app()"   (운영)
"""
from __future__ import annotations

import io
import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import click
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash


def hash_pw(password: str) -> str:
    # scrypt는 macOS 기본 Python(LibreSSL) 등 일부 환경에서 지원되지 않으므로 PBKDF2 고정
    return generate_password_hash(password, method="pbkdf2:sha256:600000")

from . import rules
from .db import audit, close_db, get_db, init_db, setting

KST = ZoneInfo("Asia/Seoul")

KIND_LABELS = {
    "annual": "연차",
    "sick": "병가",
    "family": "경조사",
    "official": "공가",
    "comp": "대체휴가",
    "unpaid": "무급휴가",
    "holiday_work": "휴일근무(대체휴가 적립)",
    "overtime": "연장근무",
    "field": "외근·출장",
    "correction": "근태정정",
}
LEAVE_KINDS = ("annual", "sick", "family", "official", "comp", "unpaid")
DEDUCT_KINDS = ("annual",)
UNIT_LABELS = {"full": "종일", "am": "오전반차", "pm": "오후반차", "quarter": "반반차"}
HW_UNITS = {"full": "종일(8시간)", "half": "반일(4시간)"}   # 휴일근무 단위
ALL_UNITS = {**UNIT_LABELS, "half": "반일", "hours": ""}
STATUS_LABELS = {"pending": "대기", "approved": "승인", "rejected": "반려", "canceled": "취소"}
WORK_TYPES = {"office": "사무실", "field": "현장", "remote": "재택", "trip": "출장"}
ROLE_LABELS = {"admin": "관리자", "manager": "부서장", "employee": "직원"}


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("HR_SECRET_KEY") or _load_or_create_secret(app.instance_path),
        DATABASE=os.environ.get("HR_DATABASE", os.path.join(app.instance_path, "hr.sqlite3")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("HR_SECURE_COOKIE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        NOW=lambda: datetime.now(KST).replace(tzinfo=None, microsecond=0),
    )
    if test_config:
        app.config.update(test_config)

    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()  # CREATE IF NOT EXISTS — 기존 데이터 유지
    _register_cli(app)
    _register_filters(app)
    _register_routes(app)
    return app


def _load_or_create_secret(instance_path: str) -> str:
    path = os.path.join(instance_path, "secret_key")
    key = secrets.token_hex(32)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(key)
    os.chmod(tmp, 0o600)
    try:
        os.link(tmp, path)  # 원자적 생성: 여러 워커가 동시에 시작해도 하나의 키만 채택
    except FileExistsError:
        pass
    finally:
        os.unlink(tmp)
    with open(path) as f:
        return f.read().strip()


# ---------------------------------------------------------------- helpers

def now() -> datetime:
    from flask import current_app
    return current_app.config["NOW"]()


def today() -> date:
    return now().date()


def parse_dt(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


def parse_d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    if "user" not in g:
        g.user = get_db().execute(
            "SELECT u.*, d.name AS dept FROM users u LEFT JOIN departments d ON d.id=u.department_id "
            "WHERE u.id=? AND u.active=1", (uid,)).fetchone()
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        user = current_user()
        if user is None:
            session.clear()
            return redirect(url_for("login", next=request.path))
        if user["must_change_pw"] and request.endpoint not in ("change_password", "logout"):
            flash("초기 비밀번호를 변경해 주세요.")
            return redirect(url_for("change_password"))
        return view(*a, **kw)
    return wrapped


def role_required(*roles):
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(*a, **kw):
            if current_user()["role"] not in roles:
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


def visible_users(include_inactive=False):
    """관리자: 전원 / 부서장: 소속 부서원 / 직원: 본인."""
    u = current_user()
    sql = ("SELECT u.*, d.name AS dept FROM users u LEFT JOIN departments d ON d.id=u.department_id "
           "WHERE 1=1")
    args: list = []
    if not include_inactive:
        sql += " AND u.active=1"
    if u["role"] == "manager":
        sql += " AND (u.department_id IS ? OR u.id=?)"
        args += [u["department_id"], u["id"]]
    elif u["role"] == "employee":
        sql += " AND u.id=?"
        args.append(u["id"])
    sql += " ORDER BY d.name, u.name"
    return get_db().execute(sql, args).fetchall()


def can_manage(user_id: int) -> bool:
    u = current_user()
    if u["role"] == "admin":
        return True
    if u["role"] == "manager":
        t = get_db().execute("SELECT department_id FROM users WHERE id=?", (user_id,)).fetchone()
        return t is not None and t["department_id"] == u["department_id"] and user_id != u["id"]
    return False


def schedule_of(user) -> tuple[str, str]:
    return (user["work_start"] or setting("work_start"), user["work_end"] or setting("work_end"))


def holiday_set(start: date, end: date) -> set[date]:
    rows = get_db().execute("SELECT day FROM holidays WHERE day BETWEEN ? AND ?",
                            (start.isoformat(), end.isoformat())).fetchall()
    return {parse_d(r["day"]) for r in rows}


def record_minutes(rec) -> int:
    return rules.worked_minutes(parse_dt(rec["check_in"]), parse_dt(rec["check_out"]),
                                int(setting("break_min")))


def week_minutes(user_id: int, any_day: date) -> int:
    ws = rules.week_start(any_day)
    rows = get_db().execute(
        "SELECT * FROM attendance WHERE user_id=? AND work_date BETWEEN ? AND ?",
        (user_id, ws.isoformat(), (ws + timedelta(days=6)).isoformat())).fetchall()
    return sum(record_minutes(r) for r in rows)


def leave_balance(user, on: date | None = None) -> dict:
    on = on or today()
    ent = rules.leave_entitlement(parse_d(user["hire_date"]), on)
    ps, pe = ent["period_start"].isoformat(), ent["period_end"].isoformat()
    db = get_db()
    used = db.execute(
        "SELECT COALESCE(SUM(days),0) s FROM requests WHERE user_id=? AND status='approved' "
        f"AND kind IN ({','.join('?' * len(DEDUCT_KINDS))}) AND start_date BETWEEN ? AND ?",
        (user["id"], *DEDUCT_KINDS, ps, pe)).fetchone()["s"]
    pending = db.execute(
        "SELECT COALESCE(SUM(days),0) s FROM requests WHERE user_id=? AND status='pending' "
        f"AND kind IN ({','.join('?' * len(DEDUCT_KINDS))}) AND start_date BETWEEN ? AND ?",
        (user["id"], *DEDUCT_KINDS, ps, pe)).fetchone()["s"]
    adj = db.execute(
        "SELECT COALESCE(SUM(days),0) s FROM leave_adjustments WHERE user_id=? AND period_start=?",
        (user["id"], ps)).fetchone()["s"]
    total = ent["granted"] + adj
    return {**ent, "adjust": adj, "total": total, "used": used, "pending": pending,
            "remain": total - used}


def comp_balance(user) -> dict:
    """대체휴가: 승인된 휴일근무로 적립(earned), 승인된 대체휴가로 차감(used)."""
    rows = get_db().execute(
        "SELECT kind, status, COALESCE(SUM(days),0) s FROM requests WHERE user_id=? "
        "AND kind IN ('holiday_work','comp') AND status IN ('approved','pending') GROUP BY kind, status",
        (user["id"],)).fetchall()
    v = {(r["kind"], r["status"]): r["s"] for r in rows}
    earned = v.get(("holiday_work", "approved"), 0)
    used = v.get(("comp", "approved"), 0)
    return {"earned": earned, "used": used, "pending": v.get(("comp", "pending"), 0),
            "earn_pending": v.get(("holiday_work", "pending"), 0), "remain": earned - used}


def month_leave_days(user_id: int, first: date, last: date) -> float:
    return get_db().execute(
        "SELECT COALESCE(SUM(days),0) s FROM requests WHERE user_id=? AND status='approved' "
        f"AND kind IN ({','.join('?' * len(LEAVE_KINDS))}) AND start_date BETWEEN ? AND ?",
        (user_id, *LEAVE_KINDS, first.isoformat(), last.isoformat())).fetchone()["s"]


def month_range(ym: str | None) -> tuple[date, date, str]:
    if ym:
        y, m = map(int, ym.split("-"))
        first = date(y, m, 1)
    else:
        first = today().replace(day=1)
    last = rules.add_months(first, 1) - timedelta(days=1)
    return first, last, first.strftime("%Y-%m")


def _csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


# ---------------------------------------------------------------- CLI

def _register_cli(app: Flask):
    @app.cli.command("init-db")
    def init_db_cmd():
        """DB 테이블 생성(기존 데이터 유지)."""
        init_db()
        click.echo("DB 초기화 완료: " + app.config["DATABASE"])

    @app.cli.command("create-admin")
    @click.option("--login", prompt="관리자 아이디")
    @click.option("--name", prompt="이름")
    @click.option("--hire-date", prompt="입사일(YYYY-MM-DD)")
    @click.password_option(prompt="비밀번호")
    def create_admin(login, name, hire_date, password):
        init_db()
        db = get_db()
        db.execute("INSERT INTO users(login_id,password_hash,name,role,hire_date,must_change_pw) "
                   "VALUES (?,?,?,'admin',?,0)",
                   (login, hash_pw(password), name, hire_date))
        db.commit()
        click.echo(f"관리자 계정 생성: {login}")


def _register_filters(app: Flask):
    @app.context_processor
    def inject():
        return {
            "me": current_user(),
            "csrf_token": _csrf_token,
            "KIND": KIND_LABELS, "UNIT": UNIT_LABELS, "ALL_UNITS": ALL_UNITS, "STATUS": STATUS_LABELS,
            "WORK_TYPES": WORK_TYPES, "ROLES": ROLE_LABELS,
            "company": setting("company_name"),
            "hm": rules.fmt_hm,
        }

    @app.template_filter("time")
    def _time(s):
        return s[11:16] if s else "-"

    @app.before_request
    def csrf_protect():
        if request.method == "POST":
            token = request.form.get("csrf") or request.headers.get("X-CSRF")
            if not token or token != session.get("csrf"):
                abort(400, "CSRF 토큰 불일치 — 페이지를 새로고침 후 다시 시도하세요.")


# ---------------------------------------------------------------- routes

def _register_routes(app: Flask):

    # ---------- PWA
    @app.route("/manifest.webmanifest")
    def manifest():
        return app.send_static_file("manifest.webmanifest"), 200, {
            "Content-Type": "application/manifest+json"}

    @app.route("/sw.js")
    def service_worker():
        resp = app.send_static_file("sw.js")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ---------- auth
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            row = get_db().execute("SELECT * FROM users WHERE login_id=? AND active=1",
                                   (request.form.get("login_id", "").strip(),)).fetchone()
            if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
                csrf = session.get("csrf")
                session.clear()
                session["uid"] = row["id"]
                session["csrf"] = csrf or secrets.token_urlsafe(24)
                session.permanent = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home"))
            flash("아이디 또는 비밀번호가 올바르지 않습니다.")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            me = current_user()
            cur, new, new2 = (request.form.get(k, "") for k in ("current", "new", "new2"))
            if not check_password_hash(me["password_hash"], cur):
                flash("현재 비밀번호가 다릅니다.")
            elif len(new) < 8 or new != new2:
                flash("새 비밀번호는 8자 이상, 두 번 동일하게 입력하세요.")
            else:
                db = get_db()
                db.execute("UPDATE users SET password_hash=?, must_change_pw=0 WHERE id=?",
                           (hash_pw(new), me["id"]))
                db.commit()
                flash("비밀번호가 변경되었습니다.")
                return redirect(url_for("home"))
        return render_template("password.html")

    # ---------- 직원 홈 (출퇴근)
    @app.route("/")
    @login_required
    def home():
        me = current_user()
        db = get_db()
        t = today()
        rec = db.execute("SELECT * FROM attendance WHERE user_id=? AND work_date=?",
                         (me["id"], t.isoformat())).fetchone()
        on_leave = db.execute(
            "SELECT * FROM requests WHERE user_id=? AND status='approved' AND kind IN "
            f"({','.join('?' * len(LEAVE_KINDS))}) AND ? BETWEEN start_date AND end_date",
            (me["id"], *LEAVE_KINDS, t.isoformat())).fetchone()
        notices = db.execute("SELECT * FROM notices ORDER BY id DESC LIMIT 3").fetchall()
        wk = week_minutes(me["id"], t)
        ws, we = schedule_of(me)
        return render_template(
            "home.html", rec=rec, on_leave=on_leave, notices=notices, week_min=wk,
            week_limit=rules.WEEKLY_LIMIT_MIN, bal=leave_balance(me), comp=comp_balance(me), sched=(ws, we),
            today=t, consent=me["location_consent"],
            require_location=setting("require_location") == "1")

    @app.route("/consent", methods=["POST"])
    @login_required
    def consent():
        me = current_user()
        val = 1 if request.form.get("agree") == "1" else 0
        db = get_db()
        db.execute("UPDATE users SET location_consent=? WHERE id=?", (val, me["id"]))
        audit(me["id"], "location_consent", me["id"], val)
        db.commit()
        return redirect(url_for("home"))

    @app.route("/punch", methods=["POST"])
    @login_required
    def punch():
        me = current_user()
        db = get_db()
        action = request.form.get("action")
        work_type = request.form.get("work_type", "office")
        if work_type not in WORK_TYPES:
            work_type = "office"
        lat = request.form.get("lat", type=float) if me["location_consent"] else None
        lon = request.form.get("lon", type=float) if me["location_consent"] else None
        place = None
        if lat is not None and lon is not None:
            for wp in db.execute("SELECT * FROM workplaces").fetchall():
                if rules.haversine_m(lat, lon, wp["lat"], wp["lon"]) <= wp["radius_m"]:
                    place = wp["name"]
                    break
        if (setting("require_location") == "1" and work_type == "office" and place is None):
            flash("등록된 근무지 반경 밖이거나 위치 정보가 없습니다. 현장/재택/출장을 선택하거나 위치 권한을 허용하세요.")
            return redirect(url_for("home"))

        n = now()
        t = n.date().isoformat()
        stamp = n.strftime("%Y-%m-%d %H:%M:%S")
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        rec = db.execute("SELECT * FROM attendance WHERE user_id=? AND work_date=?",
                         (me["id"], t)).fetchone()
        if action == "in":
            if rec and rec["check_in"]:
                flash("이미 출근 처리되었습니다.")
                return redirect(url_for("home"))
            late = rules.late_minutes(n, schedule_of(me)[0], int(setting("late_grace_min")))
            db.execute(
                "INSERT INTO attendance(user_id,work_date,check_in,in_lat,in_lon,in_place,in_ip,"
                "work_type,late_min,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (me["id"], t, stamp, lat, lon, place, ip, work_type, late,
                 request.form.get("note") or None))
            flash(f"출근 {n:%H:%M}" + (f" (지각 {late}분)" if late else ""))
        elif action == "out":
            if not rec or not rec["check_in"]:
                flash("출근 기록이 없습니다. 근태정정을 신청하세요.")
                return redirect(url_for("home"))
            db.execute("UPDATE attendance SET check_out=?, out_lat=?, out_lon=?, out_place=?, out_ip=? "
                       "WHERE id=?", (stamp, lat, lon, place, ip, rec["id"]))
            flash(f"퇴근 {n:%H:%M}" + (" (퇴근 시각 갱신)" if rec["check_out"] else ""))
        else:
            abort(400)
        db.commit()
        return redirect(url_for("home"))

    @app.route("/my/attendance")
    @login_required
    def my_attendance():
        me = current_user()
        first, last, ym = month_range(request.args.get("month"))
        return render_template("attendance_month.html", target=me, ym=ym,
                               **_month_data(me, first, last))

    def _month_data(user, first: date, last: date) -> dict:
        db = get_db()
        recs = {r["work_date"]: r for r in db.execute(
            "SELECT * FROM attendance WHERE user_id=? AND work_date BETWEEN ? AND ?",
            (user["id"], first.isoformat(), last.isoformat())).fetchall()}
        reqs = db.execute(
            "SELECT * FROM requests WHERE user_id=? AND status='approved' AND end_date>=? AND start_date<=?",
            (user["id"], first.isoformat(), last.isoformat())).fetchall()
        hol = {r["day"]: r["name"] for r in db.execute(
            "SELECT * FROM holidays WHERE day BETWEEN ? AND ?",
            (first.isoformat(), last.isoformat())).fetchall()}
        days, total, late_cnt = [], 0, 0
        d = first
        while d <= last:
            iso = d.isoformat()
            rec = recs.get(iso)
            mins = record_minutes(rec) if rec else 0
            total += mins
            if rec and rec["late_min"]:
                late_cnt += 1
            labels = [KIND_LABELS[r["kind"]] + ("" if r["unit"] == "full" else f"({ALL_UNITS.get(r['unit'], '')})")
                      for r in reqs if r["start_date"] <= iso <= r["end_date"]]
            days.append({"date": d, "rec": rec, "minutes": mins, "labels": labels,
                         "holiday": hol.get(iso), "weekend": d.weekday() >= 5})
            d += timedelta(days=1)
        return {"days": days, "total": total, "late_cnt": late_cnt,
                "work_days": sum(1 for x in days if x["rec"] and x["rec"]["check_in"])}

    # ---------- 신청(전자결재)
    @app.route("/requests", methods=["GET", "POST"])
    @login_required
    def my_requests():
        me = current_user()
        db = get_db()
        if request.method == "POST":
            kind = request.form.get("kind")
            unit = request.form.get("unit", "full")
            try:
                sd = parse_d(request.form["start_date"])
                ed = parse_d(request.form.get("end_date") or request.form["start_date"])
            except (KeyError, ValueError):
                flash("날짜를 확인하세요.")
                return redirect(url_for("my_requests"))
            if kind not in KIND_LABELS or ed < sd:
                flash("신청 내용을 확인하세요.")
                return redirect(url_for("my_requests"))
            if kind == "holiday_work":
                unit = request.form.get("wunit") if request.form.get("wunit") in HW_UNITS else "full"
            elif unit not in UNIT_LABELS or kind not in LEAVE_KINDS:
                unit = "full" if kind in LEAVE_KINDS else "hours"
            if unit != "full":
                ed = sd
            days = rules.leave_days(sd, ed, unit, holiday_set(sd, ed)) if kind in LEAVE_KINDS else 0
            minutes = 0
            corr_in = corr_out = None
            if kind == "holiday_work":
                ed = sd
                if sd.weekday() < 5 and not holiday_set(sd, sd):
                    flash("휴일근무는 토·일요일 또는 등록된 휴일에만 신청할 수 있습니다.")
                    return redirect(url_for("my_requests"))
                ratio = float(setting("comp_ratio") or 1)
                days = (1.0 if unit == "full" else 0.5) * ratio
            if kind == "overtime":
                minutes = int(float(request.form.get("hours") or 0) * 60)
                if minutes <= 0:
                    flash("연장근무 시간을 입력하세요.")
                    return redirect(url_for("my_requests"))
                ed = sd
            if kind == "correction":
                corr_in = request.form.get("corr_in") or None
                corr_out = request.form.get("corr_out") or None
                if not (corr_in or corr_out):
                    flash("정정할 출근/퇴근 시각을 입력하세요.")
                    return redirect(url_for("my_requests"))
                ed = sd
            if kind in DEDUCT_KINDS:
                bal = leave_balance(me, sd)
                if days > bal["remain"] - bal["pending"]:
                    flash(f"잔여 연차 부족 (잔여 {bal['remain']:g}일, 결재대기 {bal['pending']:g}일)")
                    return redirect(url_for("my_requests"))
            if kind == "comp":
                cb = comp_balance(me)
                if days > cb["remain"] - cb["pending"]:
                    flash(f"대체휴가 잔여 부족 (잔여 {cb['remain']:g}일, 결재대기 {cb['pending']:g}일). "
                          "휴일근무 신청이 먼저 승인되어야 합니다.")
                    return redirect(url_for("my_requests"))
            if kind in LEAVE_KINDS and days == 0:
                flash("선택한 기간에 근무일이 없습니다.")
                return redirect(url_for("my_requests"))
            db.execute(
                "INSERT INTO requests(user_id,kind,unit,start_date,end_date,days,minutes,corr_in,corr_out,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (me["id"], kind, unit, sd.isoformat(), ed.isoformat(), days, minutes, corr_in, corr_out,
                 request.form.get("reason", "").strip()))
            db.commit()
            flash(f"{KIND_LABELS[kind]} 신청 완료 — 결재 대기")
            return redirect(url_for("my_requests"))
        rows = db.execute("SELECT r.*, a.name AS approver FROM requests r LEFT JOIN users a ON a.id=r.approver_id "
                          "WHERE r.user_id=? ORDER BY r.id DESC LIMIT 100", (me["id"],)).fetchall()
        return render_template("requests.html", rows=rows, bal=leave_balance(me), comp=comp_balance(me),
                               today=today(), leave_kinds=LEAVE_KINDS, hw_units=HW_UNITS,
                               comp_ratio=float(setting("comp_ratio") or 1))

    @app.route("/requests/<int:rid>/cancel", methods=["POST"])
    @login_required
    def cancel_request(rid):
        me = current_user()
        db = get_db()
        r = db.execute("SELECT * FROM requests WHERE id=? AND user_id=?", (rid, me["id"])).fetchone()
        if not r:
            abort(404)
        if r["status"] == "pending" or (r["status"] == "approved" and r["start_date"] > today().isoformat()
                                        and r["kind"] in LEAVE_KINDS):
            db.execute("UPDATE requests SET status='canceled' WHERE id=?", (rid,))
            audit(me["id"], "request_cancel", rid)
            db.commit()
            flash("취소되었습니다.")
        else:
            flash("취소할 수 없는 신청입니다.")
        return redirect(url_for("my_requests"))

    @app.route("/notices")
    @login_required
    def notices():
        rows = get_db().execute("SELECT n.*, u.name AS author FROM notices n LEFT JOIN users u ON u.id=n.author_id "
                                "ORDER BY n.id DESC").fetchall()
        return render_template("notices.html", rows=rows)

    # ================================================================ 관리
    @app.route("/admin")
    @role_required("admin", "manager")
    def admin_home():
        db = get_db()
        t = today().isoformat()
        users = visible_users()
        recs = {r["user_id"]: r for r in db.execute("SELECT * FROM attendance WHERE work_date=?", (t,)).fetchall()}
        leaves = {}
        for r in db.execute("SELECT * FROM requests WHERE status='approved' AND ? BETWEEN start_date AND end_date",
                            (t,)).fetchall():
            leaves.setdefault(r["user_id"], []).append(r)
        board, stat = [], {"in": 0, "out": 0, "late": 0, "leave": 0, "absent": 0}
        for u in users:
            rec = recs.get(u["id"])
            lv = [x for x in leaves.get(u["id"], []) if x["kind"] in LEAVE_KINDS]
            if rec and rec["check_out"]:
                st = "퇴근"; stat["out"] += 1
            elif rec and rec["check_in"]:
                st = "근무중"; stat["in"] += 1
            elif lv:
                st = KIND_LABELS[lv[0]["kind"]]; stat["leave"] += 1
            else:
                st = "미출근"; stat["absent"] += 1
            if rec and rec["late_min"]:
                stat["late"] += 1
            board.append({"u": u, "rec": rec, "status": st, "week": week_minutes(u["id"], today())})
        ids = [u["id"] for u in users]
        pending = db.execute(
            f"SELECT COUNT(*) c FROM requests WHERE status='pending' AND user_id IN ({','.join('?' * len(ids)) or 'NULL'})",
            ids).fetchone()["c"] if ids else 0
        return render_template("admin_home.html", board=board, stat=stat, pending=pending,
                               limit=rules.WEEKLY_LIMIT_MIN, today=today())

    @app.route("/admin/approvals")
    @role_required("admin", "manager")
    def approvals():
        ids = [u["id"] for u in visible_users()]
        me = current_user()
        if me["role"] == "manager":
            ids = [i for i in ids if i != me["id"]]   # 부서장 본인 신청은 관리자가 결재
        status = request.args.get("status", "pending")
        rows = []
        if ids:
            rows = get_db().execute(
                "SELECT r.*, u.name, d.name AS dept FROM requests r JOIN users u ON u.id=r.user_id "
                "LEFT JOIN departments d ON d.id=u.department_id "
                f"WHERE r.status=? AND r.user_id IN ({','.join('?' * len(ids))}) ORDER BY r.start_date DESC LIMIT 200",
                (status, *ids)).fetchall()
        return render_template("approvals.html", rows=rows, status=status)

    @app.route("/admin/requests/<int:rid>/decide", methods=["POST"])
    @role_required("admin", "manager")
    def decide(rid):
        me = current_user()
        db = get_db()
        r = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r or not can_manage(r["user_id"]):
            abort(403)
        decision = request.form.get("decision")
        if r["status"] != "pending" or decision not in ("approved", "rejected"):
            flash("처리할 수 없는 상태입니다.")
            return redirect(url_for("approvals"))
        db.execute("UPDATE requests SET status=?, approver_id=?, decided_at=?, decision_note=? WHERE id=?",
                   (decision, me["id"], now().strftime("%Y-%m-%d %H:%M:%S"),
                    request.form.get("note", "").strip() or None, rid))
        if decision == "approved" and r["kind"] == "correction":
            _apply_correction(r)
        audit(me["id"], "request_" + decision, rid)
        db.commit()
        flash(f"{STATUS_LABELS[decision]} 처리했습니다.")
        return redirect(url_for("approvals"))

    def _apply_correction(r):
        db = get_db()
        target = db.execute("SELECT * FROM users WHERE id=?", (r["user_id"],)).fetchone()
        rec = db.execute("SELECT * FROM attendance WHERE user_id=? AND work_date=?",
                         (r["user_id"], r["start_date"])).fetchone()
        cin = f"{r['start_date']} {r['corr_in']}:00" if r["corr_in"] else (rec["check_in"] if rec else None)
        cout = f"{r['start_date']} {r['corr_out']}:00" if r["corr_out"] else (rec["check_out"] if rec else None)
        late = rules.late_minutes(parse_dt(cin), schedule_of(target)[0], int(setting("late_grace_min"))) if cin else 0
        if rec:
            db.execute("UPDATE attendance SET check_in=?, check_out=?, late_min=?, note=COALESCE(note,'')||? WHERE id=?",
                       (cin, cout, late, f" [정정#{r['id']}]", rec["id"]))
        else:
            db.execute("INSERT INTO attendance(user_id,work_date,check_in,check_out,late_min,note) VALUES (?,?,?,?,?,?)",
                       (r["user_id"], r["start_date"], cin, cout, late, f"[정정#{r['id']}]"))

    @app.route("/admin/attendance")
    @role_required("admin", "manager")
    def admin_attendance():
        users = visible_users()
        first, last, ym = month_range(request.args.get("month"))
        uid = request.args.get("user", type=int)
        if uid:
            target = next((u for u in users if u["id"] == uid), None)
            if not target:
                abort(403)
            return render_template("attendance_month.html", target=target, ym=ym, admin=True,
                                   **_month_data(target, first, last))
        summary = []
        for u in users:
            md = _month_data(u, first, last)
            leave_days = month_leave_days(u["id"], first, last)
            summary.append({"u": u, "work_days": md["work_days"], "total": md["total"],
                            "late": md["late_cnt"], "leave": leave_days})
        return render_template("admin_attendance.html", summary=summary, ym=ym)

    @app.route("/admin/attendance/edit", methods=["POST"])
    @role_required("admin")
    def admin_attendance_edit():
        me = current_user()
        db = get_db()
        uid = request.form.get("user_id", type=int)
        d = request.form.get("work_date", "")
        try:
            parse_d(d)
        except ValueError:
            abort(400)
        target = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            abort(404)
        cin = request.form.get("check_in") or None
        cout = request.form.get("check_out") or None
        cin_s = f"{d} {cin}:00" if cin else None
        cout_s = f"{d} {cout}:00" if cout else None
        late = rules.late_minutes(parse_dt(cin_s), schedule_of(target)[0], int(setting("late_grace_min"))) if cin_s else 0
        before = db.execute("SELECT check_in, check_out FROM attendance WHERE user_id=? AND work_date=?",
                            (uid, d)).fetchone()
        if not cin_s and not cout_s:
            db.execute("DELETE FROM attendance WHERE user_id=? AND work_date=?", (uid, d))
        else:
            db.execute(
                "INSERT INTO attendance(user_id,work_date,check_in,check_out,late_min,note,work_type) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id,work_date) DO UPDATE SET "
                "check_in=excluded.check_in, check_out=excluded.check_out, late_min=excluded.late_min, "
                "note=excluded.note, work_type=excluded.work_type",
                (uid, d, cin_s, cout_s, late, request.form.get("note") or None,
                 request.form.get("work_type") if request.form.get("work_type") in WORK_TYPES else "office"))
        audit(me["id"], "attendance_edit", f"{uid}/{d}",
              f"{dict(before) if before else None} -> {cin_s},{cout_s}")
        db.commit()
        flash("근태 기록을 수정했습니다.")
        return redirect(url_for("admin_attendance", user=uid, month=d[:7]))

    @app.route("/admin/export")
    @role_required("admin", "manager")
    def export_month():
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        first, last, ym = month_range(request.args.get("month"))
        users = visible_users()
        wb = Workbook()
        ws = wb.active
        ws.title = "월간집계"
        head = ["부서", "이름", "직위", "출근일수", "총 근로시간", "지각횟수", "휴가사용(일)", "연차잔여(일)",
                "대체휴가잔여(일)"]
        ws.append(head)
        detail = wb.create_sheet("일별상세")
        detail.append(["일자", "요일", "부서", "이름", "근무유형", "출근", "퇴근", "근로시간", "지각(분)",
                       "휴가·신청", "출근위치", "비고"])
        wd = "월화수목금토일"
        for u in users:
            md = _month_data(u, first, last)
            lv = month_leave_days(u["id"], first, last)
            ws.append([u["dept"] or "", u["name"], u["position"] or "", md["work_days"],
                       rules.fmt_hm(md["total"]), md["late_cnt"], lv, leave_balance(u)["remain"],
                       comp_balance(u)["remain"]])
            for x in md["days"]:
                rec = x["rec"]
                if not rec and not x["labels"]:
                    continue
                detail.append([
                    x["date"].isoformat(), wd[x["date"].weekday()], u["dept"] or "", u["name"],
                    WORK_TYPES.get(rec["work_type"], "") if rec else "",
                    (rec["check_in"] or "")[11:16] if rec else "", (rec["check_out"] or "")[11:16] if rec else "",
                    rules.fmt_hm(x["minutes"]) if rec else "", rec["late_min"] if rec else "",
                    ", ".join(x["labels"]), (rec["in_place"] or "") if rec else "", (rec["note"] or "") if rec else ""])
        for sh in (ws, detail):
            for c in sh[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="1F3A5F")
                c.alignment = Alignment(horizontal="center")
            for col in sh.columns:
                sh.column_dimensions[col[0].column_letter].width = 13
            sh.freeze_panes = "A2"
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=f"근태_{ym}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---------- 직원 관리
    @app.route("/admin/users", methods=["GET", "POST"])
    @role_required("admin")
    def admin_users():
        db = get_db()
        me = current_user()
        if request.method == "POST":
            f = request.form
            uid = f.get("id", type=int)
            dept_id = f.get("department_id", type=int) or None
            role = f.get("role") if f.get("role") in ROLE_LABELS else "employee"
            try:
                parse_d(f["hire_date"])
            except (KeyError, ValueError):
                flash("입사일 형식을 확인하세요.")
                return redirect(url_for("admin_users"))
            vals = (f["name"].strip(), role, dept_id, f.get("position") or None, f.get("phone") or None,
                    f["hire_date"], f.get("work_start") or None, f.get("work_end") or None,
                    1 if f.get("active", "1") == "1" else 0)
            if uid:
                db.execute("UPDATE users SET name=?, role=?, department_id=?, position=?, phone=?, hire_date=?, "
                           "work_start=?, work_end=?, active=? WHERE id=?", (*vals, uid))
                audit(me["id"], "user_update", uid)
                flash("직원 정보를 수정했습니다.")
            else:
                login_id = f.get("login_id", "").strip()
                if not login_id or db.execute("SELECT 1 FROM users WHERE login_id=?", (login_id,)).fetchone():
                    flash("아이디가 비었거나 이미 사용 중입니다.")
                    return redirect(url_for("admin_users"))
                temp = f.get("password") or secrets.token_urlsafe(6)
                db.execute("INSERT INTO users(name,role,department_id,position,phone,hire_date,work_start,work_end,"
                           "active,login_id,password_hash,must_change_pw) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                           (*vals, login_id, hash_pw(temp)))
                audit(me["id"], "user_create", login_id)
                flash(f"직원 등록 완료 — 아이디 {login_id} / 임시 비밀번호 {temp} (첫 로그인 시 변경)")
            db.commit()
            return redirect(url_for("admin_users"))
        users = visible_users(include_inactive=True)
        depts = db.execute("SELECT * FROM departments ORDER BY name").fetchall()
        edit = None
        if request.args.get("edit", type=int):
            edit = db.execute("SELECT * FROM users WHERE id=?", (request.args.get("edit", type=int),)).fetchone()
        return render_template("admin_users.html", users=users, depts=depts, edit=edit,
                               bals={u["id"]: leave_balance(u) for u in users})

    @app.route("/admin/users/<int:uid>/reset", methods=["POST"])
    @role_required("admin")
    def reset_password(uid):
        db = get_db()
        temp = secrets.token_urlsafe(6)
        db.execute("UPDATE users SET password_hash=?, must_change_pw=1 WHERE id=?",
                   (hash_pw(temp), uid))
        audit(current_user()["id"], "password_reset", uid)
        db.commit()
        flash(f"임시 비밀번호: {temp} (직원에게 전달 후 첫 로그인 시 변경)")
        return redirect(url_for("admin_users"))

    @app.route("/admin/departments", methods=["POST"])
    @role_required("admin")
    def add_department():
        name = request.form.get("name", "").strip()
        if name:
            db = get_db()
            db.execute("INSERT OR IGNORE INTO departments(name) VALUES (?)", (name,))
            db.commit()
        return redirect(url_for("admin_users"))

    @app.route("/admin/leave", methods=["GET", "POST"])
    @role_required("admin")
    def admin_leave():
        db = get_db()
        if request.method == "POST":
            uid = request.form.get("user_id", type=int)
            u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            days = request.form.get("days", type=float)
            reason = request.form.get("reason", "").strip()
            if not u or days is None or not reason:
                flash("직원·일수·사유를 모두 입력하세요.")
            else:
                ps = leave_balance(u)["period_start"].isoformat()
                db.execute("INSERT INTO leave_adjustments(user_id,period_start,days,reason,created_by) "
                           "VALUES (?,?,?,?,?)", (uid, ps, days, reason, current_user()["id"]))
                audit(current_user()["id"], "leave_adjust", uid, f"{days} {reason}")
                db.commit()
                flash("연차 조정을 반영했습니다.")
            return redirect(url_for("admin_leave"))
        users = visible_users()
        adjs = db.execute("SELECT a.*, u.name FROM leave_adjustments a JOIN users u ON u.id=a.user_id "
                          "ORDER BY a.id DESC LIMIT 50").fetchall()
        return render_template("admin_leave.html", rows=[(u, leave_balance(u), comp_balance(u)) for u in users], adjs=adjs)

    @app.route("/admin/settings", methods=["GET", "POST"])
    @role_required("admin")
    def admin_settings():
        db = get_db()
        if request.method == "POST":
            sec = request.form.get("section")
            if sec == "company":
                for k in ("company_name", "work_start", "work_end", "break_min", "late_grace_min", "comp_ratio"):
                    if request.form.get(k) is not None:
                        db.execute("UPDATE settings SET value=? WHERE key=?", (request.form[k].strip(), k))
                db.execute("UPDATE settings SET value=? WHERE key='require_location'",
                           ("1" if request.form.get("require_location") else "0",))
            elif sec == "workplace_add":
                db.execute("INSERT INTO workplaces(name,lat,lon,radius_m) VALUES (?,?,?,?)",
                           (request.form["name"], float(request.form["lat"]), float(request.form["lon"]),
                            int(request.form.get("radius_m") or 200)))
            elif sec == "workplace_del":
                db.execute("DELETE FROM workplaces WHERE id=?", (request.form.get("id", type=int),))
            elif sec == "holiday_add":
                parse_d(request.form["day"])
                db.execute("INSERT OR REPLACE INTO holidays(day,name) VALUES (?,?)",
                           (request.form["day"], request.form["name"]))
            elif sec == "holiday_del":
                db.execute("DELETE FROM holidays WHERE day=?", (request.form["day"],))
            audit(current_user()["id"], "settings_" + str(sec))
            db.commit()
            flash("저장했습니다.")
            return redirect(url_for("admin_settings"))
        sets = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
        return render_template("admin_settings.html", s=sets,
                               workplaces=db.execute("SELECT * FROM workplaces").fetchall(),
                               holidays=db.execute("SELECT * FROM holidays WHERE day>=? ORDER BY day",
                                                   (f"{today().year}-01-01",)).fetchall())

    @app.route("/admin/notices", methods=["POST"])
    @role_required("admin", "manager")
    def add_notice():
        title, body = request.form.get("title", "").strip(), request.form.get("body", "").strip()
        if title and body:
            db = get_db()
            db.execute("INSERT INTO notices(title,body,author_id) VALUES (?,?,?)", (title, body, current_user()["id"]))
            db.commit()
        return redirect(url_for("notices"))

    @app.route("/admin/notices/<int:nid>/delete", methods=["POST"])
    @role_required("admin")
    def del_notice(nid):
        db = get_db()
        db.execute("DELETE FROM notices WHERE id=?", (nid,))
        db.commit()
        return redirect(url_for("notices"))

    @app.route("/admin/audit")
    @role_required("admin")
    def audit_view():
        rows = get_db().execute("SELECT a.*, u.name FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id "
                                "ORDER BY a.id DESC LIMIT 300").fetchall()
        return render_template("audit.html", rows=rows)

