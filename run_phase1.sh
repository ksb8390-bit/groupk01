#!/bin/bash
# GroupK NAS 재구성 0~1단계 자동 실행 (맥에서 실행)
#   0단계: GroupK 중요 공유폴더(계약서·등록증·자격증·회의록) → groupk3 '07_경영지원' 으로 복사 + 검증
#   1단계: 같은 업무 공유폴더 간 중복 대조 (파일 변경 없음)
# 실행:  caffeinate -i bash run_phase1.sh
# 결과:  nas_reports/phase1_요약_날짜.txt  (이 파일 내용을 보내주면 다음 단계 진행)

set -u
cd "$(dirname "$0")"
mkdir -p nas_reports
STAMP=$(date +%Y%m%d_%H%M%S)
LOG="nas_reports/phase1_로그_$STAMP.txt"
SUM="nas_reports/phase1_요약_$STAMP.txt"
V="${NAS_VOLUMES:-/Volumes}"
PY=python3
exec > >(tee -a "$LOG") 2>&1

say() { echo; echo "================================================================"; echo "$@"; echo "================================================================"; }
have() { [ -d "$V/$1" ]; }
summ() { echo "$@" >> "$SUM"; }

summ "GroupK NAS 0~1단계 결과 요약 ($STAMP)"
summ ""

# ---------- 연결 상태 ----------
say "[점검] 공유폴더 연결 상태"
ALL="계약서 등록증 자격증 회의록 CM사업부 CM사업부-1 건설사업관리본부-2 설계팀-데이터 건설사업관리본부 설계본부-데이터 건설사업관리부 설계사업부-프로젝트 07_경영지원"
summ "[연결 상태]"
for s in $ALL; do
  if have "$s"; then echo "  연결됨   $s"; summ "  연결됨   $s"; else echo "  미연결   $s"; summ "  미연결   $s"; fi
done
summ ""

# ---------- 0단계 ----------
say "[0단계] GroupK 중요 자료 → 07_경영지원 복사"
summ "[0단계] 중요 자료 대피"
if have "07_경영지원"; then
  for s in 계약서 등록증 자격증 회의록; do
    if ! have "$s"; then echo "  [건너뜀] $s 미연결"; summ "  $s : 미연결로 건너뜀"; continue; fi
    echo "  복사 중: $s"
    if ditto "$V/$s" "$V/07_경영지원/$s"; then
      R=$($PY nas_manager.py compare "$V/$s" "$V/07_경영지원/$s" 2>&1 | grep -E "^[[:space:]]+(유일본|크기 다름)")
      echo "$R"
      summ "  $s :$(echo "$R" | tr -s ' ' | tr '\n' ' ')"
    else
      echo "  [실패] $s 복사 오류"; summ "  $s : 복사 실패 (로그 확인)"
    fi
  done
else
  echo "  07_경영지원 미연결 → 0단계 건너뜀 (groupk3 QTS에서 공유폴더 생성 후 Finder로 연결)"
  summ "  07_경영지원 미연결로 건너뜀"
fi
summ ""

# ---------- 1단계 ----------
run_cmp() {   # 제목, 대상, 기준...
  local title="$1"; shift
  local src="$1"; shift
  local miss=""
  for x in "$src" "$@"; do [ -d "$V/$x" ] || miss="$miss $x"; done
  say "[1단계] $title"
  summ "[$title]"
  if [ -n "$miss" ]; then echo "  [건너뜀] 미연결:$miss"; summ "  건너뜀 (미연결:$miss)"; summ ""; return; fi
  local args=()
  for x in "$@"; do args+=("$V/$x"); done
  $PY nas_manager.py compare "$V/$src" "${args[@]}" --deep | tee /tmp/_cmp_$$.txt
  awk '/^\[결과\]/{f=1} f' /tmp/_cmp_$$.txt | grep -vE "^\[소요\]" >> "$SUM"
  rm -f /tmp/_cmp_$$.txt
  summ ""
}

run_cmp "1-1 CM사업부-1 ↔ CM사업부"            "CM사업부-1"        "CM사업부"
run_cmp "1-2 CM사업부 ↔ CM 계열 3곳"           "CM사업부"          "건설사업관리부" "건설사업관리본부" "건설사업관리본부-2"
run_cmp "1-3 건설사업관리본부-2 ↔ CM 2곳"      "건설사업관리본부-2" "건설사업관리부" "건설사업관리본부"
run_cmp "1-4 설계팀-데이터 ↔ 설계 2곳"         "설계팀-데이터"     "설계사업부-프로젝트" "설계본부-데이터"
run_cmp "1-5 건설사업관리부 휴지통 ↔ 실제 6곳" "건설사업관리부/Network Trashes Folder" \
        "건설사업관리부" "건설사업관리본부" "건설사업관리본부-2" "CM사업부" "설계본부-데이터" "설계사업부-프로젝트"
if ! have "설계사업부-프로젝트"; then
  summ "[1-6 설계사업부-프로젝트 휴지통] 건너뜀 (미연결: 설계사업부-프로젝트)"
elif [ -d "$V/설계사업부-프로젝트/Network Trashes Folder" ]; then
  run_cmp "1-6 설계사업부-프로젝트 휴지통 ↔ 실제 6곳" "설계사업부-프로젝트/Network Trashes Folder" \
          "설계사업부-프로젝트" "설계본부-데이터" "설계팀-데이터" "건설사업관리부" "건설사업관리본부" "CM사업부"
else
  summ "[1-6 설계사업부-프로젝트 휴지통] 폴더 없음 (이미 삭제된 것으로 보임)"
fi

say "[완료] 요약 파일: $SUM"
echo "아래 명령으로 요약을 열어 내용을 보내주세요:"
echo "  open \"$SUM\""
