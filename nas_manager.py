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

공통
  - QNAP 시스템 폴더(@Recycle, .@__thumb, @Recently-Snapshot, .streams 등)는 자동 제외
  - 결과는 ./nas_reports/ 에 xlsx(openpyxl 설치 시) 또는 CSV(utf-8-sig, 엑셀 호환)로 저장
  - 파일을 삭제·이동하는 기능은 없음. 이름 변경은 --fix-nfd 를 명시할 때만 수행
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

    # report 옵션
    "large_file_mb": 500,
    "stale_days": 730,            # 2년 이상 미수정 → 아카이브 후보
    "top_n": 200,
    # CAD/오피스 잠금·임시·백업 파일 (정리 후보)
    "temp_patterns": [
        "*.bak", "*.sv$", "*.dwl", "*.dwl2", "*.ac$", "*.tmp", "~$*", "*.~*",
        "*.rvt.????.rvt", "*_backup*.rvt", "*.3dmbak", "*.skb", "*.swp",
    ],

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


def walk(root, cfg):
    """시스템 폴더를 제외하고 (dirpath, filename, full_path, stat) 생성"""
    ex_dirs, ex_files = cfg["exclude_dirs"], cfg["exclude_files"]
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: print(f"  [접근불가] {e}", file=sys.stderr)):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith((".@", "@")) and not _match_any(d, ex_dirs)]
        for fn in filenames:
            if _match_any(fn, ex_files):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError as e:
                print(f"  [stat 실패] {full}: {e}", file=sys.stderr)
                continue
            yield dirpath, fn, full, st


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def ts(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def write_sheets(basename, sheets):
    """sheets: {시트명: (헤더 리스트, 행 리스트)} → xlsx 1개 또는 CSV 여러 개"""
    os.makedirs("nas_reports", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
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
    else:
        for name, (header, rows) in sheets.items():
            path = os.path.join("nas_reports", f"{basename}_{name}_{stamp}.csv")
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
            print(f"[저장] {path}")


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
    for dirpath, fn, full, st in walk(args.root, cfg):
        n += 1
        iss = check("파일", dirpath, fn, full)
        if iss:
            rows.append(("파일", full, fn, " / ".join(iss)))

    print(f"[점검] 파일 {n:,}개 중 지적 {len(rows):,}건 (NFD {len(nfd_targets):,}건)")
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

    for dirpath, fn, full, st in walk(root, cfg):
        rel = os.path.relpath(full, root)
        top = rel.split(os.sep)[0] if os.sep in rel else "(루트 파일)"
        t = by_top[top]
        t[0] += st.st_size; t[1] += 1; t[2] = max(t[2], st.st_mtime)
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

    write_sheets("report", {
        "1차폴더별": (["폴더", "용량(GB)", "파일수", "비율(%)", "최근수정", f"비고(>{cfg['stale_days']}일)"], top_rows),
        "확장자별": (["확장자", "용량(GB)", "파일수"], ext_rows),
        "대용량파일": (["경로", "크기(MB)", "수정일"], large_rows),
        "임시·백업파일": (["경로", "크기(MB)", "수정일"], temp_rows),
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
    for _, fn, full, st in walk(args.root, cfg):
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
    write_sheets("dupes", {"중복파일": (["그룹", "구분", "경로", "크기(MB)", "수정일", "SHA256(앞16)"], rows)})


# =========================
# 7. 진입점
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

    args = ap.parse_args()
    cfg = load_config(args.config)
    if not os.path.isdir(args.root):
        sys.exit(f"[오류] 폴더가 없거나 접근 불가: {args.root}")
    if not HAS_OPENPYXL:
        print("[안내] openpyxl 미설치 → CSV로 저장합니다 (pip install openpyxl)")
    t0 = time.time()
    {"init": cmd_init, "lint": cmd_lint, "report": cmd_report, "dupes": cmd_dupes}[args.cmd](args, cfg)
    print(f"[소요] {time.time() - t0:,.1f}초")


if __name__ == "__main__":
    main()
