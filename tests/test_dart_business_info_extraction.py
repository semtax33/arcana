import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from engine.extractors._internal import dart_filings


def _node_script(text: str, *, name: str = "node1", ele_id: str = "9", toc_no: str = "99", length: str = "246377") -> str:
    return f"""
    function makeToc() {{
        var {name} = {{}};
        {name}['text'] = "{text}";
        {name}['id'] = "{toc_no}";
        {name}['rcpNo'] = "20260515001799";
        {name}['dcmNo'] = "11385144";
        {name}['eleId'] = "{ele_id}";
        {name}['offset'] = "10222";
        {name}['length'] = "{length}";
        {name}['dtd'] = "dart4.xsd";
        {name}['tocNo'] =  "{toc_no}";
        {name}['atocId'] =  "{toc_no}";
    }}
    """


class FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.apparent_encoding = "utf-8"
        self.encoding = "utf-8"


class DartBusinessInfoExtractionTest(unittest.TestCase):
    def test_dart_search_date_windows_split_long_ranges_by_decade(self):
        windows = dart_filings.iter_dart_search_date_windows("20000101", "20251231")

        self.assertEqual(
            windows,
            [
                ("20000101", "20091231"),
                ("20100101", "20191231"),
                ("20200101", "20251231"),
            ],
        )

    def test_dart_html_headers_use_randomized_pool_values(self):
        headers = dart_filings._dart_html_headers()

        self.assertIn(headers["User-Agent"], dart_filings.DART_USER_AGENTS)
        self.assertIn(headers["Accept-Language"], dart_filings.DART_ACCEPT_LANGUAGES)
        self.assertEqual(headers["Referer"], "https://dart.fss.or.kr/")

    def test_selects_node1_business_info_section(self):
        node = dart_filings.select_business_info_position(_node_script("II. 사업의 내용", name="node1"))

        self.assertIsNotNone(node)
        self.assertEqual(node.text, "II. 사업의 내용")
        self.assertEqual(node.eleId, "9")
        self.assertEqual(node.length, "246377")

    def test_parses_node_suffix_variants(self):
        script = "\n".join(
            [
                _node_script("I. 투자위험요소", name="node"),
                _node_script("II. 사업내용", name="node2", ele_id="12", length="3000"),
                _node_script("III. 재무제표", name="node3", ele_id="30", length="999999"),
            ]
        )

        matches = dart_filings.parse_node_business_info(script)

        self.assertEqual([match.eleId for match in matches], ["12"])

    def test_fallback_small_company_subsection_is_allowed(self):
        node = dart_filings.select_business_info_position(_node_script("1. 사업 개요", length="512"))

        self.assertIsNotNone(node)
        self.assertEqual(node.text, "1. 사업 개요")

    def test_excludes_non_business_sections(self):
        script = "\n".join(
            [
                _node_script("III. 재무제표", name="node1", length="999999"),
                _node_script("IV. 주석", name="node2", length="999999"),
                _node_script("V. 배당에 관한 사항", name="node3", length="999999"),
            ]
        )

        self.assertEqual(dart_filings.parse_node_business_info(script), [])
        self.assertIsNone(dart_filings.select_business_info_position(script))

    def test_fetch_uses_ele_id_not_toc_no_for_viewer_url(self):
        search_html = """
        <html><body>
          <a href="/dsaf001/main.do?rcpNo=20260515001799">삼성전자 (2026.03)</a>
        </body></html>
        """
        main_html = f"<html><script>{_node_script('II. 사업의 내용', ele_id='9', toc_no='99')}</script></html>"
        viewer_html = "<html><body>business info</body></html>"
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "POST":
                return FakeResponse(search_html)
            if "dsaf001/main.do" in url:
                return FakeResponse(main_html)
            if "report/viewer.do" in url:
                return FakeResponse(viewer_html)
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch.object(dart_filings, "request_with_retry", side_effect=fake_request),
                patch.object(dart_filings.time, "sleep"),
            ):
                dart_filings.fetch_dart_business_info_search(
                    "005930",
                    tmp_dir,
                    save_filename="business_info_test.html",
                    start_date="20260101",
                    end_date="20260331",
                )

            output_path = Path(tmp_dir) / "business_info_test.html"
            self.assertTrue(output_path.exists())

        viewer_url = [url for _, url, _ in calls if "report/viewer.do" in url][0]
        self.assertIn("eleId=9", viewer_url)
        self.assertIn("dcmNo=11385144", viewer_url)
        self.assertNotIn("eleId=99", viewer_url)

    def test_fetch_skips_existing_business_info_unless_force(self):
        search_html = """
        <html><body>
          <a href="/dsaf001/main.do?rcpNo=20260515001799">삼성전자 (2026.03)</a>
        </body></html>
        """
        main_html = f"<html><script>{_node_script('II. 사업의 내용')}</script></html>"
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "POST":
                return FakeResponse(search_html)
            if "dsaf001/main.do" in url:
                return FakeResponse(main_html)
            if "report/viewer.do" in url:
                raise AssertionError("viewer should not be requested for existing output")
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "business_info_test.html"
            output_path.write_text("already downloaded", encoding="utf-8")
            with patch.object(dart_filings, "request_with_retry", side_effect=fake_request):
                dart_filings.fetch_dart_business_info_search(
                    "005930",
                    tmp_dir,
                    save_filename="business_info_test.html",
                    sleep_seconds=0,
                )

        self.assertFalse(any("report/viewer.do" in url for _, url, _ in calls))

    def test_fetch_skips_existing_search_result_before_main_request(self):
        search_html = """
        <html><body>
          <a href="/dsaf001/main.do?rcpNo=20260515001799">삼성전자 (2026.03)</a>
        </body></html>
        """
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "POST":
                return FakeResponse(search_html)
            raise AssertionError(f"main/viewer should not be requested for existing output: {url}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "business_info_삼성전자 (2026.03).html"
            output_path.write_text("already downloaded", encoding="utf-8")
            with (
                patch.object(dart_filings, "request_with_retry", side_effect=fake_request),
                patch.object(dart_filings.time, "sleep") as sleep_mock,
            ):
                dart_filings.fetch_dart_business_info_search(
                    "005930",
                    tmp_dir,
                    sleep_seconds=0,
                )

        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()

    def test_fetch_statements_skips_existing_output_before_main_request(self):
        search_html = """
        <html><body>
          <a href="/dsaf001/main.do?rcpNo=20260515001799">Samsung Electronics (2026.03)</a>
        </body></html>
        """
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "POST":
                return FakeResponse(search_html)
            raise AssertionError(f"main/viewer should not be requested for existing output: {url}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "finance_statement_Samsung Electronics (2026.03).html"
            output_path.write_text("already downloaded", encoding="utf-8")
            with (
                patch.object(dart_filings, "request_with_retry", side_effect=fake_request),
                patch.object(dart_filings.time, "sleep") as sleep_mock,
            ):
                dart_filings.fetch_dart_search(
                    "005930",
                    tmp_dir,
                    start_date="20260101",
                    end_date="20260331",
                )

        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()
    def test_fetch_dividends_skips_existing_output_before_viewer_request(self):
        search_html = """
        <html><body>
          <a href="/dsaf001/main.do?rcpNo=20260624000001">현금ㆍ현물배당결정</a>
        </body></html>
        """
        main_html = """
        <html><script>viewDoc('20260624000001', '12345');</script></html>
        """
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "POST":
                return FakeResponse(search_html)
            if "dsaf001/main.do" in url:
                return FakeResponse(main_html)
            if "report/viewer.do" in url:
                raise AssertionError("viewer should not be requested for existing dividend output")
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = (
                Path(tmp_dir)
                / "finance_statement_dividend_2026-06-24_20260624000001.json"
            )
            output_path.write_text("{}", encoding="utf-8")
            with (
                patch.object(dart_filings, "request_with_retry", side_effect=fake_request),
                patch.object(dart_filings.time, "sleep") as sleep_mock,
            ):
                dart_filings.fetch_dart_dividend_search(
                    "005930",
                    tmp_dir,
                    start_date="20260624",
                    end_date="20260624",
                )

        self.assertFalse(any("report/viewer.do" in url for _, url, _ in calls))
        sleep_mock.assert_not_called()

    def test_fetch_dividends_excludes_subsidiary_reports_and_persists_identity(self):
        search_html = """
        <html><body>
          <a href="/dsaf001/main.do?rcpNo=20260624000001">현금ㆍ현물배당결정 (자회사의 주요경영사항)</a>
          <a href="/dsaf001/main.do?rcpNo=20260625000002">현금ㆍ현물배당결정</a>
        </body></html>
        """
        main_html = """
        <html><script>viewDoc('20260625000002', '12345');</script></html>
        """
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "POST":
                return FakeResponse(search_html)
            if "dsaf001/main.do" in url:
                self.assertIn("20260625000002", url)
                return FakeResponse(main_html)
            if "report/viewer.do" in url:
                return FakeResponse("<html>dividend</html>")
            raise AssertionError(f"unexpected URL: {url}")

        parsed = {
            "배당기준일": "2025-12-31",
            "1주당배당금": {"보통주식": "1000"},
            "배당금총액": "10000000",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch.object(dart_filings, "request_with_retry", side_effect=fake_request),
                patch.object(dart_filings, "parse_dividend_decision_html", return_value=parsed),
                patch.object(dart_filings.time, "sleep"),
            ):
                dart_filings.fetch_dart_dividend_search(
                    "5930",
                    tmp_dir,
                    corp_code="00126380",
                    corp_name="삼성전자",
                    start_date="20260624",
                    end_date="20260625",
                )

            output_path = (
                Path(tmp_dir)
                / "finance_statement_dividend_2026-06-25_20260625000002.json"
            )
            stored = json.loads(output_path.read_text(encoding="utf-8"))

        main_calls = [url for method, url, _ in calls if method == "GET" and "dsaf001" in url]
        self.assertEqual(len(main_calls), 1)
        self.assertEqual(stored["stock_code"], "005930")
        self.assertEqual(stored["corp_code"], "00126380")
        self.assertEqual(stored["corp_name"], "삼성전자")
        self.assertEqual(stored["rcept_no"], "20260625000002")
        post_data = next(kwargs["data"] for method, _, kwargs in calls if method == "POST")
        self.assertIn(("textCrpCik", "00126380"), post_data)
    def test_download_business_infos_uses_thread_pool_when_workers_requested(self):
        calls = []

        def fake_fetch(ticker, save_dir, **kwargs):
            calls.append((ticker, save_dir, kwargs))

        with patch.object(dart_filings, "fetch_dart_business_info_search", side_effect=fake_fetch):
            dart_filings.download_business_infos(
                ["000020", "005930"],
                0,
                start_date="20260101",
                end_date="20260331",
                max_workers=2,
                force=True,
            )

        self.assertEqual({call[0] for call in calls}, {"000020", "005930"})
        self.assertTrue(all(call[2]["force"] is True for call in calls))
        self.assertTrue(all(call[2]["start_date"] == "20260101" for call in calls))
        self.assertTrue(all(call[2]["sleep_seconds"] == 5.0 for call in calls))
        self.assertTrue(all(isinstance(call[2]["throttle"], dart_filings.DartRequestThrottle) for call in calls))

    def test_download_business_infos_retries_transient_disconnect(self):
        calls = []

        def fake_fetch(ticker, save_dir, **kwargs):
            calls.append((ticker, save_dir, kwargs))
            if len(calls) == 1:
                raise dart_filings.requests.ConnectionError("remote closed")

        with (
            patch.object(dart_filings, "fetch_dart_business_info_search", side_effect=fake_fetch),
            patch.object(dart_filings.time, "sleep"),
        ):
            dart_filings.download_business_infos(
                ["005930"],
                0,
                max_workers=1,
                stock_retries=1,
                stock_retry_backoff=0,
            )

        self.assertEqual(len(calls), 2)

    def test_dart_request_throttle_serializes_requests(self):
        throttle = dart_filings.DartRequestThrottle(10)

        with (
            patch.object(dart_filings.time, "monotonic", side_effect=[100.0, 100.0, 100.0, 110.0]),
            patch.object(dart_filings.time, "sleep") as sleep_mock,
            patch.object(dart_filings.random, "uniform", return_value=10.0),
        ):
            throttle.wait()
            throttle.wait()

        sleep_mock.assert_called_once_with(10.0)


if __name__ == "__main__":
    unittest.main()
