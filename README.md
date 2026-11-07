# 나라장터 제주 용역공고 수집기 (g2b_jeju_search) — v2.0.0

나라장터(조달청) 입찰공고정보서비스 Open API에서 **제주 지역 + 건축설계 / CM / 감리 / 안전진단** 용역 공고를 수집해 엑셀로 정리합니다.

## 1. 설치

```bash
pip install -r requirements.txt
```

## 2. 인증키 설정 (코드에 직접 쓰지 않음)

data.go.kr 마이페이지 > 개발계정 상세보기 > **일반 인증키(Decoding)** 를 아래 중 한 곳에 넣습니다.

- `.env` 파일: `.env.example`을 `.env`로 복사한 뒤 `G2B_SERVICE_KEY=...` 입력 (`.env`는 git에 올라가지 않음)
- 또는 환경변수: `export G2B_SERVICE_KEY="키"`

Encoding 키를 넣어도 자동으로 디코딩합니다.

## 3. 실행

| 목적 | 명령 |
|---|---|
| 최근 7일 (기본) | `python g2b_jeju_search.py` |
| 최근 N일 | `python g2b_jeju_search.py --days 14` |
| 기간 지정 | `python g2b_jeju_search.py --from 20261101 --to 20261107` |
| 시각까지 지정 | `python g2b_jeju_search.py --from 202611010900 --to 202611071800` |
| 결과 폴더 지정 | `python g2b_jeju_search.py --out-dir 결과` |
| 버전 확인 | `python g2b_jeju_search.py --version` |

조회 기간이 길면 `chunk_days`(기본 7일) 단위로 나눠 호출합니다. 기준은 **공고게시일시**(`inqryDiv=1`)입니다.

## 4. 결과물

| 파일 | 내용 |
|---|---|
| `jeju_filtered.xlsx` | 대상 공고. 마감 빠른 순 정렬, **신규**(노란색)·**마감 D-3 이내**(주황색) 표시, 금액 숫자서식, "공고 보기" 링크, 필터·틀고정 |
| `all_raw.csv` | 조회 기간 전체 공고 원본 (API 응답 필드 전체, 중복 제거) |
| `seen_notices.json` | 이전 실행에서 본 공고 목록 — "신규" 판정용. 삭제하면 다음 실행 때 전부 신규로 표시 |

- 같은 공고번호의 정정·재공고는 **최신 차수만** 엑셀에 남깁니다 (원본 CSV에는 차수별로 모두 남음).
- 첫 실행에서는 모든 공고가 "신규"로 표시됩니다.

## 5. 설정 (`config.json`)

| 항목 | 설명 | 기본값 |
|---|---|---|
| `subject_keywords` | 공고명에 하나라도 포함되면 대상 | 건축설계, 설계용역, 기본설계, 실시설계, 리모델링, 건설사업관리, CM용역, 감리, 안전진단, 정밀안전진단, 정밀점검, 정밀안전점검, 정기안전점검, 안전점검 |
| `exclude_keywords` | 공고명에 포함되면 제외 (예: `"전기"`, `"통신"`, `"소방"`) | 없음 |
| `region_keywords` | 지역 키워드 | 제주 |
| `region_fields` | 지역 판정 항목: `demand_inst`(수요기관), `notice_inst`(공고기관), `subject`(공고명) | 셋 다 |
| `chunk_days` | 분할 조회 단위(일) | 7 |
| `max_retries` | 네트워크 오류·5xx·429 재시도 횟수 | 3 |
| `deadline_warn_days` | 마감 임박 강조 기준(일) | 3 |

"감리", "실시설계"는 범위가 넓어 전기·통신·토목 공고도 걸릴 수 있습니다. 필요하면 `exclude_keywords`로 걸러내세요.

## 6. 오류 메시지

| 메시지 | 원인 |
|---|---|
| `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` | 인증키 오류 또는 활성화 전 (발급 직후에는 시간이 걸릴 수 있음) |
| `resultCode=07` 등 | API가 돌려준 오류 코드 — 조회 기간·파라미터를 확인 |
| `N회 시도 후 실패` | 네트워크 또는 서버 장애 지속 |

## 7. 테스트 (실제 API 호출 없음)

```bash
python -m unittest discover -s tests -v
```

## 변경 이력

### v2.0.0 (2026-11-07)
- 조회 기간 자동 설정(기본 최근 7일), `--from/--to/--days` 옵션, 기간 분할 조회
- 인증키를 환경변수 / `.env`로 분리 (코드에서 제거)
- 재시도(지수 백오프), API 오류코드·XML 오류 응답 표시
- 공고번호-차수 중복 제거, 정정·재공고는 최신 차수만 표시
- 키워드·지역 설정을 `config.json`으로 분리, 키워드 보강, 제외 키워드, 공고명의 지역명도 판정
- 엑셀 개선: 숫자 서식, 링크, 마감순 정렬, D-n 상태, 마감임박 강조, 매칭키워드 열
- 신규 공고 표시 (`seen_notices.json`)
- `all_raw.csv`에 원본 필드 전체 저장

### v1.0 (초기본)
- 고정 기간 조회, 공고명·기관명 키워드 필터, 엑셀/CSV 저장
