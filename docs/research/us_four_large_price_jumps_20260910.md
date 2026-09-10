# 미국 가격 급변 4건의 공식 기업행사·Alpha Vantage 원문 조사

작성일: 2026-09-10 KST. 범위: KEEL 2019-06-14, SKYX 2022-02-10, CAPS 2019-09-19, ASTI 2018-08-17. 원장·가격 패널·코어 코드·production은 수정하지 않았다. 가격은 기존 Alpha Vantage `TIME_SERIES_DAILY_ADJUSTED`의 **원시 OHLC/거래량**과 이번에 별도로 받은 `TIME_SERIES_DAILY`만 사용했다.

**ASTI와 CAPS의 1,000주→1주 병합 누락은 공식 자료로 확인된다. 두 건 모두 화면상 가격이 크게 바뀐 날이 실제 병합 적용일은 아니다.** KEEL은 1:1 발행사 승계가 확인되지만 극소 가격의 진위를 확정하지 못했다. SKYX는 같은 발행사 보통주의 OTC→Nasdaq IPO 전환이며, 검토한 자료에서 점프에 대응하는 주식분할은 발견되지 않았다.

| 종목·관찰일 | 공식 자료의 판단 | 실제 시장 적용일 | 남은 가격·수익률 한계 |
|---|---|---|---|
| ASTI · 2018-08-17 | 구주 1,000주→신주 1주 병합 누락 | **2018-07-23** | 7/23~8/16 Alpha 행이 없음. 8/17은 ASTID→ASTI 복귀일 |
| CAPS · 2019-09-19 | 구주 1,000주→신주 1주 병합 누락 | **2019-09-10** | 9/10~9/18 공급자 가격 단위가 미해결. 별도 CVR·단주 현금도 존재 |
| SKYX · 2022-02-10 | SQFL 보통주의 Nasdaq IPO·시장 이동 | **2022-02-10** | IPO 전 극소 거래 가격의 실행 가능성 미검증. 분할 비율을 추정할 근거 없음 |
| KEEL · 2019-06-14 | 이스라엘→캐나다 발행사 주식의 1:1 교환 | 계약 실행 **2019-06-12**; OTC 삭제 **6/18**, 신주 추가 **8/15** | 6/13의 0.0001 가격이 실제 체결인지 오류인지 미확정. 1:1 교환으로 20,000배 점프가 설명되지는 않음 |

## 1. ASTI: 병합 누락과 임시 티커 구간의 가격 공백

2018-07-23 제출 8-K는 보통주를 `one-for-one thousand`로 병합했고, **7월 20일 오후 5시 Eastern Time에 효력이 발생했다**고 과거형으로 명시한다. 같은 공시의 정관은 발행·유통 중인 1,000주가 1주로 결합되는 조항을 담고 있다. 2019-04-16 제출 10-K는 실행을 재확인하며, OTC에서 병합 조정 단위로 거래한 날짜를 **2018-07-23**으로 명시한다. [8-K](https://www.sec.gov/Archives/edgar/data/1350102/000135010218000024/asti-form8xkxreversestocks.htm), [정관](https://www.sec.gov/Archives/edgar/data/1350102/000135010218000024/finalasticharteramendment-.htm), [후속 10-K](https://www.sec.gov/Archives/edgar/data/1350102/000135010219000030/asti-20181231x10k.htm).

독립적인 FINRA Daily List **ID 127113**, dividendMasterID **40112558**은 공표 2018-07-20, `exDate=2018-07-23`, `reverseSplitRate=1:1000`, `ASTI→ASTID`를 기록한다. ID **129351**은 `ASTID→ASTI` 심볼 복귀의 `exDate=2018-08-17`을 기록한다. 따라서 8/17에 새로운 병합을 만드는 것은 같은 행사를 늦은 날짜에 배치하는 오류다. [FINRA 원문·요청 본문](../../data-lake/bronze/research/stock_splits/us/us_four_large_price_jump_samples_20260910/ASTI/finra_ASTI_effective_20180701_20180731.json), [티커 복귀 원문](../../data-lake/bronze/research/stock_splits/us/us_four_large_price_jump_samples_20260910/ASTI/finra_ASTID_effective_20180801_20180831.json).

Alpha 원문에서 7/20 종가는 **0.0002**, 거래량은 **31,503,701**이다. **7/23~8/16은 행 자체가 없고**, 8/17 종가는 **0.0565**, 거래량은 **333,765**이다. 누락된 가격 기간은 공식 ASTID 임시 티커 기간과 일치한다. 기존 SPLITS에는 2014·2016·2022·2023·2024 행사만 있고 2018 행사는 없으며, 이 구간 DAILY_ADJUSTED의 split coefficient는 모두 1이다.

보통주 단위 비율 1/1000은 확정되지만 임시 티커의 누락 시세는 복구되지 않았다. 첫 관측일 8/17을 실제 병합일로 바꾸거나, 공백 전체의 투자성과가 검증됐다고 해석해서는 안 된다. 새 CUSIP은 043635507이며 단주는 올림 처리한다. 보고된 20 billion은 변동 없는 **수권주식 수**여서 병합 비율의 분모가 아니다.

**FINRA 조회 주의:** 이 2018년 행사들의 `calendarDay`는 **2018-10-25**라는 이관된 파티션이다. 실제 행사일은 `exDate`, 공개일은 `dailyListDatetime`이다. 7~8월 `calendarDay`만 조회했을 때의 HTTP 204는 행사 부재를 의미하지 않는다. 보존된 좁은 조회는 `calendarDay=2018-10-25`와 7월 `exDate` 범위를 함께 지정한다.

## 2. CAPS: 병합은 확인되지만 공급자 가격 단위와 CVR가 별도 문제

2019-08-26 제출 8-K와 정관은 구주 **1,000주를 신주 1주로** 결합하며, 정관상 효력 시점을 **2019-08-31 00:01 Eastern**으로 지정한다. 이는 시장 `exDate`와 구분해야 하는 법적 문서의 시점이다. 2025-03-31 제출 10-K는 실제로 각 1,000주가 1주가 되었다고 후속 확인한다. [8-K](https://www.sec.gov/Archives/edgar/data/887151/000117184319005659/f8k_082619.htm), [정관](https://www.sec.gov/Archives/edgar/data/887151/000117184319005659/exh_31.htm), [후속 10-K](https://www.sec.gov/Archives/edgar/data/887151/000121390025026436/ea0235933-10k_capstone.htm).

FINRA Daily List **ID 162116**, dividendMasterID **40118783**은 2019-09-09 공개, **`exDate=2019-09-10`**, 비율 **1:1000**, `CAPS→CAPSD`를 기록한다. 취소 문구가 없는 실제 시장 적용 기록이다. ID **164774**는 `CAPSD→CAPS` 복귀가 10/8임을 확인하므로, 9/19 가격 점프는 티커 복귀일도 아니다. [병합 원문](../../data-lake/bronze/research/stock_splits/us/us_four_large_price_jump_samples_20260910/CAPS/finra_oldSymbolCode_CAPS_20190801_20191031.json), [복귀 원문](../../data-lake/bronze/research/stock_splits/us/us_four_large_price_jump_samples_20260910/CAPS/finra_oldSymbolCode_CAPSD_20190901_20191031.json).

반면 Alpha의 9/9 종가는 **0.013**, 거래량 **253,817**이고, 공식 적용일 이후 **9/10~9/18의 7개 행도 모두 종가 0.013**이다. 이 행들의 거래량은 각각 113, 123, 50, 27, 41, 87, 1로 양수다. 9/19에야 종가 **14.5**, 거래량 **600**이 나타난다. 새 DAILY 조회도 이 값들을 재현한다. 기존 SPLITS 응답은 비어 있고 coefficient도 1이다. **9/10~9/18 가격의 구주·신주 단위는 해결되지 않았으므로 병합 비율만 추가한 결과를 완전히 검증된 가격으로 간주할 수 없다.** 주가 비율로 행사일이나 대체 가격을 역산하지 않았다.

별도 주주자산도 있다. 같은 8-K와 CVR 계약은 **2019-07-10 기준 주주 등의 LDI 경제적 권리**를 보존하는 계약을 설명하며 계약일은 **2019-08-23**이다. 단주는 정관에 따라 모아서 시장에 매각하고 비용 차감 후 현금 정산한다. CVR 가치와 단주 현금은 1/1000 주식 단위와 다른 항목이다. 금액을 확보하지 못한 상태에서 split-only 종가가 완전한 주주 총자산 수익률을 나타낸다고 할 수 없다. [CVR 계약](https://www.sec.gov/Archives/edgar/data/887151/000117184319005659/exh_101.htm).

**공식 문서 내부 충돌도 보존했다.** 2025-06-12 prospectus에는 2019년을 설명하는 **1-for-750** 문구가 있고, 같은 문서의 Corporate History에는 **1,000주가 1주가 됐다**는 문구가 함께 있다. 본 조사에서 채택한 발행 보통주 비율은 2019년 효력 정관·후속 10-K·독립 FINRA 기록이 일치하는 **1/1000**이다. 750 문구가 발행사에 의해 정정됐다는 증거는 찾지 못했으며, 이를 별도 행사로 추가하지 않는다. [상충 문구가 있는 prospectus](https://www.sec.gov/Archives/edgar/data/887151/000121390025053782/ea0245522-424b4_capstone.htm).

## 3. SKYX: 같은 발행사 보통주의 IPO 전후 극단적인 유동성 차이

SQL Technologies의 2017년 등록서류는 보통주가 OTC Pink **SQFL**로 호가되지만 유동적인 공개시장이 형성되지 않았다고 설명한다. 2022-02-10 IPO prospectus는 보통주 1,650,000주를 주당 **14달러**에 공모하고 종전의 확립된 공개시장이 없었다고 명시한다. 2/14 공모종료 발표는 보통주가 **2/10부터 Nasdaq에서 SKYX로 실제 거래됐다**고 확인한다. 같은 CIK 1598981과 같은 보통주 클래스의 연속성을 뒷받침하는 자료다. [2017년 OTC 등록서류](https://www.sec.gov/Archives/edgar/data/1598981/000072174817000616/sqlposam083117.htm), [IPO prospectus](https://www.sec.gov/Archives/edgar/data/1598981/000149315222003810/form-424b4.htm), [거래 개시 후 발표](https://www.sec.gov/Archives/edgar/data/1598981/000149315222004370/ex99-1.htm).

FINRA ID **227216**의 2/8 시장 이동은 ID **227222**에서 연기·복구되며, 실제 OTC 삭제 및 Nasdaq 시장 이동은 ID **227373**, **2022-02-10**이다. 이 기록은 분할 비율을 제시하지 않는다. 검토한 IPO 문서의 split·recapitalization 문구는 주식보상 제도 등의 일반 조정 조항이며 실행된 보통주 병합 공시가 아니다. **검토한 한정 자료에서 IPO 직전 실행된 분할을 찾지 못했다**는 결론이지, 모든 과거 자본변동이 없었다는 확정은 아니다. [FINRA 시장 이동·연기 원문](../../data-lake/bronze/research/stock_splits/us/us_four_large_price_jump_samples_20260910/SKYX/finra_oldSymbolCode_SQFL_20220101_20220331.json).

Alpha의 IPO 전 마지막 양수 거래량은 **2022-01-25, 0.001달러, 30주**이고 2/10은 시가 **14**, 종가 **11.85**, 거래량 **1,099,242**이다. 기존 시계열에는 2021년 100주 이하 등의 소량 관측도 있다. 이는 거래 가능성 검증이 필요한 극단적으로 희박한 OTC 이력이다. 같은 발행사의 연속성은 확인되므로 근거 없이 별도 발행사 에피소드라고 단정하지 않는다. 반대로 0.001에서 충분한 물량을 매수해 IPO 수익을 실현할 수 있었다고 가정할 증거도 없다. 분할 비율 추정·가격 대체·수익률 절삭·에피소드 분리는 수행하지 않았다.

## 4. KEEL: 1:1 승계 확인과 0.0001 체결의 미확정

Bitfarms Canada의 AIF는 **2019-06-12** Bitfarms Israel의 발행 주식을 **이스라엘 1주당 캐나다 1주**로 교환한 거래를 확인한다. FINRA의 과거 **BLLCF** 삭제 기록 ID **155938**도 같은 1:1 교환과 6/12 실행을 명시하고, OTC 삭제 `exDate`는 **6/18**이다. 캐나다 **BFARF** 추가 기록은 ID **160225**, **8/15**이다. 계약 실행·구주 시장 삭제·신주 OTC 추가는 서로 다른 사건·날짜다. [2020 AIF의 실제 Arrangement](https://www.sec.gov/Archives/edgar/data/1812477/000121390021023256/ea139842ex99-123_bitfarms.htm), [BLLCF FINRA 원문](../../data-lake/bronze/research/stock_splits/us/us_four_large_price_jump_samples_20260910/KEEL/finra_oldSymbolCode_BLLCF_20190101_20191231.json), [BFARF FINRA 원문](../../data-lake/bronze/research/stock_splits/us/us_four_large_price_jump_samples_20260910/KEEL/finra_oldSymbolCode_BFARF_20190101_20191231.json).

다른 AIF의 BFARF 월별 OTC 가격표는 **2019-08-16부터**를 대상으로 한다. 그 표의 시작일은 과거 이스라엘 주식 BLLCF의 거래 부재를 입증하지 않는다. 현재 KEEL과의 후속 연결도 2026년 10-Q에서 확인된다. 2026-04-01 미국 이전 때 Bitfarms 보통주 1주가 Keel 보통주 1주로 바뀌며 Keel이 승계 발행사가 됐다. 따라서 현재 티커에 과거 전신 이력이 포함된다는 것은 확인되지만, 이것만으로 무관한 티커 이력 오류라고 결론 내릴 수 없다. [BFARF 가격표 범위](https://www.sec.gov/Archives/edgar/data/1812477/000121390021023256/ea139842ex99-101_bitfarms.htm), [Keel 승계 10-Q](https://www.sec.gov/Archives/edgar/data/1812477/000181247726000023/keel-20260630.htm).

Alpha의 6/12 종가는 **3**, 거래량 **90**이다. **6/13은 OHLC 모두 0.0001, 거래량 441**, 6/14는 시가 2.98·고가 3·저가 2·종가 **2**, 거래량 **6,133**이다. 새 DAILY도 같은 관측을 보인다. 6/13 극소 체결의 공식 거래내역 감사 또는 발행사별 공식 가격 정정 자료는 확보하지 못했다. **실제 희박한 OTC 체결인지 공급자 오류인지 unknown**이다. 1:1 주식 교환을 20,000배 병합으로 바꾸거나 6/13 가격을 임의 보정할 근거는 없다. 전환 구간의 개별 Alpha 행이 정확히 어느 주권·시장에 해당하는지도 완전히 검증되지 않았다.

## 보존 파일과 검증 범위

증거 패키지: [폴더 manifest](../../data-lake/bronze/research/stock_splits/us/us_four_large_price_jump_samples_20260910/manifest.json), [4건 기대 필드·literal·역할·한계](../../data-lake/silver/research/stock_splits/us/us_four_large_price_jump_samples_20260910/expected_parser_fields.json), [Alpha 원문 비교](../../data-lake/silver/research/stock_splits/us/us_four_large_price_jump_samples_20260910/alpha_daily_comparison.json), [검증 결과](../../data-lake/silver/research/stock_splits/us/us_four_large_price_jump_samples_20260910/validation.json).

- 원문 **37개**: EDGAR 14, FINRA 조회 응답 11, Alpha 12. SEC filing-index 10개를 별도로 저장했다. FINRA 3개 빈 HTTP 204 응답도 원본 0바이트와 요청 본문을 그대로 남겼다.
- 각 실질 증거는 URL, 원문 SHA-256, CIK, 문서 식별자, 공개일, 로컬 경로, 역할과 짧은 exact literal을 갖는다. FINRA는 요청 본문·Accept 헤더·레코드 ID·`dailyListDatetime`·`exDate`·`calendarDay`를 구분한다. `SEC_US_*`는 Arcana 내부 현행 종목 식별자이며 역사적 CUSIP 자체가 아니다.
- SEC 공개일은 원문 filing index의 **Filing Date**로 확인했다. Alpha DAILY의 날짜는 `Last Refreshed`이며 기업행사의 당시 공개일을 대체하지 않는다. 기존 SPLITS 파일에는 원래 공개·수집 메타데이터가 없어서 `published_date=null`로 보존했다. 2026-07-26 디렉터리는 스냅샷 이름으로만 표시한다.
- 새 DAILY 4건의 최신 행은 2026-09-09다. 기존 응답과 겹치는 전 이력의 원시 OHLC·거래량을 비교했다. 정확한 숫자 표기 차이는 KEEL 233행, SKYX 10행, CAPS 83행, ASTI 1,037행이다. 모두 DAILY의 소수점 4자리 반올림 허용 범위 안이며 **거래량 차이는 없다**. 원래의 모든 차이를 JSON에 남겼다. **네 점프일과 각각 직전 양수 거래량 관측일의 종가는 정확히 일치한다.** 같은 공급자 재조회 일치는 거래소 체결 진위 검증을 대신하지 않는다.
- SHA-256, 43개 literal의 원문 존재, FINRA 병합 두 건의 정확한 1/1000 비율과 `exDate`, SEC index의 해시를 검증한다. 가격 공백·미확인 체결·미모형화 CVR의 해결을 이 검증의 성공으로 주장하지 않는다.

재생성은 이 폴더의 `archive_sources.py`로 공식 원문과 index를 보존한 뒤 `build_findings.py`를 실행한다. 두 스크립트는 이 연구 폴더에만 기록한다. 인증정보나 인증 쿼리 URL은 결과에 저장하지 않는다.

가공·검증 자료: [us_four_large_price_jump_samples_20260910](../../data-lake/silver/research/stock_splits/us/us_four_large_price_jump_samples_20260910). 원문은 위 bronze 표본 경로에 보존한다.
