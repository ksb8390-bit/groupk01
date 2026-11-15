"""대화(메신저) 기능 테스트."""
import io
import re
from datetime import datetime

from tests.test_app import CLOCK, app, login  # noqa: F401
from hr.db import get_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def csrf_of(client):
    return re.search(r'name="csrf" content="([^"]+)"', client.get("/").get_data(as_text=True)).group(1)


def H(tok):
    return {"X-CSRF": tok, "X-Requested-With": "fetch"}


def all_room_id(app):
    with app.app_context():
        return get_db().execute("SELECT id FROM chat_rooms WHERE kind='all'").fetchone()["id"]


def test_all_room_created_and_visible_to_everyone(app):
    rid = all_room_id(app)
    for who in ("admin", "kim", "lee"):
        c = app.test_client(); login(c, who)
        assert "전체대화방" in c.get("/chat").get_data(as_text=True)
        assert c.get(f"/chat/{rid}").status_code == 200


def test_send_poll_and_unread(app):
    rid = all_room_id(app)
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    r = k.post(f"/chat/{rid}/send", data={"body": "현장 도착했습니다"}, headers=H(kt))
    assert r.status_code == 200 and r.get_json()["messages"][0]["body"] == "현장 도착했습니다"
    a = app.test_client(); login(a, "admin")
    assert a.get("/chat/unread").get_json()["unread"] == 1
    msgs = a.get(f"/chat/{rid}/messages?after=0").get_json()["messages"]
    assert [m["body"] for m in msgs] == ["현장 도착했습니다"] and msgs[0]["name"] == "kim" and not msgs[0]["mine"]
    with app.app_context():
        db = get_db(); db.execute("UPDATE users SET position='과장' WHERE login_id='kim'"); db.commit()
    m = a.get(f"/chat/{rid}/messages").get_json()["messages"][0]
    assert m["position"] == "과장" and m["dept"] == "설계팀" and m["user_id"] == 2
    assert a.get("/chat/unread").get_json()["unread"] == 0          # 조회 시 읽음 처리
    assert k.get("/chat/unread").get_json()["unread"] == 0          # 본인 메시지는 안 읽음 수에서 제외


def test_custom_room_membership_enforced(app):
    a = app.test_client(); login(a, "admin"); at = csrf_of(a)
    a.post("/chat/rooms", data={"csrf": at, "name": "애월A현장", "members": ["2"]})
    with app.app_context():
        rid = get_db().execute("SELECT id FROM chat_rooms WHERE name='애월A현장'").fetchone()["id"]
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    assert k.get(f"/chat/{rid}").status_code == 200
    l = app.test_client(); login(l, "lee"); lt = csrf_of(l)
    assert l.get(f"/chat/{rid}").status_code == 403
    assert l.get(f"/chat/{rid}/messages").status_code == 403
    assert l.post(f"/chat/{rid}/send", data={"body": "x"}, headers=H(lt)).status_code == 403
    assert "애월A현장" not in l.get("/chat").get_data(as_text=True)
    # 초대 후 접근 가능, 시스템 메시지
    a.post(f"/chat/{rid}/members", data={"csrf": at, "user_id": 3, "action": "add"})
    assert l.get(f"/chat/{rid}").status_code == 200
    bodies = [m["body"] for m in l.get(f"/chat/{rid}/messages").get_json()["messages"]]
    assert any("초대" in b for b in bodies)
    _ = kt


def test_employee_cannot_create_room_or_manage_members(app):
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    assert k.post("/chat/rooms", data={"csrf": kt, "name": "몰래방"}).status_code == 403
    rid = all_room_id(app)
    assert k.post(f"/chat/{rid}/members", data={"csrf": kt, "user_id": 3, "action": "add"}).status_code == 403


def test_status_update_posts_to_room_and_board(app):
    rid = all_room_id(app)
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    k.post("/chat/status", data={"status": "visit", "place": "제주시청 건축과", "eta": "15:30",
                                 "note": "허가 협의", "room_id": rid, "lat": "33.4996", "lon": "126.5312"}, headers=H(kt))
    a = app.test_client(); login(a, "admin")
    m = a.get(f"/chat/{rid}/messages").get_json()["messages"][-1]
    assert m["kind"] == "status" and "외근" in m["body"] and "제주시청 건축과" in m["body"] and "15:30" in m["body"]
    assert m["lat"] == 33.4996
    page = a.get("/chat").get_data(as_text=True)
    assert "제주시청 건축과" in page and "복귀 15:30" in page


def test_status_without_room_only_updates_board(app):
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    k.post("/chat/status", data={"csrf": kt, "status": "outside", "place": "은행"})
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) c FROM chat_messages").fetchone()["c"] == 0
        assert get_db().execute("SELECT status FROM user_status").fetchone()["status"] == "outside"


def test_punch_sets_status(app):
    k = app.test_client(); tok = login(k, "kim")
    CLOCK["now"] = datetime(2026, 11, 10, 8, 50)
    k.post("/punch", data={"csrf": tok, "action": "in", "work_type": "field", "note": "애월 현장"})
    with app.app_context():
        s = get_db().execute("SELECT * FROM user_status").fetchone()
    assert s["status"] == "field" and s["place"] == "애월 현장"
    k.post("/punch", data={"csrf": tok, "action": "out"})
    with app.app_context():
        assert get_db().execute("SELECT status FROM user_status").fetchone()["status"] == "off"
    CLOCK["now"] = datetime(2026, 11, 10, 9, 5)


def test_location_message(app):
    rid = all_room_id(app)
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    m = k.post(f"/chat/{rid}/send", data={"lat": "33.45", "lon": "126.56", "body": "여기 있어요"}, headers=H(kt)).get_json()["messages"][0]
    assert m["kind"] == "location" and m["lat"] == 33.45


def test_photo_upload_access_and_validation(app):
    rid = all_room_id(app)
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    m = k.post(f"/chat/{rid}/send", data={"photo": (io.BytesIO(PNG), "a.png"), "body": "골조 검측"},
               headers=H(kt), content_type="multipart/form-data").get_json()["messages"][0]
    assert m["kind"] == "photo" and m["body"] == "골조 검측"
    r = k.get(m["photo"])
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data == PNG
    # 이미지가 아닌 파일 거부
    bad = k.post(f"/chat/{rid}/send", data={"photo": (io.BytesIO(b"<script>alert(1)</script>"), "x.png")},
                 headers=H(kt), content_type="multipart/form-data")
    assert bad.status_code == 400
    # 로그인 안 한 사용자 접근 불가
    assert app.test_client().get(m["photo"]).status_code == 302


def test_photo_in_private_room_hidden_from_non_members(app):
    a = app.test_client(); login(a, "admin"); at = csrf_of(a)
    a.post("/chat/rooms", data={"csrf": at, "name": "비공개", "members": []})
    with app.app_context():
        rid = get_db().execute("SELECT id FROM chat_rooms WHERE name='비공개'").fetchone()["id"]
    m = a.post(f"/chat/{rid}/send", data={"photo": (io.BytesIO(PNG), "a.png")},
               headers=H(at), content_type="multipart/form-data").get_json()["messages"][0]
    k = app.test_client(); login(k, "kim")
    assert k.get(m["photo"]).status_code == 403


def test_delete_own_only_admin_any(app):
    rid = all_room_id(app)
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    l = app.test_client(); login(l, "lee"); lt = csrf_of(l)
    mid = k.post(f"/chat/{rid}/send", data={"body": "오타"}, headers=H(kt)).get_json()["messages"][0]["id"]
    assert l.post(f"/chat/{rid}/delete/{mid}", headers=H(lt)).status_code == 403
    assert k.post(f"/chat/{rid}/delete/{mid}", headers=H(kt)).status_code == 200
    m = l.get(f"/chat/{rid}/messages").get_json()["messages"][-1]
    assert m["kind"] == "deleted" and m["body"] == ""
    mid2 = l.post(f"/chat/{rid}/send", data={"body": "부적절"}, headers=H(lt)).get_json()["messages"][0]["id"]
    a = app.test_client(); login(a, "admin"); at = csrf_of(a)
    assert a.post(f"/chat/{rid}/delete/{mid2}", headers=H(at)).status_code == 200


def test_csrf_required_for_send(app):
    rid = all_room_id(app)
    k = app.test_client(); login(k, "kim")
    assert k.post(f"/chat/{rid}/send", data={"body": "x"}).status_code == 400


def test_html_is_not_rendered(app):
    rid = all_room_id(app)
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    k.post(f"/chat/{rid}/send", data={"body": "</script><img src=x onerror=alert(1)>"}, headers=H(kt))
    page = k.get(f"/chat/{rid}").get_data(as_text=True)
    assert "<img src=x" not in page and "</script><img" not in page


def test_archive_and_leave(app):
    a = app.test_client(); login(a, "admin"); at = csrf_of(a)
    a.post("/chat/rooms", data={"csrf": at, "name": "임시방", "members": ["2"]})
    with app.app_context():
        rid = get_db().execute("SELECT id FROM chat_rooms WHERE name='임시방'").fetchone()["id"]
    k = app.test_client(); login(k, "kim"); kt = csrf_of(k)
    k.post(f"/chat/{rid}/leave", data={"csrf": kt})
    assert k.get(f"/chat/{rid}").status_code == 403
    a.post(f"/chat/{rid}/archive", data={"csrf": at})
    assert a.get(f"/chat/{rid}").status_code == 404
    rid_all = all_room_id(app)
    assert a.post(f"/chat/{rid_all}/archive", data={"csrf": at}).status_code == 403


def test_older_messages_paging(app):
    rid = all_room_id(app)
    with app.app_context():
        db = get_db()
        for i in range(100):
            db.execute("INSERT INTO chat_messages(room_id,user_id,kind,body,created_at) VALUES (?,?,?,?,?)",
                       (rid, 1, "text", f"m{i}", "2026-11-10 09:00:00"))
        db.commit()
    k = app.test_client(); login(k, "kim")
    page = k.get(f"/chat/{rid}").get_data(as_text=True)
    assert '"m99"' in page and '"m19"' not in page
    first = int(re.search(r'"id": (\d+)', page).group(1))
    older = k.get(f"/chat/{rid}/messages?before={first}").get_json()["messages"]
    assert older[0]["body"] == "m0" and len(older) == 20
