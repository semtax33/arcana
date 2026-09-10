# SEC 직접 분기 EPS 선택 계약 — NVDA 2024·AAPL 2020

조사일 2026-09-10 KST. 로컬 SEC companyfacts 원본 두 파일, 기존에 보존한 SEC 원문 네 건, 현재 `quarterly_financial_frame` 경로를 읽기 전용으로 확인했다. 코드·DB·운영 데이터를 수정하지 않았으며, 순이익이나 현재 발행주식수로 EPS를 대체하지 않았다.

## 결론

이 두 발행사의 대상 분기는 **공시된 독립 분기 EPS를 `start/end + accn + filed`로 재현할 수 있다.** 같은 `end`의 누적 EPS를 선택한 후 직전 누적 EPS를 빼는 현재 경로는 잘못된 분기값을 만든다. 주식분할이 없는 기간에도 EPS는 가산적인 현금흐름이 아니므로, 분할 보정만 추가해서는 해결되지 않는다. Apple은 연간보고서의 분기정보 주석에서 각 분기 EPS를 따로 계산하므로 합계가 연간 EPS와 다를 수 있음을 명시한다. [Apple 2020 10-K, Note 13·p58](https://www.sec.gov/Archives/edgar/data/320193/000032019320000096/aapl-20200926.htm).

직접 분기 추출은 **분기화 오류**를 해결한다. 서로 다른 공시 시점의 분기 EPS를 합산하거나 현재 가격과 비교할 때 필요한 **주식수 기준단위 확인**은 별도 문제로 남는다. 이 문서는 기준단위가 불명확한 EPS를 자동 환산하는 규칙을 제안하지 않는다.

## 실제 companyfacts 행

아래 Basic/Diluted는 모두 표준 태그 `us-gaap:EarningsPerShareBasic`, `us-gaap:EarningsPerShareDiluted`의 `USD/shares` 단위다. 각 값은 원본 행의 `val`을 그대로 옮겼다. 원본과 동일한 모든 키는 `sec_direct_quarter_eps_samples_20260910/local_companyfacts_exact_rows.json`에 보존했다. [NVDA 공식 companyfacts](https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json), [AAPL 공식 companyfacts](https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json).

| 발행사·관측 | start | end | filed | accn | Basic | Diluted |
|---|---|---|---|---|---:|---:|
| NVDA Q1 원공시 | 2024-01-29 | 2024-04-28 | 2024-05-29 | 0001045810-24-000124 | 6.04 | 5.98 |
| NVDA Q2 직접 분기 | 2024-04-29 | 2024-07-28 | 2024-08-28 | 0001045810-24-000264 | 0.68 | 0.67 |
| NVDA Q2 공시의 6개월 누적 | 2024-01-29 | 2024-07-28 | 2024-08-28 | 0001045810-24-000264 | 1.28 | 1.27 |
| AAPL Q2 직접 분기 | 2019-12-29 | 2020-03-28 | 2020-05-01 | 0000320193-20-000052 | 2.58 | 2.55 |
| AAPL Q3 원공시 | 2020-03-29 | 2020-06-27 | 2020-07-31 | 0000320193-20-000062 | 2.61 | 2.58 |
| AAPL 같은 Q3의 후속 재표시 | 2020-03-29 | 2020-06-27 | 2020-10-30 | 0000320193-20-000096 | 0.65 | 0.65 |
| AAPL Q4 직접 분기 | 2020-06-28 | 2020-09-26 | 2020-10-30 | 0000320193-20-000096 | 0.74 | 0.73 |

NVDA의 위 2024년 Q1/Q2 행은 `fy=2025`, `fp=Q1/Q2`이다. 회계연도와 종료일의 달력연도를 동일시하지 않는다. AAPL 후속 10-K에 실린 Q3 값은 `fy=2020, fp=FY`이지만 `start/end`는 명백히 Q3이다. 이를 FY 값이나 Q4 값으로 옮기면 안 된다.

원공시의 유효 분기 행 상당수에는 `frame`이 없다. 반면 NVDA 2024 Q1의 `frame=CY2024Q1` 행은 로컬 스냅샷에서 **2025-05-28에 제출된 비교기간 값 0.60/0.60**이다. 따라서 `frame` 존재 여부는 필수 조건도, 최초 공시 시점의 증거도 아니다. SEC는 frames API를 달력 기간에 가장 가까운 최근 공시의 사실을 모은 자료로 설명하며, 발행사별 회계기간이 다를 수 있음을 명시한다. [SEC API 설명](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

## 원문의 EPS 단위와 pro forma

NVDA 2024 Q1 10-Q의 실제 손익계산서 EPS는 6.04/5.98이다. 원문의 같은 기간 `c-1` 컨텍스트에는 별도 차원이 없다. 같은 문서에 0.60/0.60도 있지만 `c-169`의 `srt:StatementScenarioAxis=srt:ProFormaMember`에 속한다. **동일 태그·동일 기간·동일 통화만으로는 두 값을 구분할 수 없다.** 로컬 companyfacts의 해당 accession에는 실제 6.04/5.98만 들어 있고, 이 원문의 pro forma 0.60은 없다. [NVDA Q1 10-Q, 손익계산서·후속사건](https://www.sec.gov/Archives/edgar/data/1045810/000104581024000124/nvda-20240428.htm).

NVDA Q2 보고서는 2024년 6월 실행한 10대1 분할을 모든 제시 주식·주당 정보에 소급 반영했다고 설명한다. 독립 분기 EPS 0.68/0.67은 그 단위다. 반면 Q1 원공시 6.04/5.98은 분할 전 단위다. [NVDA Q2 10-Q, Note 1·손익계산서](https://www.sec.gov/Archives/edgar/data/1045810/000104581024000264/nvda-20240728.htm).

AAPL Q3 보고서는 3개월 EPS 2.61/2.58과 9개월 EPS 10.25/10.16을 구분하며, 8월 31일부터 분할 조정된 가격으로 거래될 예정이라고 기술한다. 후속 10-K는 8월 28일 실행한 4대1 분할을 주당 정보에 소급 반영했다고 설명하며, 같은 Q3를 0.65/0.65로 제시한다. 두 값은 다른 공시 버전의 사실이므로 7월 백테스트에 10월 재표시를 덮어쓰면 안 된다. [AAPL Q3 10-Q, EPS 주석·Common Stock Split](https://www.sec.gov/Archives/edgar/data/320193/000032019320000062/aapl-20200627.htm), [AAPL 2020 10-K](https://www.sec.gov/Archives/edgar/data/320193/000032019320000096/aapl-20200926.htm).

SEC companyfacts는 표준 분류체계의 전체 발행사에 해당하는 사실을 집계한다. 그러나 개별 행에는 원문 `contextRef`, 차원, 소급 기준단위 설명이 없다. 위 NVDA 표본에서 pro forma가 배제됨을 확인한 것과, 모든 발행사의 모든 행에 대해 이를 보증하는 것은 다르다. 상충하는 후보가 있거나 기준단위가 불명확하면 같은 accession 원문의 실제 재무제표 컨텍스트로 검증한다. [SEC XBRL API 집계 범위](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

## 현재 계산 경로와 재현된 오류

검토한 파일의 SHA는 `code_read_manifest.json`에 있다. 조사 이후 본체 수정으로 줄 번호가 바뀔 수 있다.

1. `engine/transformers/_internal/sec_filings.py`의 `_select_current_companyfacts_unit_rows`는 accession의 현재 `end`에 대해 가장 긴 duration을 선택한다. EPS에도 이 규칙이 적용되어 Q2의 독립 분기 대신 YTD가 남는다. `_candidate_from_companyfacts_unit`는 이 경로에서 원행의 `start`를 보존하지 않고 `fy/fp`를 후보의 회계기간으로 사용한다.
2. `read_period_snapshots`는 정규화 파일을 회계기간별로 집계한다. 같은 계정 복수 값의 집계가 절댓값 최대 선택이므로, 분기/YTD를 같은 계정·기간에 섞어 저장한 뒤 여기서 선택하게 해서는 안 된다. `attach_report_metadata`는 기간별 최신 보고 메타데이터 하나를 붙이므로 이 프레임 자체는 과거 모든 공시 버전의 저장소가 아니다.
3. `filing_periods.add_quarter_and_ttm_amounts`는 `IS/CIS/CF` 기본 누적 대상의 모든 숫자 계정에 직전 YTD 차감을 적용한다. `quarterly_financial_frame`은 이 `_quarter` 열을 반환한다. 부모가 사용하는 `use_edgartools=False` 경로는 이 함수로 연결된다.

로컬 정규화 파일과 명시적인 `silver/sec/us_report_metadata.csv`를 사용하고 `fallback_to_period_end=False`로 재현한 결과는 다음과 같다. 아래 차감값의 피연산자도 companyfacts에서 직접 확인했다.

| 분기 | 현재 Basic / Diluted | 직접 공시 Basic / Diluted | 잘못된 계산 |
|---|---:|---:|---|
| NVDA 2024 Q2 | -4.76 / -4.71 | 0.68 / 0.67 | 1.28−6.04, 1.27−5.98 |
| AAPL 2020 Q2 | 2.59 / 2.57 | 2.58 / 2.55 | 7.63−5.04, 7.56−4.99 |
| AAPL 2020 Q3 | 2.62 / 2.60 | 2.61 / 2.58 | 10.25−7.63, 10.16−7.56 |
| AAPL 2020 Q4 | -6.94 / -6.88 | 0.74 / 0.73 | 3.31−10.25, 3.28−10.16 |

특히 AAPL Q2/Q3는 8월 분할 **이전**이다. EPS의 분모는 각 기간의 가중평균 주식수이고, 누적과 독립 분기의 분모가 같지 않다. AAPL Q2 기본 가중평균은 독립 분기 4,360,101,000주, 6개월 누적 4,387,570,000주이며, Q1은 4,415,040,000주다. 이들은 로컬 표준 companyfacts의 실제 `WeightedAverageNumberOfSharesOutstandingBasic` 행이다. EPS가 `N/W` 형태이므로 `(N1+N2)/W12 − N1/W1`은 일반적으로 `N2/W2`와 같지 않다. 이는 순이익으로 EPS를 재구성하자는 제안이 아니라 비가산성의 설명이다.

부수적으로 같은 현재 경로는 `BASIC_SHARES/DILUTED_SHARES`도 차감해 AAPL Q2의 기본 가중평균 주식수를 −27,470,000으로 만든다. EPS와 그 가중평균 주식수는 모두 일반 누적 flow 차감에서 제외할 대상이다. 해당 출력은 `local_aapl_quarter.csv`에 보존했다. 이 조사는 다른 계정이나 발행사로 범위를 넓히지 않았다.

## 제안하는 선택 계약

다음은 위 증거에 근거한 구현 계약 제안이며 운영 코드를 변경한 것은 아니다.

### 1. 원행과 공시 버전을 보존한다

최소 식별자는 `CIK, taxonomy, tag, unit, start, end, accn, filed`다. `val, fy, fp, form, frame`을 원문 그대로 보존하고, 가능하면 원문 `contextRef, dimensions, decimals, scale, accepted_at`, 파일 경로·SHA도 함께 둔다. 기간 사실과 그 사실이 공개된 시간을 구분한다. `fy/fp`는 제출 문서의 회계연도·구분을 설명하는 보조 필드이며, 비교기간 사실의 실제 기간을 대신하지 않는다.

### 2. 직접 분기 후보를 고른다

- 대상 태그는 Basic과 Diluted 각각의 정확한 표준 EPS 태그다. 둘은 서로 대체하지 않는다. 이 두 발행사의 검증 단위는 `USD/shares`다.
- 허용 원문은 `10-Q`, `10-Q/A`, `10-K`, `10-K/A`의 실제 GAAP EPS다. 같은 accession의 원문에서 확인되는 전체 발행사·해당 보통주·실제 보고 컨텍스트를 선택하고 pro forma, 추정, 조정 비GAAP, 다른 주식종류 컨텍스트를 섞지 않는다. companyfacts가 상충하면 원문을 확인한다.
- `end`는 대상 분기말, `start`는 그 발행사의 해당 회계분기 시작일과 정확히 일치해야 한다. NVDA의 13주 분기처럼 회계 달력을 따른다. 약 3개월이라는 duration만으로 확정하거나 가장 짧은 duration을 무조건 선택하지 않는다. 전환 회계기간·짧은 특별 기간은 검토 대상으로 둔다.
- Q1은 분기와 YTD 기간이 같으므로 직접 값이 된다. Q2/Q3의 더 긴 YTD 후보는 분기 선택에서 제외한다. Q4는 AAPL 사례처럼 10-K의 분기정보에 직접 태그된 값이 있으면 선택할 수 있지만, 모든 회사에 존재한다고 가정하지 않는다.
- 같은 accession·태그·단위·기간·값의 동일 중복은 제거할 수 있다. 값이나 컨텍스트가 충돌하면 크기·절댓값·행 순서로 선택하지 않고 QA 상태를 반환한다. Basic/Diluted의 한 쌍이 필요하면 같은 공시 버전을 사용한다.

### 3. 공시 시점별로 조회한다

`as_of`까지 공개된 사실만 남긴 뒤, **같은 실제 분기 구간**의 확인된 공시 버전 중 최신 버전을 선택한다. 과거 시점의 원공시 재현이 목적이면 그 accession을 고정한다. 두 방식 모두 전체 버전 이력을 지우지 않는다.

동일 날짜에 여러 다른 accession이 충돌하면 접수시각이나 명시적인 정정관계를 확인한다. accession 문자열 순서만으로 우선순위를 정하지 않는다. 후속 정정 문서에 해당 EPS가 없다면 그 사실을 임의 삭제하거나 누적값으로 대체하지 않는다. 후속 10-K의 비교기간 값도 그 **후속 공개 시점 이후에만** 사용 가능하다.

companyfacts의 `filed`는 날짜 정밀도다. 본 연구의 `filed <= as_of` 예시는 해당 날짜까지의 공시 버전 조회이며, 당일 종가 매매에 이용 가능하다는 뜻은 아니다. 거래 신호에는 SEC 접수시각과 시장 시간대·컷오프를 확인하거나, 시각이 없으면 다음 거래 세션부터 이용하는 보수적 규칙이 필요하다. 결산일을 공시일로 대체하지 않는다.

### 4. 누락·불명확성은 명시적으로 반환한다

직접 분기 행이 없으면 `missing_direct_quarter_eps`, 공개시점을 확인할 수 없으면 `missing_disclosure_time`, 후보가 충돌하면 `ambiguous_context`, 주당 기준단위가 불명확하면 `unverified_share_basis`처럼 이유를 남긴다. 운영 정책에 따라 같은 accession의 공식 iXBRL에서 직접 분기 EPS를 추가 확인할 수 있지만, **YTD 차감·연간에서 9개월 차감·NI/주식수·다른 EPS 종류로 메우지 않는다.** 0이나 음수 EPS는 값 자체만으로 누락 처리하지 않는다.

### 5. 독립 분기 선택과 기준단위 통일을 분리한다

2024-08-28을 기준으로 NVDA companyfacts의 Q1 직접 분기를 조회하면 여전히 원공시 6.04/5.98이다. Q2는 0.68/0.67이고, 그때까지 같은 Q1 기간의 분할후 직접 재표시 행은 이 로컬 스냅샷에 없다. 0.60/0.60의 직접 비교기간 행은 2025-05-28에야 나타난다. 이를 2024년에 당겨 쓰면 미래정보다.

따라서 직접 분기 조회 성공을 곧바로 TTM 합산이나 현재가격 대비 EPS 사용 승인으로 해석하지 않는다. 각 공시의 소급 반영 범위·주식수 기준단위가 입증될 때에만 별도의 명시적인 단위 통일 절차를 검토한다. 단순 `report_date` 기준 split factor 자동 적용은 이 계약에 포함하지 않는다. 기준단위가 서로 다른 분기값을 원상태로 합산하지 않는다.

## 검증 및 저장물

`docs/research/sec_direct_quarter_eps_samples_20260910/`에 다음을 저장했다.

- `local_input_manifest.json`: 로컬 companyfacts 두 파일의 정확한 경로·공식 URL·SHA-256·크기·mtime. NVDA 스냅샷은 로컬 2026-06-17 수정, AAPL은 2026-05-01 수정 파일이며 재다운로드하지 않았다.
- `local_companyfacts_exact_rows.json`: 대상 기간의 EPS와 비가산성 확인용 가중평균 주식수 원행. 비교기간으로 나중에 다시 제출된 행도 함께 보존했다.
- `filing_originals_manifest.json`: 기존 공식 원문 네 건의 정확한 URL·경로·SHA. 네 건 모두 원본 SHA를 재검증했다.
- `eps_ixbrl_context_observations.json`: 원문 EPS 사실 22개와 컨텍스트·차원. NVDA 실제와 pro forma를 명시적으로 분리했다.
- `asof_selection_examples.json`: 공시 전 누락, 최초 공개, 후속 재표시 등 11개 예시. 모두 원행에서 선택하고 기대값을 확인했다.
- `local_nvda_snapshot.csv`, `local_nvda_quarter.csv`, `local_aapl_snapshot.csv`, `local_aapl_quarter.csv`: 현재 로컬 계산의 관찰값.
- `filing_text_observations.json`, `code_read_manifest.json`, `validation.json`: EPS 설명·분할 기준단위·비가산성 문구와 코드 버전·산술 확인.

이는 현 로컬 스냅샷에 남은 과거 공시 행과 공식 원문을 통한 재현이다. SEC API 전체의 역사적 응답·배포 지연을 보존한 시점별 아카이브를 구축한 것은 아니다. 조사한 두 발행사 밖의 직접 분기 EPS 커버리지나 pro forma 배제율은 측정하지 않았다.
