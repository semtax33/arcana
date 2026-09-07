from __future__ import annotations

from pathlib import Path

import pandas as pd


def test_parse_english_xbrl_search_page_keeps_pit_fields() -> None:
    from scripts.recover_kr_pvgo_english_xbrl import (
        parse_english_xbrl_search_page,
    )

    html = """
    <html><body><div>[1/2]</div><table><tr>
      <td><a href="/dsbc001/selectPopup.ax?selectKey=00126380">Samsung</a></td>
      <td>2012-05-15</td>
      <td><a href="/dsbh002/main.do?rcpNo=20120515001281">
        Quarterly Report (2012.03)
      </a></td>
      <td>XBRL</td>
    </tr></table></body></html>
    """

    frame, page_count = parse_english_xbrl_search_page(html, "005930")

    assert page_count == 2
    assert frame.loc[0, "security_id"] == "SEC_KR_005930"
    assert frame.loc[0, "fiscal_year"] == 2012
    assert frame.loc[0, "fiscal_month"] == 3
    assert frame.loc[0, "period_end_date"] == "2012-03-31"
    assert frame.loc[0, "report_date"] == "2012-05-15"
    assert frame.loc[0, "rcept_no"] == "20120515001281"


def test_parse_english_xbrl_statement_uses_cumulative_interim_amount() -> None:
    from scripts.recover_kr_pvgo_english_xbrl import (
        parse_english_xbrl_statement,
    )

    html = """
    <html><body>
      <div id="P_EDITOR_CONS_D310000">
        <p>Unit : million KRW</p>
        <table><tr><td>cover</td></tr></table>
        <table>
          <tr>
            <th rowspan="2">Account</th>
            <th colspan="2">Current period</th>
            <th colspan="2">Prior period</th>
          </tr>
          <tr><th>Quarter</th><th>Cumulative</th><th>Quarter</th><th>Cumulative</th></tr>
          <tr><td>Revenue</td><td>10</td><td>100</td><td>20</td><td>200</td></tr>
          <tr><td>Operating income</td><td>2</td><td>15</td><td>3</td><td>25</td></tr>
        </table>
      </div>
    </body></html>
    """

    frame = parse_english_xbrl_statement(
        html,
        symbol="005930",
        fiscal_year=2012,
        fiscal_month=6,
        rcept_no="20120814001232",
    ).set_index("canonical_account_id")

    assert frame.loc["REVENUE", "amount"] == "100000000"
    assert frame.loc["OPERATING_INCOME", "amount"] == "15000000"
    assert frame.loc["REVENUE", "fiscal_quarter"] == 2


def test_parse_english_xbrl_statement_prefers_consolidated_scope() -> None:
    from scripts.recover_kr_pvgo_english_xbrl import (
        parse_english_xbrl_statement,
    )

    html = """
    <html><body>
      <div id="P_EDITOR_CONS_D210000">
        <p>Unit : won</p>
        <table><tr><td>Total assets</td><td>123</td></tr></table>
      </div>
      <div id="P_EDITOR_SEPT_D210000">
        <p>Unit : won</p>
        <table><tr><td>Total assets</td><td>999</td></tr></table>
      </div>
    </body></html>
    """

    frame = parse_english_xbrl_statement(
        html,
        symbol="005930",
        fiscal_year=2012,
        fiscal_month=12,
        rcept_no="20130401003031",
    )

    assert frame.loc[0, "canonical_account_id"] == "TOTAL_ASSETS"
    assert frame.loc[0, "amount"] == "123"


def test_parse_english_xbrl_statement_handles_legacy_cis_numbering() -> None:
    from scripts.recover_kr_pvgo_english_xbrl import (
        parse_english_xbrl_statement,
    )

    html = """
    <html><body>
      <div id="P_EDITOR_SEPT_D431415">
        <p>Unit : million KRW</p>
        <table>
          <tr><td>Ⅰ.Revenue(Sales)</td><td>100</td></tr>
          <tr><td>Ⅳ.Operating Income(Loss)</td><td>12</td></tr>
          <tr><td>XI.Income tax expense</td><td>3</td></tr>
          <tr><td>XⅡ.Profit (loss)</td><td>9</td></tr>
        </table>
      </div>
    </body></html>
    """

    frame = parse_english_xbrl_statement(
        html,
        symbol="000020",
        fiscal_year=2013,
        fiscal_month=12,
        rcept_no="20140328001203",
    ).set_index("canonical_account_id")

    assert frame.loc["REVENUE", "amount"] == "100000000"
    assert frame.loc["OPERATING_INCOME", "amount"] == "12000000"
    assert frame.loc["TAX_EXPENSE", "amount"] == "3000000"
    assert frame.loc["NET_INCOME", "amount"] == "9000000"


def test_consolidate_metadata_parts_keeps_latest_filing_known_by_2016(
    tmp_path: Path,
) -> None:
    from scripts.recover_kr_pvgo_english_xbrl import consolidate_metadata_parts

    target_path = tmp_path / "targets.csv"
    pd.DataFrame(
        [{"security_id": "SEC_KR_005930", "symbol": "005930"}]
    ).to_csv(target_path, index=False)
    part_dir = tmp_path / "parts"
    part_dir.mkdir()
    pd.DataFrame(
        [
            {
                "stock_code": "005930",
                "fiscal_year": 2012,
                "fiscal_month": 3,
                "report_date": "2012-05-15",
                "rcept_no": "20120515000001",
            },
            {
                "stock_code": "005930",
                "fiscal_year": 2012,
                "fiscal_month": 3,
                "report_date": "2012-05-16",
                "rcept_no": "20120516000001",
            },
            {
                "stock_code": "005930",
                "fiscal_year": 2016,
                "fiscal_month": 12,
                "report_date": "2017-03-31",
                "rcept_no": "20170331000001",
            },
        ]
    ).to_csv(part_dir / "005930.csv", index=False)
    output_path = tmp_path / "selected.csv"

    frame = consolidate_metadata_parts(
        target_path=target_path,
        part_dir=part_dir,
        metadata_path=output_path,
        legacy_metadata_path=tmp_path / "missing-legacy.csv",
    )

    assert len(frame) == 1
    assert frame.loc[frame.index[0], "rcept_no"] == "20120516000001"
    assert output_path.exists()
