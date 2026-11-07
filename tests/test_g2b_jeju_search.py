# -*- coding: utf-8 -*-
"""가짜 API 응답으로 g2b_jeju_search 동작 검증 (실제 API 호출 없음)

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import g2b_jeju_search as g  # noqa: E402

NOW = datetime(2026, 11, 7, 12, 0)


def item(no, ord_="000", name="제주시 OO청사 건축설계용역", demand="제주특별자치도 제주시",
         notice="조달청 제주지방조달청", close="2026-11-09 18:00:00", budget="150000000"):
    return {
        "bidNtceNo": no, "bidNtceOrd": ord_, "ntceKindNm": "일반", "bidNtceNm": name,
        "ntceInsttNm": notice, "dminsttNm": demand, "asignBdgtAmt": budget, "presmptPrce": "136363636",
        "bidNtceDt": "2026-11-03 10:00:00", "bidClseDt": close, "opengDt": "2026-11-20 11:00:00",
        "pubPrcrmntClsfcNm": "건축설계서비스", "bidNtceDtlUrl": f"https://www.g2b.go.kr/link?no={no}",
    }


def ok_payload(items, total):
    return {"response": {"header": {"resultCode": "00", "resultMsg": "정상"},
                         "body": {"items": items, "totalCount": total, "numOfRows": 100, "pageNo": 1}}}


class FakeResp:
    def __init__(self, payload=None, status=200, text=None):
        self.payload, self.status_code = payload, status
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self.payload is None:
            raise ValueError("not json")
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


FAST = dict(g.DEFAULT_CONFIG, request_delay_sec=0, num_of_rows=2)


class TestDates(unittest.TestCase):
    def test_arg_parsing(self):
        self.assertEqual(g.parse_arg_datetime("20261101"), datetime(2026, 11, 1, 0, 0))
        self.assertEqual(g.parse_arg_datetime("20261107", is_end=True), datetime(2026, 11, 7, 23, 59))
        self.assertEqual(g.parse_arg_datetime("202611010930"), datetime(2026, 11, 1, 9, 30))
        with self.assertRaises(Exception):
            g.parse_arg_datetime("2026-11")

    def test_split_period(self):
        chunks = g.split_period(datetime(2026, 10, 1), datetime(2026, 10, 20, 23, 59), 7)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0], (datetime(2026, 10, 1), datetime(2026, 10, 7, 23, 59)))
        self.assertEqual(chunks[-1][1], datetime(2026, 10, 20, 23, 59))
        for (_, e), (s, _) in zip(chunks, chunks[1:]):
            self.assertEqual((s - e).total_seconds(), 60)  # 빈틈·겹침 없음

    def test_default_period_is_last_7_days(self):
        args = g.build_arg_parser().parse_args([])
        start, end = g.resolve_period(args, NOW)
        self.assertEqual((end - start).days, 7)


class TestFetch(unittest.TestCase):
    def test_pagination(self):
        s = FakeSession([FakeResp(ok_payload([item("A1"), item("A2")], 3)),
                         FakeResp(ok_payload([item("A3")], 3))])
        out = g.fetch_period(s, "KEY", datetime(2026, 11, 1), datetime(2026, 11, 7, 23, 59), FAST)
        self.assertEqual([i["bidNtceNo"] for i in out], ["A1", "A2", "A3"])
        self.assertEqual([c["pageNo"] for c in s.calls], [1, 2])
        self.assertEqual(s.calls[0]["inqryBgnDt"], "202611010000")

    def test_single_item_dict_response(self):
        s = FakeSession([FakeResp(ok_payload({"item": item("B1")}, 1))])
        out = g.fetch_period(s, "KEY", datetime(2026, 11, 1), datetime(2026, 11, 2), FAST)
        self.assertEqual(len(out), 1)

    def test_retry_then_success(self):
        s = FakeSession([requests.ConnectionError("끊김"), FakeResp(status=503, text="busy"),
                         FakeResp(ok_payload([item("C1")], 1))])
        with mock.patch.object(g.time, "sleep"):
            out = g.fetch_period(s, "KEY", datetime(2026, 11, 1), datetime(2026, 11, 2), FAST)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(s.calls), 3)

    def test_retry_exhausted(self):
        s = FakeSession([requests.Timeout()] * 3)
        with mock.patch.object(g.time, "sleep"), self.assertRaises(g.ApiError):
            g.fetch_period(s, "KEY", datetime(2026, 11, 1), datetime(2026, 11, 2), FAST)

    def test_api_error_json(self):
        err = {"nkoneps.com.response.ResponseError": {"header": {"resultCode": "07", "resultMsg": "입력범위값 초과 에러"}}}
        s = FakeSession([FakeResp(err)])
        with self.assertRaises(g.ApiError) as cm:
            g.fetch_period(s, "KEY", datetime(2026, 11, 1), datetime(2026, 11, 2), FAST)
        self.assertIn("07", str(cm.exception))
        self.assertEqual(len(s.calls), 1)  # 재시도하지 않음

    def test_api_error_xml(self):
        xml = ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
               "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
               "<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
        s = FakeSession([FakeResp(None, status=200, text=xml)])
        with self.assertRaises(g.ApiError) as cm:
            g.fetch_period(s, "KEY", datetime(2026, 11, 1), datetime(2026, 11, 2), FAST)
        self.assertIn("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", str(cm.exception))


class TestFilter(unittest.TestCase):
    def test_match_rules(self):
        cfg = dict(g.DEFAULT_CONFIG)
        self.assertTrue(g.match_info(item("1"), cfg)[0])
        # 다른 지역 기관이지만 공고명에 '제주'
        self.assertTrue(g.match_info(item("2", name="제주 OO센터 정밀안전진단 용역",
                                          demand="한국OO공단", notice="한국OO공단"), cfg)[0])
        # 지역 불일치
        self.assertFalse(g.match_info(item("3", name="서울 OO청사 건축설계용역", demand="서울특별시", notice="조달청"), cfg)[0])
        # 업무 불일치
        self.assertFalse(g.match_info(item("4", name="제주시 청소용역"), cfg)[0])
        # 제외 키워드
        cfg["exclude_keywords"] = ["전기"]
        self.assertFalse(g.match_info(item("5", name="제주 OO 전기감리 용역"), cfg)[0])

    def test_dedupe_and_latest_order(self):
        items = [item("X1", "000"), item("X1", "000"), item("X1", "001", name="제주 건축설계용역(정정)"), item("X2")]
        raw = g.dedupe_exact(items)
        self.assertEqual(len(raw), 3)
        latest = g.keep_latest_order(raw)
        self.assertEqual([(i["bidNtceNo"], i["bidNtceOrd"]) for i in latest], [("X1", "001"), ("X2", "000")])

    def test_rows_sort_status_new(self):
        items = [item("L", close="2026-11-30 18:00:00"), item("U", close="2026-11-09 18:00:00"),
                 item("C", close="2026-11-01 18:00:00")]
        rows = g.build_rows(items, g.DEFAULT_CONFIG, {"L-000"}, NOW)
        self.assertEqual([r["공고번호"] for r in rows], ["U-000", "L-000", "C-000"])
        self.assertEqual([r["상태"] for r in rows], ["D-2", "D-23", "마감"])
        self.assertEqual([r["_urgent"] for r in rows], [True, False, False])
        self.assertEqual([r["신규"] for r in rows], ["신규", "", "신규"])
        self.assertEqual(rows[0]["배정예산액"], 150000000)


class TestMainEndToEnd(unittest.TestCase):
    def test_main_writes_outputs_and_tracks_new(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"G2B_SERVICE_KEY": "abc%2Bdef"}):
            def run(items):
                s = FakeSession([FakeResp(ok_payload(items, len(items)))])
                code = g.main(["--from", "20261101", "--to", "20261107", "--out-dir", d,
                               "--config", os.path.join(d, "none.json")], session=s, now=NOW)
                return code, s

            code, s = run([item("N1"), item("N2", demand="서울특별시", notice="조달청", name="서울 설계용역")])
            self.assertEqual(code, 0)
            self.assertEqual(s.calls[0]["serviceKey"], "abc+def")  # Encoding 키 자동 디코딩

            wb = load_workbook(os.path.join(d, "jeju_filtered.xlsx"))
            ws = wb.active
            headers = [c.value for c in ws[1]]
            self.assertEqual(ws.max_row, 2)
            row = {h: ws.cell(row=2, column=i + 1) for i, h in enumerate(headers)}
            self.assertEqual(row["신규"].value, "신규")
            self.assertEqual(row["배정예산액"].value, 150000000)
            self.assertEqual(row["배정예산액"].number_format, "#,##0")
            self.assertEqual(row["상세URL"].hyperlink.target, "https://www.g2b.go.kr/link?no=N1")

            with open(os.path.join(d, "all_raw.csv"), encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertIn("bidNtceDtlUrl", lines[0])  # 원본 필드 전체 저장

            # 두 번째 실행: N1 은 기존, N3 은 신규
            run([item("N1"), item("N3")])
            ws = load_workbook(os.path.join(d, "jeju_filtered.xlsx")).active
            got = {ws.cell(row=r, column=headers.index("공고번호") + 1).value:
                   ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)}
            self.assertEqual(got, {"N1-000": None, "N3-000": "신규"})

    def test_main_without_key(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(g, "SCRIPT_DIR", d), mock.patch.object(g.os, "getcwd", return_value=d):
            self.assertEqual(g.main(["--out-dir", d], session=FakeSession([]), now=NOW), 1)

    def test_version(self):
        self.assertEqual(g.__version__, "2.0.0")


if __name__ == "__main__":
    unittest.main()
