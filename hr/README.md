# GroupK 근태관리 앱 — 설치·운영 가이드

출퇴근 기록 · 연차 · 전자결재 · 주 52시간 관리 · 월간 엑셀 출력을 제공하는 사내용 설치형 웹앱(PWA)입니다.
기능 분석·설계 근거는 [`docs/근태앱_분석_및_설계.md`](../docs/근태앱_분석_및_설계.md) 참고.

## 1. 로컬에서 실행해 보기

```bash
pip install -r requirements.txt
```

```bash
flask --app hr.app create-admin
```

```bash
flask --app hr.app run --host 0.0.0.0 --port 5000
```

브라우저에서 `http://localhost:5000` 접속 → 관리자 로그인. 
(로컬 HTTP에서는 휴대폰 GPS 기능이 동작하지 않습니다. 실제 운영은 아래 HTTPS 구성으로.)

## 2. 운영 서버 설치

클라우드 서버 개설부터 도메인 연결까지 단계별 안내: [`docs/클라우드_서버_개설_가이드.md`](../docs/클라우드_서버_개설_가이드.md)

Ubuntu 서버에서 저장소를 `/opt/groupk`에 받은 뒤 설치 스크립트를 실행합니다. 설치되는 것은 KST 시간대, gunicorn 서비스, Caddy HTTPS, 방화벽, 일일 백업입니다.

```bash
sudo bash /opt/groupk/deploy/setup_server.sh hr.example.co.kr
```

```bash
sudo -u groupk HR_DATABASE=/opt/groupk/instance/hr.sqlite3 /opt/groupk/.venv/bin/flask --app hr.app create-admin
```

## 3. 초기 설정 순서 (관리자)

1. **설정** → 회사명, 기본 출퇴근 시각, 휴게시간, 지각 유예
2. **설정 → 근무지** → 본사(현재위치 버튼으로 좌표 입력), 주요 현장 추가 / 반경 200m 권장
3. **설정 → 휴일** → 올해 공휴일·대체공휴일·회사 휴무일 등록
4. **직원** → 부서 추가 → 직원 등록(아이디·입사일) → 임시 비밀번호 전달
5. **연차** → 시프티 기준 잔여와 차이 나는 직원은 '연차 조정'으로 맞춤
6. 위치 확인을 강제하려면 설정에서 "'사무실' 출근은 근무지 반경 내에서만 허용" 체크

## 4. 직원 휴대폰에 앱 설치

| 기기 | 방법 |
|---|---|
| 아이폰 | Safari로 접속 → 공유 버튼 → **홈 화면에 추가** |
| 안드로이드 | Chrome으로 접속 → 메뉴(⋮) → **앱 설치** 또는 **홈 화면에 추가** |

첫 로그인 시 비밀번호 변경 → 출퇴근 화면에서 위치수집 동의(선택) → 출근하기.

## 5. 환경변수

| 변수 | 설명 | 기본값 |
|---|---|---|
| `HR_DATABASE` | SQLite 파일 경로 | `instance/hr.sqlite3` (운영 서버: `/opt/groupk/instance/hr.sqlite3`) |
| `HR_SECRET_KEY` | 세션 암호화 키 | 최초 실행 시 `instance/secret_key` 자동 생성 |
| `HR_SECURE_COOKIE` | HTTPS 전용 쿠키(운영 시 `1`) | `0` |

## 6. 테스트

```bash
python -m pytest -q
```

## 7. 업데이트

```bash
cd /opt/groupk && sudo git pull && sudo bash deploy/setup_server.sh hr.example.co.kr
```

DB 테이블은 시작 시 자동 생성(기존 데이터 유지)됩니다.
