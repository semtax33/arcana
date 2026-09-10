# 미국 주식분할 거래일 충돌 8건: 공식 실행일과 벤더 가격 단위의 구분

조사일: 2026-09-10. JXG, PMI, POCI, RDGL, SHIP, SMTK, SPRB, WLFC만 조사했다. EDGAR 원문과 FINRA 공식 OTC Daily List를 사용했다. 원본 **39건**, SEC 접수일 확인용 filing index **21건**을 보존했고, 짧은 원문 문구 **57개**와 FINRA 선택 레코드 **4개**를 검증했다. 가격에서 비율이나 행사일을 추정하지 않았으며 파이프라인·원장·가격·DB를 변경하지 않았다.

## 판정

비율은 **신주/구주**다. 시장 적용일과 법적 주식 단위 변경일을 따로 기록한다.

| 종목 | 정확한 비율 | 공식 시장 적용일 | 이전 후보와 벤더 차이 | 남은 가격 검증 |
|---|---:|---|---|---|
| JXG, 당시 KBSF | 1/15 | **2017-02-09** | 2월 8일 예정을 후속 실제 거래 서술로 대체 | 행사일 재적용 후 일반 검증 |
| PMI | 1/50 | **8월 3일은 공시상 예정일** | 정관의 7월 31일 17:00 EDT 단위 변경과 후속 실제 실행은 확인 | 실제 8월 3일 거래를 명시한 공식 문서는 미확보. 정관·실행·첫 실제 가격의 연결 검증 필요 |
| POCI, 당시 PEYE | 1/3 | **2022-11-02** | 최초 10월 27일 계획 취소. Alpha는 11월 3일 | 공식일과 벤더 가격 단위가 하루 어긋나는 문제 별도 확인 |
| RDGL | 1/8 | **2019-06-28** | 6월 26일 발표를 명시적으로 정정 | 행사일 재적용 후 일반 검증 |
| SHIP | 1/15 | **2011-06-27** | Alpha는 6월 28일 | Alpha 일별·시간별 원시 가격이 직접 충돌. 전체 과거 가격 검증 완료로 볼 수 없음 |
| SMTK | 1/35 | **2023-09-21** | 최초 9월 20일 일정과 정관 날짜를 정정. Alpha 행사 행 없음 | 해당일 원시 가격 행 부재, 최초 후속 가격 단위 확인 필요 |
| SPRB | 1/75 | **2025-08-07** | 8월 5일 예정을 FINRA와 후속 실행 자료로 대체 | 임시 SPRBD·이후 Nasdaq 복귀를 추가 병합으로 중복 처리하지 않음 |
| WLFC | 3/1 | **2026-07-21** | 최초 7월 20일 예정을 갱신 | 7월 17일 장후 법적 효력과 구별 |

## JXG: KBSF의 2월 8일 예상과 2월 9일 실제 거래

2017년 2월 3일 보도자료는 최종 1-for-15를 승인하고 2월 8일 Nasdaq 개장부터 적용할 계획이라고 설명한다. 그러나 2020년 20-F는 **2월 9일 실제로 분할 반영 거래를 시작했다**고 반복해서 명시하며, Nasdaq의 최소 호가 회복 확인 기간도 2월 9일부터 연결한다. 따라서 2월 8일 후보는 실행 원장에서 대체할 오래된 예상일이다. 별도의 명시적인 취소 공시는 찾지 못했으므로 “취소 발표 확보”라고 표현하지 않는다. [최초 발표](https://www.sec.gov/Archives/edgar/data/1546383/000114420417005951/v458403_ex99-2.htm), [후속 실제 거래 확인 20-F](https://www.sec.gov/Archives/edgar/data/1546383/000121390021026934/f20f2020_kbsfashion.htm)

2024년 20-F의 Item 9.A는 KBSF → LLL → JXJT → JXG의 동일 보통주 티커 이력을 설명한다. 현재 JXG와 과거 KBSF를 연결하는 근거를 함께 저장했다. 이 조사에서는 별도의 법적 효력일을 확정하지 않았다. [티커 이력](https://www.sec.gov/Archives/edgar/data/1546383/000121390025043744/ea0239227-20f_jxluxven.htm)

## PMI: 실제 병합은 확인되지만 실제 시장 거래일 확인은 별도

정관 Section 2의 주식 단위 조항은 **2026년 7월 31일 17:00 Eastern Daylight Time**, 구주 50주를 신주 1주로 전환하도록 명시한다. Section 4의 “제출 시 정관 수정 효력”과 Section 2의 미래 주식 단위 변경 시각은 서로 다른 규정이다. 후속 10-Q는 7월 31일 병합이 실제 완료됐으며 50주가 1주로 변환됐다고 확인한다. 다만 후속 10-Q는 시각을 반복하지 않는다. [정관 Section 2](https://www.sec.gov/Archives/edgar/data/2030617/000143774926024025/ex_991235.htm), [실제 실행 10-Q Note 1·후속사건](https://www.sec.gov/Archives/edgar/data/2030617/000143774926028565/pmi20260630_10q.htm)

8월 3일 개장부터 기존 NYSE American의 PMI 티커로 거래한다는 문구는 최초 8-K의 미래형이다. 실제 그날 거래를 시작했다거나 거래를 재개했다는 후속 공식 문서는 확보하지 못했다. 따라서 JSON의 `first_split_adjusted_trading_date`는 null이고 `announced_first_split_adjusted_trading_date`는 2026-08-03이다. 정관·후속 실행·그 이후 첫 양의 거래량 가격을 연결하는 검증은 본체 작업에 남겼다. 시장 재개 증거로 역할을 바꾸지 않았다. [최초 8-K Item 8.01](https://www.sec.gov/Archives/edgar/data/2030617/000143774926024025/pmi20260721_8k.htm)

IPO 증권신고서 표지의 문서 날짜는 2025년 8월 29일이고 SEC 접수일은 9월 2일이다. 옛 PMI Group과 구별할 자료로 보존했다. 본체가 확인한 Alpha 가격은 2025년 8월 29일부터 시작하여 옛 보험사 장기 시계열은 아니었다. 마지막 사실은 본체의 가격 점검 결과이고 이번 공식 소스 검증과 구분한다. [Picard IPO 증권신고서](https://www.sec.gov/Archives/edgar/data/2030617/000182912625006957/picardmedical_424b4.htm)

## POCI: 10월 27일 계획 취소, 11월 2일 실제 적용

접수번호 0001683168-22-007213에 최초 계획, 지연 발표, 최종 실행 발표가 함께 첨부되어 있다. 본문은 10월 26일 효력 예정이던 정관 수정을 다시 덮어써 그날 병합이 발생하지 않게 했고, **11월 1일 23:59 Eastern Time**으로 다시 정했다고 설명한다. 이후 문단은 **11월 2일 OTCQB에서 실제 분할 반영 거래를 시작했다**고 명시한다. [본문 8-K](https://www.sec.gov/Archives/edgar/data/867840/000168316822007213/poci_8k.htm), [명시적인 지연 발표](https://www.sec.gov/Archives/edgar/data/867840/000168316822007213/poci_ex9902.htm), [최종 1-for-3 실행 발표](https://www.sec.gov/Archives/edgar/data/867840/000168316822007213/poci_ex9903.htm)

FINRA의 `OTCDailyListID=247068`, `exDate=2022-11-02`, `reverseSplitRate=1:3`, PEYE→PEYED가 동일 날짜와 비율을 독립 확인한다. PEYED는 당시 임시 기호이며 원 공시는 Nasdaq 이전 시 POCI로 바뀔 기호를 설명한다. Alpha의 11월 3일 행사 행을 공식일로 이동시키는 것만으로 모든 가격 단위가 고쳐진다고 볼 수 없다. 본체는 11월 2일의 양의 거래량 일별 종가가 아직 구단위로 보이는 문제를 별도 점검 중이다. [FINRA 원본](us_eight_remaining_date_conflict_samples_20260910/POCI/finra_peye_20221001_20221130.json), [최초 발표의 티커 연결](https://www.sec.gov/Archives/edgar/data/867840/000168316822007213/poci_ex9901.htm)

## RDGL: 6월 26일을 6월 28일로 명시 정정

6월 27일 정정 보도자료는 6월 26일 발표를 고친다고 명시하고 **6월 28일 시장 개장**으로 변경한다. 후속 본문 8-K는 법적 효력 6월 25일 23:59 Eastern Time, FINRA 승인 6월 27일, 실제 시장 적용 6월 28일을 분리한다. FINRA 레코드 `156947`도 `exDate=2019-06-28`, `1:8`, RDGL→RDGLD다. 같은 병합을 6월 26일과 28일에 두 번 적용하면 안 된다. [정정 발표](https://www.sec.gov/Archives/edgar/data/1449349/000149315219010114/ex99-2.htm), [실제 실행 8-K](https://www.sec.gov/Archives/edgar/data/1449349/000149315219010114/form8-k.htm), [FINRA 원본](us_eight_remaining_date_conflict_samples_20260910/RDGL/finra_rdgl_20190601_20190731.json)

## SHIP: 공식 거래일은 6월 27일, Alpha 내부 가격 충돌은 별도 문제

6월 23일 최초 6-K는 1-for-15 병합의 법적 효력일을 6월 24일, 거래 시작을 6월 27일 Nasdaq 개장으로 정했다. 8월 9일 후속 실적 6-K는 **6월 27일 실제 분할 반영 거래를 개시했다**고 과거형으로 확인한다. 6월 28일에 별도로 두 번째 병합을 했다는 근거는 없다. [최초 발표](https://www.sec.gov/Archives/edgar/data/1448397/000091957411003908/d1207198_6-k.htm), [정관](https://www.sec.gov/Archives/edgar/data/1448397/000091957411003954/d1207693_6-k.htm), [후속 실제 거래 확인](https://www.sec.gov/Archives/edgar/data/1448397/000091957411004401/d1218694_6-k.htm)

본체의 별도 Alpha 점검에서는 일별 6월 27일 종가 0.3488·거래량 18,000과, `adjusted=false` 시간별 6월 27일 14:00 가격 6.0·거래량 1,119가 충돌했다. 이를 가격으로 비율을 도출한 증거로 사용하지 않는다. 공식 행사일은 확인됐지만 공급자 원시 가격의 날짜별 주식 단위는 별도 QA 대상이다. 이 사건은 2017년 이후 전략 평가 기간 이전이지만 전체 과거 가격의 검증 완료를 주장할 수 없게 한다. [본체가 받은 Alpha 시간별 원본](../../deliverables/cross_market_top70_20260909/corrected/price_source_checks/SHIP_TIME_SERIES_INTRADAY_2011-06.json)

## SMTK: 9월 21일 실제 시장 적용, “거래 재개”라는 뜻은 아님

최초 발표의 9월 20일은 정정 발표와 Certificate of Correction에서 **9월 21일**로 바뀐다. 수정 정관의 단위 변경 시각은 00:01 Eastern Time이다. 최종 비율은 1-for-35이며, 후속 감사된 10-K의 주식분할 주석은 9월 21일 실제 실행을 확인한다. [최종 비율 8-K](https://www.sec.gov/Archives/edgar/data/1817760/000110465923102289/tm2326434d1_8k.htm), [정정 발표](https://www.sec.gov/Archives/edgar/data/1817760/000110465923102289/tm2326434d1_ex99-2.htm), [정관 날짜 정정](https://www.sec.gov/Archives/edgar/data/1817760/000110465923102289/tm2326434d1_ex3-2.htm), [실행 확인 10-K](https://www.sec.gov/Archives/edgar/data/1817760/000155837024004098/smtk-20231231x10k.htm)

FINRA 레코드 `268874`는 `exDate=2023-09-21`, `1:35`, SMTK→SMTKD다. 발행사 보도자료가 기존 SMTK 기호라고 설명한 것과 FINRA의 임시 D 표기를 모두 보존했다. 이는 동일 보통주의 시장 적용 확인이며 거래 정지 후 재개라는 별도 사실을 뜻하지 않는다. Alpha에는 해당 행사 행과 9월 21·22일 가격 행이 없고, 본체가 발견한 첫 후속 9월 25일 가격도 추가 점검이 필요하다. [FINRA 원본](us_eight_remaining_date_conflict_samples_20260910/SMTK/finra_smtk_20230901_20231031.json)

## SPRB: 8월 5일 예상과 8월 7일 실제 OTCQB 거래

7월 24일 발표는 8월 4일 17:00 Eastern Time 효력·8월 5일 거래를 예상했지만, 8월 14일 실적 발표와 2025년 10-K는 **8월 7일 실제 OTCQB 분할 반영 거래 개시**를 명시한다. FINRA 레코드 `310719`도 `exDate=2025-08-07`, `1:75`, SPRB→SPRBD다. 별도 취소 발표를 찾았다고 주장하지 않고, 예정일을 독립적인 규제기관 자료와 후속 실제 거래 확인으로 대체한다. [최초 일정](https://www.sec.gov/Archives/edgar/data/1683553/000095017025098340/sprb-ex99_1.htm), [후속 실행 발표](https://www.sec.gov/Archives/edgar/data/1683553/000095017025108868/sprb-ex99_1.htm), [후속 10-K](https://www.sec.gov/Archives/edgar/data/1683553/000119312526097558/sprb-20251231.htm), [좁은 기간 FINRA 원본](us_eight_remaining_date_conflict_samples_20260910/SPRB/finra_sprb_20250701_20250831.json)

처음 조회한 7~9월 FINRA 응답은 뒤의 기호 변경·Nasdaq 복귀 관련 행도 포함하여 원본으로 보존했다. 설치 proof에는 **7~8월의 단일 행사 행 응답**을 사용했다. 해당 source의 게시일 8월 6일과 sidecar 게시일이 일치하며, 뒤의 기호 변경을 추가 병합으로 세지 않는다.

## WLFC: 7월 17일 장후 분할, 7월 21일 실제 거래

6월 23일 발표는 7월 20일 개장 적용을 예상했다. 7월 10일 8-K가 예상 거래일을 7월 21일경으로 바꾸며, 후속 10-Q가 **7월 21일 실제 분할 반영 거래 개시**를 확인한다. 비율은 3-for-1이다. 7월 17일 정관은 구주 한 주를 신주 세 주로 나누고 **7월 17일 16:05 Eastern Time**에 효력이 생긴다고 명시한다. 법적 효력일과 시장 적용일을 합치지 않는다. [최초 일정](https://www.sec.gov/Archives/edgar/data/1018164/000119312526279754/d129925dex991.htm), [일정 갱신](https://www.sec.gov/Archives/edgar/data/1018164/000119312526300912/d142690d8k.htm), [최종 정관](https://www.sec.gov/Archives/edgar/data/1018164/000119312526307870/d85905dex31.htm), [후속 실제 거래 확인](https://www.sec.gov/Archives/edgar/data/1018164/000101816426000068/wlfc-20260630.htm)

## 저장물과 검증 범위

- [manifest.json](us_eight_remaining_date_conflict_samples_20260910/manifest.json): 원본 URL·SHA256·접수번호·CIK·실제 SEC 접수일·security_id·원문 경로. FINRA는 provider와 POST 요청 본문을 별도로 기록했다.
- [expected_parser_fields.json](us_eight_remaining_date_conflict_samples_20260910/expected_parser_fields.json): 8개 사건별 정확한 비율, 실제·예정 거래일, 법적 효력, 증거 역할, 짧은 literal marker, 대체할 과거 후보의 SHA와 정정·실제 확인 근거, 남은 한계.
- [filing_metadata_provenance.json](us_eight_remaining_date_conflict_samples_20260910/filing_metadata_provenance.json): SEC 접수일을 보도자료 작성일·효력일과 구별한 filing index 21건.
- [validation.json](us_eight_remaining_date_conflict_samples_20260910/validation.json): 39개 source hash와 신원 필드, 57개 literal, 4개 FINRA 레코드 필드 검사 통과. 가격 환산 검증은 false로 명시했다.

FINRA 조회는 [공식 공개 Query API](https://developer.finra.org/docs)의 읽기용 POST 요청이다. `calendarDay`는 조회 파티션이고 사건의 시장 적용일은 `exDate`다. 여기서 검증한 4개 선택 행은 active DA이며 취소 문구가 없다. 원문·과거 후보는 보존하며, 이 조사가 벤더 가격의 단위 수정이나 실제 파이프라인 실행을 완료했다는 뜻은 아니다.
