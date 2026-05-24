from __future__ import annotations

import argparse
import json
import os
import random
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from engine.core.paths import DATA_LAKE


# =============================================================================
# DART API 설정
# =============================================================================

DART_ALOT_MATTER_URL = "https://opendart.fss.or.kr/api/alotMatter.json"

# DART 공식 보고서 코드
REPORT_CODES = {
    "11011": "annual",  # 사업보고서
    "11012": "half",    # 반기보고서
    "11013": "q1",      # 1분기보고서
    "11014": "q3",      # 3분기보고서
}

RETRY_STATUS = {429, 500, 502, 503, 504}

# DART API status
DART_OK_STATUS = {
    "000",  # 정상
    "013",  # 조회된 데이터 없음
}

DART_KEY_LIMIT_STATUS = {
    "020",  # 요청 제한 초과
}

DART_BAD_KEY_STATUS = {
    "010",  # 등록되지 않은 키
    "011",  # 사용할 수 없는 키
    "012",  # 접근할 수 없는 IP
}

DART_FATAL_STATUS = {
    "100",  # 필드의 부적절한 값
    "101",  # 부적절한 접근
    "901",  # 계정 개인정보 보유기간 만료
}

DART_TRANSIENT_STATUS = {
    "800",  # 시스템 점검
    "900",  # 정의되지 않은 오류
}


# =============================================================================
# 예외
# =============================================================================

class AllDartApiKeysLimited(RuntimeError):
    pass


class NoDartApiKeyError(RuntimeError):
    pass


# =============================================================================
# DART API KEY POOL
# =============================================================================

def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


@dataclass
class DartApiKeyState:
    key: str
    disabled_until: float = 0.0
    disabled_reason: str = ""


class DartApiKeyPool:
    """
    여러 DART API KEY를 round-robin으로 돌려 쓰는 thread-safe key pool.

    환경변수 예:

    PowerShell:
        $env:DART_API_KEYS="key1,key2,key3"

    또는 단일 key:
        $env:DART_API_KEY="key1"

    동작:
    - DART status == "020" 이 나오면 해당 key 비활성화
    - key_cooldown_sec=None 이면 이번 실행 중 해당 key 재사용 안 함
    - key_cooldown_sec=60 이면 60초 뒤 재사용 가능
    """

    def __init__(
        self,
        keys: list[str],
        *,
        key_cooldown_sec: float | None = None,
        key_wait_max_sec: float = 60.0,
    ):
        clean_keys: list[str] = []
        seen: set[str] = set()

        for key in keys:
            key = str(key).strip()
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            clean_keys.append(key)

        if not clean_keys:
            raise NoDartApiKeyError(
                "DART API KEY가 없습니다. "
                "환경변수 DART_API_KEYS 또는 DART_API_KEY를 설정하세요."
            )

        self._states = [DartApiKeyState(key=key) for key in clean_keys]
        self._idx = 0
        self._lock = threading.Lock()
        self.key_cooldown_sec = key_cooldown_sec
        self.key_wait_max_sec = key_wait_max_sec

    @classmethod
    def from_env(
        cls,
        *,
        multi_env_name: str = "DART_API_KEYS",
        single_env_name: str = "DART_API_KEY",
        key_cooldown_sec: float | None = None,
        key_wait_max_sec: float = 60.0,
    ) -> "DartApiKeyPool":
        """
        우선순위:
        1. DART_API_KEYS="key1,key2,key3"
        2. DART_API_KEY="key1"
        """

        multi = os.getenv(multi_env_name, "").strip()
        single = os.getenv(single_env_name, "").strip()

        if multi:
            raw_keys = (
                multi.replace("\n", ",")
                .replace(";", ",")
                .split(",")
            )
        elif single:
            raw_keys = [single]
        else:
            raw_keys = []

        return cls(
            raw_keys,
            key_cooldown_sec=key_cooldown_sec,
            key_wait_max_sec=key_wait_max_sec,
        )

    def get_key(self) -> str:
        """
        사용 가능한 key 하나를 round-robin으로 반환.
        모든 key가 비활성화 상태면 AllDartApiKeysLimited 발생.

        key_cooldown_sec가 설정되어 있고 일시적으로만 막힌 상태라면
        key_wait_max_sec 범위 안에서 잠깐 기다렸다가 재시도한다.
        """

        started = time.monotonic()

        while True:
            now = time.monotonic()

            with self._lock:
                n = len(self._states)

                for _ in range(n):
                    state = self._states[self._idx]
                    self._idx = (self._idx + 1) % n

                    if state.disabled_until <= now:
                        return state.key

                earliest = min(s.disabled_until for s in self._states)

                snapshot = [
                    {
                        "key": _mask_key(s.key),
                        "disabled_until": s.disabled_until,
                        "disabled_reason": s.disabled_reason,
                    }
                    for s in self._states
                ]

            # 전부 영구 비활성화
            if earliest == float("inf"):
                raise AllDartApiKeysLimited(
                    f"사용 가능한 DART API KEY가 없습니다. states={snapshot}"
                )

            # 일시 비활성화지만 너무 오래 기다려야 함
            now = time.monotonic()
            wait_s = max(0.0, earliest - now)

            if time.monotonic() - started + wait_s > self.key_wait_max_sec:
                raise AllDartApiKeysLimited(
                    f"모든 DART API KEY가 일시적으로 비활성화되었습니다. "
                    f"key_wait_max_sec={self.key_wait_max_sec}, states={snapshot}"
                )

            if wait_s > 0:
                print(f"[DART KEY WAIT] all keys temporarily disabled. sleep={wait_s:.2f}s")
                time.sleep(wait_s)

    def mark_limited(self, key: str, reason: str = "DART status 020") -> None:
        """
        호출 한도 초과 key 비활성화.

        key_cooldown_sec is None:
        - 이번 실행 중 영구 비활성화

        key_cooldown_sec is not None:
        - 지정 초 동안만 비활성화
        """

        now = time.monotonic()

        if self.key_cooldown_sec is None:
            disabled_until = float("inf")
        else:
            disabled_until = now + self.key_cooldown_sec

        with self._lock:
            for state in self._states:
                if state.key == key:
                    state.disabled_until = disabled_until
                    state.disabled_reason = reason
                    print(
                        f"[DART KEY LIMITED] key={_mask_key(key)}, "
                        f"reason={reason}, cooldown={self.key_cooldown_sec}"
                    )
                    return

    def mark_bad_key(self, key: str, reason: str) -> None:
        """
        등록되지 않은 key, 사용할 수 없는 key, 접근 불가 IP key 등은
        이번 실행에서 완전히 제외한다.
        """

        with self._lock:
            for state in self._states:
                if state.key == key:
                    state.disabled_until = float("inf")
                    state.disabled_reason = reason
                    print(
                        f"[DART KEY DISABLED] key={_mask_key(key)}, "
                        f"reason={reason}"
                    )
                    return

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "key": _mask_key(s.key),
                    "disabled_until": s.disabled_until,
                    "disabled_reason": s.disabled_reason,
                }
                for s in self._states
            ]


# =============================================================================
# 공통 유틸
# =============================================================================

def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]+', "_", str(name)).strip()
    return name or "output"


def _normalize_numeric_code(value: Any, width: int) -> str:
    """
    CSV에서 005930이 5930, 5930.0 등으로 읽힌 경우를 보정.
    """

    if pd.isna(value):
        return ""

    s = str(value).strip()

    if re.fullmatch(r"\d+\.0", s):
        s = s.split(".")[0]

    s = re.sub(r"\D", "", s)

    if not s:
        return ""

    return s.zfill(width)


def make_stock_dividend_dir(
    out_root: str | Path,
    stock_code: str,
) -> Path:
    """
    stock별 폴더 생성.

    요구사항:
    - 폴더명은 stock_code만 사용

    예:
    ../data-lake/bronze/dart/dividend/005930/
    """

    stock_code = str(stock_code).zfill(6)
    out_dir = Path(out_root) / _safe_filename(stock_code)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def save_json_atomic(data: dict[str, Any], out_path: Path) -> None:
    """
    중간에 프로그램이 죽었을 때 깨진 JSON 파일이 남는 것을 줄이기 위해
    tmp 파일에 먼저 쓰고 replace.
    """

    out_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    tmp_path.replace(out_path)


def _append_summary_csv(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    write_header = not out_path.exists()

    df.to_csv(
        out_path,
        mode="a",
        header=write_header,
        index=False,
        encoding="utf-8-sig",
    )


# =============================================================================
# requests retry
# =============================================================================

def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_retries: int = 6,
    timeout: int | float = 30,
    base_backoff: float = 0.7,
    max_backoff: float = 20.0,
    retry_statuses: set[int] = RETRY_STATUS,
    **kwargs,
) -> requests.Response:
    """
    requests 기반 HTTP 재시도 함수.

    재시도 대상:
    - timeout
    - connection error
    - 429, 500, 502, 503, 504
    """

    for attempt in range(max_retries + 1):
        sleep_s = None

        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)

            if resp.status_code in retry_statuses:
                retry_after = resp.headers.get("Retry-After")

                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except ValueError:
                        sleep_s = None

                if attempt == max_retries:
                    resp.raise_for_status()

            else:
                resp.raise_for_status()
                return resp

        except (requests.Timeout, requests.ConnectionError):
            if attempt == max_retries:
                raise

        except requests.HTTPError:
            raise

        if attempt == max_retries:
            raise RuntimeError(f"Failed after {max_retries} retries: {method} {url}")

        if sleep_s is None:
            sleep_s = min(max_backoff, base_backoff * (2 ** attempt)) + random.uniform(0, 0.3)

        time.sleep(sleep_s)

    raise RuntimeError("request_with_retry: unexpected fallthrough")


# =============================================================================
# 1. 보고서 타입별 배당내역 JSON 다운로드
# =============================================================================

def fetch_dividend_by_report_type(
    session: requests.Session,
    key_pool: DartApiKeyPool,
    corp_code: str,
    bsns_year: int | str,
    reprt_code: str,
    *,
    timeout: int | float = 30,
    max_dart_retries: int = 8,
    base_backoff: float = 1.0,
    max_backoff: float = 30.0,
) -> dict[str, Any]:
    """
    특정 회사 / 특정 연도 / 특정 보고서 타입의 배당내역 JSON 다운로드.

    단위:
    - corp_code 1개
    - bsns_year 1개
    - reprt_code 1개

    핵심:
    - status == 020이면 현재 API KEY를 비활성화하고 다음 KEY로 재시도
    - status == 010/011/012이면 잘못된 KEY로 보고 비활성화 후 다음 KEY로 재시도
    - status == 000/013이면 그대로 반환
    """

    corp_code = str(corp_code).zfill(8)
    last_data: dict[str, Any] | None = None

    for attempt in range(max_dart_retries + 1):
        api_key = key_pool.get_key()

        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bsns_year": str(bsns_year),
            "reprt_code": str(reprt_code),
        }

        resp = request_with_retry(
            session,
            "GET",
            DART_ALOT_MATTER_URL,
            params=params,
            timeout=timeout,
        )

        data = resp.json()
        last_data = data

        dart_status = str(data.get("status", "")).strip()
        dart_message = str(data.get("message", "")).strip()

        if dart_status in DART_OK_STATUS:
            return data

        if dart_status in DART_KEY_LIMIT_STATUS:
            key_pool.mark_limited(
                api_key,
                reason=f"status={dart_status}, message={dart_message}",
            )

            print(
                f"[DART KEY ROTATE] status={dart_status}, "
                f"message={dart_message}, "
                f"corp_code={corp_code}, year={bsns_year}, reprt_code={reprt_code}"
            )
            continue

        if dart_status in DART_BAD_KEY_STATUS:
            key_pool.mark_bad_key(
                api_key,
                reason=f"status={dart_status}, message={dart_message}",
            )

            print(
                f"[DART BAD KEY ROTATE] status={dart_status}, "
                f"message={dart_message}, "
                f"corp_code={corp_code}, year={bsns_year}, reprt_code={reprt_code}"
            )
            continue

        if dart_status in DART_TRANSIENT_STATUS:
            if attempt == max_dart_retries:
                raise RuntimeError(
                    f"DART transient error after retries: "
                    f"status={dart_status}, message={dart_message}, "
                    f"corp_code={corp_code}, year={bsns_year}, reprt_code={reprt_code}"
                )

            sleep_s = min(max_backoff, base_backoff * (2 ** attempt)) + random.uniform(0, 0.5)

            print(
                f"[DART TRANSIENT RETRY] status={dart_status}, "
                f"message={dart_message}, sleep={sleep_s:.2f}s, "
                f"corp_code={corp_code}, year={bsns_year}, reprt_code={reprt_code}"
            )

            time.sleep(sleep_s)
            continue

        if dart_status in DART_FATAL_STATUS:
            raise RuntimeError(
                f"DART fatal error: status={dart_status}, "
                f"message={dart_message}, "
                f"corp_code={corp_code}, year={bsns_year}, reprt_code={reprt_code}"
            )

        raise RuntimeError(
            f"Unknown DART status: status={dart_status}, "
            f"message={dart_message}, "
            f"corp_code={corp_code}, year={bsns_year}, reprt_code={reprt_code}, "
            f"data={data}"
        )

    raise AllDartApiKeysLimited(
        f"DART API KEY 전부 한도 초과 또는 비활성화됨. "
        f"corp_code={corp_code}, year={bsns_year}, reprt_code={reprt_code}, "
        f"last_data={last_data}"
    )


# =============================================================================
# 2. 특정 stock / 특정 연도에서 보고서 타입별 다운로드
# =============================================================================

def download_dividends_by_year_and_report_type(
    session: requests.Session,
    key_pool: DartApiKeyPool,
    stock_code: str,
    corp_code: str,
    bsns_year: int,
    out_stock_dir: Path,
    *,
    report_codes: dict[str, str] = REPORT_CODES,
    skip_existing: bool = True,
    sleep_sec: float = 0.1,
) -> pd.DataFrame:
    """
    특정 stock의 특정 연도에 대해
    보고서 타입별로 순회하면서 JSON 다운로드 후 파일 저장.

    저장 예:
    ../data-lake/bronze/dart/dividend/005930/2024/11011_annual.json
    ../data-lake/bronze/dart/dividend/005930/2024/11012_half.json
    """

    result_rows: list[dict[str, Any]] = []

    stock_code = str(stock_code).zfill(6)
    corp_code = str(corp_code).zfill(8)

    for reprt_code, report_name in report_codes.items():
        out_path = out_stock_dir / str(bsns_year) / f"{reprt_code}_{report_name}.json"

        if skip_existing and out_path.exists():
            print(f"[SKIP] {stock_code} {bsns_year} {reprt_code} already exists")

            result_rows.append(
                {
                    "stock_code": stock_code,
                    "corp_code": corp_code,
                    "bsns_year": bsns_year,
                    "reprt_code": reprt_code,
                    "report_name": report_name,
                    "dart_status": "SKIP_EXISTS",
                    "dart_message": "already exists",
                    "row_count": None,
                    "out_path": str(out_path),
                    "ok": True,
                }
            )
            continue

        print(f"[DOWNLOAD] {stock_code} {corp_code} {bsns_year} {reprt_code}")

        try:
            data = fetch_dividend_by_report_type(
                session=session,
                key_pool=key_pool,
                corp_code=corp_code,
                bsns_year=bsns_year,
                reprt_code=reprt_code,
            )

            dart_status = str(data.get("status", "")).strip()
            dart_message = str(data.get("message", "")).strip()
            row_count = len(data.get("list", []) or [])

            # 020 같은 key limit 응답은 fetch 단계에서 저장하지 않고 key rotate 처리한다.
            # 여기까지 온 데이터는 정상 또는 조회 데이터 없음 등 저장해도 되는 데이터다.
            save_json_atomic(data, out_path)

            result_rows.append(
                {
                    "stock_code": stock_code,
                    "corp_code": corp_code,
                    "bsns_year": bsns_year,
                    "reprt_code": reprt_code,
                    "report_name": report_name,
                    "dart_status": dart_status,
                    "dart_message": dart_message,
                    "row_count": row_count,
                    "out_path": str(out_path),
                    "ok": dart_status in DART_OK_STATUS,
                }
            )

        except AllDartApiKeysLimited:
            raise

        except Exception as e:
            print(f"[ERROR] {stock_code} {corp_code} {bsns_year} {reprt_code}: {repr(e)}")

            result_rows.append(
                {
                    "stock_code": stock_code,
                    "corp_code": corp_code,
                    "bsns_year": bsns_year,
                    "reprt_code": reprt_code,
                    "report_name": report_name,
                    "dart_status": "PYTHON_ERROR",
                    "dart_message": repr(e),
                    "row_count": None,
                    "out_path": str(out_path),
                    "ok": False,
                }
            )

        time.sleep(sleep_sec)

    return pd.DataFrame(result_rows)


# =============================================================================
# 3. 특정 stock 전체 연도 다운로드
# =============================================================================

def download_dividends_for_one_stock(
    session: requests.Session,
    key_pool: DartApiKeyPool,
    stock_code: str,
    corp_code: str,
    *,
    corp_name: str = "",
    download_offset: int | None = None,
    start_year: int = 2015,
    end_year: int | None = None,
    out_root: str | Path = DATA_LAKE.bronze("dart", "dividend"),
    report_codes: dict[str, str] = REPORT_CODES,
    skip_existing: bool = True,
    sleep_sec: float = 0.1,
) -> pd.DataFrame:
    """
    stock별 폴더를 만들고,
    해당 stock의 모든 연도 × 모든 보고서 타입 JSON을 stock 폴더에 저장.

    폴더명:
    - stock_code만 사용
    """

    if end_year is None:
        end_year = datetime.now().year

    stock_code = str(stock_code).zfill(6)
    corp_code = str(corp_code).zfill(8)

    out_stock_dir = make_stock_dividend_dir(
        out_root=out_root,
        stock_code=stock_code,
    )

    all_dfs: list[pd.DataFrame] = []

    try:
        for bsns_year in range(start_year, end_year + 1):
            year_df = download_dividends_by_year_and_report_type(
                session=session,
                key_pool=key_pool,
                stock_code=stock_code,
                corp_code=corp_code,
                bsns_year=bsns_year,
                out_stock_dir=out_stock_dir,
                report_codes=report_codes,
                skip_existing=skip_existing,
                sleep_sec=sleep_sec,
            )

            all_dfs.append(year_df)

    except AllDartApiKeysLimited:
        # 이미 받은 구간에 대한 summary라도 남긴 뒤 다시 raise
        if all_dfs:
            partial_df = pd.concat(all_dfs, ignore_index=True)
            partial_df["corp_name"] = corp_name
            partial_df["download_offset"] = download_offset

            summary_path = out_stock_dir / "_download_summary_partial.csv"
            partial_df.to_csv(summary_path, index=False, encoding="utf-8-sig")

        raise

    if all_dfs:
        summary_df = pd.concat(all_dfs, ignore_index=True)
    else:
        summary_df = pd.DataFrame()

    if not summary_df.empty:
        summary_df["corp_name"] = corp_name
        summary_df["download_offset"] = download_offset

    summary_path = out_stock_dir / "_download_summary.csv"
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")

    return summary_df


# =============================================================================
# stocks_df 정규화
# =============================================================================

def normalize_stocks_df(stocks_df: pd.DataFrame) -> pd.DataFrame:
    """
    입력 DataFrame 정규화.

    필수 컬럼:
    - stock_code
    - corp_code

    선택 컬럼:
    - corp_name
    """

    required_cols = {"stock_code", "corp_code"}
    missing_cols = required_cols - set(stocks_df.columns)

    if missing_cols:
        raise ValueError(f"stocks_df에 필수 컬럼이 없습니다: {missing_cols}")

    df = stocks_df.copy()

    df = df.dropna(subset=["stock_code", "corp_code"])

    df["stock_code"] = df["stock_code"].map(lambda x: _normalize_numeric_code(x, 6))
    df["corp_code"] = df["corp_code"].map(lambda x: _normalize_numeric_code(x, 8))

    df = df[(df["stock_code"] != "") & (df["corp_code"] != "")]

    if "corp_name" not in df.columns:
        df["corp_name"] = ""

    df["corp_name"] = df["corp_name"].fillna("").astype(str)

    df = (
        df[["stock_code", "corp_code", "corp_name"]]
        .drop_duplicates(subset=["stock_code", "corp_code"])
        .sort_values(["stock_code", "corp_code"])
        .reset_index(drop=True)
    )

    return df


def load_stocks_mapping_csv(path: str | Path) -> pd.DataFrame:
    """
    stock_code, corp_code 매핑 CSV 로드.

    CSV 예:
    stock_code,corp_code,corp_name
    005930,00126380,삼성전자
    000660,00164779,SK하이닉스
    """

    return pd.read_csv(
        path,
        dtype={
            "stock_code": str,
            "corp_code": str,
            "corp_name": str,
        },
    )


# =============================================================================
# 4. 전체 stock 비동기 다운로드
# =============================================================================

def _download_one_stock_worker(
    *,
    key_pool: DartApiKeyPool,
    stock_code: str,
    corp_code: str,
    corp_name: str,
    download_offset: int,
    start_year: int,
    end_year: int | None,
    out_root: str | Path,
    report_codes: dict[str, str],
    skip_existing: bool,
    sleep_sec: float,
) -> pd.DataFrame:
    """
    ThreadPoolExecutor에서 실행할 stock 단위 worker.

    requests.Session은 thread-safe하게 공유하지 않는 편이 안전하므로
    worker 내부에서 stock마다 Session을 새로 만든다.
    """

    print(
        f"\n========== START stock={stock_code}, corp_name={corp_name}, "
        f"corp_code={corp_code}, download_offset={download_offset} =========="
    )

    with requests.Session() as session:
        stock_summary_df = download_dividends_for_one_stock(
            session=session,
            key_pool=key_pool,
            stock_code=stock_code,
            corp_code=corp_code,
            corp_name=corp_name,
            download_offset=download_offset,
            start_year=start_year,
            end_year=end_year,
            out_root=out_root,
            report_codes=report_codes,
            skip_existing=skip_existing,
            sleep_sec=sleep_sec,
        )

    print(
        f"========== DONE stock={stock_code}, "
        f"download_offset={download_offset} ==========\n"
    )

    return stock_summary_df


def fetch_all_stock_dividends_async(
    stocks_df: pd.DataFrame,
    *,
    download_offset: int = 0,
    start_year: int = 2015,
    end_year: int | None = None,
    out_root: str | Path = DATA_LAKE.bronze("dart", "dividend"),
    report_codes: dict[str, str] = REPORT_CODES,
    skip_existing: bool = True,
    sleep_sec: float = 0.1,
    max_workers: int = 4,
    key_cooldown_sec: float | None = None,
    key_wait_max_sec: float = 60.0,
) -> pd.DataFrame:
    """
    전체 stock을 stock 단위로 비동기/병렬 다운로드.

    offset 기준:
    - stocks_df를 stock_code, corp_code 기준으로 정렬
    - 정렬된 DataFrame에서 download_offset 이후부터 실행
    - 중간에 끊기면 마지막으로 출력된 download_offset 근처부터 재시작
    - skip_existing=True면 이미 받은 JSON은 건너뜀

    병렬화 단위:
    - stock 단위
    - 각 stock 내부에서는 연도 × 보고서 타입을 순차 다운로드

    API KEY:
    - DART_API_KEYS 환경변수 우선 사용
    - 없으면 DART_API_KEY 사용
    """

    key_pool = DartApiKeyPool.from_env(
        multi_env_name="DART_API_KEYS",
        single_env_name="DART_API_KEY",
        key_cooldown_sec=key_cooldown_sec,
        key_wait_max_sec=key_wait_max_sec,
    )

    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    df = normalize_stocks_df(stocks_df)

    if download_offset < 0:
        raise ValueError("download_offset은 0 이상이어야 합니다.")

    if download_offset >= len(df):
        print(f"[DONE] download_offset={download_offset}, total_stocks={len(df)}")
        return pd.DataFrame()

    target_df = df.iloc[download_offset:].copy()

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_summary_path = out_root / f"_all_download_summary_{run_id}.csv"

    targets: list[tuple[int, dict[str, Any]]] = [
        (int(idx), row.to_dict())
        for idx, row in target_df.iterrows()
    ]

    all_summary_dfs: list[pd.DataFrame] = []
    pending = {}
    next_pos = 0
    stop_due_to_key_limit = False

    def submit_until_full(executor: ThreadPoolExecutor) -> None:
        nonlocal next_pos

        while next_pos < len(targets) and len(pending) < max_workers:
            absolute_offset, row = targets[next_pos]
            next_pos += 1

            stock_code = row["stock_code"]
            corp_code = row["corp_code"]
            corp_name = row.get("corp_name", "")

            future = executor.submit(
                _download_one_stock_worker,
                key_pool=key_pool,
                stock_code=stock_code,
                corp_code=corp_code,
                corp_name=corp_name,
                download_offset=absolute_offset,
                start_year=start_year,
                end_year=end_year,
                out_root=out_root,
                report_codes=report_codes,
                skip_existing=skip_existing,
                sleep_sec=sleep_sec,
            )

            pending[future] = {
                "stock_code": stock_code,
                "corp_code": corp_code,
                "corp_name": corp_name,
                "download_offset": absolute_offset,
            }

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        submit_until_full(executor)

        while pending:
            done_futures, _ = wait(
                pending.keys(),
                return_when=FIRST_COMPLETED,
            )

            for future in done_futures:
                meta = pending.pop(future)

                try:
                    stock_summary_df = future.result()

                except AllDartApiKeysLimited as e:
                    print(
                        f"[STOP] 모든 DART API KEY가 한도 초과 또는 비활성화되었습니다. "
                        f"마지막 stock={meta['stock_code']}, "
                        f"download_offset={meta['download_offset']}, "
                        f"error={repr(e)}"
                    )

                    stock_summary_df = pd.DataFrame(
                        [
                            {
                                "stock_code": meta["stock_code"],
                                "corp_code": meta["corp_code"],
                                "corp_name": meta["corp_name"],
                                "download_offset": meta["download_offset"],
                                "dart_status": "ALL_KEYS_LIMITED",
                                "dart_message": repr(e),
                                "row_count": None,
                                "out_path": None,
                                "ok": False,
                            }
                        ]
                    )

                    all_summary_dfs.append(stock_summary_df)
                    _append_summary_csv(stock_summary_df, run_summary_path)

                    for f in pending:
                        f.cancel()

                    stop_due_to_key_limit = True
                    break

                except Exception as e:
                    print(
                        f"[STOCK LEVEL ERROR] stock={meta['stock_code']}, "
                        f"download_offset={meta['download_offset']}, error={repr(e)}"
                    )

                    stock_summary_df = pd.DataFrame(
                        [
                            {
                                "stock_code": meta["stock_code"],
                                "corp_code": meta["corp_code"],
                                "corp_name": meta["corp_name"],
                                "download_offset": meta["download_offset"],
                                "dart_status": "STOCK_LEVEL_ERROR",
                                "dart_message": repr(e),
                                "row_count": None,
                                "out_path": None,
                                "ok": False,
                            }
                        ]
                    )

                    all_summary_dfs.append(stock_summary_df)
                    _append_summary_csv(stock_summary_df, run_summary_path)

                else:
                    all_summary_dfs.append(stock_summary_df)
                    _append_summary_csv(stock_summary_df, run_summary_path)

            if stop_due_to_key_limit:
                break

            submit_until_full(executor)

    if all_summary_dfs:
        total_summary_df = pd.concat(all_summary_dfs, ignore_index=True)
    else:
        total_summary_df = pd.DataFrame()

    print(f"\n[DONE] run summary saved: {run_summary_path}")
    print(f"[DART KEY SNAPSHOT] {key_pool.snapshot()}")

    return total_summary_df
