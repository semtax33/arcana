# 주식분할·생존편향 데이터 저장 규칙

수집·가공·사용자 제공 단계의 파일을 `data-lake` 아래에 저장한다. 확장자가 아니라 데이터의 역할로 계층을 결정한다.

| 계층 | 내용 | 기본 경로 예시 |
| --- | --- | --- |
| bronze | 공급자가 반환한 원문과 수집 출처·요청·해시 메타데이터 | `bronze/dart/stock_splits`, `bronze/sec/stock_splits`, `bronze/dart/listings`, `bronze/consensus/alpha-vantage` |
| silver | 정규화 사건 원장, 수정주가 계산 패널, 상장 이력, 시총 계산, 검증 결과 | `silver/corporate_actions`, `silver/survivorship/{market}` |
| gold | 사용자에게 제공하는 사건·상장 이력·권리·미해결 항목·수정종가·시총 파일과 요약 | `gold/corporate_actions/{market}`, `gold/survivorship/{market}` |

`market`은 `kr` 또는 `us`다. API 응답의 원본 바이트는 변경하지 않는다. 갱신 전 원문의 보관 사본도 `bronze/source-archive/{market}/{run_id}`에 쓴다. 가공 결과에는 출처와 해시를 유지한다. API 키는 출처 URL과 메타데이터에 넣지 않는다. 검토용 JSON이나 정규화 CSV는 silver에 저장한다. 문서·수집기·테스트 코드는 기존 코드 폴더에 둔다.

## 파이프라인 출력

주식분할 갱신은 silver 사건 원장과 가격 패널을 만든 뒤 gold의 `stock_splits.json`, `prices/{market}_{symbol}.parquet`, `summary.json`을 저장한다. 주식병합도 같은 기업행위 경로를 사용한다. 독립 실행의 `--gold-output` 또는 `run_stock_split_refresh(gold_dir=...)`로 gold 위치를 지정할 수 있다.

생존편향 갱신은 gold에 `listing_episodes.json`, `events.json`, `entitlements.json`, `unresolved.json`, `prices/{market}_{symbol}.parquet`, `market_cap_factors.parquet`, `summary.json`을 저장한다. `--survivorship-output`은 silver, `--survivorship-gold-output`은 gold 경로다. `--skip-clickhouse` 실행에서도 파일은 두 계층에 저장된다. 검토 목록이 없으면 gold 요약에 `awaiting_review`를 표시한다.

gold 가격 파일의 `adj_close`는 분할·병합만 반영한 수정종가다. `open`, `high`, `low`, `close`, `volume`은 거래 당시 값이며, 배당을 포함한 총수익률 가격이 아니다. 요약의 `artifacts`가 해당 실행에서 생성한 파일과 SHA-256을 가리킨다. 파일별 교체 후 요약을 마지막에 쓴다. 전체 디렉터리를 한 번에 교체하는 트랜잭션은 아니므로 동시 열람 시 파일 해시를 확인해야 한다.

gold에 있다는 사실이 검증 완료를 의미하지 않는다. `coverage_complete=false`와 미해결 사건을 함께 제공하며, 미확정 현금·CVR·비상장 주식·단주 권리를 0으로 바꾸지 않는다. 생존편향의 전체 시장 보정 여부는 [파이프라인 현황](survivorship-pipeline.md)을 따른다.

## 연구 자료와 재현

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
