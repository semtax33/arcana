# DART 주식분할·주식병합 수집 및 가격 조정 계약

- 확인일: 2026-09-09
- 범위: 공식 DART 공시에서 단순 주식분할·주식병합의 비율과 거래 적용일을 추출하는 방법. 회사분할·합병·주식교환, 감자, 유무상증자, 배당 총수익 조정은 별도 사건이다.
- 검증: 공식 API 개발가이드와 실제 비인증 DART 검색·본문 HTTP 응답을 확인했다. 원문 표본은 `tests/fixtures/stock_splits/`에 원래 바이트와 인코딩을 보존했다. 이 문서 작성 과정에서 DB나 애플리케이션 코드는 변경하지 않았다.

## 적용할 원칙

가격 조정 사건은 **증권별 신주/구주 비율과 변경된 단위로 처음 거래하는 날짜**를 가져야 한다. 이사회 결의일, 주주총회일, 법적 효력일을 거래 적용일로 대신 쓰지 않는다. 공시는 원문과 정정 계보를 모두 보관하고, 최종 본문을 파싱한 뒤 실제 변경상장·거래정지해제 공시로 날짜를 확인한다. 다음은 확인된 원문에 근거한 구현 제안이며 DART가 제공하는 표준 데이터 스키마는 아니다.

## 공식 수집 경로

### 인증키가 있는 경우

| 기능 | 경로·최소 인자 | 수집에 필요한 의미 |
| --- | --- | --- |
| 목록 | `GET https://opendart.fss.or.kr/api/list.json`, `crtfc_key`, 기간, 페이지 | `bgn_de/end_de`는 접수일. `corp_code`가 없으면 최대 3개월 검색. `page_count=100`, 모든 `page_no` 순회. `last_reprt_at=N`으로 원공시·정정을 함께 저장한다. |
| 원문 | `GET https://opendart.fss.or.kr/api/document.xml`, `crtfc_key`, `rcept_no` | `.xml` URL이지만 정상 결과는 ZIP 바이너리다. 오류 XML과 ZIP을 구별한다. |
| 기업 식별 | `GET https://opendart.fss.or.kr/api/corpCode.xml`, `crtfc_key` | DART 고유번호와 종목코드의 공식 매핑 원본을 함께 보관한다. |

목록의 거래소공시는 `pblntf_ty=I`이며 수시공시와 시장조치/안내는 각각 `I001`, `I003`이다. 문서화된 목록 API에는 보고서명 검색 인자가 없으므로 `report_nm`을 로컬에서 선별한다. `A001/A002/A003` 재무보고서 조건으로 제한하면 주식분할을 놓친다. 정정 접두사와 `rm`의 정정·철회 표시를 보존한다. [OpenDART 공시검색](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001), [원문 파일](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019003), [고유번호](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018).

현재 주요사항보고서 구조화 API 목록에는 주식분할·주식병합 전용 항목이 없다. `회사분할`, `회사분할합병`, `주식교환·이전`, `감자` API를 단순 주식분할 대신 사용하면 사건의 경제적 의미가 달라진다. [OpenDART 주요사항보고서 API 목록](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS005).

### 인증키가 없는 경우: 작동 확인한 공식 공개 HTML

Arcana의 기존 `fetch_dart_dividend_search`와 같은 공개 경로를 사용할 수 있다. 이것은 공식 사이트의 HTML 요청이며 OpenDART의 안정된 API 계약과 동일하다고 가정하지 않는다. [기존 구현](../../engine/extractors/_internal/dart_filings.py), [DART 상세검색](https://dart.fss.or.kr/dsab007/main.do).

1. `POST https://dart.fss.or.kr/dsab007/detailSearch.ax`: `reportName`과 `reportName2`를 각각 `주식분할결정` 또는 `주식병합결정`으로 지정한다. 실제 성공한 나머지 인자는 fixture의 `dart_marketwide_search_request.json`에 보존했다. `textCrpNm/textCrpCik`를 비워 시장 전체 검색도 가능했다. 2018-01-01~2018-05-10 분할 검색 1페이지에서 **45건, 총 1페이지**를 받았다. 시장 전체 장기 수집을 실행한 결과는 아니다.
2. 각 행에서 `/dsaf001/main.do?rcpNo=...`, 보고서명, 접수일, 회사 링크·소관·정정 표시를 읽는다. `currentPage`를 늘리고 결과의 총 건수/페이지와 실제 고유 receipt 수를 대조한다. 오류·차단 페이지를 0건 성공으로 기록하지 않는다.
3. 기업 링크 `openCorpInfoNew('00126380',...)`의 인자는 **8자리 corp_code**다. 6자리 종목코드가 아니다. 공식 `common.js`는 기업 팝업에 `POST /dsae001/selectPopup.ax`, `selectKey=00126380`을 보낸다. 이를 그대로 확인해 팝업 `종목코드` 행에서 `005930`을 얻었다. 이 매핑은 현재 기업개황이며 과거 이름·증권 변화를 자동으로 증명하지 않는다. [DART 공식 스크립트](https://dart.fss.or.kr/js/common.js?ver=7).
4. main HTML의 실제 숫자 인자를 가진 `viewDoc(...)` 호출에서 해당 receipt의 `dcmNo`를 선택한다. 함수 정의나 다른 문서의 호출을 첫 일치로 사용하지 않는다. 표본의 본문은 `GET /report/viewer.do?rcpNo=...&dcmNo=...&dtd=HTML`로 받았다.
5. **인코딩을 응답마다 처리한다.** 확인된 main·검색·기업 팝업은 UTF-8, viewer는 HTTP `charset=MS949`/HTML `euc-kr`였다. 원본 bytes를 저장하고 선언 charset을 우선한다. UTF-8 강제 디코딩, `apparent_encoding`만의 선택, 오류 문자를 무시하는 파싱은 금지한다.

현재 배당 수집 함수의 단일 페이지 처리와 재무보고서용 `parse_report_period_from_title` 조건은 그대로 재사용할 수 없다. 회사별 캐시, 기존 throttle/retry, 재개 가능한 체크포인트는 활용하되 공시 목록·원문·추출 결과를 구분해 저장한다. HTTP 실패와 수집 범위의 미완료 상태를 남긴다. 인증키는 로그/URL manifest에 기록하지 않는다. [Arcana DART 구현](../../engine/extractors/_internal/dart_filings.py).

## 원문 파싱·정정 규칙

- 표의 `rowspan/colspan`을 펼친 논리 그리드에서 **행 라벨과 분할전/후 또는 병합전/후 열**로 추출한다. 삼성 정정 문서는 table 1에 정정전/후 비교, table 3에 최종 본문이 있다. 문서 전체 첫 날짜·첫 숫자를 추출하면 안 된다. table 번호는 fixture의 기대값이며 운영 parser는 번호를 고정하지 않는다.
- 최종 본문에서 `1주당 가액`, `보통주식(주)`, `종류주식(주)`, 일정, 기타사항을 함께 읽는다. `split_ratio = old_par / new_par = new_shares_per_old_share`. 분할은 1 초과, 병합은 0 초과 1 미만이다. 비율은 가능한 한 정수 분자·분모로 저장한다.
- 액면가 0 또는 외화 단위가 섞이면 0/0으로 추정하지 않는다. 본문에 명시된 교환 단위·통화·DR 단위를 찾고 불명확하면 검토 대상으로 남긴다. 총주식수 비율은 단수주·동시 소각·신주 발행의 영향을 받으므로 검산용이며 비율의 유일한 근거가 아니다.
- main의 `select#family option[value]`는 동일 본문의 원공시·정정 receipt 계보를 제공했다. `select#att`는 이사회의사록 등 별도 첨부이므로 구분한다. 정정 본문의 `정정관련 공시서류제출일`과 계보를 함께 저장한다. 회사와 사건 종류가 같다고 서로 다른 해의 분할을 합치지 않는다.
- 사건은 하나, 공시 버전은 여러 개다. `event_id`는 원공시 계보와 증권 식별자에 연결하고, `revision_rcept_no`별로 변경 전·후 값을 보존한다. 최종가격 복원에는 실제 시행된 최종 사건을 사용한다. 과거 신호의 당시 공개정보를 재현할 때는 그 시점까지 알려진 버전만 선택한다.
- 정정/철회/연기/소송 문구와 후속 시장조치가 충돌하면 자동 적용하지 않는다. `candidate/planned/confirmed/cancelled/review` 같은 상태와 이유를 남긴다. 미래 상장예정일이나 아직 검증되지 않은 계획을 이미 발생한 조정으로 적용하지 않는다.

이 규칙의 직접 표본은 [삼성전자 정정공시](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20180316800856)와 [캔버스엔 정정공시](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260811900499)이며, 동일 문서의 실제 HTML은 아래 fixture에 보존했다.

## 날짜 의미와 확인 표본

| 필드 | 의미 | 가격 조정에 사용할지 |
| --- | --- | --- |
| `rcept_dt` / 공시일 | 시장에 공시가 접수된 날 | 정보 가용성·버전 선택에 사용 |
| 이사회결의일 | 제안·결정일 | 거래 단위 적용일로 사용하지 않음 |
| 주주총회일 | 승인 예정일 또는 승인 결과일 | 계획의 상태를 판단 |
| 기준일 / 구주권제출 종료 / 명의개서정지 | 권리·등록 절차의 날짜 | 각각 보존, 거래일로 대체하지 않음 |
| 신주의 효력발생일 | 법적 주식 단위 변경일 | 거래정지 중일 수 있음 |
| 신주권교부일 | 증서 수령 일정 | 변경상장과 다를 수 있음 |
| 신주권상장예정일 | 예정된 변경상장일 | 최종 정정 및 실제 시장조치와 대조 |
| 거래정지해제일 / 실제 변경상장일 | 새로운 단위로 거래 재개하는 날짜 | 증권별 `effective_trade_date`의 우선 증거 |

### 삼성전자: 50 대 1 분할

| 항목 | 원공시 | 정정·확인 |
| --- | --- | --- |
| DART receipt / dcm | `20180131800068` / `5935944` | `20180316800856` / `6000758` |
| 액면가 | 5,000원 → 100원 | 동일, 신주/구주 = **50** |
| 보통주 | 128,386,494 → 6,419,324,700 | 동일 |
| 종류주 | 18,072,580 → 903,629,000 | 동일 |
| 이사회 / 주주총회 | 2018-01-31 / 예정 2018-03-23 | 3월 23일 승인 사실을 4월 20일 IR가 확인 |
| 신주권상장예정일 | 2018-05-16 | **2018-05-04** |

정정 본문은 예탁 주주의 거래 가능일을 5월 4일, 실물주권을 직접 보유한 주주의 증서 교부 후 거래 가능일을 5월 11일로 구분한다. 시세 자료에 적용할 거래 날짜는 전자다. 발행사 4월 20일 발표도 4월 30일~5월 3일 정지와 5월 4일 상장을 확인한다. **GDR은 분할하지 않고 원주 전환비율만 변경**했으므로 회사 단위로 모든 증권에 50배를 적용하면 안 된다. [DART 원공시](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20180131800068), [DART 정정](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20180316800856), [삼성전자 승인 후 공지](https://www.samsung.com/global/ir/reports-disclosures/public-disclosure-view.71265/).

발행사의 반기보고서는 법적 분할 효력일을 5월 3일로 설명한다. 법적 효력일과 첫 거래일이 다른 사례다. [Samsung 2018 Half Year Report](https://images.samsung.com/is/content/samsung/p5/global/ir/docs/2018_Half_Year_Report.pdf).

로컬 KRX 표본의 마지막 거래 종가 2018-04-27 2,650,000원과 2018-05-04 51,900원은 조정 전 약 -98.04%, 50배 단위 조정 후 `51900 / (2650000/50) - 1 ≈ -2.07547%`다. 이는 로컬 등락률 -2.08%와 반올림 범위에서 일치한다. 이 가격 비교는 공식 비율의 검산이며 비율을 가격 점프로 역추정한 근거가 아니다. [기존 연구의 KRX 진단 추가 확인](cross_market_factor_economic_rationale_20260909.md).

### 캔버스엔: 5주를 2주로 병합, 일정 여러 차례 연기

원공시 receipt는 `20260424900689`, 최종 확인 정정은 `20260811900499` / dcm `11518568`이다. 액면가 200원 → 500원이므로 **신주/구주 = 2/5 = 0.4**다. 본문의 보통주수는 24,237,478 → 9,694,991이며 단수주 현금지급 때문에 총수는 변할 수 있다고 명시한다. 따라서 9,694,991/24,237,478을 정밀 조정 비율로 쓰지 않는다. 이 공시는 자본금 감소를 목적으로 하는 감자가 아니라고 명시한다. [DART 8월 11일 정정](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260811900499).

- 이사회 2026-04-24, 주주총회 2026-05-11, **법적 효력 2026-06-12**.
- 8월 11일 정정에서 상장예정일 8월 13일 → **8월 20일**, 거래정지 종료 8월 12일 → 8월 19일.
- `20260819900375` / dcm `11542651` 거래정지해제 공시가 대상 **캔버스엔 보통주**, 사유 **액면병합 주권 변경상장**, 해제일 **2026-08-20**을 명시한다. 따라서 거래 적용일을 6월 12일로 설정하지 않는다. [DART 실제 시장조치](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260819900375).

## 조정식·사건 분류와 파이프라인 요구사항

단순 분할/병합 사건 `e`의 신주/구주 비율을 `s_e`, 거래 적용일을 `d_e`라 하면 기준일 `T` 단위의 과거 가격은 다음과 같다.

```text
split_adjusted_close(t; T) = raw_close(t) / product(s_e for t < d_e <= T)
```

삼성은 과거 가격을 50으로 나누며, 0.4 병합은 과거 가격을 0.4로 나눈다. 원시 종가는 보존하고 분할 조정 종가·누적 조정계수·적용 사건 ID를 별도로 저장한다. 동일한 단위가 필요한 O/H/L과 가격 기반 팩터 입력도 일관되게 조정한다. 거래량을 비교 가능한 주식 단위로 바꿀 때는 반대 방향 계수를 사용한다. 이미 분할 조정된 공급자 값에 다시 적용하지 않도록 원시 가격의 단위 계약을 확인한다.

거래정지 기간에 이전 종가만 반복하고 O/H/L·거래량이 0인 로컬 행은 거래 관측값과 구분한다. 법적 효력일에 그런 반복 가격을 바꾸면 허위 수익률이 생긴다. 실제 거래일 경계에서 사건을 적용하고, 체결 가능성 판단에는 거래정지를 유지한다.

단순 액면병합, 감자 목적 주식병합, 합병대가 주식교환, 회사분할/재상장, DR 원주비율 변경을 별도 분류한다. 조정하려는 것이 단순 주식 단위인지 경제적 권리 변화인지 본문에서 확인한다. 특히 감자·회사분할의 기준가격 형성은 별도 규칙이 존재하므로 단순 분할 조정으로 전체 주주 수익을 복원했다고 주장하지 않는다. [KRX 기준가격 규정 안내](https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100T6.jsp).

권장 저장 항목은 `market/security_id/stock_code/corp_code/share_class`, `event_id/action_type/status`, `ratio_new/ratio_old`, 모든 날짜 필드와 `effective_trade_date`, `original_rcept_no/revision_rcept_no/dcm_no`, `source_url/source_sha256/retrieved_at/encoding`, `table_locator/source_labels/excerpt`, `parser_version/validation_status/validation_reason`이다. 결과 재계산은 해당 증권·기간을 식별해 재실행 가능해야 하며, 사건 추가·정정·취소를 같은 이벤트의 수정으로 처리한다.

가격 연속성·일별 등락률 비교는 검증에만 사용한다. 주가 점프만으로 공식 사건을 생성하거나 공식 비율을 최적화하지 않는다. 배당·유상증자·감자·분할합병 등이 별도로 남아 있으므로 산출물 이름은 `split_adjusted_close`처럼 처리 범위를 드러내야 한다. 배당을 포함한 total-return이라고 표시하지 않는다.

## 보존한 회귀 검증 자료

위치: [fixture 디렉터리](../../tests/fixtures/stock_splits/). `dart_source_manifest.json`에 원문별 정확 URL, receipt/dcm, 인코딩, SHA256과 수집시각이 있다. 원문 파일은 네 공시의 main/viewer **8개**다.

| 파일 접두사 / receipt | 기대 검증 |
| --- | --- |
| `samsung_original_20180131800068` | 원래 상장계획 2018-05-16, 비율 50 |
| `samsung_amended_20180316800856` | 최종 본문 상장 2018-05-04; 앞 정정표의 2018-05-16 선택 금지 |
| `canvasn_amended_20260811900499` | 비율 2/5, 효력 6/12와 상장 8/20 분리, 감자 제외 문구 |
| `canvasn_resumption_20260819900375` | 실제 거래정지해제 2026-08-20, 보통주 확인 |

`dart_expected_events.json`에는 수동 확인한 네 공시의 추출 기대값을 보존했다. 추가 자료는 `marketwide_split_search_20180101_20180510_page1.html`, 그 POST 인자 JSON, `samsung_corp_popup_00126380.html`, 그 POST 인자 JSON이다. 시장 전체 수집의 완료율이나 모든 과거 사건의 포괄성을 이 소규모 표본으로 주장하지 않는다.
