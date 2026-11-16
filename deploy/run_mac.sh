#!/usr/bin/env bash
# 사무실 iMac에서 근태앱 임시 운영 (사내망 전용)
#   사용:  bash ~/groupk-test/deploy/run_mac.sh
# 하는 일: 예전에 켜 둔 앱 종료 → 가상환경·패키지 확인 → 운영용 서버(gunicorn)로 실행 (실행 중 잠자기 방지)
set -u
cd "$(dirname "$0")/.." || exit 1
PORT="${PORT:-5050}"

echo "== 1/4 예전에 켜 둔 앱 종료 (포트 $PORT)"
PIDS="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  kill $PIDS 2>/dev/null; sleep 2
  PIDS="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
  [ -n "$PIDS" ] && kill -9 $PIDS 2>/dev/null
  echo "   종료함"
else
  echo "   실행 중인 앱 없음"
fi

echo "== 2/4 가상환경"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || { echo "가상환경을 만들지 못했습니다."; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "== 3/4 필요한 패키지 확인"
if ! python -c "import flask, openpyxl, gunicorn, reportlab.pdfgen" 2>/dev/null; then
  pip install -q -r requirements.txt || { echo "패키지 설치 실패 — 인터넷 연결을 확인하세요."; exit 1; }
fi

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
echo "== 4/4 실행"
echo "   이 컴퓨터:     http://localhost:$PORT"
echo "   직원(사내망):  http://${IP:-이_컴퓨터_IP}:$PORT"
echo "   종료: Ctrl + C   (이 창은 닫지 마세요)"
echo

# macOS에서 gunicorn 작업 프로세스가 비정상 종료되는 것을 막는 설정
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
# --preload: DB 준비(테이블·컬럼 추가)를 워커들이 뜨기 전에 한 번만 실행
CMD=(gunicorn -w 3 --threads 8 --preload -b "0.0.0.0:$PORT" --timeout 60 --access-logfile - "hr.app:create_app()")
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i "${CMD[@]}"
else
  exec "${CMD[@]}"
fi
