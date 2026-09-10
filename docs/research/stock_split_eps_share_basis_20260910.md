# 분할 전후 보고 EPS의 주식 수 단위: AAPL·NVDA·삼성전자 표본

- 조사일: 2026-09-10 한국시간.
- 범위: 3개 회사의 공식 SEC/KIND 보고서와 삼성전자 공식 영문 재무제표. 요청받은 로컬 NVDA 읽기 결과를 함께 검산했다. 전 종목 수집, 소스 코드·가격·DB·팩터 정의 변경은 하지 않았다.
- 결론: **`financial_period`나 `report_date`만으로 EPS의 주식 수 단위를 자동 판정하면 안 된다.** 실제 보고서 안에서도 과거 단위, 소급 조정 단위, 미래 분할을 가정한 단위가 다르다. 특히 삼성전자 2018년 1분기 보고서는 분할 거래일 이후 제출됐지만 EPS가 분할 전 단위이고, NVDA 로컬 분기화에서는 두 단위를 뺀 음수 EPS가 재현된다.

## 공식 숫자로 확인한 세 가지 경우

| 회사·문서 | 회계기간 종료 / 제출일 | 확인한 기본·희석 EPS | 분모와 단위 판정 |
| --- | --- | --- | --- |
| AAPL 2020년 Q3 10-Q | 2020-06-27 / 2020-07-31 | 2.61 / 2.58달러 | 기본 4,312,573천주, 희석 4,354,788천주. 4대1 분할 전 단위 |
| AAPL 2020년 10-K의 같은 Q3 비교표 | 같은 2020년 Q3 / 2020년 10-K | 0.65 / 0.65달러 | 보고서 전체 주당 수치를 이미 4대1로 소급 조정 |
| NVDA 2024-04-28 분기 10-Q, 실제 실적 | 2024-04-28 / 2024-05-29 | 6.04 / 5.98달러 | 기본 2,462백만주, 희석 2,489백만주. 10대1 분할 전 단위 |
| **같은 NVDA 공시**, 후속사건 pro forma 표 | 같은 기간·같은 제출일 | 0.60 / 0.60달러 | 기본 24,620백만주, 희석 24,890백만주. 예정된 분할을 먼저 가정 |
| 삼성전자 연결 2018년 Q1 분기보고서 | 2018-03-31 / **2018-05-15** | **85,435 / 85,435원** | 보통주 가중평균 119,450천주. **5월 4일 분할 거래 이후 제출돼도 분할 전 단위** |

표의 원문 출처: [AAPL Q3 10-Q EPS 주석](https://www.sec.gov/Archives/edgar/data/320193/000032019320000062/aapl-20200627.htm), [AAPL Q3 제출시각·회계기간](https://www.sec.gov/Archives/edgar/data/320193/0000320193-20-000062-index.htm), [AAPL 2020 10-K 주석 1·13](https://www.sec.gov/Archives/edgar/data/320193/000032019320000096/aapl-20200926.htm), [NVDA Q1 주석 4·15](https://www.sec.gov/Archives/edgar/data/1045810/000104581024000124/nvda-20240428.htm), [NVDA 제출시각](https://www.sec.gov/Archives/edgar/data/1045810/000104581024000124/0001045810-24-000124-index.htm), [삼성전자 분기보고서](https://kind.krx.co.kr/external/2018/05/15/001849/20180515003807/11013.htm).

### AAPL: 같은 회계기간의 값도 어느 공시에서 읽었는가에 따라 달라진다

7월 Q3 공시는 8월 31일부터 분할 기준으로 거래된다는 계획을 기재하지만 실제 EPS 표는 이전 단위다. 10-K는 8월 28일 분할 실행과 모든 주당 정보의 소급 조정을 명시하고, 주석 13의 같은 Q3 EPS는 0.65달러다. 분할 전 Q3 EPS를 8월 31일 이후 가격과 비교하려면 이후 분할을 반영해야 한다. 반대로 10-K에서 읽은 소급 Q3 EPS에 회계기간 이후의 분할을 다시 적용하면 이중 조정된다. [Q3 10-Q](https://www.sec.gov/Archives/edgar/data/320193/000032019320000062/aapl-20200627.htm), [10-K](https://www.sec.gov/Archives/edgar/data/320193/000032019320000096/aapl-20200926.htm).

Q3 순이익은 두 문서에서 모두 11,253백만달러다. `11,253백만 / 4,354.788백만주 = 약 2.58405달러`, 분모에 공식 비율 4를 반영하면 약 0.64601달러다. 따라서 표시 EPS `2.58 / 4 = 0.645`를 계산한 값과 나중에 보고한 0.65가 마지막 자리까지 같아야 한다는 검증도 잘못될 수 있다. EPS 표의 반올림 전 분자·분모가 우선이다.

### NVDA: 같은 공시·같은 EPS 태그에도 두 주식 수 단위가 있다

5월 29일 공시의 실제 실적 표와 예정 분할을 가정한 표는 순이익 14,881백만달러가 같고 주식 수만 10배다. 분할 주식 배분은 6월 7일 장 종료 후로 예정돼 있다. 미래 분할을 이미 반영한 것은 명확히 **미감사 pro forma** 표이며, 실제 실적 전체가 그 단위로 바뀌었다는 뜻은 아니다. [NVDA Q1 10-Q 주석 4·15](https://www.sec.gov/Archives/edgar/data/1045810/000104581024000124/nvda-20240428.htm).

원본 iXBRL에서도 둘 다 `us-gaap:EarningsPerShareDiluted` 태그다. 실제 5.98은 `contextRef=c-1`, pro forma 0.60은 `contextRef=c-169`이며 후자는 `srt:StatementScenarioAxis = srt:ProFormaMember` 차원을 갖는다. 날짜와 태그명만 남기고 context를 버리는 파이프라인은 이를 구분할 수 없다. 원본에서 추출한 45개 관련 태그·context를 `nvda_2024q1_eps_ixbrl_contexts.json`에 보존했다.

### 삼성전자: 제출일에 분할이 끝났다는 사실만으로 단위를 정할 수 없다

5월 15일 분기보고서의 연결 주당이익 주석은 보통주 귀속이익 10,205,137백만원과 가중평균 119,450천주를 써서 85,435원을 보고한다. 표지 제출일은 5월 15일이고 후속사항은 5월 3일 법적 효력, 5월 4일 변경상장 완료를 명시한다. 따라서 `report_date`까지의 분할을 이미 반영했다고 간주하면 50배 단위 오류를 놓친다. [KIND 원문: 표지, 연결 주당이익, 작성기준일 이후 사항](https://kind.krx.co.kr/external/2018/05/15/001849/20180515003807/11013.htm).

삼성전자 공식 영문 1Q 연결재무제표도 같은 값이며, 검토보고서 날짜도 5월 15일이다. 다만 이 표본에서 **재무제표 발행 승인일을 별도로 확정하지 못했다.** 회계처리의 이유나 적정성을 추측하지 않는다. 확인된 사실은 이 문서의 EPS 분모가 분할 전 주식 수라는 점이다. [삼성전자 공식 PDF, 인쇄면 2·6·53·67쪽](https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2018_con_quarter01_all.pdf).

## 회계기준과 날짜 기반 추정의 한계

IAS 33 문단 64는 주식분할·병합에 따른 주당 계산을 제시된 모든 기간에 소급 반영하도록 하며, 결산일 후 **재무제표 발행 승인 전** 발생한 변경도 다룬다. 여기서 발행 승인 시점은 데이터베이스의 공시 접수일과 동일하다고 정의돼 있지 않다. 이 문단은 미국 보고서에 적용되는 ASC 260의 대체 근거가 아니며, 미국 사례는 SEC 실제 공시로 확인했다. [IFRS Foundation IAS 33 문단 64](https://www.ifrs.org/content/dam/ifrs/publications/pdf-standards/english/2021/issued/part-a/ias-33-earnings-per-share.pdf), [IFRS Foundation EPS 개요](https://www.ifrs.org/issued-standards/list-of-standards/ias-33-earnings-per-share.html/).

`report_date`를 EPS 단위 기준일로 쓰는 것은 **그 공시의 해당 fact가 그 날짜까지의 분할만 반영한 실제 실적이라는 별도 증거가 있을 때**에만 대용값으로 안전하다. AAPL 10-K의 소급 정보에는 그 원칙을 적용할 수 있지만, 다음은 안전하지 않다.

- 삼성전자처럼 공시일 이후가 아닌 **공시일 이전에 완료된 분할도 EPS에 아직 반영되지 않은 문서**.
- NVDA처럼 같은 공시의 actual과 미래 분할 pro forma가 공존하는 경우.
- 최신 공시의 소급 비교값에 최초 보고일을 붙였거나, 원래 공시일을 잃은 데이터.
- 주식 수 단위를 맞추기 전에 EPS 누적값 차감·성장률·TTM을 계산한 데이터.

원칙적으로 필요한 것은 날짜 하나가 아니라 `source accession/receipt`, 기간의 시작·끝, actual/pro forma, basic/diluted, 증권 종류, 통화·배율, 가중평균주식 수, **해당 fact가 이미 반영한 분할 사건**이다. 수정 공시 및 소급 비교값의 사용 가능 시점도 원문별로 유지해야 한다. 이는 권고하는 데이터 계약이며 이번에 전 종목에 구현한 것이 아니다.

## Arcana NVDA 읽기 검증: 실제 단위 혼합을 재현

다음 호출을 그대로 읽기 전용으로 실행했다. 두 함수에 미국 메타데이터 경로를 명시했다. 함수 기본값은 한국 메타데이터 경로이므로, 이번 재현에서 사용한 인자를 생략한 결과와 혼동하지 않는다.

```python
read_annual_financials(
    'NVDA', market='us',
    report_metadata_path='data-lake/silver/sec/us_report_metadata.csv',
    require_report_metadata=True, use_edgartools=False,
)
read_quarterly_financials(
    'NVDA', market='us',
    report_metadata_path='data-lake/silver/sec/us_report_metadata.csv',
    require_report_metadata=True, use_edgartools=False,
)
```

| financial_period | report_date | normalized 누적 BASIC / DILUTED EPS | quarterly reader BASIC / DILUTED EPS | quarterly NI |
| --- | --- | --- | --- | ---: |
| 2024-04-28 | 2024-05-29 | 6.04 / 5.98 | 6.04 / 5.98 | 14,881,000,000달러 |
| 2024-07-28 | 2024-08-28 | 1.28 / 1.27 | **-4.76 / -4.71** | **16,599,000,000달러** |

NVDA의 로컬 회계연도 표시는 위 두 행 모두 FY2025이며 실제 종료일은 2024년이다. Q1 실제값을 읽고 있으므로 이 표본의 문제는 pro forma를 고른 것이 아니다. 수치상 `1.28 - 6.04 = -4.76`, `1.27 - 5.98 = -4.71`가 정확히 재현된다.

후속 Q2 공식 공시는 모든 주당 수치를 분할 소급 기준으로 작성했다고 명시하며, 독립 Q2 EPS는 기본 0.68·희석 0.67이다. 반면 NI 누적 차감 `31,480 - 14,881 = 16,599`백만달러는 공식 Q2 순이익과 일치한다. **이 표본의 NI 분기화는 일치하고 EPS 분기화는 틀린다.** [NVDA Q2 10-Q 주석 1·4](https://www.sec.gov/Archives/edgar/data/1045810/000104581024000264/nvda-20240728.htm).

분할 단위를 맞춘 뒤에도 누적 EPS의 차가 항상 정확한 독립 분기 EPS인 것은 아니다. 누적·분기의 가중평균 분모와 반올림이 다르기 때문이다. 위 사례에서 공식 독립 분기값을 우선해야 하며, 이번 조사에서는 수정 코드를 만들지 않았다.

## NI / 현재 시가총액을 별도 가설로 쓰는 범위

분할은 추가 자원 없이 주식 수 단위를 바꾸므로 보고 NI를 주식 수로 나누기 전의 총액은 단순 분할 자체로 바뀌지 않는 것이 원칙이다. 실제 검증한 범위는 AAPL의 동일 Q3 순익 11,253백만달러, NVDA Q1 actual/pro forma의 14,881백만달러 및 Q2 누적 차감 일치다. **전체 재무 총액·전 종목 NI의 단위·PIT 품질을 검증했다는 뜻은 아니다.** [IAS 33 문단 26~28](https://www.ifrs.org/content/dam/ifrs/publications/pdf-standards/english/2024/issued/part-a/ias-33-earnings-per-share.pdf?bypass=on).

검증된 이익을 현재 주식 수로 나눈 뒤 가격으로 나누는 지표는 다음과 같이 정의할 수 있다.

```text
current_share_earnings = disclosed_common_earnings_TTM / current_common_shares
earnings_yield_current_cap = disclosed_common_earnings_TTM / current_common_market_cap
```

이는 보고 기본·희석 EPS의 E/P와 **정의가 다르다**. 보고 EPS는 기간 가중평균 주식 수와 희석 규정을 쓰고, 위 가설은 현재 시점의 주식 수·시총을 쓴다. 자사주 취득, 증자, 희석, TTM 구성 및 종류주식 때문에 차이가 난다. 순수 분할 비율에 대해 단위를 맞추기 쉽다는 장점이 보고 EPS와 동일함을 뜻하지 않는다.

삼성전자는 특히 분자 권리범위를 주의해야 한다. 공식 Q1의 지배기업 귀속순익 11,611,833백만원은 보통주 귀속 10,205,137백만원과 우선주 귀속 1,406,696백만원으로 구분된다. `parent NI / 보통주 시총`은 우선주에 귀속되는 이익까지 보통주 분모에 얹는 별도 정의다. 보통주 귀속이익 또는 전체 종류주식을 포함한 일관된 분자·분모가 없으면 이 차이를 명시하거나 검토 대상으로 둔다. [삼성전자 연결 주당이익 주석](https://kind.krx.co.kr/external/2018/05/15/001849/20180515003807/11013.htm).

이번 전략 연구에서는 원문 단위가 입증되지 않은 EPS·주당배당·주당추정치 및 그 성장률·밸류에이션 파생값을 `source QA` 사유로 제외하는 판단을 지지한다. NI/시총 가설도 통화·배율, 연결 범위, 보통주 귀속, 현재 시총의 증권 범위, 공시 가용 시점을 확인한 데이터에 한정한다. 이 문서는 팩터 정의나 제외 목록을 실제 변경하지 않았다.

## 저장된 원문과 재현 자료

경로: `data-lake/bronze/research/financial_statements/eps/stock_split_eps_basis_samples_20260910/`.

- `aapl_2020q3_10q.html`, `aapl_2020_10k.html`: 같은 과거 Q3의 분할 전후 단위.
- `nvda_2024q1_10q.html`, `nvda_2024q2_10q.html`: actual/pro forma 구분 및 실제 Q2 검산.
- `samsung_2018q1_kind_report.html`, `samsung_2018q1_consolidated_en.pdf`: 분할 거래 후 제출됐지만 EPS는 구주 단위인 표본.
- `manifest.json`: 6개 공식 원문 URL, 다운로드 시각, SHA-256. 전부 정상 HTTP 200 응답으로 내려받았다.
- `nvda_2024q1_eps_ixbrl_contexts.json`: 관련 태그·단위·context 증거.
- `local_nvda_2024_2025_normalized_extract.csv`, `local_nvda_annual_reader_extract.csv`, `local_nvda_quarterly_reader_extract.csv`, `local_nvda_reader_audit.json`: 로컬 읽기 결과. 다른 날짜·전 종목 전체에 대한 보증이 아니다.
- `numeric_validation.json`: 분모 기반 EPS 검산, 로컬 EPS 차감 오류, NI 차감 일치.

IFRS 기준서는 공식 검색 색인에서 관련 문단을 확인했다. 원문 PDF 접근이 로그인으로 이동해 기준서 전체 파일은 저장하지 않았다. 삼성전자 규제보고서는 접근 가능한 공식 KIND 원문을 사용했고 DART 원문 API를 새로 호출하지 않았다. 발행 승인일과 모든 원천 데이터의 소급 정책이 확인된 것은 아니다.

가공·검증 자료: [stock_split_eps_basis_samples_20260910](../../data-lake/silver/research/financial_statements/eps/stock_split_eps_basis_samples_20260910). 원문은 위 bronze 표본 경로에 보존한다.
