"""Write a factual report only after locked assessment and native reproduction."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd

from scripts.research_cross_market_corrected_cache import OUT, digest, load_json, save_json
from scripts.research_cross_market_factorlab import verify_inputs
from engine.workflows.stock_splits import ledger_path


def percent(value):
    return '—' if value is None else f'{value:.2%}'


def number(value):
    return '—' if value is None else f'{value:.3f}'


def main():
    files = verify_inputs()
    selection = load_json(OUT / 'frozen_selection.json')
    assessment = load_json(OUT / 'holdout_assessment.json')
    parity = load_json(OUT / 'factorlab_parity.json')
    stage = load_json(OUT / 'factorlab_stage.json')
    validation = load_json(OUT / 'factorlab_validation.json')
    if assessment['selection_sha256'] != digest(OUT / 'frozen_selection.json'):
        raise ValueError('Assessment does not match frozen selection')
    if parity['status'] != 'passed' or stage['files'] != files:
        raise ValueError('Complete native FactorLab reproduction with current inputs')
    expected_runs = {(r['market'], r['recipe_id']) for r in validation['runs']}
    frozen_runs = {(market, spec['id']) for market in ['KR', 'US']
                   for spec in [selection['common'], selection['country_specific'][market]]}
    if expected_runs != frozen_runs:
        raise ValueError('Native run coverage does not match all frozen recipes')
    if expected_runs != {(r['market'], r['recipe_id']) for r in parity['checks']}:
        raise ValueError('Native parity coverage is incomplete')

    policy = load_json(OUT / 'factor_policy.json')
    source_qa_document = load_json(OUT / 'factor_source_qa.json')
    source_qa = source_qa_document['withheld_factors']
    user_excluded=source_qa_document.get('excluded_by_user',{})
    screen = load_json(OUT / 'single_factor_screen.json')
    combinations = load_json(OUT / 'combinations.json')['candidates']
    last_price_dates={market:load_json(OUT/f'{market}_corrected_manifest.json')['last_price_date'] for market in ['KR','US']}
    rows = []
    for factor in policy['factors']:
        row = dict(factor)
        row['source_qa_withheld_reason'] = source_qa.get(factor['factor_id'], '')
        row['user_exclusion_reason']=user_excluded.get(factor['factor_id'],'')
        for market in ['KR', 'US']:
            row[f'{market}_source_qa_withheld_reason'] = source_qa_document.get('withheld_by_market', {}).get(market, {}).get(factor['factor_id'], '')
            cov = screen['coverage'][market].get(factor['factor_id'], {})
            row[f'{market}_min_signal_count'] = cov.get('min_count')
            row[f'{market}_min_signal_fraction'] = cov.get('min_fraction')
            row[f'{market}_screened'] = any(
                r['factor_id'] == factor['factor_id'] and market in r['results']
                for r in screen['candidates'])
        rows.append(row)
    pd.DataFrame(rows).to_csv(OUT / 'all_nonlab_factor_audit.csv', index=False, encoding='utf-8-sig')

    common = [r for r in assessment['results'] if r['recipe_kind'] == 'common' and r.get('cost_bps') == 50]
    native_ok = {(r['market'], r['recipe_id']) for r in parity['checks']
                 if r['holdings_match'] and r['returns_verified']}
    passed = (len(common) == 2 and all(r['validated_target_met'] and
              (r['market'], r['recipe_id']) in native_ok for r in common))
    country_pair = [r for r in assessment['results'] if r['recipe_kind'] == 'country_specific' and r.get('cost_bps') == 50]
    country_passed = (len(country_pair) == 2 and all(r['validated_target_met'] and
                     (r['market'], r['recipe_id']) in native_ok for r in country_pair))
    conclusion = ('동일한 조합이 양국의 보류 구간에서 샤프 1을 넘고 수익률 검증을 통과했다.' if passed else
                  '국가별로 고정한 전략이 한국·미국 각각의 보류 구간에서 샤프 1을 넘고 수익률 검증을 통과했다.' if country_passed else
                  '이번 후보 집합에서는 양국의 보류 구간과 수익률 검증을 모두 통과한 전략 쌍을 확인하지 못했다.')
    text = ['# 미국·한국 시총 상위 70% 팩터 연구', '', conclusion, '',
            '샤프 통과 여부는 연 4% 무위험수익률을 차감한 일별 수익률 기준이다. '
            '표본 밖 성과나 모든 가능한 전략 중 최대 수익을 보장하는 결과는 아니다.', '',
            '## 고정한 전략', '',
            f"공통 후보 `{selection['common']['id']}`: 분기 리밸런싱, 최대 {selection['common']['positions']}종목, "
            f"동일 초기 비중, 조건 `{selection['common'].get('gate') or '없음'}`.", '',
            '| 팩터 | 경제적 역할 | 방향 | 비중 |', '| --- | --- | --- | ---: |']
    by_factor = {r['factor_id']: r for r in policy['factors']}
    for factor, weight in selection['common']['weights'].items():
        spec = by_factor[factor]
        text.append(f"| `{factor}` | {spec['economic_family']} | {spec['direction']} | {percent(weight)} |")
    text += ['', '국가별 후보는 공통 조합과 해당 국가에서 사용 가능한 모든 단일 팩터·종목 수 후보를 비교해 따로 고정했다.', '',
             '| 국가 | 후보 | 최대 종목 수 | 조건 | 팩터 비중 |', '| --- | --- | ---: | --- | --- |']
    for market,spec in selection['country_specific'].items():
        weights=', '.join(f'{f} {percent(w)}' for f,w in spec['weights'].items())
        text.append(f"| {market} | `{spec['id']}` | {spec['positions']} | {spec.get('gate') or '없음'} | {weights} |")
    text += ['', '순위는 각 신호일의 시총 상위 70% 전체 모집단 안에서 계산한다. '
             '음수 가치배수는 저평가로 취급하지 않으며, 조합 구성요소가 결측이면 최종 점수를 만들지 않는다.', '',
             '## 비용 차감 성과', '',
             '선택에 쓰지 않은 보류 구간은 2024-01-01~2026-09-04다. '
             '아래 50bp는 편도 비용이며 이후 리밸런싱에는 매도·매수 합계 100bp를 차감한다.', '',
             '| 후보 | 시장 | 구간 | CAGR | 샤프(0%) | 샤프(4%) | MDD | 수익률 검증 |',
             '| --- | --- | --- | ---: | ---: | ---: | ---: | --- |']
    for result in assessment['results']:
        if result.get('cost_bps') != 50:
            continue
        for period, label in [('train', '2017~2020'), ('validation', '2021~2023'),
                              ('holdout', '2024~'+last_price_dates[result['market']]), ('full', '전체')]:
            m = result[period]
            text.append(f"| {result['recipe_kind']} | {result['market']} | {label} | {percent(m['cagr'])} | "
                        f"{number(m['sharpe'])} | {number(m['sharpe_rf4'])} | {percent(m['mdd'])} | "
                        f"{'통과' if result['returns_verified'] else '미해결 항목 있음'} |")
    text += ['', '## 비용 스트레스와 실행 점검', '',
             '| 후보 | 시장 | 편도 비용(bp) | 보류 CAGR | 보류 샤프(4%) | 검증된 목표 충족 |',
             '| --- | --- | ---: | ---: | ---: | --- |']
    for result in assessment['results']:
        if 'cost_bps' not in result:
            text.append(f"| {result['recipe_kind']} | {result['market']} | — | — | — | {result['status']} |")
            continue
        m = result['holdout']
        text.append(f"| {result['recipe_kind']} | {result['market']} | {result['cost_bps']} | {percent(m['cagr'])} | "
                    f"{number(m['sharpe_rf4'])} | {'예' if result['validated_target_met'] else '아니요'} |")
    issues = []
    for result in assessment['results']:
        if result.get('cost_bps') == 50:
            issues += [dict(market=result['market'], recipe_kind=result['recipe_kind'], **issue)
                       for issue in result['execution_issues']]
    save_json(OUT / 'selected_execution_issues.json', {'issues': issues})
    text += ['', f"검토 항목 {len(issues)}건은 [보유 구간별 실행 점검](selected_execution_issues.json)에 보존했다. "
             '여기에는 보유 중 재산변동·체결 문제뿐 아니라 편입 신호의 미해결 과거 가격, '
             '주별 베타 입력 검토도 포함한다. 검토 표지는 종목이나 수익률을 제거하는 필터가 아니다.', '',
             '## 데이터와 전체 팩터 범위', '',
             f"`lab_*`를 제외한 카탈로그 {len(policy['factors'])}개 중 사용자 요청으로 스타일 {len(user_excluded)}개를 제외하고 원시 팩터를 평가했다. 역할별 개수는 " +
             ', '.join(f'{k} {v}개' for k, v in Counter(r['role'] for r in policy['factors'] if r['factor_id'] not in user_excluded).items()) + '다. '
             f"주당 단위 또는 분모·구성요소의 유효성이 확인되지 않은 {len(source_qa)}개는 성과 순위 사용을 보류했다. "
             '[전체 팩터별 역할·가용성·보류 이유](all_nonlab_factor_audit.csv)를 함께 제공한다.', '',
             f"단일 팩터·종목 수 후보 {len(screen['candidates'])}개와 조합 후보 {len(combinations)}개를 기록했다. "
             '2017~2023년 자료로 후보를 고정한 뒤 보류 구간을 열었다. 양국 수익률을 각각 계산하며 통화를 합산하지 않는다.', '',
             '| 시장 | 확정 원장 사건 | 재상장 경계 | 상위 70% 진입 종목 | 가격 점검 오류 | 최종 가격 관측일 |',
             '| --- | ---: | ---: | ---: | ---: | --- |']
    for market in ['KR', 'US']:
        ledger = load_json(ledger_path(market.lower()))
        qa = load_json(OUT / f'{market}_observed_price_qa.json')
        manifest = load_json(OUT / f'{market}_corrected_manifest.json')
        text.append(f"| {market} | {len(ledger['events'])} | {len(ledger.get('listing_episodes', []))} | "
                    f"{manifest['securities']} | {len(qa['errors'])} | {manifest['last_price_date']} |")
    text += ['', '미국 시세는 Alpha Vantage, 한국 시세는 KRX 원시 가격을 사용했다. '
             'DART·KIND·EDGAR 등 공식 자료로 분할·주식 병합 및 균등 무상감자의 비율과 실제 적용일을 검증했다. '
             '수정종가는 주식 단위 변경을 반영한 가격 수익 기준이며 현금배당 재투자 총수익이 아니다.', '',
             '현재 증권 마스터·업종 분류와 불완전한 과거 상장폐지·공시 자료가 남아 있어 완전한 생존편향 제거를 주장하지 않는다. '
             '미국은 과거 Alpha Vantage 상장 목록과 공시된 실제 보통주식수를 추가 사용했다. '
             '공시 공개일 이전 재무값 사용을 막았지만, 원천 자료의 모든 정정본 이력이 복원됐다는 뜻은 아니다.', '',
             'ROE는 같은 재무 기준의 평균 지배주주자본과 현재 지배주주자본, 부채비율은 총자본이 양수인 관측만 순위에 사용했다. '
             '원시 계산값과 시총 모집단은 보존했으며 [한국](KR_economic_validity.json)·[미국](US_economic_validity.json)의 제외 관측을 기록했다. '
             'ROE 성장률 3종은 과거 분모까지 확인되지 않아 성과 순위에서 보류했다.', '',
             '기존 미국 시장지수의 Alpha Vantage 출처가 확인되지 않아, 그 입력에 의존하는 미국 베타·WACC 파생 팩터도 선정에서 보류했다. '
             '한국 사용 여부와 원시 계산값은 보존했으며 국가별 보류 이유를 전체 팩터 감사표에 기록했다.', '',
             '## 실제 FactorLab 재현', '',
             f"별도 연구 DB `{stage['database']}`에서 실제 FactorLab 서비스를 실행했다. "
             '신호일·매매일·선정 종목·점수·모든 거래일 NAV를 연구 계산과 비교했다. '
             'FactorLab은 첫 편입에도 100bp를 차감하므로 연구의 첫 50bp 비용과 그 차이를 명시적으로 맞춰 비교했다.', '',
             '연구 DB 적재 시 숫자 `-0.0`과 `+0.0`을 같은 0으로 통일했다. '
             'ClickHouse에서 정렬상 같은 두 표현의 동점 집계가 분리되는 현상을 확인했기 때문이다. '
             '원본 팩터 파일과 선택한 전략은 보존했으며 이 적재 규칙도 연구 DB 식별 해시에 포함했다.', '',
             '[실행 내역 및 그래프 파일](factorlab_validation.json) · [수치 재현 검증](factorlab_parity.json) · '
             '[고정 후보](frozen_selection.json) · [보류 구간 원결과](holdout_assessment.json)', '',
             '파이프라인 실행법과 원천 경로는 [분할·병합 파이프라인 문서](../../../docs/stock-split-pipeline.md), '
             '경제적 방향의 근거는 [사전 가설](../../../docs/research/cross_market_factor_economic_rationale_20260909.md)에 있다.', '']
    (OUT / 'report.md').write_text('\n'.join(text), encoding='utf-8')
    save_json(OUT / 'report_manifest.json', {'validated_common_target_met': passed,
              'validated_country_pair_target_met':country_passed,
              'files': files, 'report_sha256': digest(OUT / 'report.md'),
              'holdout_sha256': digest(OUT / 'holdout_assessment.json'),
              'parity_sha256': digest(OUT / 'factorlab_parity.json')})
    print(conclusion, flush=True)


if __name__ == '__main__':
    main()
