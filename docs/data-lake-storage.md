# 주식분할·생존편향 데이터 저장 규칙

수집·가공·사용자 제공 단계의 파일을 `data-lake` 아래에 저장한다. 확장자가 아니라 데이터의 역할로 계층을 결정한다.

| 계층 | 내용 | 기본 경로 예시 |
| --- | --- | --- |
| bronze | 공급자가 반환한 원문과 수집 출처·요청·해시 메타데이터 | `bronze/dart/stock_splits`, `bronze/sec/stock_splits`, `bronze/dart/listings`, `bronze/consensus/alpha-vantage` |
| silver | 정규화 사건 원장, 수정주가 계산 패널, 상장 이력, 시총 계산, 검증 결과 | `silver/corporate_actions`, `silver/survivorship/{market}` |
| gold | 사용자에게 제공하는 사건·상장 이력·권리·미해결 항목·수정종가·시총 파일과 요약 | `gold/corporate_actions/{market}`, `gold/survivorship/{market}` |

`market`은 `kr` 또는 `us`다. API 응답의 원본 바이트는 변경하지 않는다. 갱신 전 원문의 보관 사본도 `bronze/source-archive/{market}/{run_id}`에 쓴다. 가공 결과에는 출처와 해시를 유지한다. API 키는 출처 URL과 메타데이터에 넣지 않는다. 검토용 JSON이나 정규화 CSV는 silver에 저장한다. 문서·수집기·테스트 코드는 기존 코드 폴더에 둔다.

## 파이프라인 출력

DART 본문과 안내 페이지의 실패 응답도 `bronze/dart/stock_splits/public_documents/{receipt}`에 바이트·해시·요청 단계와 함께 보존한다. HTTP 응답 자체가 없으면 원문 파일 없이 수집 실패 메타데이터만 남긴다. 기존 제공 불가 캐시를 읽을 때 보완하는 보류 메타데이터에는 원래 캐시의 경로·해시를 연결하고 원문이나 캐시를 수정하지 않는다. 이 경로의 테스트·구현 보관본은 `silver/survivorship/financial_research/dart_previewer_failures_20260911`, 사용자용 검증 요약은 `gold/survivorship/pipeline_verification/20260911_dart_previewer_failures`에 있다.

실제 접수일 근거에는 검색 원문 또는 정정 이력 안내 페이지의 경로·SHA-256을 연결한다. 캐시의 날짜를 정정할 때 이전 메타데이터는 `bronze/dart/stock_splits/metadata_versions/{receipt}/{sha256}.metadata.json`에 그대로 보존한다. 현재 메타데이터의 `publication_date_prior_metadata`가 그 버전을 가리키며 공시 본문은 바꾸지 않는다. 날짜 보정 검증은 `silver/survivorship/financial_research/dart_publication_dates_20260911`, 사용자용 요약은 `gold/survivorship/pipeline_verification/20260911_dart_publication_dates`에 둔다.

날짜 미확정 수집 기록은 `published_date=null`로 보존하며 충돌한 날짜와 원문 경로·해시를 `publication_date_conflict`에 남긴다. API ZIP의 날짜가 없으면 `document_archives`의 원본을 보존하고, 날짜 검증 실패 메타데이터에서 ZIP 경로·해시를 참조한다. 회귀 테스트·최종 CLI 재검증과 구현 보관본은 `silver/survivorship/financial_research/dart_date_validation_20260911`, 사용자용 검증 요약은 `gold/survivorship/pipeline_verification/20260911_dart_date_validation`에 있다. 과거 실패를 해제하더라도 그 원문과 메타데이터를 지우지 않는다.

후속 날짜 검증에서 실제 수집한 KIND 원문·메타데이터는 `bronze/dart/corporate_actions/followup_dates_20260911`에 보존한다. KIND의 검색 행 날짜·시각 원문은 유지하며 DART와 KIND의 서로 다른 접수번호를 각각 기록한다. 공개 수집·CLI 테스트 131개의 결과, 실제 9개 보관 파일의 재실행 대조와 구현 사본은 `silver/survivorship/financial_research/split_followup_dates_20260911`, 사용자용 요약은 `gold/survivorship/pipeline_verification/20260911_split_followup_dates`에 둔다. 조회 상한 이후임을 새로 확인한 DART 원문도 Bronze에 남기고 수집 보고서의 `outside_cutoff`에 날짜 근거를 연결한다.

주식분할 갱신은 silver 사건 원장과 가격 패널을 만든 뒤 gold의 `stock_splits.json`, `prices/{market}_{symbol}.parquet`, `summary.json`을 저장한다. 주식병합도 같은 기업행위 경로를 사용한다. 독립 실행의 `--gold-output` 또는 `run_stock_split_refresh(gold_dir=...)`로 gold 위치를 지정할 수 있다.

생존편향 갱신은 gold에 `listing_episodes.json`, `events.json`, `entitlements.json`, `trading_halts.json`, `unresolved.json`, `prices/{market}_{symbol}.parquet`, `market_cap_factors.parquet`, `summary.json`을 저장한다. `--survivorship-output`은 silver, `--survivorship-gold-output`은 gold 경로다. `--skip-clickhouse` 실행에서도 파일은 두 계층에 저장된다. 검토 목록이 없으면 gold 요약에 `awaiting_review`를 표시한다.

gold 가격 파일의 `adj_close`는 분할·병합만 반영한 수정종가다. `open`, `high`, `low`, `close`, `volume`은 거래 당시 값이며, 배당을 포함한 총수익률 가격이 아니다. 요약의 `artifacts`가 해당 실행에서 생성한 파일과 SHA-256을 가리킨다. 파일별 교체 후 요약을 마지막에 쓴다. 전체 디렉터리를 한 번에 교체하는 트랜잭션은 아니므로 동시 열람 시 파일 해시를 확인해야 한다.

gold에 있다는 사실이 검증 완료를 의미하지 않는다. `coverage_complete=false`와 미해결 사건을 함께 제공하며, 미확정 현금·CVR·비상장 주식·단주 권리를 0으로 바꾸지 않는다. 생존편향의 전체 시장 보정 여부는 [파이프라인 현황](survivorship-pipeline.md)을 따른다.

한국 시총·주식수의 1996~2026년 통합 검증은 `silver/survivorship/financial_research/kr_current_capitalization_export_20260911`에 보존한다. 최신 native·스냅샷 각각 369개 월별 파일은 `gold/survivorship/kr/capitalization_factors/1996_2026/kr_current_capitalization_export_20260911/{native|snapshot}`에 있으며, `capitalization_factors/current.json`이 검증된 요약의 경로·해시를 가리킨다. 기존 범위별 파일은 보존한다. 이 통합의 완료로 전체 주식수 의존 팩터 변경 기록을 해제하지 않는다.

부채 결측 수정의 운영 코드·테스트·실제 입력 대조는 `silver/survivorship/financial_research/debt_abstention_main_20260911`, 실제 판독 전후 결과는 `reviewed_debt_inputs_before_20260911`과 `reviewed_debt_inputs_after_20260911`에 둔다. 사용자용 기능 검증은 `gold/survivorship/pipeline_verification/20260911_debt_abstention_main`에 있다. 미국 4종목의 전체 팩터 재계산 준비본은 `silver/survivorship/financial_research/us_reviewed_full_factor_preparation_20260911`에 있으며, 운영 native·스냅샷 반영 완료와 구분한다.

일반 팩터 스크리너의 상장 이력·거래정지 연결 검증은 `silver/survivorship/financial_research/factor_screen_lifecycle_20260911`에 SQL, 실패·통과 기록, 코드 사본과 해시를 보존하고 `gold/survivorship/pipeline_verification/20260911_factor_screen_lifecycle`에 사용자용 요약을 둔다. 테스트는 고유한 격리 DB와 임시 Bronze 원문을 사용하며 운영 상장 이력을 추가하지 않는다.

상장 종료 후 미해결 보유분 검증은 `silver/survivorship/financial_research/closed_listing_outcome_20260911`에 둔다. 한국 추가 상장 이력의 전체 등록안은 `kr_missing_listing_refresh_proposal_20260911`, 실제 원본과 격리 DB·FactorLab 대조는 `kr_missing_listing_isolated_verification_20260911`에 둔다. 사용자에게는 `gold/survivorship/kr/listing_refresh_proposal/20260911`에서 운영 미반영 상태와 함께 제공한다. 등록안은 기존 이력을 보존하며 원문을 복제·변경하지 않고 Bronze 경로와 해시로 연결한다.

미국 목록의 원본은 `bronze/alpha-vantage/listings/snapshot_date={date}`에 보존한다. 정규 갱신의 스냅샷 비교·중복·충돌 감사는 `silver/survivorship/us/listing_source_quality/as_of={date}/source_set={hash}/audit.json`에 입력별로 저장하고, 사용자 요약은 `gold/survivorship/us/listing_source_quality.json`에 제공한다. Gold의 생존편향 요약은 감사 파일의 경로와 해시를 연결한다. 비교는 보관된 이전 날짜만 사용하며 공급자 식별 조합이 같다는 사실을 발행사·주식 종류의 연속성으로 해석하지 않는다.

## 연구 자료와 재현

거래정지의 DART 원문·ZIP 응답은 `bronze/dart/listings/issuer_review_20260911/trading_halts`와 정규 수집 검증의 `bronze/dart/listings/regular_halt_collection_20260911`에 저장한다. 추출 텍스트·구간 대조는 `silver/survivorship/financial_research/kr_trading_halt_source_review_20260911`, 파이프라인 실패 재현·통과 기록·코드 버전은 `silver/survivorship/financial_research/trading_halt_pipeline_20260911`에 둔다. 사용자용 구간 검토는 `gold/survivorship/kr/trading_halt_reviews/20260911`, 기능 검증 상태는 `gold/survivorship/pipeline_verification/20260911_trading_halts`에서 제공한다. 원문 확보와 운영 등록 상태를 별도로 표시한다.

2026-09-11에 추가로 확인한 한국 누락 증권 5개의 발행사별 DART 목록과 사업·반기보고서, 청산 공시 원본은 `bronze/dart/listings/issuer_review_20260911/missing_membership`에 보존한다. 정규 수집기를 통한 실제 재수집 원본은 `bronze/dart/listings/liquidation_collection_20260911`에 둔다. 원문에서 추출한 검토 텍스트·종목군 누락 비교는 `silver/survivorship/financial_research/kr_remaining_missing_membership_20260911`, 수집 테스트·재실행 결과는 `kr_liquidation_collection_20260911`에 둔다. 이 자료는 신규 상장 이력 승인이나 청산대금 확정과 구분한다.

미국 상장 상태의 기준일별 원문은 `bronze/alpha-vantage/listings/snapshot_date={date}`에 둔다. 날짜 간 원문 비교·중복·상장폐지일 변동은 `silver/survivorship/financial_research/us_listing_status_20260910`, 사용자용 현황은 `gold/survivorship/us/source_coverage/20260910/listing_status.json`에 둔다. RTN 합병의 SEC 원문 5개는 `bronze/sec/listings/issuer_review_20260911/RTN`, 추출 텍스트와 검토는 Silver의 `us_rtn_source_review_20260911`, 사용자용 요약은 `gold/survivorship/us/issuer_reviews/RTN/20260911`에 보존한다.

수집 표본은 `bronze/research/stock_splits/{market}/{bundle}` 또는 `bronze/research/financial_statements/eps/{bundle}`에 둔다. 추출 텍스트·예상 파서 값·정규화·검증 결과는 같은 분류의 silver 경로에 둔다. `docs/research`에는 설명과 Python 수집·검증 코드가 남는다. 테스트가 읽는 원문은 `bronze/fixtures/stock_splits`, 예상 정규화 값은 `silver/fixtures/stock_splits`에 둔다. 소규모 오프라인 테스트 표본만 `.gitignore` 예외로 관리한다.

과거 한국 재무자료의 정규화·감사·검토·스테이징 실행기는 새 결과를 `silver/survivorship/financial_research`에 쓴다. `--data-root`로 출력 루트를, `--input-root`로 기존 배치의 입력 루트를 지정한다. 정규화 실행기는 입력 목록에 `--source-manifest`를 사용한다. 재무자료 게시기는 원문의 경로와 해시를 인덱스에 기록하고, 원문 HTML과 DART 목록 응답을 silver에 다시 복사하지 않는다. 과거 `deliverables`에 고정해 둔 배치와 구현 스냅샷은 재현 근거로 보존한다.

검토 재무 이력의 자동 재계산 준비 파일은 `silver/financial_history_rebuilds`에 저장한다. 검증 후 공개한 연간·분기·TTM 팩터 파일은 `gold/survivorship/kr/financial_factors/20260910`에 있으며, 정정 전 gold 파일은 해당 공개 작업의 silver `gold_before` 폴더에 보존한다. 최신 공개 보고서의 해시와 파일별 행 수로 버전을 확인한다.

같은 공시에서 검토 계정을 추가할 때는 검토 JSON의 `expected_manifest_sha256`과 `revision_reason`으로 대상 버전과 이유를 명시한다. 이전 값·공시일·회계기간·연결 범위·원문 해시는 바꿀 수 없다. 이전 인덱스는 `silver/dart/normalized/history/{symbol}/manifests/{sha256}.json`에 보존하고, 새 인덱스의 `review_extensions`에서 근거를 추적한다. 이는 새 계정의 검토 범위를 넓히는 경로이며, 기존 값 정정이나 원문 변경을 허용하는 경로가 아니다.

시장 입력의 독립 검증·준비 계산·DB의 월별 변경 전후 행은 `silver/survivorship/financial_research`에 저장한다. 검증된 시장 팩터 공개 파일은 `gold/survivorship/kr/market_factors/{date}/{attempt}`에 실행별로 저장하며, 원문 분할·병합 HTML은 기존 bronze 위치와 해시를 참조한다. 공개 상태에는 DB 반영 여부와 스냅샷 반영 여부를 각각 기록한다.

과거 시점 스냅샷의 월별 변경 전후 행, 독립적인 as-of 대조 결과, 실제 SQL 검증 결과는 `silver/survivorship/financial_research/kr_market_snapshot_publication/{attempt}`에 저장한다. 게시와 최종 DB 대조가 모두 통과한 경우에만 gold의 `snapshot_summary.json`을 갱신한다. 실제 게시 시각과 DB 버전 순서용 시각이 다르면 두 시각을 명시한다.

계산 불가 상태도 가공 자료다. 과거 값의 사용을 중단하는 결측 사건과 검증용 준비 파일은 silver에 저장한다. 사용자용 gold 요약에서는 유효한 팩터 셀 수와 결측 사건 수를 따로 표시한다. 결측 사건을 수치 0으로 저장하거나 팩터 커버리지에 포함하지 않는다. `kr_capital_abstention_preparation_20260910`은 준비 당시의 기록으로 보존한다. 이후 반영한 32,541개 유효 값과 94개 결측 사건의 공개 보고서는 `gold/survivorship/kr/capital_factors/20260910`에 있으며, DB·스냅샷 검증 결과를 함께 제공한다.

2013~2015년 시총 누락의 원본은 `bronze/marcap/data/marcap-{year}.parquet`에 있다. 원문 해시·가격×주식수 대조·기존 주식수 입력과의 비교는 `silver/survivorship/financial_research/kr_historical_capitalization_source_audit_20260911_v2`에, 복구 진행 상태는 `gold/survivorship/kr/capitalization_coverage/20260911`에 둔다. 이 검증은 원문 수치의 검증이며 새로운 발행사·상장 이력을 승인하는 절차가 아니다.

정규 `normalize_shares`는 `silver/krx/shares/historical_sources.json`에 등록된 원본을 매 실행마다 병합한다. 원본은 `bronze/registered-market-sources/sha256={hash}/{filename}`에 바이트 그대로 보관하고, 공급자의 갱신 가능한 캐시 경로는 `original_path`로 남긴다. 등록에는 출처, SHA-256, 관측 연도·날짜 범위를 보존한다. 날짜가 없는 과거 listing CSV는 검증된 고정 관측일을 명시하며 현재 날짜로 대체하지 않는다. 해시·산식·중복·기존 값 충돌을 검증한 후 임시 CSV를 원자적으로 교체한다.

2026-09-11에는 1996~2026년 Marcap 원본 31개와 기존 고정일 목록 원본 1개를 이 경로에 등록했다. 정규 주식수 입력은 15,384,392행이며, 기존 7,102,067행의 값을 유지하면서 누락 관측 8,282,325행을 추가했다. 전체 연도별 원문 대조, 변경 전 CSV, 변경 기록 검증은 `silver/survivorship/financial_research/kr_full_share_input_publication_20260911`에 저장한다. 사용자용 최신 상태는 `gold/survivorship/kr/share_inputs/1996_2026/summary.json`에서 입력 반영과 DB·스냅샷 반영을 구분한다.

후속 2024년 시총·주식수 팩터의 검증·DB 변경 전후 행은 Silver의 `kr_full_share_native_preparation_20260911_2024`, `kr_full_share_native_publication_20260911_2024`에 둔다. 실제 FactorLab의 244거래일 대조 결과는 `kr_full_share_native_factorlab_20260911_2024`에 있다. 사용자용 월별 파일과 검증 요약은 `gold/survivorship/kr/capitalization_factors/2024`에 저장하며 기존 `2013_2015` 결과와 구분한다. 입력 Gold 요약의 `market_factor_scopes`에서 완료한 개별 연도 범위를 제공하고, 전체 재계산 상태를 완료로 바꾸지 않는다.

같은 2024년 원천 범위의 스냅샷 반영·DB 대조·후속 원천 날짜와 달력의 재검증은 Silver의 `kr_full_share_snapshot_publication_20260911_2024_v2`에 보존한다. 실제 사용자 조회 검증은 `kr_full_share_snapshot_factorlab_20260911_2024`에 있다. Gold의 `capitalization_factors/2024/snapshots/{month}.parquet` 33개는 실제 DB의 검증된 최신 스냅샷이며, 원천 날짜와 `is_market_trading_date`를 함께 제공한다. 이 파일의 날짜는 as-of 날짜로서, 거래일이 아닌 기존 캐시 날짜를 주가 관측으로 바꾸지 않는다. Gold 파일을 Silver 검증본과 다시 읽어 비교한 기록은 `closeout/serving_artifacts.json`에 있다.

원문의 결측·0 이하 가격/주식수 등으로 정규화할 수 없는 13,426행은 원본을 보존한 채 `silver/krx/shares/source_quarantine/{source_sha256}/observations.parquet`에 격리했다. 등록의 `quarantine`에는 경로·해시·행 수·이유가 포함된다. 정규화는 격리 파일의 키와 수치가 해당 원문의 실제 부적합 행 전체와 정확히 같을 때만 제외를 허용한다. 유효 행 제외, 원문 수치 변경, 해시 불일치는 실패한다. 사용자용 미해결 관측 목록은 같은 Gold 폴더의 `quarantined_observations.parquet`에 출처와 함께 제공한다. 격리를 값 0 대입이나 상장 적격성 승인으로 해석하지 않는다.

주식수 입력의 변경 기록은 `silver/market_input_changes/kr/{run}/report.json`과 `changed_observations.parquet`에 남는다. CSV 교체 전후 해시, 종목별 최초 변경일, 추가·수정·철회 행 수를 보존한다. `latest.json`은 최근 정규화 입력을 가리키며, 연간·분기·TTM의 팩터·스냅샷 재계산 상태는 `rebuild_state.json`에서 각각 관리한다. 스테이징용 별도 CSV 출력은 정규 입력의 완료 상태를 바꾸지 않는다.

이 변경에 따른 계산 준비본과 월별 DB 변경 전후 행은 `silver/share_input_rebuilds/kr/{symbol}/{basis}/{run}` 및 `silver/share_input_snapshot_rebuilds/kr/{symbol}/{basis}/{run}`에 저장한다. 검증된 사용자용 월별 파일과 요약은 `gold/share_input_rebuilds/kr/{symbol}/{basis}/{factors|snapshots}/{run}`에 둔다. 이 요약의 완료는 해당 입력 변경 범위의 반영을 뜻하며, 전체 시장 생존편향 제거를 뜻하지 않는다. 원본은 이 과정에서 수정하지 않는다.

2026-09-11 실제 입력 복구의 전체 변경 전 CSV·추가 행·정규 파이프라인 스테이징 결과는 `silver/survivorship/financial_research/kr_historical_share_publication_20260911_v2`에 있다. 2013~2015년 1,455,590행을 추가했고, 기존 최근 구간 68,944행도 원본을 연결해 유지했다. 등록 원본 보관의 변경 전후 manifest와 재실행 검증은 `kr_historical_share_source_freeze_20260911`에 있다.

이 입력으로 복구한 `mcap_mil`·`csho` 8,726,945개의 DB 변경 전후 행은 `silver/survivorship/financial_research/kr_historical_capitalization_native_publication_20260911_v2`에 보존한다. 사용자용 월별 팩터 파일 36개는 `gold/survivorship/kr/capitalization_factors/2013_2015`에 있으며, 740거래일의 실제 FactorLab 순위·모집단·상위 70% 대조 자료는 silver의 `kr_historical_capitalization_factorlab_20260911_v3`에 둔다. gold의 `factorlab_verification.json`은 그 근거와 해시를 참조한다. native 값 검증과 과거 시점 스냅샷 검증은 별도 상태로 표시한다.

시총·주식수 스냅샷 19,374,780개의 월별 변경 전후 행·독립 as-of 계산·SQL 검증 결과는 silver의 `kr_historical_capitalization_snapshot_publication_20260911_v2`와 `kr_historical_capitalization_existing_snapshot_publication_20260911`에 있다. 두 범위는 같은 스냅샷 키를 중복 포함하지 않으며, 통합 근거는 `kr_historical_capitalization_snapshot_completion_20260911`에 둔다. 사용자용 통합 상태와 실제 팩터랩 검증 결과는 gold의 `capitalization_factors/2013_2015/snapshot_summary.json`, `snapshot_consumer_verification.json`에 제공한다. 기준일 2026-09-10과 실제 한국 가격 달력의 마지막 관측일 2026-09-04를 구분한다.

2026-09-10 이동의 파일별 원래 위치·새 위치·SHA-256은 `silver/storage_migrations/docs_tests_20260910/inventory.json`과 `journal.json`에 있다. 경로를 수정한 수집 메타데이터의 원본 바이트는 `bronze/storage_migrations/docs_tests_20260910/preimages`에 보존했다. 이동 검증은 아래 명령으로 반복할 수 있다.

```powershell
& .\.venv-llama\Scripts\python.exe -X utf8 scripts/maintenance/migrate_research_storage.py --verify
```


일반 SEC 파일의 공시 시점별 재계산 테스트·수정 전 코드·실제 준비본 비교는 `silver/survivorship/financial_research/us_period_vintage_refresh_20260911`에 저장한다. 최종 준비본은 `us_reviewed_full_factor_preparation_period_vintage_20260911_v2`, 독립 부채비율 대조는 `us_reviewed_leverage_validation_period_vintage_20260911_v2`에 보존한다. 사용자용 검증 상태는 `gold/survivorship/pipeline_verification/20260911_us_period_vintage/summary.json`에 제공하며, 팩터의 운영 게시 여부와 분리한다. 이전 실패 준비본과 검토 상태는 덮어쓰지 않고 Silver에 유지한다.


일반 과거 재무 입력의 변경 서명과 기준별 재계산 상태는 기존 `silver/{dart|sec}/normalized/history/rebuild_state.json`에 함께 저장한다. 일반 파일 항목은 실제 사용 파일의 해시와 발행사별 공시 메타데이터 해시를 기록하며 공시 이력 manifest 항목과 구분한다. 재적재 검증의 실패·통과 기록, 코드 사본, 실제 dry-run 계획과 SEC 공시별 관측 목록은 `silver/survivorship/financial_research/us_financial_source_refresh_20260911`에 있다. 사용자용 상태는 `gold/survivorship/pipeline_verification/20260911_financial_source_refresh/summary.json`에 제공한다. SEC 원본은 기존 Bronze 파일을 참조하며 Silver 관측 목록을 원본이나 승인된 공시 이력으로 표시하지 않는다.

ALXN의 2015년 재무 표시 변경·정정 범위 검토 원문과 SEC 접수 화면은 `bronze/sec/financial_history/us_alxn_2015_filing_versions_20260911/ALXN`에 보존한다. 단위·날짜 열·12개 수치 대조와 캐시 재실행, 기존 기간별 정규화 자료 비교 및 실행 코드 사본은 `silver/survivorship/financial_research/us_alxn_2015_filing_versions_20260911`에, 사용자용 검토 요약은 `gold/survivorship/us/financial_version_reviews/20260911/ALXN`에 둔다. 운영 정규화 파일이나 공시 묶음의 원천 우선순위를 변경하지 않았다.

미국 공시번호별 정규화 선택 결과는 `silver/sec/normalized/accessions/{symbol}`에 보존한다. 내용 해시를 이름으로 갖는 CSV를 먼저 저장하고 `manifest.json`을 마지막에 교체하며, 과거 CSV는 유지한다. CompanyFacts 항목에는 Bronze 원문 경로·SHA-256·단위·기간 시작과 종료·기간 길이를 남긴다. 이 자료는 디버그 출력과 무관한 일별 계산 입력이다. 공개 실행의 실패·통과, 코드 사본과 실제 네 종목의 별도 준비본은 `silver/survivorship/financial_research/us_sec_accession_refresh_20260911`에, 사용자용 검증 요약은 `gold/survivorship/pipeline_verification/20260911_sec_accessions`에 둔다. 실제 최종 준비본은 `actual_normalization_v3`이며 앞선 준비·실패 자료도 보존한다.

EPS·가중평균 주식수의 추가 기간 관측은 같은 공시별 CSV의 `reported_durations`에 시작일·종료일·길이·금액·단위·문맥과 함께 저장한다. 부모 행의 공시번호와 원천 추적 정보를 공유하며 기간별 최종 CSV를 덮어쓰는 분기값으로 사용하지 않는다. ATVI의 실제 분기 검증 원문과 접수 화면·다운로드 메타데이터는 `bronze/sec/financial_history/us_per_share_periods_20260911/ATVI`에, 표 추출·CompanyFacts 대조·정규화 준비본·공개 일별 계산·테스트 결과·코드 사본은 `silver/survivorship/financial_research/us_per_share_periods_20260911`에 보존한다. 사용자용 상태는 `gold/survivorship/pipeline_verification/20260911_us_per_share_periods`에 제공하며 운영 재적재 완료와 구분한다.

합산 가능한 계정의 동일 공시 내 기간 관측도 `reported_durations`에 보존한다. 당기 종료일의 분기값과 네 분기 합계의 근거를 보유하되, EPS·평균 주식수와 다른 계산 규칙을 사용한다. ATVI·ALXN·CELG의 원문·접수 화면·다운로드 응답은 `bronze/sec/financial_history/us_disclosed_flow_periods_20260911`, 표 추출·기간 검토·실패 및 통과 테스트·코드 사본은 같은 범위의 `silver/survivorship/financial_research`에 둔다. ALXN의 짧은 iXBRL 화면 응답도 원본으로 유지하고 실제 본문은 별도 파일로 저장했다.

전체 미국 준비본은 `silver/survivorship/financial_research/us_accession_full_factor_preparation_flows_20260911`, 공시별 기간의 독립 대조는 `us_accession_full_factor_validation_flows_20260911`에 있다. 앞선 `us_accession_full_factor_preparation_20260911`과 검증 결과도 덮어쓰지 않는다. 사용자용 결과는 `gold/survivorship/pipeline_verification/20260911_us_disclosed_flow_periods`에 제공하며, CELG의 R&D 계정 범위 문제와 운영 미게시 상태를 명시한다.

CELG 2010년 2분기 R&D의 원천 복구 자료는 정규 경로 `bronze/sec/fillings/10-Q/CELG/0000950123-10-072016`에 저장한다. 먼저 수집해 비교한 공시 묶음은 `bronze/sec/financial_history/us_celg_rnd_scope_20260911/filings`에 유지한다. 원문 XBRL·본문과 그 출처 메타데이터는 Bronze에, 문맥·표시 역할·재무상태표 행 대조·실패/통과 검사·규칙 마이그레이션·코드 사본은 `silver/survivorship/financial_research/us_celg_rnd_scope_20260911`에 보존한다. 규칙 자체는 실행 설정인 `meta/rules/semantic_us_v4.arcana`에 두고 이전 버전을 덮어쓰지 않는다. 원문 복구 후 준비본과 v4 반영 후 준비본은 각각 `us_accession_full_factor_preparation_rnd_20260911`, `us_accession_full_factor_preparation_rnd_v4_20260911`의 별도 Silver 범위로 구분한다.

v4 준비본의 공시별 독립 기간 대조는 `silver/survivorship/financial_research/us_accession_full_factor_validation_rnd_v4_20260911`에 둔다. 원문 복구 전후·규칙 변경 전후의 전체 팩터 비교는 검토 범위 아래 `rnd_prepared_changes`, `v4_prepared_changes`에 저장하며 모든 계정의 의미를 독립 검증했다는 자료로 사용하지 않는다. 사용자용 완료 범위와 남은 검토는 `gold/survivorship/pipeline_verification/20260911_celg_rnd_scope/summary.json`에, 최신 준비본 연결은 기존 `gold/survivorship/us/financial_rebuild_preparation/20260911/summary.json`에 제공한다. 앞선 준비본과 Gold 연결의 이전 내용은 보존한다.

SEC 재무 정정본은 `bronze/sec/fillings/10-K_A/{symbol}/{accession}`, `10-Q_A/{symbol}/{accession}`에 저장한다. JSON 내부에는 원래 `10-K/A`, `10-Q/A` 양식을 유지한다. 공시 메타데이터를 보완할 때 이전 원문은 같은 묶음의 `metadata_versions/{sha256}.json`에 보존하며, 수집 시각만 달라지는 반복 요청은 기존 원문을 교체하지 않는다. 과거 CELG 메타데이터 12건도 기존 사본과 해시를 대조해 이 위치에 보존했다.

4종목의 180개 보존 공시 이력과 총 182개 수집 묶음의 대조, 최초 검사의 실패와 메타데이터 보완 내역, 재개 검증, 추가 정정본의 설명문 검토, 전체 Silver 정규화와 141개 계정 차이, 테스트·실행 코드 사본은 `silver/survivorship/financial_research/us_filing_bundle_coverage_20260911`에 둔다. 사용자용 범위·제한 사항은 `gold/survivorship/pipeline_verification/20260911_us_filing_bundles/summary.json`에 제공한다. 재무값을 바꾸지 않는 두 추가 정정본의 원문도 Bronze에서 유지한다.

계정 선택 후속 검증은 `silver/survivorship/financial_research/us_statement_account_scope_20260911`에 원문 경로·해시, 실패·통과 테스트, 규칙 마이그레이션, 코드 사본과 공개 계산을 보존한다. 사용한 원본 공시 묶음은 계속 `bronze/sec/fillings`에 있으며 새 다운로드나 원문 교체는 하지 않았다. 중간 정규화는 해당 검토 폴더의 `full_normalization`, 최종 v5 정규화는 별도 `us_statement_account_scope_v5_20260911/full_normalization`에 둔다. 첫 공개 계산의 기대값 오류는 `public_factors`에 유지하고 원문 대조 후 재검증은 `public_factors_v2`에 저장했다. 사용자용 범위·제한은 `gold/survivorship/pipeline_verification/20260911_us_statement_accounts/summary.json`에 제공한다. 실행 규칙 v5는 `meta/rules`에 새 버전으로 추가하며 이전 버전을 수정하지 않는다.


판관비·리스 구성요소의 원본 공시는 `bronze/sec/fillings`, CompanyFacts는 `bronze/sec/companyfacts`에 유지한다. v6 규칙 검토·원문 기준값·실패와 통과 XML·마이그레이션·실행 전 사본은 `silver/survivorship/financial_research/us_complete_components_20260911`에, 전체 정규화와 v5 대비 차이는 `us_complete_components_v6_20260911/full_normalization`에 둔다. 정규화된 공시별 CSV에 합산 구성요소의 태그·원래 금액·문맥·기간과 원천 경로가 남으며 원문 자체를 수정하지 않는다. 사용자용 검증 범위는 `gold/survivorship/pipeline_verification/20260911_us_complete_components/summary.json`에 제공한다. Gold의 최신 재무 준비본 연결은 v6 이후 재준비 필요 상태로 갱신하고 이전 내용은 Silver에 보존했다. 운영 팩터·스냅샷의 새 발행은 아직 수행하지 않았다.


혼합된 직접 총액·구성요소 기간의 수정 근거는 `silver/survivorship/financial_research/us_component_duration_scope_20260911`에 저장한다. `full_normalization`은 기존 v6를 기준으로 한 재실행이며, `implementation/{sha256}`에 당시 코드·규칙의 바이트 사본을 보존한다. 후속 감가상각·상각 검토용 공시 표 추출·계정 목록과 `cash_flow_da_public_red`의 실제 공개 팩터 실패도 이 검토 범위에 둔다. 해당 표는 가공된 추출문이므로 Silver에 두며 원본 HTML·XBRL은 Bronze에 유지한다. 사용자용 결과는 `gold/survivorship/pipeline_verification/20260911_us_component_durations/summary.json`이다.


US v7 공통 본표 범위 검증(2026-09-11)은 기존 Bronze SEC 공시를 읽어 Silver `survivorship/financial_research/us_primary_statement_scope_20260911`에 정규화 결과, 규칙 이관 기록, 코드 사본, 테스트 결과와 원인별 검토 목록을 저장한다. 사용자용 범위·커버리지 요약은 Gold `survivorship/pipeline_verification/20260911_us_primary_statement_scope/summary.json`에 둔다. 이번 실행의 입력 1,455개 해시를 재확인했으며 원문은 변경하지 않았다. 정규화의 원인별 검토 때문에 과거 투자 가능 증권을 제거하지 않는다.


상장 관측 재사용: Alpha 원본 CSV와 수집 메타데이터는 Bronze `alpha-vantage/listings/snapshot_date=...`에 유지한다. 정규 `survivorship` 실행은 Silver `survivorship/us/listing_history/partitions`에 원문·코드 버전별 관측을, `generations`에 기준일별 후보 인덱스와 매니페스트를 저장한다. Gold `survivorship/us/listing_history.json`은 해당 처리 범위와 근거를 연결한다. 연구 검증에서는 동일 구조를 별도 Silver/Gold 출력 디렉터리에 생성한다. 테스트·코드 사본·처리 성능 자료는 Silver에 두며 docs/tests에 원본 CSV·JSON·HTML을 생성하지 않는다.


SEC 공시 식별 관측은 Bronze의 공시 원문을 재사용하고 Silver 정규화 출력의 `security_identity/{symbol}/{accession}.json`에 저장한다. 같은 위치의 `versions`에는 내용 해시별 과거 관측을 보존한다. 원문 경로·해시·발표일과 문맥별 증권 정보를 포함하며 상장 기간은 검증되지 않은 상태로 남긴다. 별도 검증 범위는 Silver `survivorship/financial_research/us_reported_security_identity_20260911`, 사용자용 검증 요약은 Gold `survivorship/pipeline_verification/20260911_us_reported_security_identity`에 둔다.


상장 후보–SEC 식별 연결 결과는 Silver `survivorship/us/identity_linkage/generations/{generation}`에 저장한다. 세대는 목록·SEC 관측·기준일·코드로 구분하며, 모든 원래 공급자 후보 열과 미해결 상태를 보존한다. Gold `survivorship/us/identity_linkage.json`은 연결 범위를 제공한다. 현재 별도 검증은 Silver `survivorship/financial_research/us_listing_identity_linkage_20260911`, Gold `survivorship/pipeline_verification/20260911_us_listing_identity_linkage`를 사용했다. SEC와 Alpha 원본은 Bronze에 유지하고 다운로드를 반복하지 않았다.


SEC 종목 목록 수집은 `bronze/sec/company-tickers/{수집ID}`에 원본 응답과 URL·해시·UTC 수집 시각·HTTP 상태를 보존한다. JSON 오류, 차단 HTML, 빈 응답과 HTTP 오류 본문도 보존하며 정상 Silver 파일을 바꾸지 않는다. 가공 CSV는 `silver/sec/company_tickers.csv`, 과거 변환본과 출처는 `silver/sec/company_tickers.versions`에 둔다. 기본 소비자는 Silver를 읽으며 Silver가 없는 설치에서는 이전 meta 파일을 읽을 수 있다. 명시적으로 지정한 이전 파일은 대체하지 않는다. 기존 meta 파일은 이번에 수정하지 않았다. 사용자용 검증 범위는 Gold `survivorship/pipeline_verification/20260911_sec_ticker_source_retention`에 있다.

SEC 코드 CSV를 읽을 때 `NA` 같은 실제 종목코드를 결측값으로 바꾸지 않는다. 이번 전체 대조는 가공 CSV를 다시 읽어 기대값으로 삼지 않고 원본 JSON의 코드·CIK·이름을 기준으로 수행했다.


SEC 전체 제출 이력은 `bronze/sec/submissions-bulk/{수집ID}/submissions.zip`에 원본 그대로 저장한다. 실패·중단 응답도 같은 수집 폴더에 남기며 정상 `latest.json` 포인터를 대체하지 않는다. 현재·이전 이름, 보고된 코드/거래소 배열, 추가 이력 파일 참조는 `silver/sec/submissions/generations/{generation}/issuer_index.parquet`에 저장한다. 원본 ZIP을 파일별로 풀어 중복 보관하지 않는다. 각 행에 ZIP 멤버 이름과 해시가 남는다. Gold `survivorship/sec_submissions/summary.json`은 처리 범위·오류·검토 사유를 제공한다. 전체 CIK에는 개인 신고자 등도 포함되므로 상장기업 수로 해석하지 않는다.


SEC CIK 조사 결과는 Silver `survivorship/us/issuer_discovery/generations`에 저장한다. `candidate_discovery.parquet`는 모든 공급자 후보와 현재·이전 이름 일치 근거를, `collection_candidates.parquet`는 공시 수집용 CIK별 대상과 관련 후보를 보존한다. 각 결과는 `collection_only=true`이고 확정 증권으로 등록되지 않는다. 원문 ZIP은 Bronze, 메타데이터 인덱스는 기존 Silver에 유지한다. Gold `survivorship/us/issuer_discovery.json`은 조사 범위와 출처를 제공한다. 실제 검증은 Silver `survivorship/financial_research/sec_bulk_issuer_discovery_20260911`, Gold `survivorship/pipeline_verification/20260911_sec_bulk_issuer_discovery`에 있다.

SEC 공시 이력 목록은 Silver `survivorship/us/filing_inventory/generations/{generation}`의 `filings.parquet`, `reviews.json`, `cik_coverage.json`에 저장한다. 원본은 기존 Bronze ZIP이고 가공 행에 멤버 위치·해시를 남긴다. Gold `survivorship/us/filing_inventory.json`은 범위·검토 상태와 가공 데이터 위치를 제공한다. 실제 검증 결과는 Silver `survivorship/financial_research/sec_disclosure_inventory_20260911`, Gold `survivorship/pipeline_verification/20260911_sec_disclosure_inventory`에 있다. 실제 검증의 Parquet는 앞선 조사 단계의 Silver 검증 폴더를 재사용하며 Gold에 원본이나 전체 가공 행을 복제하지 않는다.

Form 25·15 통지의 완전 제출문과 수집 메타데이터는 Bronze `sec/notice-submissions/{CIK}/{접수번호}/{수집ID}`에 보존한다. `latest.json`은 검증된 정상 원문, `last_attempt.json`은 마지막 시도에 대한 해시 고정 포인터다. 실패 응답도 수집 폴더에 남는다. 수집 상태·관측 참조는 Silver `survivorship/us/notice_documents/runs`, XML 증권 종류 관측은 Silver `survivorship/us/notice_observations/generations`에 저장한다. Gold의 `notice_documents.json`과 `notice_observations.json`은 각 단계의 범위·검토 상태·자료 위치를 제공한다. 공시 본문에서 추출한 증권 종류는 승인된 상장 이력과 구분한다.

EDGAR 라이브러리의 원본 HTTP 캐시 기본 위치도 Bronze `sec/edgar-cache/_tcache`로 변경했다. 기존 `data-lake/cache/edgar`의 627,553개 파일·235,999,511,441바이트를 같은 드라이브에서 폴더 이동했다. 이전 경로는 NTFS junction으로 새 위치에 연결해 기존 출처 참조를 유지한다. 원본 응답과 `.meta` 수집 메타데이터를 함께 보존했으며, 자료를 다시 다운로드하거나 파생 재무 수치를 재계산한 작업은 아니다.

이동 전후 전체 상대 경로·크기·수정 시각 목록과 디렉터리 파일 식별자가 같고, 표본 64개의 전체 내용 해시도 일치했다. 모든 파일의 내용 해시를 새로 계산한 검증은 아니다. 실제 라이브러리의 새 경로 캐시 조회와 관련 18개 테스트가 통과했다. 검증 목록은 Silver `survivorship/financial_research/edgar_cache_bronze_20260911`, 결과는 Gold `survivorship/pipeline_verification/20260911_edgar_cache_bronze`에 있다.
