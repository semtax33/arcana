from pathlib import Path

import pandas as pd
from glob import glob



def normalize_price(path: str):
    files = glob(path)

    if not files:
        raise FileNotFoundError("CSV 파일을 찾지 못했습니다.")

    df = pd.concat(
        [
            pd.read_csv(file).assign(source_file=Path(file).name)
            for file in files
        ],
        ignore_index=True
    )

    df.to_csv('./data-lake/silver/krx/price/normalized_price.csv')

def normalize_shares(path: str):
    files = glob(path)

    if not files:
        raise FileNotFoundError("CSV 파일을 찾지 못했습니다.")

    df = pd.concat(
        [
            pd.read_csv(file).assign(source_file=Path(file).name)
            for file in files
        ],
        ignore_index=True
    )

    df.to_csv('./data-lake/silver/krx/price/normalized_shares.csv')

normalize_price("./data-lake/bronze/krx/price/*")
normalize_shares("./data-lake/bronze/krx/shares/*")