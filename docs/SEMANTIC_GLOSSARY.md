# Semantic rule engine 용어집

| 용어 | 정의 |
| --- | --- |
| Document IR | 원문 표, 행/열 병합, section 경로, 문서 dialect와 provenance를 보존한 중간 표현. |
| ReportedFact | 발행사가 보고한 label·금액·단위·기간·scope를 원문 의미 그대로 보존한 사실. |
| CanonicalFact | 규칙의 문맥 제약을 통과해 표준 계정 ID에 연결된 사실. 원문 사실과 provenance를 계속 보유한다. |
| HarmonizedFact | 비교 목적의 bridge rule까지 적용해 분석 지표로 사용할 수 있는 사실. CanonicalFact와 동일하지 않다. |
| accounting regime | K_GAAP, GENERAL_K_GAAP, K_IFRS 또는 UNKNOWN. 2009–2012도 날짜로 강제하지 않는다. |
| document dialect | DART legacy HTML, transitional XBRL, XBRL 등 문서 표현 계열. |
| scope | CONSOLIDATED, SEPARATE 또는 UNKNOWN. 서로 다른 scope는 항등식에서 섞지 않는다. |
| cash direction | inflow/outflow. 표시 부호와 경제적 현금 방향을 분리한다. |
| comparability | EXACT, 표시/집계/측정 차이, 정책 단절 등 기간 간 비교 가능성 표지. |
| semantic loss | 원문 qualifier나 세부 의미가 canonical/harmonized 변환에서 보존됐는지 나타내는 증거. |
| NOT_TESTABLE | 항등식을 안전하게 계산할 증거가 부족한 상태. 실패나 0으로 해석하지 않는다. |
| invariant REVIEW | 허용오차를 넘는 residual. 오매핑 후보 증거일 뿐 자동 교정 명령이 아니다. |
| narrative candidate | 주석/사업내용 prose에서 spaCy matcher로 발견한 계정–금액–관계 후보. 승인 전에는 canonical fact가 아니다. |
| coverage waterfall | Document IR, ReportedFact, CanonicalFact, HarmonizedFact, factor input, factor cell을 각자의 분모로 보여주는 단계표. |
| factor dependency coverage | 필요한 canonical 입력이 모두 관측된 재무입력 의존 팩터 비율. 실제 계산 셀 비율과 다르다. |
| factor cell coverage | materialized 종목×날짜×팩터 셀 중 유한값이 있는 비율. |
| immutable bundle | 공개 후 제자리 수정하지 않는 버전 규칙 파일. manifest의 SHA-256으로 검증한다. |
| executable dependency path | 코드의 `first`/fallback/derivation 중 하나의 완전한 대체 경로. 모든 대체 입력의 합집합과 다르다. |
| strict-union dependency | 팩터 코드에 언급된 모든 canonical 입력을 AND로 본 과거 비교용 지표. 실행 가능 coverage로 해석하지 않는다. |
| financial availability date | 결산일이 아니라 해당 정규화 값이 공개된 것으로 보수적으로 인정하는 DART 공시일. |
| PIT-safe | 특정 거래일의 계산이 그 날짜까지 공개된 데이터만 사용한다는 속성. |
| company-year completeness | 회사·회계연도별 BS/IS/CF 존재, 핵심 사실, 팩터 입력, 항등식 시험 가능성을 분리한 상태. |
| golden contract corpus | 이관된 원천 규칙의 실행 계약을 확인하는 회귀 사례. 독립 라벨 정확도 corpus와는 다르다. |
| portfolio drift | 규칙 변경 전후의 값·백분위·decile·포트폴리오 편입·IC·long-short·turnover 변화. |
