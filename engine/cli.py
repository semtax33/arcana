from __future__ import annotations

from data_download import (
    download_all_dividend,
    download_all_prices,
    download_all_report_metadata,
    download_all_shares,
    download_all_statement_comments,
    download_all_statements,
)
from statement_normalizer import main


__all__ = [
    "download_all_dividend",
    "download_all_prices",
    "download_all_report_metadata",
    "download_all_shares",
    "download_all_statement_comments",
    "download_all_statements",
    "main",
]


if __name__ == "__main__":
    main()
