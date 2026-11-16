# groupk01

GroupK(건축사사무소) 업무 자동화 스크립트 모음.

- `g2b_jeju_search.py`: 나라장터 제주 입찰공고 수집
- `nas_manager.py`: QNAP NAS 관리 (report, lint, dupes, cleanup, cleanup-apply, cleanup-all, compare, init)
- `run_phase1.sh`: NAS 재구성 0~1단계 실행 스크립트

## NAS 작업 시 규칙

- **작업을 시작하기 전에 `docs/NAS_작업현황.md`를 먼저 읽고, 끝나면 갱신한다.** 주의 사항(3장)에 적힌 보류 항목은 사용자 확인 없이 해제하지 않는다.
- 삭제는 반드시 계획서 → 미리보기 → 실행 순서. 휴지통·스냅샷 정리는 확인 기간 뒤에 사용자에게 안내만 한다.
- 맥에서 NAS 경로는 `/Volumes/<공유폴더>`. 한글 파일명은 NFC/NFD가 섞여 있으므로 경로 비교 시 정규화한다.
- 응답은 한국어, 실무자 대상. 결론 먼저.
