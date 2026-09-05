# ADR-0001: Semantic v4는 precision-first 증거 게이트를 사용한다

- 상태: Accepted
- 날짜: 2026-09-05

## 맥락

K-GAAP, 일반기업회계기준, K-IFRS가 섞인 2000–2012 DART 문서는 같은 표면 문자열이라도
재무제표 종류, 연결/별도 scope, 상위 section, 산업, 문서 dialect에 따라 의미가 달라진다.
서술형 본문에는 계정과 금액이 함께 나타나지만 기간·관계·단위가 모호할 수 있다. 단순 alias
확장은 false positive를 늘리고, 항등식 실패를 곧바로 재매핑 근거로 쓰면 서로 다른 기간이나
scope를 억지로 맞추게 된다.

## 결정

1. 규칙 실행은 HMRB에서 참고한 선언적 match→constraint→emit 사고방식을 자체 typed rule로
   구현한다. HMRB 라이브러리는 의존하지 않는다.
2. spaCy `PhraseMatcher`/token pattern은 후보 생성과 정확 일치 인덱스로 사용하고, 최종 판정은
   회계체계·문서영역·재무제표·scope·sector·period 제약을 모두 통과해야 한다.
3. `ReportedFact → CanonicalFact → HarmonizedFact`를 서로 다른 계층으로 유지한다. 문맥이
   부족하면 harmonized-ready로 세지 않는다.
4. 회계 항등식은 `PASS/REVIEW/NOT_TESTABLE` 증거만 만들며 자동 매핑을 변경하지 않는다.
   `NOT_TESTABLE`은 폐쇄형 사유 코드로 집계한다.
5. 서술형 후보와 빈번한 미매핑 군집은 자동 승격하지 않는다. 사람이 규칙을 검토하고 독립
   golden test를 추가해야 활성화한다.
6. 배포된 규칙 bundle은 수정하지 않는다. 새 버전을 만들고 SHA-256 manifest를 갱신한다.
7. coverage는 문서·행·팩터·셀처럼 서로 다른 분모를 단계별로 명시하며 곱하지 않는다.

## 대안과 기각 이유

- 문자열 유사도 임계치만 낮추기: sector·statement 동음이의 계정을 오매핑한다.
- 항등식 residual이 줄어드는 후보를 자동 채택: 누락 사실이나 섞인 scope가 원인일 때 오탐한다.
- 기존 debug 행을 모두 harmonized fact로 간주: scope/regime가 없는 과거 산출물의 비교 가능성을
  과장한다.
- rule YAML을 제자리 수정: 과거 결과를 재현할 수 없고 audit hash가 무의미해진다.

## 결과

coverage 숫자는 보수적으로 보일 수 있으나 각 탈락 사유를 개선 가능한 작업 목록으로 바꿀 수
있다. 현재 운영 debug corpus는 scope가 없어 strict harmonized-ready가 0이며, 이는 재정규화가
필요하다는 명시적 병목이다. 항등식과 서술형 후보는 사람이 검토할 수 있는 증거를 남기되
production fact를 오염시키지 않는다.
