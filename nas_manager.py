# -*- coding: utf-8 -*-
"""
GroupK NAS(QNAP QTS) 관리 자동화 스크립트

실행 위치
  - 사무실 PC에서 NAS 공유폴더를 연결한 상태로 실행 (권장)
      Windows : python nas_manager.py report "\\\\NAS\\Projects"   (또는 Z:\\Projects)
      macOS   : python3 nas_manager.py report /Volumes/Projects
  - QNAP 본체 SSH에서 실행 (Python 3 QPKG 또는 Container Station 필요)
      python3 nas_manager.py report /share/Projects

명령
  init    업무별 표준 프로젝트 폴더 생성         (기본 미리보기, --apply 시 실제 생성)
  lint    파일명 규칙 점검 + 한글 자소분리(NFD) 교정 (기본 점검만, --fix-nfd 시 이름 변경)
  report  용량(폴더·확장자별)·대용량·장기미사용·CAD 임시파일 리포트
  dupes   중복 파일 탐지 (크기 → 앞부분 해시 → 전체 SHA-256 3단계)
  cleanup        정리 계획서(엑셀) 작성 — 파일 변경 없음
                 ① 중복: 1개만 보존 ② 백업·임시: 같은 폴더에 원본 있을 때만 ③ 압축: 전부 풀려 있을 때만
  cleanup-apply  검토·수정한 계획서대로 삭제 (기본 미리보기, --apply 시 실제 삭제)
  cleanup-all    공유폴더 바로 아래 폴더들을 하나씩 계획→삭제 (완료 목록 기록, 이어하기 가능)
  compare        폴더 A(휴지통·옛 사본)의 파일이 폴더 B(실제 자료)에 이미 있는지 대조

공통
  - QNAP 시스템 폴더(@Recycle, .@__thumb, @Recently-Snapshot, .streams 등)는 자동 제외
  - 결과는 ./nas_reports/ 에 xlsx(openpyxl 설치 시) 또는 CSV(utf-8-sig, 엑셀 호환)로 저장
  - 삭제는 cleanup-apply --apply 로만 수행(계획서 검토 → 미리보기 → 확인 입력). 이름 변경은 --fix-nfd 명시 시만
  - 설정은 nas_config.json 으로 덮어쓸 수 있음 (nas_config.example.json 참고)
"""

import argparse
import csv
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime

try:
    from openpyxl import Workbook
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

# =========================
# 1. 기본 설정 (nas_config.json 으로 덮어쓰기 가능)
# =========================
DEFAULT_CONFIG = {
    # QNAP/Windows/macOS 시스템·휴지통 폴더 (스캔 제외)
    "exclude_dirs": [
        "@Recycle", "#recycle", "@Recently-Snapshot", "@__thumb", ".@__thumb",
        ".@__desc", ".@__qini", ".streams", "@eaDir", ".AppleDouble",
        "$RECYCLE.BIN", "System Volume Information", ".Trash*", ".git",
    ],
    "exclude_files": ["Thumbs.db", ".DS_Store", "desktop.ini", "._*"],

    # 업무별 프로젝트 하위폴더 표준
    "project_templates": {
        "설계": [
            "00_계약·행정", "01_자료조사/대지·현황", "01_자료조사/법규검토",
            "02_계획설계", "03_중간설계", "04_실시설계/건축", "04_실시설계/구조",
            "04_실시설계/기계·전기·통신·소방", "05_인허가", "06_구조·에너지·인증",
            "07_적산·내역", "08_심의·위원회", "09_감리·현장지원", "10_준공·성과품",
            "90_회의록", "91_사진", "99_참고",
        ],
        "CM": [
            "00_계약·행정", "01_설계단계/설계검토", "01_설계단계/VE",
            "02_시공단계/공정관리", "02_시공단계/원가·기성", "02_시공단계/품질관리",
            "02_시공단계/안전관리", "02_시공단계/설계변경·클레임", "02_시공단계/하도급",
            "03_시공후단계/준공·인수인계", "04_보고서/월간", "04_보고서/분기·최종",
            "90_회의록", "91_사진", "99_참고",
        ],
        "정비": [
            "00_계약·행정", "01_정비구역지정", "02_추진위원회", "03_조합설립",
            "04_사업시행계획", "05_시공자선정", "06_관리처분계획", "07_이주·철거·착공",
            "08_준공·이전고시", "09_청산", "10_총회·대의원회", "11_동의서·명부",
            "12_정보공개", "90_회의록", "99_참고",
        ],
        "안전진단": [
            "00_계약·행정", "01_자료수집/설계도서·이력", "02_현장조사/외관조사",
            "02_현장조사/재료시험", "02_현장조사/사진", "03_구조해석", "04_상태평가·등급",
            "05_보고서/초안", "05_보고서/최종", "06_FMS등록", "99_참고",
        ],
        "인테리어": [
            "00_계약·행정", "01_실측·현황", "02_디자인/컨셉", "02_디자인/도면",
            "03_마감재·샘플", "04_견적·내역", "05_시공/공정", "05_시공/사진",
            "06_준공·하자", "99_참고",
        ],
        "연구용역": [
            "00_계약·행정", "01_착수", "02_자료조사·문헌", "03_분석·설문",
            "04_중간보고", "05_최종보고", "06_자문·심의", "90_회의록", "99_참고",
        ],
        "강의": ["00_강의계획서", "01_강의자료", "02_과제·시험", "03_성적", "99_참고"],
        "소송": [
            "00_사건개요·타임라인", "01_소장·준비서면", "02_증거/계약서",
            "02_증거/도면·사진", "02_증거/서신·메일", "03_감정", "04_판결·결정",
            "05_판례·법령", "99_참고",
        ],
    },

    # 파일명 규칙: 날짜 접두(YYYYMMDD_ 또는 YYMMDD_)를 요구할 확장자
    "require_date_prefix_ext": [".pdf", ".hwp", ".hwpx", ".docx", ".xlsx", ".pptx"],
    # 버전관리 경고 패턴 (정규식, 대소문자 무시)
    "bad_name_patterns": [
        r"최종.*최종", r"최최종", r"진짜\s*최종", r"수정본?\s*\d*\s*수정",
        r"복사본", r"\bcopy\b", r"\(\d+\)\.\w+$", r"- 사본",
    ],
    "max_path_len": 240,          # Windows 260자 제한 대비 경고선
    "max_name_len": 120,
    "max_path_bytes": 900,        # macOS 1024바이트 한계 대비 경고선 (NFD 기준)

    # report 옵션
    "large_file_mb": 500,
    "stale_days": 730,            # 2년 이상 미수정 → 아카이브 후보
    "top_n": 200,
    # CAD/오피스 잠금·임시·백업 파일 (report 집계용. 삭제 판정은 BACKUP_RULES 의 '원본 동반' 기준)
    "temp_patterns": [
        "*.bak", "*.sv$", "*.dwl", "*.dwl2", "*.ac$", "*.tmp", "~$*",
        "*.[0-9][0-9][0-9][0-9].rvt", "*.[0-9][0-9][0-9][0-9].rfa",
        "*.3dmbak", "*.skb", "*~.skp", "*.bpn",
    ],

    # cleanup 옵션
    # 이 패턴이 경로 중 어느 폴더명에 걸리면 삭제 대상에서 제외 (중복 그룹에서는 '보존본'으로 우선)
    "protect_dirs": [],
    "archive_exts": [".zip", ".7z", ".rar", ".alz", ".egg", ".tar", ".gz", ".tgz"],

    # dupes 옵션
    "dupes_min_mb": 1,
}

ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
DATE_PREFIX = re.compile(r"^(\d{8}|\d{6})[_\-\s]")


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        print(f"[설정] {path} 적용")
    return cfg


# =========================
# 2. 공통 유틸
# =========================

def _match_any(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def path_bytes(p):
    return len(unicodedata.normalize("NFD", p).encode("utf-8"))


def walk(root, cfg, errors=None, skip=()):
    """시스템 폴더를 제외하고 (dirpath, filename, full_path, stat) 생성.
    읽지 못한 파일·폴더는 errors 리스트에 (경로, 사유, 경로바이트) 로 기록 (화면에는 처음 3건만 표시)"""
    ex_dirs, ex_files = cfg["exclude_dirs"], cfg["exclude_files"]
    if errors is None:
        errors = []

    def note(path, e):
        reason = "경로 과다(macOS 1024바이트 초과)" if getattr(e, "errno", None) == 63 else f"{type(e).__name__}: {e.strerror or e}"
        errors.append((path, reason, path_bytes(path)))
        if len(errors) <= 3:
            print(f"  [읽기 실패] {reason} … {path[-80:]}", file=sys.stderr)
        elif len(errors) == 4:
            print("  [읽기 실패] 이후 건은 화면 표시 생략, 리포트 '읽기실패' 시트에 기록", file=sys.stderr)

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: note(e.filename or "", e)):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith((".@", "@")) and not _match_any(d, ex_dirs)
                       and not (skip and unicodedata.normalize("NFC", os.path.join(dirpath, d)) in skip)]
        for fn in filenames:
            if _match_any(fn, ex_files):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError as e:
                note(full, e)
                continue
            yield dirpath, fn, full, st


ERR_SHEET_HEADER = ["경로", "사유", "경로 바이트(NFD)"]


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def ts(t):
    """수정시각 → 문자열. 깨진 날짜(예: 30828년, 1601년)도 중단 없이 표시"""
    try:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return f"날짜이상({t:.0f})"


def valid_mtime(t, now):
    """1980-01-01 ~ 현재+1일 범위만 정상으로 인정"""
    return 315532800 <= t <= now + 86400


def write_sheets(basename, sheets):
    """sheets: {시트명: (헤더 리스트, 행 리스트)} → xlsx 1개 또는 CSV 여러 개"""
    os.makedirs("nas_reports", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if HAS_OPENPYXL:
        path = os.path.join("nas_reports", f"{basename}_{stamp}.xlsx")
        wb = Workbook()
        wb.remove(wb.active)
        for name, (header, rows) in sheets.items():
            ws = wb.create_sheet(name[:31])
            ws.append(header)
            for r in rows:
                ws.append(list(r))
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for i, h in enumerate(header, 1):
                ws.column_dimensions[ws.cell(1, i).column_letter].width = 60 if "경로" in h else max(12, len(h) * 2 + 2)
        wb.save(path)
        print(f"[저장] {path}")
        return path
    else:
        main_path = None
        for name, (header, rows) in sheets.items():
            path = os.path.join("nas_reports", f"{basename}_{name}_{stamp}.csv")
            if name == "삭제계획":
                main_path = path
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
            print(f"[저장] {path}")
        return main_path or path


# =========================
# 3. init : 표준 폴더 생성
# =========================

def cmd_init(args, cfg):
    tpl = cfg["project_templates"].get(args.type)
    if tpl is None:
        sys.exit(f"[오류] 업무유형 '{args.type}' 없음. 가능: {', '.join(cfg['project_templates'])}")
    name = f"{args.code}_{args.name}"
    if ILLEGAL_CHARS.search(name):
        sys.exit(f"[오류] 프로젝트명에 사용할 수 없는 문자 포함: {name}")
    base = os.path.join(args.root, unicodedata.normalize("NFC", name))
    if os.path.exists(base) and not args.apply:
        print(f"[주의] 이미 존재: {base} (없는 하위폴더만 생성됨)")
    print(f"[{'생성' if args.apply else '미리보기'}] {base}")
    made = 0
    for sub in tpl:
        p = os.path.join(base, *sub.split("/"))
        exists = os.path.isdir(p)
        print(f"   {'=' if exists else '+'} {sub}")
        if args.apply and not exists:
            os.makedirs(p, exist_ok=True)
            made += 1
    if args.apply:
        print(f"[완료] 신규 폴더 {made}개 생성")
    else:
        print("※ 실제 생성하려면 --apply 를 붙여 다시 실행")


# =========================
# 4. lint : 파일명 점검
# =========================

def cmd_lint(args, cfg):
    bad_pats = [re.compile(p, re.I) for p in cfg["bad_name_patterns"]]
    date_exts = {e.lower() for e in cfg["require_date_prefix_ext"]}
    rows, nfd_targets = [], []
    n = 0

    def check(kind, dirpath, name, full):
        issues = []
        if unicodedata.normalize("NFC", name) != name:
            issues.append("한글 자소분리(NFD, macOS 업로드)")
            nfd_targets.append((dirpath, name))
        if name != name.strip() or name.endswith("."):
            issues.append("앞뒤 공백/끝 마침표")
        if len(full) > cfg["max_path_len"]:
            issues.append(f"경로 길이 {len(full)}자 > {cfg['max_path_len']}")
        if path_bytes(full) > cfg["max_path_bytes"]:
            issues.append(f"경로 {path_bytes(full):,}바이트 > {cfg['max_path_bytes']} (macOS 접근 위험)")
        if len(name) > cfg["max_name_len"]:
            issues.append(f"이름 길이 {len(name)}자")
        if kind == "파일":
            stem, ext = os.path.splitext(name)
            if any(p.search(name) for p in bad_pats):
                issues.append("버전 혼란 표기(최종/복사본/(1) 등) → _v01 형식 권장")
            if ext.lower() in date_exts and not DATE_PREFIX.match(name):
                issues.append("날짜 접두 없음(YYYYMMDD_)")
            if not ext:
                issues.append("확장자 없음")
        return issues

    for dirpath, dirnames, _ in os.walk(args.root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".@", "@")) and not _match_any(d, cfg["exclude_dirs"])]
        for d in dirnames:
            iss = check("폴더", dirpath, d, os.path.join(dirpath, d))
            if iss:
                rows.append(("폴더", os.path.join(dirpath, d), d, " / ".join(iss)))
    errors = []
    for dirpath, fn, full, st in walk(args.root, cfg, errors):
        n += 1
        iss = check("파일", dirpath, fn, full)
        if iss:
            rows.append(("파일", full, fn, " / ".join(iss)))

    for path, reason, nb in errors:
        rows.append(("읽기실패", path, os.path.basename(path), f"{reason} / {nb:,}바이트"))
    print(f"[점검] 파일 {n:,}개 중 지적 {len(rows):,}건 (NFD {len(nfd_targets):,}건, 읽기실패 {len(errors):,}건)")
    write_sheets("lint", {"파일명점검": (["구분", "경로", "이름", "지적사항"], rows)})

    if args.fix_nfd and nfd_targets:
        # 깊은 경로부터 변경해야 상위 폴더 변경 후 하위 경로가 깨지지 않음
        nfd_targets.sort(key=lambda t: t[0].count(os.sep), reverse=True)
        log = []
        for dirpath, name in nfd_targets:
            src = os.path.join(dirpath, name)
            dst = os.path.join(dirpath, unicodedata.normalize("NFC", name))
            if os.path.exists(dst) and os.path.normcase(src) != os.path.normcase(dst):
                log.append((src, dst, "건너뜀: 동일 NFC 이름 존재"))
                continue
            try:
                os.rename(src, dst)
                log.append((src, dst, "변경"))
            except OSError as e:
                log.append((src, dst, f"실패: {e}"))
        print(f"[NFD 교정] {sum(1 for r in log if r[2] == '변경'):,}건 변경")
        write_sheets("nfd_fix_log", {"변경기록": (["원래경로", "변경경로", "결과"], log)})


# =========================
# 5. report : 용량·대용량·장기미사용·임시파일
# =========================

def cmd_report(args, cfg):
    root = os.path.abspath(args.root)
    now = time.time()
    large_b = cfg["large_file_mb"] * 1024 ** 2
    stale_s = cfg["stale_days"] * 86400
    by_top = defaultdict(lambda: [0, 0, 0.0])   # 용량, 개수, 최근수정
    by_ext = defaultdict(lambda: [0, 0])
    large, temps = [], []
    total_b = total_n = 0
    errors, bad_dates = [], []

    for dirpath, fn, full, st in walk(root, cfg, errors):
        rel = os.path.relpath(full, root)
        top = rel.split(os.sep)[0] if os.sep in rel else "(루트 파일)"
        t = by_top[top]
        t[0] += st.st_size; t[1] += 1
        if valid_mtime(st.st_mtime, now):
            t[2] = max(t[2], st.st_mtime)
        else:
            bad_dates.append((full, round(st.st_size / 1024 ** 2, 2), ts(st.st_mtime)))
        ext = os.path.splitext(fn)[1].lower() or "(없음)"
        by_ext[ext][0] += st.st_size; by_ext[ext][1] += 1
        total_b += st.st_size; total_n += 1
        if st.st_size >= large_b:
            large.append((full, st.st_size, ts(st.st_mtime)))
        if _match_any(fn, cfg["temp_patterns"]):
            temps.append((full, st.st_size, ts(st.st_mtime)))
        if total_n % 20000 == 0:
            print(f"  … {total_n:,}개 스캔")

    top_rows = sorted(
        ((k, round(v[0] / 1024 ** 3, 2), v[1], round(v[0] / total_b * 100, 1) if total_b else 0,
          ts(v[2]) if v[2] else "", "아카이브 후보" if v[2] and now - v[2] > stale_s else "")
         for k, v in by_top.items()),
        key=lambda r: -r[1])
    ext_rows = sorted(((k, round(v[0] / 1024 ** 3, 2), v[1]) for k, v in by_ext.items()), key=lambda r: -r[1])
    large.sort(key=lambda r: -r[1])
    large_rows = [(p, round(s / 1024 ** 2, 1), m) for p, s, m in large[: cfg["top_n"]]]
    temp_rows = [(p, round(s / 1024 ** 2, 2), m) for p, s, m in sorted(temps, key=lambda r: -r[1])]

    print(f"[요약] {root}\n  파일 {total_n:,}개 / {human(total_b)}")
    for r in top_rows[:10]:
        print(f"  {r[1]:>9,.2f} GB  {r[3]:>5}%  {r[0]}  {r[5]}")
    print(f"  대용량(≥{cfg['large_file_mb']}MB) {len(large):,}개, "
          f"CAD·오피스 임시/백업 {len(temps):,}개 / {human(sum(s for _, s, _ in temps))}")
    if bad_dates:
        print(f"  수정일 이상 {len(bad_dates):,}건 ('날짜이상' 시트 참고, 최근수정 집계에서 제외)")
    if errors:
        print(f"  읽기 실패 {len(errors):,}건 (대부분 경로 과다 → 폴더 단계 축소 필요, '읽기실패' 시트 참고)")

    write_sheets("report", {
        "1차폴더별": (["폴더", "용량(GB)", "파일수", "비율(%)", "최근수정", f"비고(>{cfg['stale_days']}일)"], top_rows),
        "확장자별": (["확장자", "용량(GB)", "파일수"], ext_rows),
        "대용량파일": (["경로", "크기(MB)", "수정일"], large_rows),
        "임시·백업파일": (["경로", "크기(MB)", "수정일"], temp_rows),
        "읽기실패": (ERR_SHEET_HEADER, sorted(errors, key=lambda r: -r[2])),
        "날짜이상": (["경로", "크기(MB)", "수정일"], bad_dates[:50000]),
    })


# =========================
# 6. dupes : 중복 파일
# =========================

def _hash(path, limit=None, chunk=1024 * 1024):
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk if limit is None else min(chunk, limit - read))
            if not b:
                break
            h.update(b)
            read += len(b)
            if limit is not None and read >= limit:
                break
    return h.hexdigest()


def cmd_dupes(args, cfg):
    min_b = cfg["dupes_min_mb"] * 1024 ** 2
    by_size = defaultdict(list)
    errors = []
    for _, fn, full, st in walk(args.root, cfg, errors):
        if st.st_size >= min_b:
            by_size[st.st_size].append((full, st.st_mtime))
    cand = {s: v for s, v in by_size.items() if len(v) > 1}
    print(f"[1단계] 크기 동일 그룹 {len(cand):,}개")

    groups = []
    for size, files in cand.items():
        by_head = defaultdict(list)
        for p, m in files:
            try:
                by_head[_hash(p, limit=64 * 1024)].append((p, m))
            except OSError:
                continue
        for same in (v for v in by_head.values() if len(v) > 1):
            by_full = defaultdict(list)
            for p, m in same:
                try:
                    by_full[_hash(p)].append((p, m))
                except OSError:
                    continue
            for h, v in by_full.items():
                if len(v) > 1:
                    groups.append((h, size, sorted(v, key=lambda x: x[1])))

    groups.sort(key=lambda g: -g[1] * (len(g[2]) - 1))
    rows, waste = [], 0
    for gi, (h, size, files) in enumerate(groups, 1):
        waste += size * (len(files) - 1)
        for i, (p, m) in enumerate(files):
            rows.append((gi, "원본(최초)" if i == 0 else "중복", p, round(size / 1024 ** 2, 2), ts(m), h[:16]))
    print(f"[결과] 중복 그룹 {len(groups):,}개, 절감 가능 {human(waste)}")
    print("※ 삭제는 수행하지 않음. 리포트 확인 후 NAS 휴지통(@Recycle) 사용 가능한 상태에서 수동 정리 권장")
    if errors:
        print(f"  읽기 실패 {len(errors):,}건은 비교에서 제외됨")
    write_sheets("dupes", {"중복파일": (["그룹", "구분", "경로", "크기(MB)", "수정일", "SHA256(앞16)"], rows),
                           "읽기실패": (ERR_SHEET_HEADER, errors)})


# =========================
# 7. cleanup : 정리 계획 수립 (파일 변경 없음)
# =========================
# 규칙 (2026-11 사내 방침)
#   ① 중복 파일   : 내용(SHA-256) 동일 그룹에서 1개만 보존, 나머지 삭제
#   ② 백업·임시   : 같은 폴더에 원본이 있을 때만 삭제. 원본 없는 백업은 '보류'(유일본일 수 있음)
#   ③ 압축 파일   : 압축 내용 전부가 디스크에 같은 크기(+CRC)로 풀려 있을 때만 압축파일 삭제
# 처리 순서: ② → ③ → ① (앞 단계에서 삭제될 파일은 중복 판정에서 제외 → 보존본이 삭제되는 일 없음)

PLAN_HEADER = ["처리", "구분", "경로", "크기(MB)", "수정일", "근거", "보존본/원본 경로", "그룹", "_bytes", "_mtime"]

# 중복 그룹 보존본 선정 시 감점 단어 (경로에 많이 포함될수록 삭제 쪽으로)
KEEP_PENALTY = re.compile(
    r"백업|backup|사본|복사본|copy|임시|\btemp\b|\btmp\b|다운로드|download|바탕\s*화면|desktop|"
    r"개인|personal|휴지통|trash|\bold\b|이전|구버전|새\s*폴더|new folder|받은\s*파일|카톡|kakaotalk|"
    r"_정리대기|\(\d+\)", re.I)


def _exists_any(path):
    """SMB(맥) 환경의 한글 정규화 차이(NFC/NFD)를 고려한 존재 확인 → 실제 경로 또는 None"""
    for form in ("NFC", "NFD"):
        p = unicodedata.normalize(form, path)
        if os.path.exists(p):
            return p
    return None


def backup_original(dirpath, fn):
    """백업·임시 파일이면 (원본 경로 또는 None, 유형) 반환. 백업 유형이 아니면 None"""
    low = fn.lower()
    join = lambda n: _exists_any(os.path.join(dirpath, n))

    def same_stem_any(stem, exclude_exts):
        try:
            names = os.listdir(dirpath)
        except OSError:
            return None
        stem_n = unicodedata.normalize("NFC", stem).lower()
        for n in names:
            b, e = os.path.splitext(unicodedata.normalize("NFC", n))
            if b.lower() == stem_n and e.lower() not in exclude_exts:
                return os.path.join(dirpath, n)
        return None

    if low.endswith(".bak"):                                  # X.dwg.bak → X.dwg / X.bak → X.(dwg 등)
        stem = fn[:-4]
        if os.path.splitext(stem)[1]:
            return join(stem), "BAK"
        return (join(stem + ".dwg") or same_stem_any(stem, {".bak", ".dwl", ".dwl2"})), "BAK"
    if low.endswith((".dwl", ".dwl2")):                       # AutoCAD 잠금파일
        return join(os.path.splitext(fn)[0] + ".dwg"), "DWL(잠금)"
    if low.endswith(".sv$"):                                  # AutoCAD 자동저장  X_1_1_1234.sv$
        stem = re.sub(r"(_\d+){2,3}_\d+$", "", fn[:-4])
        return join(stem + ".dwg"), "SV$(자동저장)"
    m = re.match(r"^(.*)\.\d{4}\.(rvt|rfa|rte)$", fn, re.I)  # Revit  X.0001.rvt
    if m:
        return join(f"{m.group(1)}.{m.group(2)}"), "Revit 백업"
    if low.endswith(".3dmbak"):
        return join(fn[:-7] + ".3dm"), "Rhino 백업"
    if low.endswith(".skb"):
        return join(fn[:-4] + ".skp"), "SketchUp 백업"
    if low.endswith("~.skp"):
        return join(fn[:-5] + ".skp"), "SketchUp 백업"
    if low.endswith(".bpn"):
        return join(fn[:-4] + ".pln"), "ArchiCAD 백업"
    if fn.startswith("~$"):                                   # Office 잠금 ~$보고서.docx (앞 2글자 치환)
        tail = unicodedata.normalize("NFC", fn[2:])
        try:
            for n in os.listdir(dirpath):
                nn = unicodedata.normalize("NFC", n)
                if not nn.startswith("~$") and (nn == tail or nn[2:] == tail):
                    return os.path.join(dirpath, n), "Office 잠금"
        except OSError:
            pass
        return None, "Office 잠금"
    return None


def _zip_name(zi):
    if zi.flag_bits & 0x800:
        return zi.filename
    raw = zi.filename.encode("cp437", errors="replace")
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return zi.filename


def _crc_file(path):
    import zlib
    c = 0
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            c = zlib.crc32(b, c)
    return c & 0xFFFFFFFF


def check_archive(full, fast=False):
    """(판정, 근거, 풀린 위치) — 판정: 삭제 / 보류"""
    import zipfile
    dirpath, fn = os.path.split(full)
    stem = os.path.splitext(fn)[0]
    if stem.lower().endswith(".tar"):
        stem = stem[:-4]
    if not fn.lower().endswith(".zip"):
        folder = _exists_any(os.path.join(dirpath, stem))
        if folder and os.path.isdir(folder):
            return "보류", "같은 이름 폴더 있음(이 형식은 내용 대조 불가 → 직접 확인 후 '삭제'로 변경)", folder
        return "보류", "풀린 폴더 없음(형식상 내용 대조 불가)", ""
    try:
        with zipfile.ZipFile(full) as z:
            members = [(zi, _zip_name(zi)) for zi in z.infolist()
                       if not zi.is_dir() and "__MACOSX/" not in _zip_name(zi)
                       and not _zip_name(zi).endswith(".DS_Store")]
    except (zipfile.BadZipFile, OSError, NotImplementedError) as e:
        return "보류", f"압축 열기 실패({type(e).__name__}) — 분할압축·손상 가능", ""
    if not members:
        return "보류", "빈 압축파일", ""

    bases = [os.path.join(dirpath, stem), os.path.join(dirpath, stem + " 2"), dirpath]
    best = (0, None, [])
    for base in bases:
        base = _exists_any(base)
        if not base or not os.path.isdir(base):
            continue
        found = []
        for zi, name in members:
            p = _exists_any(os.path.join(base, *name.split("/")))
            if p and os.path.isfile(p) and os.path.getsize(p) == zi.file_size:
                found.append((zi, p))
        if len(found) > best[0]:
            best = (len(found), base, found)
    n_found, base, found = best
    total = len(members)
    if n_found < total:
        if n_found == 0:
            return "보류", f"풀린 파일 없음(0/{total})", ""
        return "보류", f"일부만 풀림({n_found}/{total}) — 압축 유지", base
    if not fast:
        for zi, p in found:
            try:
                if _crc_file(p) != zi.CRC:
                    return "보류", f"풀린 파일 내용 불일치(CRC): {os.path.basename(p)}", base
            except OSError:
                return "보류", f"풀린 파일 읽기 실패: {os.path.basename(p)}", base
    check_archive.last_found = [p for _, p in found]
    return "삭제", f"전체 {total}개 파일이 풀려 있음({'크기' if fast else '크기+CRC'} 일치)", base


def _protected(path, root, pats):
    if not pats:
        return False
    parts = os.path.relpath(path, root).split(os.sep)[:-1]
    return any(_match_any(unicodedata.normalize("NFC", d), pats) for d in parts)


def cmd_cleanup(args, cfg):
    root = os.path.abspath(args.root)
    now = time.time()
    min_b = cfg["dupes_min_mb"] * 1024 ** 2
    pats = cfg["protect_dirs"]
    arc_exts = tuple(e.lower() for e in cfg["archive_exts"])
    rows, held, errors = [], [], []
    to_delete = set()
    anchors = set()   # 백업의 원본, 압축 삭제 근거가 된 풀린 파일 → 중복 판정에서 반드시 보존
    nfc = lambda p: unicodedata.normalize("NFC", p)
    archives, by_size = [], defaultdict(list)
    n = 0

    print("[1/4] 스캔 + 백업·임시 파일 판정")
    for dirpath, fn, full, st in walk(root, cfg, errors):
        n += 1
        if n % 20000 == 0:
            print(f"  … {n:,}개")
        prot = _protected(full, root, pats)
        rec = (full, st.st_size, st.st_mtime)
        bk = backup_original(dirpath, fn)
        if bk is not None:
            orig, kind = bk
            if prot:
                held.append(("보호폴더", kind, full, st.st_size, st.st_mtime, "protect_dirs 해당", ""))
            elif orig:
                rows.append(("삭제", f"백업·임시({kind})", full, st.st_size, st.st_mtime, "같은 폴더에 원본 있음", orig, ""))
                to_delete.add(full)
                anchors.add(nfc(orig))
            else:
                held.append(("보류", f"백업·임시({kind})", full, st.st_size, st.st_mtime, "원본 없음 → 유일본 가능성", ""))
            continue
        if fn.lower().endswith(arc_exts) and not prot:
            archives.append(rec)
        if st.st_size >= min_b:
            by_size[st.st_size].append((full, st.st_mtime, prot))

    print(f"[2/4] 압축파일 {len(archives):,}개 대조 ({'크기' if args.fast else '크기+CRC'})")
    for i, (full, size, mt) in enumerate(archives, 1):
        if i % 200 == 0:
            print(f"  … {i:,}/{len(archives):,}")
        verdict, why, base = check_archive(full, args.fast)
        if verdict == "삭제":
            rows.append(("삭제", "압축(풀림)", full, size, mt, why, base, ""))
            to_delete.add(full)
            anchors.update(nfc(p) for p in check_archive.last_found)
        else:
            held.append(("보류", "압축", full, size, mt, why, base))

    print("[3/4] 중복 파일 대조 (크기 → 앞 64KB → 전체 SHA-256)")
    cand = [v for v in by_size.values() if len([x for x in v if x[0] not in to_delete]) > 1]
    gid = 0
    for gi, files in enumerate(cand, 1):
        if gi % 500 == 0:
            print(f"  … 크기 그룹 {gi:,}/{len(cand):,}")
        files = [x for x in files if x[0] not in to_delete]
        heads = defaultdict(list)
        for x in files:
            try:
                heads[_hash(x[0], limit=64 * 1024)].append(x)
            except OSError:
                continue
        for same in (v for v in heads.values() if len(v) > 1):
            fulls = defaultdict(list)
            for x in same:
                try:
                    fulls[_hash(x[0])].append(x)
                except OSError:
                    continue
            for h, grp in fulls.items():
                if len(grp) < 2:
                    continue
                gid += 1
                size = os.path.getsize(grp[0][0]) if os.path.exists(grp[0][0]) else 0
                protected = [x for x in grp if x[2] or nfc(x[0]) in anchors]
                if protected:
                    keep = protected
                else:
                    # 보존본 우선순위: 감점 단어 적음 → 먼저 만들어진 파일(원본) → 경로 짧음
                    keep = [min(grp, key=lambda x: (len(KEEP_PENALTY.findall(os.path.relpath(x[0], root))),
                                                     x[1] if valid_mtime(x[1], now) else 9e18,
                                                     x[0].count(os.sep)))]
                keep_paths = {k[0] for k in keep}
                for x in grp:
                    if x[0] in keep_paths:
                        why = ("보호폴더" if x[2] else "압축·백업 판정 근거") if x in protected else "우선순위 1위"
                        held.append(("보존", "중복(보존본)", x[0], size, x[1], f"그룹 {gid} 보존본({why})", ""))
                    else:
                        rows.append(("삭제", "중복", x[0], size, x[1], f"SHA256 {h[:16]} 동일", keep[0][0], gid))

    print("[4/4] 계획서 작성")
    fmt = lambda r: (r[0], r[1], r[2], round(r[3] / 1024 ** 2, 2), ts(r[4]), r[5], r[6],
                     r[7] if len(r) > 7 else "", r[3], round(r[4], 3))
    plan = [fmt(r) for r in sorted(rows, key=lambda r: (r[1], -r[3]))]
    hold = [fmt(r + ("",)) for r in sorted(held, key=lambda r: (r[1], -r[3]))]

    summary = defaultdict(lambda: [0, 0])
    for r in rows:
        k = "중복" if r[1] == "중복" else r[1].split("(")[0]
        summary[k][0] += 1; summary[k][1] += r[3]
    total = sum(v[1] for v in summary.values())
    sum_rows = [(k, v[0], round(v[1] / 1024 ** 3, 2)) for k, v in summary.items()]
    sum_rows.append(("합계", sum(v[0] for v in summary.values()), round(total / 1024 ** 3, 2)))

    print(f"\n[계획] 스캔 {n:,}개 / 읽기실패 {len(errors):,}개")
    for k, c, g in sum_rows:
        print(f"  {k:<10} {c:>9,}개  {g:>9,.2f} GB")
    print(f"  보류·보존 {len(held):,}건 ('보류' 시트)")
    if len(plan) > 1_000_000 and HAS_OPENPYXL:
        print("[주의] 삭제 후보가 100만 행을 넘어 엑셀 한도 초과 → 폴더를 나눠 실행하세요")
    tag = getattr(args, "tag", "")
    path = write_sheets("cleanup_plan" + (f"_{tag}" if tag else ""), {
        "요약": (["구분", "건수", "용량(GB)"], sum_rows),
        "삭제계획": (PLAN_HEADER, plan),
        "보류": (PLAN_HEADER, hold),
        "읽기실패": (ERR_SHEET_HEADER, errors),
    })
    if getattr(args, "batch", False):
        return path, sum(v[0] for v in summary.values()), total
    print("\n다음 단계")
    print("  1) 엑셀 '삭제계획' 시트 검토 → 지우면 안 되는 행은 '처리' 칸을 '보류'로 변경 후 저장")
    print("     ('보류' 시트의 행도 '처리'를 '삭제'로 바꿔 '삭제계획' 시트로 옮기면 함께 처리됨)")
    print(f"  2) 미리보기 : python3 nas_manager.py cleanup-apply \"{path}\"")
    print(f"  3) 실제삭제 : python3 nas_manager.py cleanup-apply \"{path}\" --apply")


# =========================
# 8. cleanup-apply : 계획서대로 삭제
# =========================

def _read_plan(path):
    if path.lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True)
        ws = wb["삭제계획"]
        it = ws.iter_rows(values_only=True)
        header = [str(h) for h in next(it)]
        return [dict(zip(header, r)) for r in it if r and r[0] is not None]
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def cmd_cleanup_apply(args, cfg):
    rows = [r for r in _read_plan(args.plan) if str(r.get("처리", "")).strip() == "삭제"]
    total_b = sum(int(float(r["_bytes"] or 0)) for r in rows)
    print(f"[계획] 삭제 표시 {len(rows):,}건 / {human(total_b)}")
    if not rows:
        return {}
    if args.apply and not args.yes:
        ans = input(f"정말 {len(rows):,}개 파일을 삭제합니까? 계속하려면 '삭제' 입력: ").strip()
        if ans != "삭제":
            sys.exit("[취소] 아무것도 삭제하지 않았습니다.")

    os.makedirs("nas_reports", exist_ok=True)
    log_path = os.path.join("nas_reports", f"cleanup_log_{(getattr(args, 'tag', '') + '_') if getattr(args, 'tag', '') else ''}{datetime.now():%Y%m%d_%H%M%S}{'' if args.apply else '_미리보기'}.csv")
    stat = defaultdict(lambda: [0, 0])
    with open(log_path, "w", newline="", encoding="utf-8-sig") as lf:
        log = csv.writer(lf)
        log.writerow(["결과", "구분", "경로", "크기(bytes)", "사유"])
        t_start = time.time()
        for i, r in enumerate(rows, 1):
            if i % 50 == 0 or i == 1:
                el = time.time() - t_start
                eta = el / i * (len(rows) - i) if i > 1 else 0
                print(f"\r  … {i:,}/{len(rows):,}  경과 {el / 60:,.1f}분  남은 예상 {eta / 60:,.1f}분   ", end="", flush=True)
            path, kind, ref = r["경로"], r["구분"], (r.get("보존본/원본 경로") or "")
            size, mt = int(float(r["_bytes"] or 0)), float(r["_mtime"] or 0)
            real = _exists_any(path)
            if not real:
                res, why = "건너뜀", "이미 없음"
            else:
                st = os.stat(real)
                if st.st_size != size or abs(st.st_mtime - mt) > 2:
                    res, why = "건너뜀", "계획 이후 파일이 변경됨"
                elif ref and not _exists_any(ref):
                    res, why = "건너뜀", "보존본/원본/풀린 폴더가 사라짐"
                elif kind == "중복" and os.path.getsize(_exists_any(ref)) != size:
                    res, why = "건너뜀", "보존본 크기 변경됨"
                elif kind == "중복" and args.verify and _hash(real) != _hash(_exists_any(ref)):
                    res, why = "건너뜀", "보존본 내용 변경됨(재해시)"
                elif not args.apply:
                    res, why = "삭제예정", ""
                else:
                    try:
                        os.remove(real)
                        res, why = "삭제", ""
                    except OSError as e:
                        res, why = "실패", str(e)
            stat[res][0] += 1
            stat[res][1] += size if res in ("삭제", "삭제예정") else 0
            log.writerow([res, kind, path, size, why])
            if i % 50 == 0:
                lf.flush()
    print()
    print(f"[{'완료' if args.apply else '미리보기'}]")
    for k, (c, b) in stat.items():
        print(f"  {k:<6} {c:>9,}건  {human(b) if b else ''}")
    print(f"[기록] {log_path}")
    if args.apply:
        print("※ 실제 여유공간은 ① QNAP 휴지통(@Recycle) 비우기 ② 해당 기간 스냅샷 만료/삭제 후에 늘어납니다.")
    else:
        print("※ 실제 삭제하려면 --apply 를 붙여 다시 실행")
    return stat


# =========================
# 8-2. cleanup-all : 하위 폴더를 하나씩 계획 → 삭제 (이어하기 지원)
# =========================

def cmd_cleanup_all(args, cfg):
    import argparse as _ap
    root = os.path.abspath(args.root)
    skip = {unicodedata.normalize("NFC", x) for x in (args.skip or [])}
    os.makedirs("nas_reports", exist_ok=True)
    done_path = os.path.join("nas_reports", "cleanup_all_완료목록.txt")
    done = set()
    if os.path.exists(done_path):
        with open(done_path, encoding="utf-8") as f:
            done = {unicodedata.normalize("NFC", l.rstrip("\n")) for l in f if l.strip()}

    subs = sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d)) and not d.startswith((".", "@", "#"))
                  and not _match_any(d, cfg["exclude_dirs"]))
    todo, skipped = [], []
    key = lambda d: unicodedata.normalize("NFC", os.path.join(root, d))   # 완료 기록은 전체 경로로(공유폴더 간 동명 폴더 구분)
    for d in subs:
        dn = unicodedata.normalize("NFC", d)
        if dn in skip or (args.apply and key(d) in done) or _match_any(dn, cfg["protect_dirs"]):
            skipped.append(dn)
        else:
            todo.append(d)
    print(f"[대상] {root}")
    print(f"  처리할 폴더 {len(todo)}개: {', '.join(unicodedata.normalize('NFC', d) for d in todo)}")
    if skipped:
        print(f"  제외 {len(skipped)}개(지정 제외·완료·보호): {', '.join(skipped)}")
    if not todo:
        return
    if args.apply and not args.yes:
        print("  각 폴더마다 계획서를 만든 뒤 검토 없이 바로 삭제합니다(판정 기준은 cleanup 과 동일).")
        ans = input("계속하려면 '삭제' 입력 후 스페이스, 엔터: ").strip()
        if ans != "삭제":
            sys.exit("[취소] 아무것도 삭제하지 않았습니다.")

    summary_path = os.path.join("nas_reports", f"cleanup_all_요약_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as sf:
        sw = csv.writer(sf)
        sw.writerow(["폴더", "계획 건수", "계획 용량(GB)", "삭제 건수", "삭제 용량(GB)", "건너뜀", "실패", "계획서", "소요(분)"])
        tot_n = tot_b = 0
        for i, d in enumerate(todo, 1):
            dn = unicodedata.normalize("NFC", d)
            tag = re.sub(r"[^\w가-힣-]+", "_", dn)[:40]
            t0 = time.time()
            print(f"\n{'=' * 60}\n[{i}/{len(todo)}] {dn}\n{'=' * 60}")
            plan, n, b = cmd_cleanup(_ap.Namespace(root=os.path.join(root, d), fast=args.fast, tag=tag, batch=True), cfg)
            st = {}
            if args.apply and n:
                st = cmd_cleanup_apply(_ap.Namespace(plan=plan, apply=True, yes=True, verify=False, tag=tag), cfg)
            dc, db = st.get("삭제", [0, 0])
            tot_n += dc; tot_b += db
            sw.writerow([dn, n, round(b / 1024 ** 3, 2), dc, round(db / 1024 ** 3, 2),
                         st.get("건너뜀", [0])[0], st.get("실패", [0])[0], plan, round((time.time() - t0) / 60, 1)])
            sf.flush()
            if args.apply:
                with open(done_path, "a", encoding="utf-8") as f:
                    f.write(key(d) + "\n")
    print(f"\n{'=' * 60}")
    if args.apply:
        print(f"[전체 완료] 삭제 {tot_n:,}건 / {human(tot_b)}")
        print("※ 실제 여유공간은 ① QNAP 휴지통(@Recycle) 비우기 ② 이전 스냅샷 삭제 후에 늘어납니다.")
    else:
        print("[계획만 작성] 실제 삭제하려면 --apply 를 붙여 다시 실행")
    print(f"[요약] {summary_path}")


# =========================
# 8-3. compare : 폴더 A(예: 휴지통·옛 사본)가 폴더 B(실제 자료)에 이미 있는지 대조 — 파일 변경 없음
# =========================

def cmd_compare(args, cfg):
    src = os.path.abspath(args.src)
    dsts = [os.path.abspath(d) for d in args.dst]
    nfc = lambda p: unicodedata.normalize("NFC", p)
    now = time.time()
    index = {}
    if args.deep:
        k = 0
        for dst in dsts:
            print(f"[색인] 기준 폴더(파일명+크기) : {dst}")
            for _, fn, full, st in walk(dst, cfg, [], skip={nfc(src)} | {nfc(x) for x in cfg.get("compare_skip", [])}):
                if nfc(full).startswith(nfc(src) + os.sep):
                    continue
                index.setdefault((nfc(fn).lower(), st.st_size), full)
                k += 1
                if k % 50000 == 0:
                    print(f"  … {k:,}개")
        print(f"  색인 합계 {k:,}개")

    print(f"[대조] {src}  →  기준 {len(dsts)}곳: {', '.join(dsts)}")
    cats = ("동일(같은 경로)", "동일(다른 위치)", "크기 다름", "내용 다름", "유일본(기준 폴더에 없음)")
    tot = {c: [0, 0] for c in cats}
    by_top = defaultdict(lambda: {**{c: [0, 0] for c in cats}, "최근수정": 0.0})
    detail, errors = [], []
    n = 0
    for _, fn, full, st in walk(src, cfg, errors):
        n += 1
        if n % 20000 == 0:
            print(f"  … {n:,}개")
        rel = os.path.relpath(full, src)
        top = rel.split(os.sep)[0] if os.sep in rel else "(루트 파일)"
        if valid_mtime(st.st_mtime, now):
            by_top[top]["최근수정"] = max(by_top[top]["최근수정"], st.st_mtime)
        other = None
        for dst in dsts:
            other = _exists_any(os.path.join(dst, rel))
            if other and os.path.isfile(other):
                break
            other = None
        where = other or ""
        if other and os.path.isfile(other):
            if os.path.getsize(other) != st.st_size:
                cat = "크기 다름"
            elif args.hash and _hash(full) != _hash(other):
                cat = "내용 다름"
            else:
                cat = "동일(같은 경로)"
        else:
            hit = index.get((nfc(fn).lower(), st.st_size)) if args.deep else None
            if hit and (not args.hash or _hash(full) == _hash(hit)):
                cat, where = "동일(다른 위치)", hit
            else:
                cat, where = "유일본(기준 폴더에 없음)", ""
        tot[cat][0] += 1; tot[cat][1] += st.st_size
        by_top[top][cat][0] += 1; by_top[top][cat][1] += st.st_size
        if not cat.startswith("동일"):
            detail.append((cat, full, round(st.st_size / 1024 ** 2, 2), ts(st.st_mtime), where))

    gb = lambda b: round(b / 1024 ** 3, 2)
    all_b = sum(v[1] for v in tot.values()) or 1
    sum_rows = [(c, v[0], gb(v[1]), round(v[1] / all_b * 100, 1)) for c, v in tot.items()]
    top_rows = []
    for t, d in sorted(by_top.items(), key=lambda kv: -sum(v[1] for k, v in kv[1].items() if k != "최근수정")):
        same = d["동일(같은 경로)"][1] + d["동일(다른 위치)"][1]
        uniq = d["유일본(기준 폴더에 없음)"][1] + d["크기 다름"][1] + d["내용 다름"][1]
        top_rows.append((t, gb(same + uniq), gb(same), gb(uniq),
                         round(same / (same + uniq) * 100, 1) if same + uniq else 0,
                         ts(d["최근수정"]) if d["최근수정"] else ""))

    print(f"\n[결과] {src}\n  파일 {n:,}개 / 읽기실패 {len(errors):,}개")
    for c, cnt, g, pct in sum_rows:
        print(f"  {c:<18} {cnt:>9,}개 {g:>10,.2f} GB  {pct:>5}%")
    if not args.deep:
        print("  ※ 위치가 바뀐 파일은 '유일본'으로 잡힘 → 정확히 보려면 --deep (기준 폴더 전체 색인)")
    detail.sort(key=lambda r: (r[0], -r[2]))
    write_sheets("compare", {
        "요약": (["구분", "파일수", "용량(GB)", "비율(%)"], sum_rows),
        "1차폴더별": (["폴더", "전체(GB)", "기준에 있음(GB)", "없음·다름(GB)", "중복률(%)", "최근수정"], top_rows),
        "유일본·다름": (["구분", "경로", "크기(MB)", "수정일", "기준 폴더 쪽 경로"], detail[:1_000_000]),
        "읽기실패": (ERR_SHEET_HEADER, errors),
    })


# =========================
# 9. 진입점
# =========================

def main():
    ap = argparse.ArgumentParser(description="GroupK NAS(QNAP) 관리 자동화")
    ap.add_argument("--config", default="nas_config.json", help="설정 파일 (기본 nas_config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="표준 프로젝트 폴더 생성")
    p.add_argument("root", help="업무 공유폴더 경로 (예: Z:\\01_설계)")
    p.add_argument("--type", required=True, help="설계/CM/정비/안전진단/인테리어/연구용역/강의/소송")
    p.add_argument("--code", required=True, help="프로젝트 코드 (예: 2026-D012)")
    p.add_argument("--name", required=True, help="프로젝트명 (예: 애월읍_근린생활시설)")
    p.add_argument("--apply", action="store_true", help="실제 생성 (없으면 미리보기)")

    p = sub.add_parser("lint", help="파일명 규칙 점검")
    p.add_argument("root")
    p.add_argument("--fix-nfd", action="store_true", help="한글 자소분리(NFD) 이름을 NFC로 실제 변경")

    p = sub.add_parser("report", help="용량·대용량·임시파일 리포트")
    p.add_argument("root")

    p = sub.add_parser("dupes", help="중복 파일 탐지")
    p.add_argument("root")

    p = sub.add_parser("cleanup", help="정리 계획서 작성 (중복·원본동반 백업·풀린 압축) — 파일 변경 없음")
    p.add_argument("root")
    p.add_argument("--fast", action="store_true", help="압축 대조 시 CRC 생략(크기만 비교)")

    p = sub.add_parser("cleanup-apply", help="검토한 계획서대로 삭제 (기본 미리보기)")
    p.add_argument("plan", help="cleanup 이 만든 계획서(.xlsx/.csv)")
    p.add_argument("--apply", action="store_true", help="실제 삭제")
    p.add_argument("--yes", action="store_true", help="확인 질문 생략")
    p.add_argument("--verify", action="store_true", help="중복 삭제 직전 보존본과 전체 해시 재대조")

    p = sub.add_parser("compare", help="폴더 A의 파일이 폴더 B에 이미 있는지 대조 (파일 변경 없음)")
    p.add_argument("src", help="확인할 폴더 (예: 휴지통·옛 사본)")
    p.add_argument("dst", nargs="+", help="기준 폴더 (실제 자료). 여러 개 지정 가능 (예: 다른 NAS 공유폴더들)")
    p.add_argument("--deep", action="store_true", help="기준 폴더 전체를 파일명+크기로 색인해 위치가 바뀐 파일도 찾음")
    p.add_argument("--hash", action="store_true", help="동일 판정 시 전체 해시까지 비교 (느림, 정확)")

    p = sub.add_parser("cleanup-all", help="하위 폴더(연도 등)를 하나씩 계획→삭제. 중단 후 재실행 시 완료 폴더는 건너뜀")
    p.add_argument("root", help="공유폴더 경로 (그 바로 아래 폴더들이 처리 단위)")
    p.add_argument("--skip", nargs="*", help="제외할 폴더명 (예: --skip 2023 소송)")
    p.add_argument("--apply", action="store_true", help="실제 삭제 (없으면 전 폴더 계획서만 작성)")
    p.add_argument("--yes", action="store_true", help="확인 질문 생략")
    p.add_argument("--fast", action="store_true", help="압축 대조 시 CRC 생략")

    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.cmd == "cleanup-apply":
        if not os.path.isfile(args.plan):
            sys.exit(f"[오류] 계획서 없음: {args.plan}")
    elif args.cmd == "compare":
        for x in [args.src] + args.dst:
            if not os.path.isdir(x):
                sys.exit(f"[오류] 폴더가 없거나 접근 불가: {x}")
    elif not os.path.isdir(args.root):
        sys.exit(f"[오류] 폴더가 없거나 접근 불가: {args.root}")
    if not HAS_OPENPYXL:
        print("[안내] openpyxl 미설치 → CSV로 저장합니다 (pip install openpyxl)")
    t0 = time.time()
    try:
        _run(args, cfg)
    except KeyboardInterrupt:
        print("\n[중단] 사용자가 멈췄습니다(control+C). 처리된 부분까지는 기록 파일에 남아 있고, 같은 명령을 다시 실행하면 이어서 진행됩니다.")
        sys.exit(130)
    print(f"[소요] {time.time() - t0:,.1f}초")


def _run(args, cfg):
    {"init": cmd_init, "lint": cmd_lint, "report": cmd_report, "dupes": cmd_dupes,
     "cleanup": cmd_cleanup, "cleanup-apply": cmd_cleanup_apply,
     "cleanup-all": cmd_cleanup_all, "compare": cmd_compare}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
