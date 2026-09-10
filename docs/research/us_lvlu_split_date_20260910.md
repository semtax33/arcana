# LVLU 병합 최종 거래일 검증

조사일: 2026-09-10. 대상: `SEC_US_LVLU`, CIK `1780201`, Lulu’s Fashion Lounge Holdings, Inc. 해당 CIK의 기존 SEC corpus 원문 5개만 사용했다. 가격으로 날짜·비율을 추정하지 않았으며 코드·원장·DB를 변경하지 않았다.

**확정 결과: 보통주 구주 15주 → 신주 1주, 실제 병합 단위 거래일은 2025-07-07이다.** 최초 2025-06-30은 대체된 예정일이다. 법적 정관 효력은 2025-07-03 오후 5시 미국 동부시간으로, 거래 적용일과 다르다. 비율은 최종 정관, 거래일은 이후 실행을 확인한 공시까지 일치한다. [최종 정관](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008916/tmb-20250623xex3d2.htm), [8월 실행 확인](https://www.sec.gov/Archives/edgar/data/1780201/000155837025011376/tmb-20250813xex99d1.htm).

| 제출일 | 접수번호·원문 | 판단에 사용한 내용 |
|---|---|---|
| 2025-06-12 | `0001558370-25-008550`, [8-K Item 3.01](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008550/tmb-20250606x8k.htm) | 이사회가 1-for-15를 선택했으며 6-30 개장부터 거래할 것으로 예상. 아직 정관 제출 의향을 서술한 단계 |
| 2025-06-26 | `0001558370-25-008916`, [8-K Item 5.03](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008916/tmb-20250623x8k.htm) | 같은 6-10 주총 승인과 6-11 이사회 선택에 따라 정관을 제출. 법적 효력 7-03 17:00, 시장 거래 7-07 개장 |
| 2025-06-26 | 같은 접수번호, [Exhibit 3.2](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008916/tmb-20250623xex3d2.htm) | 최종 서명 정관: 기존 보통주 fifteen shares를 one share로 병합, 법적 효력 시각 명시 |
| 2025-06-26 | 같은 접수번호, [Exhibit 99.1](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008916/tmb-20250623xex99d1.htm) | 대외 발표도 1-for-15와 거래 7-07로 일치 |
| 2025-08-13 | `0001558370-25-011376`, [Exhibit 99.1 재무표 주석 (1)](https://www.sec.gov/Archives/edgar/data/1780201/000155837025011376/tmb-20250813xex99d1.htm) | 실제 7-07 개장에 효력이 발생한 병합을 재무표에 반영했다고 과거형으로 확인 |

마지막 원문의 직접 실행 증거는 “1-for-15 reverse stock split that became effective as of the opening of business on July 7, 2025.”이다. 이후 실적 공시의 회고적 확인이므로 최종 거래일을 단순 미래 예정일 수준으로 남길 이유가 없다. 다만 거래소 첫 체결 tick을 별도로 조회한 검증은 아니다. [8월 실행 확인](https://www.sec.gov/Archives/edgar/data/1780201/000155837025011376/tmb-20250813xex99d1.htm).

## 이전 계획과 연결하는 방법

검토한 5개 문서에는 6-30에서 연기됐다는 직접 문장이나 별도 8-K/A가 없다. 따라서 원래 사건을 “명시적으로 취소됐다”고 표현하지 않는다. 같은 발행사·보통주·6-10 주총 승인·이사회가 선택한 1-for-15가 후속 정관과 실행 확인에 이어지는 점을 근거로 **기존 예정일을 같은 사건의 최종 일정으로 대체한다는 분석적 연결**이다. 원장에는 6-30과 7-07 두 건을 중복 적용해서는 안 된다. 이전 후보는 `superseded_schedule`로 보존하고 7-07에 1/15를 한 번만 적용할 수 있다. [초기 계획](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008550/tmb-20250606x8k.htm), [최종 일정·동일 승인 근거](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008916/tmb-20250623x8k.htm).

다음 숫자·날짜는 별도로 구분해야 한다.

- 1-for-2부터 1-for-22까지는 주주가 승인한 이사회 선택 범위이며 실행 비율이 아니다. 최종 선택과 정관은 1-for-15다.
- 6-10 Nasdaq Global Market → Capital Market 이전은 상장 시장 구분의 변경이다. 병합 거래일로 쓰지 않는다.
- 후속 8-K의 표지상 6-23은 별도 금융기관 forbearance 계약일이다. 정관 제출일 6-26, 정관 효력일 7-03, 거래일 7-07과 구분한다.
- 발행가능 보통주 250,000,000주는 변경되지 않는다. 이 숫자나 대략적인 전후 발행 총수에서 비율을 산출하지 않는다.

앞의 두 항목은 [초기 8-K](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008550/tmb-20250606x8k.htm), 뒤의 두 항목은 [후속 8-K](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008916/tmb-20250623x8k.htm)에 근거한다.

병합 후 티커는 LVLU 유지, CUSIP은 `55003A207`, 보통주 액면가는 $0.001 유지다. 단수주는 정수 1주로 올림하며 현금을 지급하지 않는다. 신주 1주/구주 15주가 정확한 계약상 주식수 비율이다. [후속 8-K 및 정관](https://www.sec.gov/Archives/edgar/data/1780201/000155837025008916/tmb-20250623x8k.htm).

## 보존 증거

폴더: `data-lake/bronze/research/stock_splits/us/us_lvlu_split_date_samples_20260910/`.

- `manifest.json`: 원문 5개, CIK·접수번호·문서 ID·제출일·security_id·공식 URL·SHA-256·원래 corpus 위치·복사 위치.
- `disclosures/`: 원문 그대로 복사한 HTML와 메타데이터. 제출일은 기존 SEC 검색 메타데이터 및 공시 서명·발표 날짜로 확인했다.
- `expected_parser_fields.json`: 정확한 15→1, 7-07 거래일, 7-03 법적 효력, 6-30 계획의 대체 관계, 원문 literal 11개와 텍스트 위치.
- `validation.json`: 원문 SHA 및 필수 메타데이터·literal 검증 결과.

Alpha Vantage와의 비교는 이번 조사에서 수행하지 않았다. 본 자료는 공식 행사 단위와 최종 날짜를 제공하며, 벤더 가격에 이미 반영됐는지는 별도 가격 단위 QA 사항이다.

가공·검증 자료: [us_lvlu_split_date_samples_20260910](../../data-lake/silver/research/stock_splits/us/us_lvlu_split_date_samples_20260910). 원문은 위 bronze 표본 경로에 보존한다.
