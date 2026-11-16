# -*- coding: utf-8 -*-
"""직원 계정 안내 PDF: 1쪽 관리자 보관용 전체 목록 + 직원별 개인 안내장(잘라서 배부).

사용 (iMac 터미널):
    flask --app hr.app account-sheet                 # 바탕화면 '직원계정_임시비밀번호.xlsx' → PDF
    flask --app hr.app account-sheet --reissue       # 아직 로그인 안 한 직원의 임시 비밀번호를 새로 발급해 PDF 작성
"""
from __future__ import annotations

import os
import socket

FONT_CANDIDATES = [  # (경로, ttc 내부 번호) — 맥 → 리눅스 순
    ("/System/Library/Fonts/Supplemental/AppleGothic.ttf", 0),
    ("/Library/Fonts/AppleGothic.ttf", 0),
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 0),
    ("/Library/Fonts/NanumGothic.ttf", 0),
    (os.path.expanduser("~/Library/Fonts/NanumGothic.ttf"), 0),
    ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
]


def _register_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for path, idx in FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont("KR", path, subfontIndex=idx))
            return "KR"
        except Exception:  # noqa: BLE001 — 형식이 맞지 않는 글꼴은 다음 후보로
            continue
    raise RuntimeError("한글 글꼴을 찾지 못했습니다. NanumGothic.ttf 를 설치한 뒤 다시 실행하세요.")


def default_url(port: int = 5050) -> str:
    """사내망 주소 추정 (예: http://192.168.1.119:5050)."""
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # 실제 전송 없음 — 사용할 사내 IP만 확인
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    return f"http://{ip}:{port}"


def read_accounts_xlsx(path: str) -> list[dict]:
    import openpyxl
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(c or "").strip() for c in rows[0]]
    need = ("이름", "아이디", "임시 비밀번호")
    if not all(h in head for h in need):
        raise ValueError("계정 엑셀 형식이 아닙니다 (이름·아이디·임시 비밀번호 열 필요).")
    ix = {h: i for i, h in enumerate(head)}
    out = []
    for r in rows[1:]:
        if not r or not r[ix["이름"]] or not r[ix["아이디"]]:
            continue
        get = lambda k: (str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) and r[ix[k]] is not None else "")  # noqa: E731
        out.append({"name": get("이름"), "login_id": get("아이디"), "password": get("임시 비밀번호"),
                    "role": get("권한"), "dept": get("부서"), "position": get("직위")})
    return out


def build_pdf(accounts: list[dict], out_path: str, url: str, company: str = "GroupK", as_of: str = ""):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    font = _register_font()
    W, H = A4
    c = canvas.Canvas(out_path, pagesize=A4)
    c.setTitle(f"{company} 근태관리 계정 안내")

    # ---- 1쪽: 관리자 보관용 목록
    def list_header(page_no):
        c.setFont(font, 15)
        c.drawString(18 * mm, H - 20 * mm, f"{company} 근태관리 — 직원 계정 목록")
        c.setFont(font, 9)
        c.setFillColor(colors.HexColor("#c94545"))
        c.drawString(18 * mm, H - 26 * mm, "관리자 보관용 · 개인 안내장 배부 후 이 목록은 파기하세요 (첫 로그인 시 비밀번호 변경 강제)")
        c.setFillColor(colors.black)
        c.drawRightString(W - 18 * mm, H - 20 * mm, f"{as_of}  접속 {url}  ({page_no}쪽)")
        cols = [("No", 9), ("이름", 20), ("부서", 34), ("직위", 16), ("권한", 14), ("아이디", 24), ("임시 비밀번호", 36), ("배부 확인", 0)]
        x, y = 18 * mm, H - 36 * mm
        c.setFillColor(colors.HexColor("#1f3a5f"))
        c.rect(18 * mm, y - 2 * mm, W - 36 * mm, 7 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(font, 9)
        xs = []
        for title, w in cols:
            xs.append(x)
            c.drawString(x + 1.5 * mm, y, title)
            x += w * mm
        c.setFillColor(colors.black)
        return xs, y - 8 * mm

    page = 1
    xs, y = list_header(page)
    for n, a in enumerate(accounts, 1):
        if y < 20 * mm:
            c.showPage()
            page += 1
            xs, y = list_header(page)
        c.setFont(font, 9)
        vals = [str(n), a["name"], a["dept"], a["position"], a["role"], a["login_id"], a["password"] or "-"]
        for x, v in zip(xs, vals):
            c.drawString(x + 1.5 * mm, y, v[:22])
        c.setStrokeColor(colors.HexColor("#d0d5dc"))
        c.line(18 * mm, y - 2.5 * mm, W - 18 * mm, y - 2.5 * mm)
        y -= 7.5 * mm
    c.showPage()

    # ---- 개인 안내장: A4 한 장에 2열 × 4행 = 8명, 점선으로 잘라 배부
    slips = [a for a in accounts if a["password"] and not a["password"].startswith("(")]
    cw, ch = (W - 20 * mm) / 2, (H - 20 * mm) / 4
    for i, a in enumerate(slips):
        k = i % 8
        if i and k == 0:
            c.showPage()
        col, row = k % 2, k // 2
        x0, y0 = 10 * mm + col * cw, H - 10 * mm - (row + 1) * ch
        c.setDash(3, 3)
        c.setStrokeColor(colors.HexColor("#9aa3ad"))
        c.rect(x0, y0, cw, ch, fill=0, stroke=1)
        c.setDash()
        px, py = x0 + 6 * mm, y0 + ch - 9 * mm
        c.setFillColor(colors.HexColor("#1f3a5f"))
        c.setFont(font, 11)
        c.drawString(px, py, f"{company} 근태관리 앱 계정 안내")
        c.setFillColor(colors.black)
        c.setFont(font, 10)
        c.drawString(px, py - 7 * mm, f"{a['name']} {a['position']}  ·  {a['dept']}")
        c.setFont(font, 9)
        c.drawString(px, py - 14 * mm, "접속 주소")
        c.drawString(px, py - 20 * mm, "아이디")
        c.drawString(px, py - 27 * mm, "임시 비밀번호")
        c.setFont("Courier-Bold", 10)
        c.drawString(px + 24 * mm, py - 14 * mm, url)
        c.setFont("Courier-Bold", 12)
        c.drawString(px + 24 * mm, py - 20 * mm, a["login_id"])
        c.setFont("Courier-Bold", 15)
        c.drawString(px + 24 * mm, py - 27.5 * mm, a["password"])
        c.setFont(font, 7)
        c.setFillColor(colors.HexColor("#4a5563"))
        tips = ["① 휴대폰·PC 브라우저 주소창에 접속 주소 입력",
                "② 아이디·임시 비밀번호로 로그인",
                "③ 새 비밀번호(8자 이상)로 변경 → 30일간 로그인 유지",
                "※ 분실 시 관리자에게 초기화 요청 · 본인만 보관 후 파기"]
        for j, t in enumerate(tips):
            c.drawString(px, y0 + 17 * mm - j * 4.2 * mm, t)
        c.setFillColor(colors.black)
    c.showPage()
    c.save()
    return {"listed": len(accounts), "slips": len(slips)}


def register(app):
    import click

    from .db import get_db

    @app.cli.command("account-sheet")
    @click.option("--xlsx", default=None, help="계정 엑셀 (기본: 바탕화면 '직원계정_임시비밀번호.xlsx')")
    @click.option("--out", default=None, help="PDF 저장 위치 (기본: 바탕화면 '직원계정_안내.pdf')")
    @click.option("--url", default=None, help="안내장에 인쇄할 접속 주소 (기본: 이 컴퓨터의 사내 주소:5050)")
    @click.option("--reissue", is_flag=True,
                  help="아직 첫 로그인을 하지 않은 직원의 임시 비밀번호를 새로 발급해 PDF 작성 (엑셀 분실 시)")
    def account_sheet(xlsx, out, url, reissue):
        """직원 아이디·임시 비밀번호 안내 PDF를 만듭니다."""
        import secrets
        from datetime import date

        from .app import ROLE_LABELS, hash_pw
        from .importer import PW_CHARS, save_accounts_xlsx

        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        base = desk if os.path.isdir(desk) else os.getcwd()
        url = url or default_url()
        out = out or os.path.join(base, "직원계정_안내.pdf")
        db = get_db()
        if reissue:
            rows = db.execute(
                "SELECT u.*, d.name AS dept FROM users u LEFT JOIN departments d ON d.id=u.department_id "
                "WHERE u.active=1 ORDER BY d.name, u.name").fetchall()
            pending = [u for u in rows if u["must_change_pw"] and u["role"] != "admin"]
            pending_ids = {u["id"] for u in pending}
            if not pending:
                raise click.ClickException("첫 로그인 전인 직원이 없습니다. (이미 모두 비밀번호를 바꿨습니다)")
            click.confirm(f"첫 로그인 전인 {len(pending)}명의 임시 비밀번호를 새로 발급합니다. "
                          "이전에 전달한 임시 비밀번호는 쓸 수 없게 됩니다. 계속할까요?", abort=True)
            accounts, xl = [], []
            for u in rows:
                pw = ""
                if u["id"] in pending_ids:
                    pw = "".join(secrets.choice(PW_CHARS) for _ in range(8))
                    db.execute("UPDATE users SET password_hash=?, must_change_pw=1 WHERE id=?", (hash_pw(pw), u["id"]))
                else:
                    pw = "(본인 비밀번호 사용 중)"
                accounts.append({"name": u["name"], "login_id": u["login_id"], "password": pw,
                                 "role": ROLE_LABELS.get(u["role"], u["role"]), "dept": u["dept"] or "",
                                 "position": u["position"] or ""})
                xl.append([u["name"], u["login_id"], pw, ROLE_LABELS.get(u["role"], u["role"]), u["dept"] or "",
                           u["position"] or "", u["hire_date"]])
            db.execute("INSERT INTO audit_log(actor_id, action, target, detail) VALUES (NULL,'password_reissue',?,?)",
                       (f"{len(pending)}명", "account-sheet --reissue"))
            db.commit()
            save_accounts_xlsx(xl, os.path.join(base, "직원계정_임시비밀번호.xlsx"))
        else:
            xlsx = xlsx or os.path.join(base, "직원계정_임시비밀번호.xlsx")
            if not os.path.exists(xlsx):
                raise click.ClickException(f"계정 엑셀이 없습니다: {xlsx}\n"
                                           "엑셀을 잃어버렸다면 --reissue 로 새로 발급하세요.")
            accounts = read_accounts_xlsx(xlsx)
        company = db.execute("SELECT value FROM settings WHERE key='company_name'").fetchone()
        res = build_pdf(accounts, out, url, company["value"] if company else "GroupK", date.today().isoformat())
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        click.echo(f"완료: 목록 {res['listed']}명, 개인 안내장 {res['slips']}장")
        click.echo(f"PDF: {out}")
        click.echo(f"접속 주소(안내장 인쇄): {url}  — 다르면 --url 로 지정")
        click.echo("※ 배부 후 PDF·엑셀 파일은 삭제하세요.")
