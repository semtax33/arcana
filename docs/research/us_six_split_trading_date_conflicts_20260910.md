# 미국 주식분할 거래일 충돌 6건: 최종 일정과 이전 계획의 연결

조사일: 2026-09-10. AMZE, ATDS, CLRO, CREX, DVA, ESEA만 조사했다. AGEN은 제외했다. 기존 EDGAR bronze 원문을 우선 사용하고, 필요한 정관·후속 공시·발행사 발표 및 FINRA 공식 Daily List 원본을 추가로 받았다. 가격으로 비율이나 날짜를 추정하지 않았으며 코드·DB를 수정하지 않았다.

## 최종 판정

| 종목 | 정확한 신주/구주 | 최종 시장 적용일 | 법적 효력일 또는 배당 지급일 | 이전 후보 처리 |
|---|---:|---|---|---|
| AMZE | 1/8 | 2026-07-27: 발행사가 당일 NYSE American 재개 발표 | 정관·후속 10-Q: 2026-07-24 00:01 Eastern Time | 7월 27일 계획의 철회 근거 없음. 벤더 split 행 부재 원인은 미확정 |
| ATDS | 1/750 | **2019-10-29**, FINRA 최종 exDate | 정관은 제출 시 효력으로 규정; 날짜·시각 별도 확정하지 않음 | **10월 16일 최초 일정 취소** 후 재공고 |
| CLRO | 1/15 | **2025-06-10**, 후속 실행 8-K 확인 | 2025-06-09 17:00 Eastern Time | **6월 3일 계획을 대체** |
| CREX | 1/30 | **2018-10-31**, FINRA exDate | 8-K/A가 2018-10-18로 법적 효력일 정정 | 10월 25일경·늦어도 29일이라는 오래된 예측을 확정일에서 제외 |
| DVA | 2/1 | **2013-09-09**, 후속 실적 공시 확인 | 주식배당 지급일 2013-09-06; 기준일 8월 23일 유지 | **9월 23일 계획을 대체** |
| ESEA | 1/10 | **2015-07-23**, 완료 공시 확인 | 2015-07-22; 장중 시각 표현은 정관과 보도자료가 다름 | **8월 3일 계획을 대체** |

아래 원문들이 각 행의 비율·일정·이전 계획 연결 근거다. 확정 행사와 폐기할 이전 예정 후보는 [expected_parser_fields.json](us_amze_atds_date_conflict_samples_20260910/expected_parser_fields.json)에 함께 저장했다. 원본 삭제를 의미하지 않으며, 이전 예정일을 활성 가격조정 원장에 중복 적용하지 않도록 구분한 것이다.

## AMZE: 7월 24일 주식 단위 변경과 7월 27일 거래 재개

최초 8-K Item 5.03은 구주 8주를 신주 1주로 병합하고 7월 24일 00:01 Eastern Time 효력, 7월 27일 NYSE American 개장 시 거래를 예상했다. 제출 당시 Nevada 수리 대기라는 조건도 명시했다. 정관 스캔의 field 4는 정확한 8→1, field 7은 07/24/2026·12:01 AM이다. 첨부 목록의 설명에 등장하는 7월 27일을 정관 효력일보다 우선하면 안 된다. [최초 8-K](https://www.sec.gov/Archives/edgar/data/1880343/000149315226033243/form8-k.htm), [정관 원본 이미지](https://www.sec.gov/Archives/edgar/data/1880343/000149315226033243/ex3-1_001.jpg)

7월 14일 보도자료는 NYSE Regulation이 7월 13일부터 거래를 정지했다고 설명했다. 7월 27일 발행사 공식 발표는 병합 완료와 NYSE American 상장유지 요건 충족, **당일 개장 시 동일 AMZE 티커로 동일 시장 거래 재개**를 발표했다. OTC 이전이나 새 티커 전환을 설명하는 문구는 없다. [거래정지 발표](https://www.sec.gov/Archives/edgar/data/1880343/000149315226033243/ex99-1.htm), [7월 27일 공식 거래 재개 발표](https://ir.amaze.co/news-events/press-releases/detail/162/amaze-announces-full-compliance-with-nyse-american-continued-listing-standards-and-resumption-of-trading)

8월 14일 제출 10-Q Note 1과 위험요인 설명은 7월 24일 1-for-8 병합을 실제 실행했다고 재확인한다. 다만 7월 27일 재개 보도자료는 효력일을 7월 27일이라고도 표현한다. 정관과 10-Q의 **법적 단위 변경일**, 시장 재개 발표의 **거래일**을 각각 보존했다. [후속 10-Q](https://www.sec.gov/Archives/edgar/data/1880343/000149315226038506/form10-q.htm)

7월 15일 이후 제출목록에서 확인한 8월 3·18·20일 8-K는 경영진 변경 및 투자 LOI 관련이며, 이 병합의 철회·연기를 확인하지 못했다. 7월 27일 발표는 발행사의 당일 거래 재개 발표이고, 독립적인 거래소 첫 체결 시각까지 검증한 것은 아니다. Alpha DAILY의 행사 행 누락을 설명하는 벤더 처리 방식과 전체 일별 가격의 주식 단위는 이번 조사로 확정하지 않았다. 공식 병합이 실행됐다는 이유만으로 이미 조정됐을 수 있는 가격을 다시 환산하라는 결론도 아니다.

## ATDS: 같은 8-K 안에 최초 발표·취소·실행이 모두 있다

접수번호 0001493152-19-016127의 모든 첨부는 SEC에 **2019-10-30** 제출됐다. 그러나 각 보도자료 자체의 날짜와 단계는 다르다.

| 문서 | 보도자료 날짜 | 의미 |
|---|---|---|
| Exhibit 99.1 | 2019-10-15 | 10월 16일 거래 예정이라는 최초 발표 |
| Exhibit 99.2 | 2019-10-24 | FINRA가 10월 15일 Daily List에서 기존 기업행사를 제거했고 추가 검토가 필요하다는 경과 설명 |
| Exhibit 99.3 | 2019-10-29 | 병합·명칭 변경 완료 및 10월 29일 시장 적용 발표 |

본문 Item 5.03은 10월 15일 최초 공고와 같은 날 취소, 10월 28일 재공고, 10월 29일 영업 개시 적용을 연결한다. Item 7.01은 최초 보도자료가 취소 전에 발행됐다고 직접 설명한다. 따라서 10월 16일은 단순 오기가 아니라 **취소된 시장 일정**이다. [본문 8-K](https://www.sec.gov/Archives/edgar/data/1068689/000149315219016127/form8-k.htm), [최초 발표](https://www.sec.gov/Archives/edgar/data/1068689/000149315219016127/ex99-1.htm), [취소 경과](https://www.sec.gov/Archives/edgar/data/1068689/000149315219016127/ex99-2.htm), [최종 실행 발표](https://www.sec.gov/Archives/edgar/data/1068689/000149315219016127/ex99-3.htm)

FINRA 공식 API에서도 다음 네 행을 받았다. 최초와 취소 행의 동일 `dividendMasterID`가 취소 연결을 뒷받침한다.

| OTCDailyListID | 게시 시각 dailyListDatetime | exDate | 이벤트·연결 |
|---:|---|---|---|
| 165103 | 2019-10-15 00:00:00 | 2019-10-16 | DA, 1:750, dividendMasterID 40119287 |
| 165114 | 2019-10-15 10:32:21 | 2019-10-15 10:32:21 | SC, commentText=cancelled |
| 165116 | 2019-10-15 10:33:06 | 2019-10-16 | DD, commentText=cancelled, **동일 dividendMasterID 40119287** |
| 165774 | 2019-10-28 12:02:05 | **2019-10-29** | 최종 DA, 1:750, dividendMasterID 40119432 |

자료: [저장한 FINRA 원본 응답](us_amze_atds_date_conflict_samples_20260910/ATDS/finra_ldsr_daily_list_201910_201911.json), [FINRA 공식 OTC Daily List API 문서](https://developer.finra.org/docs). 요청 본문·공식 URL·SHA는 원본 옆 `.metadata.json`에 있다. 이벤트 코드 하나만으로 일반적인 취소 규칙을 추론하지 않고, 이 사례의 취소 문구·연결 식별자·issuer 공시를 함께 확인했다.

정관의 보통주 집행 조항도 750→1을 명시한다. 단주는 정수 주식으로 올리며 현금을 주지 않는다. 거래 기호는 최초 LDSR, 최종 적용일로부터 20영업일 동안 LDSRD, 이후 ATDS로 공시됐다. 정관의 선택적 효력일 칸은 비어 있어 시장 적용일과 별개의 법적 효력 날짜·시각은 채우지 않았다. [정관 본문](https://www.sec.gov/Archives/edgar/data/1068689/000149315219016127/ex3-1_003.jpg), [정관 날짜 칸](https://www.sec.gov/Archives/edgar/data/1068689/000149315219016127/ex3-1_002.jpg)

## CLRO: 6월 3일 예정을 6월 10일로 변경

5월 21일 최초 공시는 주주 승인 조건부로 6월 2일 장후 효력과 6월 3일 시장 적용을 제시했다. 6월 2일 후속 공시는 그 이전 계획을 직접 언급한 뒤 **6월 9일 17:00 Eastern Time 효력·6월 10일 개장 적용**으로 바꾼다. 같은 접수번호의 최종 정관은 정확한 15→1과 6월 9일 시각을 명시한다. [최초 발표](https://www.sec.gov/Archives/edgar/data/840715/000175392625000875/ex991_1.htm), [일정 변경 발표](https://www.sec.gov/Archives/edgar/data/840715/000175392625000914/ex991_2.htm), [변경 8-K](https://www.sec.gov/Archives/edgar/data/840715/000175392625000914/clro-20250602.htm), [정관](https://www.sec.gov/Archives/edgar/data/840715/000175392625000914/ex31_1.htm)

6월 25일 별도 8-K는 6월 10일 개장 시 1-for-15 병합을 실제 실행했다고 확인한다. 따라서 6월 3일과 6월 10일을 별도 병합 두 건으로 저장하면 안 된다. [실행 이후 8-K, Item 1.01의 Nasdaq 설명](https://www.sec.gov/Archives/edgar/data/840715/000175392625001008/clro-20250625.htm)

## CREX: 법적 효력 정정과 FINRA 시장 적용일을 분리

11월 9일 8-K/A의 Explanatory Note는 최초 8-K를 수정하는 목적이 **법적 효력일을 10월 18일로 명확히 하는 것**이라고 한정한다. 그런데 본문에는 10월 25일경, 늦어도 29일이라는 기존 거래 예측이 남아 있다. 최신 제출 공시에 있다는 이유로 이 예측을 실제 거래일로 사용할 수 없다. [최초 8-K](https://www.sec.gov/Archives/edgar/data/1356093/000121390018014348/f8k101718_creativerealities.htm), [법적 효력일 정정 8-K/A](https://www.sec.gov/Archives/edgar/data/1356093/000121390018015267/f8k101818a1_creativereal.htm)

FINRA 원본의 `OTCDailyListID=135638`은 `reverseSplitRate=1:30`, **`exDate=2018-10-31 00:00:00.0`**, 게시 시각 `2018-10-30 13:11:23.0`을 명시한다. 종목은 CREX→CREXD, 사유는 Reverse Split/CUSIP Change다. 같은 행의 `calendarDay=2018-10-25`는 실제 적용일이 아니다. 공식 metadata는 `calendarDay`를 파티션 필드로 지정하고 장중 종목 변경의 달력 날짜로 설명하며, `exDate`를 Effective/Ex Date/Time으로 별도 정의한다. [FINRA 원본](us_amze_atds_date_conflict_samples_20260910/CREX/finra_crex_daily_list_201810_201811.json), [공식 필드 metadata 원본](us_amze_atds_date_conflict_samples_20260910/CREX/finra_otcdailylist_metadata.json), [공식 API 문서](https://developer.finra.org/docs)

10월 25일 후보를 대체할 근거는 확보됐지만, 이 사건을 별도로 취소했다고 표현한 issuer 문구까지 찾은 것은 아니다. JSON에는 **오래된 예정일을 FINRA의 최종 exDate로 대체**하는 것으로 표시했다.

## DVA: 지급일과 거래일을 앞당긴 동일 주식배당

8월 12일 발표는 two-for-one 주식배당의 기준일 8월 23일, 지급일 9월 20일, 거래일 9월 23일을 제시했다. 8월 23일 업데이트는 이전 two-for-one 발표의 **일정 변경**임을 직접 밝히고, 기준일을 유지하면서 지급일을 **9월 6일**, 거래일을 **9월 9일**로 앞당겼다. 이 업데이트의 SEC 제출일은 8월 27일이다. [최초 발표](https://www.sec.gov/Archives/edgar/data/927066/000119312513331018/d581496dex991.htm), [변경 발표](https://www.sec.gov/Archives/edgar/data/927066/000119312513348117/d588850dex991.htm), [변경을 첨부한 8-K](https://www.sec.gov/Archives/edgar/data/927066/000119312513348117/d588850d8k.htm)

11월 5일 실적 공시는 9월 6일 지급과 **9월 9일 실제 주식분할 반영 거래 개시**를 과거형으로 확인한다. 여기서는 신주/구주 2/1의 주식분할이며, 최초 발표의 수령 주식 문장을 기존 주식에 두 주를 추가하는 3/1로 해석하면 안 된다. [3분기 실적 공시의 Stock split 항목](https://www.sec.gov/Archives/edgar/data/927066/000119312513427692/d622230dex991.htm)

## ESEA: 8월 3일 예정을 7월 23일로 변경

7월 2일 최초 6-K는 1-for-10 병합을 7월 31일 장후 효력·8월 3일 거래로 예정했다. 7월 6일 별도 6-K는 **7월 2일 발표한 병합의 효력일을 7월 22일로 재설정**했다고 명시하고, 7월 23일 거래를 제시한다. 7월 23일 완료 6-K는 병합 완료와 7월 22일 장후 효력·7월 23일 시장 적용을 확인한다. [최초 발표](https://www.sec.gov/Archives/edgar/data/1341170/000091957415005205/d6686096_6-k.htm), [일정 재설정 발표](https://www.sec.gov/Archives/edgar/data/1341170/000131786115000049/f070615esea6k.htm), [완료 발표](https://www.sec.gov/Archives/edgar/data/1341170/000131786115000052/f072315esea6k.htm)

7월 22일 제출 정관의 비율도 1-for-10이다. 단, 정관은 그날 **영업 개시 시**, 보도자료는 **거래 종료 후**로 효력 시점을 표현한다. 날짜와 다음 날 시장 적용에는 충돌이 없지만, 정확한 법적 장중 시각은 JSON에서 미확정으로 남겼다. 최초 6-K에 함께 실린 신주인수권 공모·선박 용선 발표는 병합의 비율·일정과 분리한다. [정관 6-K의 새 paragraph (d)](https://www.sec.gov/Archives/edgar/data/1341170/000091957415005504/d6719836_6-k.htm)

## 저장물과 재현 범위

- [manifest](us_amze_atds_date_conflict_samples_20260910/manifest.json): 원본 **37건**. 개별 URL, SHA256, 접수번호·문서 식별자, 실제 SEC 제출일, CIK, security_id 포함. issuer IR와 FINRA는 EDGAR로 가장하지 않고 provider를 구분했다.
- [expected_parser_fields](us_amze_atds_date_conflict_samples_20260910/expected_parser_fields.json): 6개 사건, 취소·대체할 후보, 최종 비율·시장 적용일, 법적 효력일의 한계, 원문 위치와 짧은 정확 문구, FINRA 레코드 필드.
- [filing_metadata_provenance](us_amze_atds_date_conflict_samples_20260910/filing_metadata_provenance.json): **SEC filing index 19건**에서 제출일을 확인한 근거. 보도자료 작성일·기준일·효력일과 구분했다.
- [validation](us_amze_atds_date_conflict_samples_20260910/validation.json): 37개 원본 해시, HTML 발췌 **45개**, 스캔 원문 발췌 **5개**의 육안 확인, FINRA **5개 레코드**의 선택 필드 일치 검증.

FINRA는 공식 개발자 문서가 안내하는 공개 Query API를 사용했다. CREX 한 종목의 2018년 10~11월, LDSR 한 종목의 2019년 10~11월을 제한 조회했다. POST 요청은 읽기용 데이터 질의이며 payload는 메타데이터에 저장했다. FINRA 배치 응답의 `published_date`는 포함된 행의 가장 늦은 게시일로 표시했으며, 개별 행의 시점 판단에는 `dailyListDatetime`을 사용해야 한다. 원문 응답이나 과거 후보를 삭제하지 않았다.

이 검증은 소스 계약과 일정 검증이다. 애플리케이션 파서 실행 테스트, 벤더 데이터 수정, 가격 환산 검증을 수행했다는 뜻은 아니다.
