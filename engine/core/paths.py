from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


DEFAULT_MARKET = "kr"


def _normalize_market(market: str = DEFAULT_MARKET) -> str:
    normalized = str(market or DEFAULT_MARKET).strip().lower()
    return normalized or DEFAULT_MARKET


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol).strip()


def _safe_filename_part(value: Any) -> str:
    text = str(value).strip()
    text = re.sub(r'[\\/*?:"<>|]+', "_", text)
    return text or "output"


@dataclass(frozen=True)
class DataLakePaths:
    root: Path

    @classmethod
    def from_project_root(cls, project_root: str | Path) -> "DataLakePaths":
        return cls(Path(project_root) / "data-lake")

    def bronze(self, provider: str, *parts: str) -> Path:
        return self.root.joinpath("bronze", provider, *parts)

    def silver(self, provider: str, *parts: str) -> Path:
        return self.root.joinpath("silver", provider, *parts)

    def meta(self, *parts: str) -> Path:
        return self.root.joinpath("meta", *parts)

    def rules(self, *parts: str) -> Path:
        return self.meta("rules", *parts)

    def canonical_accounts(self) -> Path:
        return self.meta("CanonicalAccount.csv")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_LAKE = DataLakePaths.from_project_root(PROJECT_ROOT)


def market_csv_name(dataset: str, market: str = DEFAULT_MARKET) -> str:
    return f"{_normalize_market(market)}_{_safe_filename_part(dataset)}.csv"


def market_symbol_csv_name(symbol: Any, market: str = DEFAULT_MARKET) -> str:
    symbol_text = _safe_filename_part(_normalize_symbol(symbol))
    return market_csv_name(symbol_text, market=market)


def statement_snapshot_name(
    stock_code: Any,
    year: int,
    month: int,
    market: str = DEFAULT_MARKET,
) -> str:
    stock_code_text = str(stock_code).strip().zfill(6)
    return (
        f"{_normalize_market(market)}_normalized_"
        f"{stock_code_text}_{int(year)}.{int(month):02d}.csv"
    )


_STATEMENT_SNAPSHOT_RE = re.compile(
    r"(?:(?P<market>[a-z][a-z0-9]*)_)?normalized_"
    r"(?P<stock_code>\d{6})_"
    r"(?P<year>\d{4})[._](?P<month>\d{2})\.csv$",
    re.IGNORECASE,
)


def parse_statement_snapshot_filename(path: str | Path) -> dict[str, int | str] | None:
    match = _STATEMENT_SNAPSHOT_RE.match(Path(path).name)
    if not match:
        return None
    return {
        "market": (match.group("market") or DEFAULT_MARKET).lower(),
        "stock_code": match.group("stock_code"),
        "year": int(match.group("year")),
        "month": int(match.group("month")),
    }


def first_existing_path(primary: str | Path, *legacy_paths: str | Path) -> Path:
    primary_path = Path(primary)
    for path in (primary_path, *(Path(path) for path in legacy_paths)):
        if path.exists():
            return path
    return primary_path
