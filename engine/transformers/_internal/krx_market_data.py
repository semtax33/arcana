from glob import glob
import hashlib
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from engine.core.paths import DATA_LAKE, PROJECT_ROOT, market_csv_name

ENGINE_DIR = Path(__file__).resolve().parent

DATE_COLUMN = "\ub0a0\uc9dc"
OPEN_COLUMN = "\uc2dc\uac00"
HIGH_COLUMN = "\uace0\uac00"
LOW_COLUMN = "\uc800\uac00"
CLOSE_COLUMN = "\uc885\uac00"
VOLUME_COLUMN = "\uac70\ub798\ub7c9"
CHANGE_RATE_COLUMN = "\ub4f1\ub77d\ub960"
MARKET_CAP_COLUMN = "\uc2dc\uac00\ucd1d\uc561"
TRADING_VALUE_COLUMN = "\uac70\ub798\ub300\uae08"
LISTED_SHARES_COLUMN = "\uc0c1\uc7a5\uc8fc\uc2dd\uc218"


def _glob_files(path: str) -> list[str]:
    files = glob(path)
    if files:
        return files

    path_obj = Path(path)
    if path_obj.is_absolute():
        return files

    for base_dir in (PROJECT_ROOT, ENGINE_DIR):
        files = glob(str(base_dir / path_obj))
        if files:
            return files

    return files


def _write_csv(df: pd.DataFrame, output_path: Path, *, before_replace=None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        df.to_csv(temporary, index=False, encoding="utf-8-sig")
        if before_replace is not None:
            before_replace(temporary)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)


def _stock_code_from_path(path: str | Path) -> str:
    stem = Path(path).stem
    if stem.lower().startswith("kr_"):
        stem = stem[3:]
    return stem


def _dedupe_market_symbol_files(files: list[str]) -> list[str]:
    by_stock_code: dict[str, str] = {}
    for file in files:
        stock_code = _stock_code_from_path(file)
        if stock_code not in by_stock_code or Path(file).name.lower().startswith("kr_"):
            by_stock_code[stock_code] = file
    return list(by_stock_code.values())


def _read_market_symbol_files(path: str) -> pd.DataFrame:
    files = _dedupe_market_symbol_files(_glob_files(path))
    if not files:
        raise FileNotFoundError("CSV file not found")
    return pd.concat(
        [pd.read_csv(file).assign(stock_code=_stock_code_from_path(file)) for file in files],
        ignore_index=True,
    )


def _parse_trade_dates(values):
    dates = pd.to_datetime(values, errors="coerce", format="mixed")
    invalid = dates.isna()
    if invalid.any():
        sample = values.loc[invalid].head(3).astype(str).tolist() if hasattr(values, "loc") else []
        raise ValueError(f"invalid KRX trade_date values: {sample}")
    return dates.dt.normalize()


def _require_columns(df: pd.DataFrame, columns: list[str]) -> None:
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise KeyError(f"missing KRX columns: {missing}")


def _security_id(stock_code) -> str:
    return f"SEC_KR_{str(stock_code).strip().zfill(6)}"


def normalize_price(path: str):
    df = _read_market_symbol_files(path)
    _require_columns(
        df,
        [DATE_COLUMN, OPEN_COLUMN, HIGH_COLUMN, LOW_COLUMN, CLOSE_COLUMN, VOLUME_COLUMN],
    )

    result = pd.DataFrame()
    result["security_id"] = df["stock_code"].apply(_security_id)
    result["trade_date"] = _parse_trade_dates(df[DATE_COLUMN])
    result["open"] = df[OPEN_COLUMN]
    result["high"] = df[HIGH_COLUMN]
    result["low"] = df[LOW_COLUMN]
    result["close"] = df[CLOSE_COLUMN]
    result["volume"] = df[VOLUME_COLUMN]
    result["adj_close"] = df[CLOSE_COLUMN]
    result["currency"] = "KRW"

    from engine.transformers.stock_splits import adjust_prices, load_events
    events = load_events("kr", DATA_LAKE.silver("corporate_actions", "kr_stock_splits.json"))
    if events:
        result["adj_close"] = adjust_prices(result, events, price_basis="raw")["split_adj_close"]

    _write_csv(result, DATA_LAKE.silver("krx", "price", market_csv_name("normalized_price")))
    return result


def normalize_shares(path: str, *, output_path: str | Path | None = None):
    """Normalize KRX and registered historical observations to a silver input."""
    output = Path(output_path) if output_path is not None else DATA_LAKE.silver(
        "krx", "shares", market_csv_name("normalized_shares"),
    )
    if not output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Normalized share data must be inside data-lake/silver")
    default = DATA_LAKE.silver("krx", "shares", market_csv_name("normalized_shares"))
    # The refresh command owns the market lock across collection and publication.
    return _normalize_shares(path, output, track_changes=output.resolve() == default.resolve())


def _normalize_shares(path, output, *, track_changes):
    df = _read_market_symbol_files(path)
    _require_columns(df, [DATE_COLUMN, LISTED_SHARES_COLUMN, MARKET_CAP_COLUMN])

    result = pd.DataFrame()
    result["security_id"] = df["stock_code"].apply(_security_id)
    result["trade_date"] = _parse_trade_dates(df[DATE_COLUMN])
    result["shares"] = df[LISTED_SHARES_COLUMN]
    result["market_cap"] = df[MARKET_CAP_COLUMN]

    historical = _historical_shares()
    quarantined_rows = historical.attrs.get("quarantined_share_source_rows", 0)
    if not historical.empty:
        result = pd.concat([result, historical], ignore_index=True)
        keys = ["security_id", "trade_date"]
        duplicates = result.loc[result.duplicated(keys, keep=False)]
        reference = duplicates.drop_duplicates(keys).set_index(keys)
        comparison = duplicates.join(reference, on=keys, rsuffix="_first")
        consistent = np.isclose(comparison.shares, comparison.shares_first, rtol=0, atol=0) & np.isclose(
            comparison.market_cap, comparison.market_cap_first, rtol=1e-8, atol=1,
        )
        if not consistent.all():
            sample = comparison.loc[~consistent, keys].head(3).to_dict("records")
            raise ValueError(f"Historical share observations conflict: {sample}")
        result = result.drop_duplicates(keys, keep="first")
        result = result.sort_values(keys).reset_index(drop=True)
    if track_changes:
        from engine.transformers.share_input_history import prepare_share_input_change, finish_share_input_change
        publication = {}
        def prepare(candidate):
            publication["path"] = prepare_share_input_change(output, candidate, data_lake=DATA_LAKE)
        _write_csv(result, output, before_replace=prepare)
        finish_share_input_change(publication["path"])
        result.attrs["share_input_change_report"] = str(publication["path"].resolve())
    else:
        _write_csv(result, output)
    result.attrs["quarantined_share_source_rows"] = quarantined_rows
    return result


def _historical_shares() -> pd.DataFrame:
    """Read explicitly registered bronze observations, without listing inference."""
    manifest_path = DATA_LAKE.silver("krx", "shares", "historical_sources.json")
    if not manifest_path.exists():
        return pd.DataFrame()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["schema_version"] != 1:
        raise ValueError("Unsupported historical share source manifest")
    sources = manifest["sources"]
    identities = [(source["path"], source.get("start_date"), source.get("end_date"), source.get("observation_date")) for source in sources]
    if not sources or len(set(identities)) != len(sources):
        raise ValueError("Historical share registration has empty or duplicate sources")
    frames = []
    quarantined_rows = 0
    for source in sources:
        source_path = (DATA_LAKE.root / source["path"]).resolve()
        if not source_path.is_relative_to((DATA_LAKE.root / "bronze").resolve()):
            raise ValueError("Historical share originals must be inside data-lake/bronze")
        source_bytes = source_path.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != source["sha256"]:
            raise ValueError(f"Historical share source hash mismatch: {source_path.name}")
        source_format = source.get("format", "marcap_parquet")
        if source_format == "marcap_parquet":
            raw = pd.read_parquet(BytesIO(source_bytes), columns=["Code", "Date", "Close", "Stocks", "Marcap"])
        elif source_format == "fdr_listing_csv":
            # A pinned, independently dated listing snapshot, never today's listing.
            raw = pd.read_csv(BytesIO(source_bytes), usecols=["Code", "Close", "Stocks", "Marcap"], dtype={"Code": str})
            raw["Date"] = source["observation_date"]
        else:
            raise ValueError(f"Unsupported historical share source format: {source_format}")
        raw["Date"] = _parse_trade_dates(raw["Date"])
        start = pd.Timestamp(source.get("start_date", f"{source['year']}-01-01"))
        end = pd.Timestamp(source.get("end_date", f"{source['year']}-12-31"))
        if start > end or start.year != source["year"] or end.year != source["year"]:
            raise ValueError("Invalid registered capitalization date interval")
        if not raw["Date"].dt.year.eq(source["year"]).all():
            raise ValueError("Historical capitalization source contains the wrong year")
        raw = raw.loc[raw["Date"].between(start, end)].copy()
        codes = raw["Code"].astype(str).str.strip().str.zfill(6)
        dates = _parse_trade_dates(raw["Date"])
        values = raw[["Close", "Stocks", "Marcap"]].apply(pd.to_numeric, errors="raise")
        valid = (
            codes.str.fullmatch(r"[0-9A-Z]{6}") & dates.dt.year.eq(source["year"])
            & np.isfinite(values).all(axis=1) & values.gt(0).all(axis=1)
            & values.Stocks.mod(1).eq(0)
            & np.isclose(values.Close * values.Stocks, values.Marcap, rtol=1e-8, atol=1)
        )
        frame = pd.DataFrame({
            "security_id": "SEC_KR_" + codes,
            "trade_date": dates,
            "shares": values.Stocks,
            "market_cap": values.Marcap,
        })
        if frame.empty or not codes.str.fullmatch(r"[0-9A-Z]{6}").all() or frame.duplicated(["security_id", "trade_date"]).any():
            raise ValueError(f"Invalid historical capitalization observations: {source_path.name}")
        if source.get("quarantine") is not None:
            rejected = frame.assign(raw_close=values.Close).loc[~valid]
            _verify_share_quarantine(source["quarantine"], rejected)
            quarantined_rows += len(rejected)
            frame = frame.loc[valid].copy()
        elif not valid.all():
            raise ValueError(f"Invalid historical capitalization observations: {source_path.name}")
        frames.append(frame)
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    result.attrs["quarantined_share_source_rows"] = quarantined_rows
    return result


def _verify_share_quarantine(registration, rejected):
    """Accept only an exact, pinned inventory of already-invalid observations."""
    path = (DATA_LAKE.root / registration["path"]).resolve()
    if not path.is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Share source quarantine evidence must be inside data-lake/silver")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != registration["sha256"]:
        raise ValueError("Share source quarantine hash mismatch")
    expected = pd.read_parquet(BytesIO(raw))
    keys = ["security_id", "trade_date"]
    fields = ["raw_close", "shares", "market_cap"]
    _require_columns(expected, keys + fields)
    expected["trade_date"] = _parse_trade_dates(expected.trade_date)
    if expected.empty or expected.duplicated(keys).any():
        raise ValueError("Share source quarantine must contain unique rejected observations")
    expected, actual = expected.set_index(keys).sort_index(), rejected.set_index(keys).sort_index()
    if not expected.index.equals(actual.index):
        raise ValueError("Share source quarantine differs from the invalid source keys")
    if not np.array_equal(expected[fields].to_numpy(float), actual[fields].to_numpy(float), equal_nan=True):
        raise ValueError("Share source quarantine differs from original values")

