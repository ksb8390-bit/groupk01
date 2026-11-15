# -*- coding: utf-8 -*-
"""사내 대화(메신저): 대화방 · 현재 상태판 · 위치 공유 · 현장 사진.

- 전체대화방(kind='all'): 재직 중인 전 직원이 자동 참여
- 일반 대화방(kind='custom'): 관리자·부서장이 만들고 참여자를 지정
- 새 메시지는 화면이 열려 있는 동안 몇 초 간격으로 확인(폴링)
"""
from __future__ import annotations

import os
import uuid

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)

from .app import audit, current_user, login_required, now, today, valid_hm
from .db import get_db

bp = Blueprint("chat", __name__, url_prefix="/chat")

STATUS_LABELS = {
    "office": "사무실",
    "visit": "외근",
    "outside": "외출",
    "field": "현장",
    "trip": "출장",
    "remote": "재택",
    "leave": "휴가",
    "off": "퇴근",
}
STATUS_ICONS = {"office": "🏢", "visit": "🚗", "outside": "🚶", "field": "🏗️", "trip": "✈️",
                "remote": "🏠", "leave": "🌴", "off": "🌙"}
WORK_TYPE_TO_STATUS = {"office": "office", "field": "field", "remote": "remote", "trip": "trip"}

MAX_TEXT = 2000
MAX_PHOTO_BYTES = 10 * 1024 * 1024
PAGE = 80
IMAGE_SIGS = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)


def _stamp() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 접근 권한

def get_room(rid: int):
    return get_db().execute("SELECT * FROM chat_rooms WHERE id=? AND archived=0", (rid,)).fetchone()


def is_member(room, user) -> bool:
    if room["kind"] == "all":
        return True
    return get_db().execute("SELECT 1 FROM chat_members WHERE room_id=? AND user_id=?",
                            (room["id"], user["id"])).fetchone() is not None


def can_manage_room(room, user) -> bool:
    return user["role"] == "admin" or (room["kind"] != "all" and room["created_by"] == user["id"])


def room_or_403(rid: int):
    room = get_room(rid)
    if room is None:
        abort(404)
    if not is_member(room, current_user()):
        abort(403)
    return room


def my_rooms(user):
    """참여 중인 대화방 + 안 읽은 수 + 마지막 메시지."""
    db = get_db()
    rows = db.execute(
        "SELECT r.*, COALESCE(m.last_read_id, 0) AS last_read FROM chat_rooms r "
        "LEFT JOIN chat_members m ON m.room_id=r.id AND m.user_id=? "
        "WHERE r.archived=0 AND (r.kind='all' OR m.user_id IS NOT NULL) "
        "ORDER BY r.kind='all' DESC, r.name", (user["id"],)).fetchall()
    out = []
    for r in rows:
        unread = db.execute(
            "SELECT COUNT(*) c FROM chat_messages WHERE room_id=? AND id>? AND deleted=0 "
            "AND (user_id IS NULL OR user_id<>?)", (r["id"], r["last_read"], user["id"])).fetchone()["c"]
        last = db.execute(
            "SELECT m.*, u.name FROM chat_messages m LEFT JOIN users u ON u.id=m.user_id "
            "WHERE m.room_id=? ORDER BY m.id DESC LIMIT 1", (r["id"],)).fetchone()
        members = (db.execute("SELECT COUNT(*) c FROM users WHERE active=1").fetchone()["c"] if r["kind"] == "all"
                   else db.execute("SELECT COUNT(*) c FROM chat_members cm JOIN users u ON u.id=cm.user_id "
                                   "WHERE cm.room_id=? AND u.active=1", (r["id"],)).fetchone()["c"])
        out.append({"room": r, "unread": unread, "last": last, "members": members})
    return out


def total_unread(user) -> int:
    return sum(x["unread"] for x in my_rooms(user))


def mark_read(room_id: int, user_id: int, msg_id: int):
    get_db().execute(
        "INSERT INTO chat_members(room_id, user_id, last_read_id) VALUES (?,?,?) "
        "ON CONFLICT(room_id, user_id) DO UPDATE SET last_read_id=MAX(last_read_id, excluded.last_read_id)",
        (room_id, user_id, msg_id))


# ---------------------------------------------------------------- 메시지

def post_message(room_id, user_id, kind, body=None, lat=None, lon=None, file_path=None) -> int:
    cur = get_db().execute(
        "INSERT INTO chat_messages(room_id, user_id, kind, body, lat, lon, file_path, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)", (room_id, user_id, kind, body, lat, lon, file_path, _stamp()))
    return cur.lastrowid


def message_json(m, me) -> dict:
    deleted = bool(m["deleted"])
    return {
        "id": m["id"],
        "kind": "deleted" if deleted else m["kind"],
        "user_id": m["user_id"],
        "name": m["name"] or "",
        "dept": m["dept"] or "",
        "position": m["position"] or "",
        "body": "" if deleted else (m["body"] or ""),
        "lat": None if deleted else m["lat"],
        "lon": None if deleted else m["lon"],
        "photo": (url_for("chat.photo", mid=m["id"]) if (m["file_path"] and not deleted) else None),
        "time": m["created_at"][11:16],
        "date": m["created_at"][:10],
        "mine": m["user_id"] == me["id"],
        "can_delete": (not deleted) and m["kind"] != "system" and (m["user_id"] == me["id"] or me["role"] == "admin"),
    }


def fetch_messages(room_id, me, after=0, before=None):
    sql = ("SELECT m.*, u.name, u.position, d.name AS dept FROM chat_messages m LEFT JOIN users u ON u.id=m.user_id "
           "LEFT JOIN departments d ON d.id=u.department_id WHERE m.room_id=?")
    args: list = [room_id]
    if before:
        sql += " AND m.id<? ORDER BY m.id DESC LIMIT ?"
        args += [before, PAGE]
        rows = list(reversed(get_db().execute(sql, args).fetchall()))
    elif after:
        sql += " AND m.id>? ORDER BY m.id LIMIT 500"
        args.append(after)
        rows = get_db().execute(sql, args).fetchall()
    else:
        sql += " ORDER BY m.id DESC LIMIT ?"
        args.append(PAGE)
        rows = list(reversed(get_db().execute(sql, args).fetchall()))
    return [message_json(m, me) for m in rows]


# ---------------------------------------------------------------- 상태

def set_status(user_id, status, place=None, eta=None, note=None):
    get_db().execute(
        "INSERT INTO user_status(user_id, status, place, eta, note, updated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, place=excluded.place, eta=excluded.eta, "
        "note=excluded.note, updated_at=excluded.updated_at",
        (user_id, status, place, eta, note, _stamp()))


def status_text(name, status, place=None, eta=None, note=None) -> str:
    parts = [f"{STATUS_ICONS.get(status, '')} {name} · {STATUS_LABELS.get(status, status)}"]
    if place:
        parts.append(f"— {place}")
    if eta:
        parts.append(f"(복귀 {eta} 예정)")
    text = " ".join(parts)
    if note:
        text += f"\n{note}"
    return text


def status_board():
    """재직자 전원의 오늘 상태. 오늘 갱신되지 않은 상태는 '미확인'."""
    t = today().isoformat()
    rows = get_db().execute(
        "SELECT u.id, u.name, u.position, d.name AS dept, s.status, s.place, s.eta, s.note, s.updated_at "
        "FROM users u LEFT JOIN departments d ON d.id=u.department_id "
        "LEFT JOIN user_status s ON s.user_id=u.id WHERE u.active=1 ORDER BY d.name, u.name").fetchall()
    board = []
    for r in rows:
        fresh = bool(r["updated_at"] and r["updated_at"][:10] == t)
        board.append({"r": r, "status": r["status"] if fresh else None})
    return board


def _float(v):
    try:
        f = float(v)
        return f if -180 <= f <= 180 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 사진

def _upload_dir() -> str:
    d = os.path.join(current_app.instance_path, "uploads", "chat", now().strftime("%Y%m"))
    os.makedirs(d, exist_ok=True)
    return d


def save_photo(fs) -> str | None:
    data = fs.read(MAX_PHOTO_BYTES + 1)
    if not data or len(data) > MAX_PHOTO_BYTES:
        return None
    ext = next((e for sig, e, _ in IMAGE_SIGS if data.startswith(sig)), None)
    if ext is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    if ext is None:
        return None
    path = os.path.join(_upload_dir(), f"{uuid.uuid4().hex}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return os.path.relpath(path, current_app.instance_path)


# ---------------------------------------------------------------- 화면

@bp.route("")
@login_required
def index():
    me = current_user()
    db = get_db()
    users = db.execute("SELECT u.id, u.name, d.name AS dept FROM users u LEFT JOIN departments d "
                       "ON d.id=u.department_id WHERE u.active=1 ORDER BY d.name, u.name").fetchall()
    mine = db.execute("SELECT * FROM user_status WHERE user_id=?", (me["id"],)).fetchone()
    return render_template("chat_index.html", rooms=my_rooms(me), board=status_board(), users=users,
                           STATUS=STATUS_LABELS, ICON=STATUS_ICONS, mine=mine,
                           can_create=me["role"] in ("admin", "manager"))


@bp.route("/rooms", methods=["POST"])
@login_required
def create_room():
    me = current_user()
    if me["role"] not in ("admin", "manager"):
        abort(403)
    name = (request.form.get("name") or "").strip()[:40]
    if not name:
        flash("대화방 이름을 입력하세요.")
        return redirect(url_for("chat.index"))
    db = get_db()
    cur = db.execute("INSERT INTO chat_rooms(name, kind, description, created_by) VALUES (?, 'custom', ?, ?)",
                     (name, (request.form.get("description") or "").strip()[:200] or None, me["id"]))
    rid = cur.lastrowid
    ids = {int(x) for x in request.form.getlist("members") if x.isdigit()} | {me["id"]}
    for uid in ids:
        db.execute("INSERT OR IGNORE INTO chat_members(room_id, user_id) VALUES (?,?)", (rid, uid))
    post_message(rid, None, "system", f"{me['name']}님이 '{name}' 대화방을 만들었습니다.")
    audit(me["id"], "chat_room_create", rid, name)
    db.commit()
    return redirect(url_for("chat.room", rid=rid))


@bp.route("/<int:rid>")
@login_required
def room(rid):
    me = current_user()
    r = room_or_403(rid)
    db = get_db()
    msgs = fetch_messages(rid, me)
    if msgs:
        mark_read(rid, me["id"], msgs[-1]["id"])
        db.commit()
    if r["kind"] == "all":
        members = db.execute("SELECT u.id, u.name, u.position, d.name AS dept FROM users u LEFT JOIN departments d "
                             "ON d.id=u.department_id WHERE u.active=1 ORDER BY u.name").fetchall()
    else:
        members = db.execute("SELECT u.id, u.name, u.position, d.name AS dept FROM chat_members cm JOIN users u ON u.id=cm.user_id "
                             "LEFT JOIN departments d ON d.id=u.department_id WHERE cm.room_id=? AND u.active=1 "
                             "ORDER BY u.name", (rid,)).fetchall()
    others = []
    if can_manage_room(r, me) and r["kind"] != "all":
        ids = {m["id"] for m in members}
        others = [u for u in db.execute("SELECT u.id, u.name, d.name AS dept FROM users u LEFT JOIN departments d "
                                        "ON d.id=u.department_id WHERE u.active=1 ORDER BY u.name").fetchall()
                  if u["id"] not in ids]
    mine = db.execute("SELECT * FROM user_status WHERE user_id=?", (me["id"],)).fetchone()
    return render_template("chat_room.html", room=r, msgs=msgs, members=members, others=others,
                           manage=can_manage_room(r, me), STATUS=STATUS_LABELS, mine=mine)


@bp.route("/<int:rid>/messages")
@login_required
def messages(rid):
    me = current_user()
    room_or_403(rid)
    msgs = fetch_messages(rid, me, after=request.args.get("after", type=int) or 0,
                          before=request.args.get("before", type=int))
    if msgs and not request.args.get("before"):
        mark_read(rid, me["id"], msgs[-1]["id"])
        get_db().commit()
    return jsonify({"messages": msgs})


@bp.route("/<int:rid>/send", methods=["POST"])
@login_required
def send(rid):
    me = current_user()
    room_or_403(rid)
    f = request.form
    lat, lon = _float(f.get("lat")), _float(f.get("lon"))
    body = (f.get("body") or "").strip()[:MAX_TEXT]
    photo = request.files.get("photo")
    if photo and photo.filename:
        path = save_photo(photo)
        if not path:
            return jsonify({"error": "사진은 10MB 이하의 JPG·PNG·GIF·WEBP만 보낼 수 있습니다."}), 400
        mid = post_message(rid, me["id"], "photo", body or None, file_path=path)
    elif lat is not None and lon is not None:
        mid = post_message(rid, me["id"], "location", body or "현재 위치", lat, lon)
    elif body:
        mid = post_message(rid, me["id"], "text", body)
    else:
        return jsonify({"error": "보낼 내용이 없습니다."}), 400
    mark_read(rid, me["id"], mid)
    get_db().commit()
    return jsonify({"messages": fetch_messages(rid, me, after=mid - 1)[:1]})


@bp.route("/<int:rid>/delete/<int:mid>", methods=["POST"])
@login_required
def delete(rid, mid):
    me = current_user()
    room_or_403(rid)
    db = get_db()
    m = db.execute("SELECT * FROM chat_messages WHERE id=? AND room_id=?", (mid, rid)).fetchone()
    if not m or m["kind"] == "system" or (m["user_id"] != me["id"] and me["role"] != "admin"):
        abort(403)
    db.execute("UPDATE chat_messages SET deleted=1 WHERE id=?", (mid,))
    if m["file_path"]:
        try:
            os.remove(os.path.join(current_app.instance_path, m["file_path"]))
        except OSError:
            pass
    audit(me["id"], "chat_delete", mid)
    db.commit()
    return jsonify({"ok": True})


@bp.route("/photo/<int:mid>")
@login_required
def photo(mid):
    m = get_db().execute("SELECT * FROM chat_messages WHERE id=? AND deleted=0", (mid,)).fetchone()
    if not m or not m["file_path"]:
        abort(404)
    room_or_403(m["room_id"])
    path = os.path.realpath(os.path.join(current_app.instance_path, m["file_path"]))
    if not path.startswith(os.path.realpath(current_app.instance_path) + os.sep) or not os.path.exists(path):
        abort(404)
    ext = path.rsplit(".", 1)[-1]
    mime = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext)
    resp = send_file(path, mimetype=mime, max_age=86400)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@bp.route("/status", methods=["POST"])
@login_required
def update_status():
    me = current_user()
    f = request.form
    status = f.get("status")
    if status not in STATUS_LABELS:
        abort(400)
    place = (f.get("place") or "").strip()[:60] or None
    eta = valid_hm(f.get("eta"))
    note = (f.get("note") or "").strip()[:300] or None
    set_status(me["id"], status, place, eta, note)
    rid = f.get("room_id", type=int)
    if rid:
        room = get_room(rid)
        if room is None or not is_member(room, me):
            abort(403)
        lat, lon = _float(f.get("lat")), _float(f.get("lon"))
        mid = post_message(rid, me["id"], "status", status_text(me["name"], status, place, eta, note), lat, lon)
        mark_read(rid, me["id"], mid)
    get_db().commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    flash(f"상태를 '{STATUS_LABELS[status]}'(으)로 바꿨습니다." + (" 대화방에 알렸습니다." if rid else ""))
    return redirect(request.referrer or url_for("chat.index"))


@bp.route("/<int:rid>/members", methods=["POST"])
@login_required
def members(rid):
    me = current_user()
    r = room_or_403(rid)
    if not can_manage_room(r, me) or r["kind"] == "all":
        abort(403)
    db = get_db()
    uid = request.form.get("user_id", type=int)
    u = db.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    if not u:
        abort(400)
    if request.form.get("action") == "add":
        db.execute("INSERT OR IGNORE INTO chat_members(room_id, user_id) VALUES (?,?)", (rid, uid))
        post_message(rid, None, "system", f"{me['name']}님이 {u['name']}님을 초대했습니다.")
    else:
        db.execute("DELETE FROM chat_members WHERE room_id=? AND user_id=?", (rid, uid))
        post_message(rid, None, "system", f"{u['name']}님이 대화방에서 나갔습니다.")
    audit(me["id"], "chat_member_" + (request.form.get("action") or "remove"), rid, uid)
    db.commit()
    return redirect(url_for("chat.room", rid=rid))


@bp.route("/<int:rid>/leave", methods=["POST"])
@login_required
def leave(rid):
    me = current_user()
    r = room_or_403(rid)
    if r["kind"] == "all":
        abort(403)
    db = get_db()
    db.execute("DELETE FROM chat_members WHERE room_id=? AND user_id=?", (rid, me["id"]))
    post_message(rid, None, "system", f"{me['name']}님이 대화방에서 나갔습니다.")
    db.commit()
    return redirect(url_for("chat.index"))


@bp.route("/<int:rid>/archive", methods=["POST"])
@login_required
def archive(rid):
    me = current_user()
    r = room_or_403(rid)
    if not can_manage_room(r, me) or r["kind"] == "all":
        abort(403)
    db = get_db()
    db.execute("UPDATE chat_rooms SET archived=1 WHERE id=?", (rid,))
    audit(me["id"], "chat_room_archive", rid, r["name"])
    db.commit()
    flash(f"'{r['name']}' 대화방을 닫았습니다. (대화 내용은 보관됩니다)")
    return redirect(url_for("chat.index"))


@bp.route("/unread")
@login_required
def unread():
    return jsonify({"unread": total_unread(current_user())})


def register(app):
    app.register_blueprint(bp)
    app.config.setdefault("MAX_CONTENT_LENGTH", MAX_PHOTO_BYTES + 512 * 1024)

    @app.template_filter("initials")
    def _initials(name):
        """프로필 동그라미 글자: 한글 3~4자 이름은 뒤 두 글자, 그 외는 첫 글자 (화면 JS와 동일 규칙)."""
        n = (name or "?").strip()
        if 3 <= len(n) <= 4 and all("가" <= ch <= "힣" for ch in n):
            return n[-2:]
        return n[:1].upper()

    @app.template_filter("avcolor")
    def _avcolor(uid):
        colors = ["#3b6ea8", "#2e8b57", "#b0603a", "#7a5ab5", "#c0476b", "#2a8a96", "#8a7a2e", "#5a6b7d"]
        return colors[(uid or 0) % len(colors)]

    @app.context_processor
    def _chat_ctx():
        return {"STATUS_LABELS": STATUS_LABELS, "STATUS_ICONS": STATUS_ICONS}
