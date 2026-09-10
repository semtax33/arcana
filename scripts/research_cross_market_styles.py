"""Recompute the seven native style factors inside each top-70% population."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from api.service.style_score_catalog import STYLE_SCORE_FACTORS
from engine.transformers.style_score_definitions import (
    STYLE_FACTOR_DEFINITIONS, US_CONSENSUS_CORE_FACTORS,
    style_profile_weights, style_weights_for_country,
)
from engine.workflows._internal.score_workflow import calculate_factor_scores, STYLE_SCORE_COLUMNS
from scripts.research_cross_market_corrected_cache import OUT, digest, load_json, save_frame, save_json


def aggregate_native_style_values(factor_scores, country):
    """Same missing-weight and US-consensus rules as calculate_style_scores."""
    valid=factor_scores.factor_id.notna() & factor_scores.is_valid.astype(bool)
    values=factor_scores.loc[valid].pivot(index='security_id',columns='factor_id',values='percentile_score')
    result=pd.DataFrame(index=values.index)
    for style,weights in style_weights_for_country(country).items():
        matrix=values.reindex(columns=list(weights));w=pd.Series(weights)
        available=matrix.notna().mul(w).sum(axis=1)
        score=matrix.fillna(0).mul(w).sum(axis=1)/available.where(available.gt(0))
        if country.upper()=='US' and style=='CONSENSUS':
            score=score.where(values.reindex(columns=sorted(US_CONSENSUS_CORE_FACTORS)).notna().all(axis=1))
        result[STYLE_SCORE_COLUMNS[style]]=score
    weights={STYLE_SCORE_COLUMNS[k]:v for k,v in style_profile_weights('DEFAULT').items()}
    matrix=result.reindex(columns=list(weights));w=pd.Series(weights)
    available=matrix.notna().mul(w).sum(axis=1)
    result['total_score']=matrix.fillna(0).mul(w).sum(axis=1)/available.where(available.gt(0))
    return result.rename(columns={d.column_name:k for k,d in STYLE_SCORE_FACTORS.items()})


def calculate_style_year(args):
    """Independent native style calculation; only the parent writes artifacts."""
    path,label,classification=args
    raw=pd.read_parquet(path);parts=[]
    for day,group in raw.groupby(level='trade_date',sort=True):
        universe=classification[classification.security_id.isin(group.index.get_level_values('security_id'))]
        selected=group.reindex(columns=[f for f in STYLE_FACTOR_DEFINITIONS if f in group])
        factors=selected.stack(dropna=False).rename('factor_value').reset_index()
        factors.columns=['trade_date','security_id','factor_id','factor_value']
        scores=calculate_factor_scores(universe,factors,trade_date=day.date(),exclude_financials=True)
        if scores.empty:continue
        style=aggregate_native_style_values(scores,label)
        style['trade_date']=day;style=style.reset_index().set_index(['trade_date','security_id'])
        parts.append(style.reindex(columns=STYLE_SCORE_FACTORS))
    styles=pd.concat(parts) if parts else pd.DataFrame(index=raw.index,columns=STYLE_SCORE_FACTORS)
    result=raw.drop(columns=list(STYLE_SCORE_FACTORS),errors='ignore').join(styles,how='left')
    return path,result


def build_styles(market,workers=3):
    label=market.upper();manifest_path=OUT/f'{label}_corrected_manifest.json'
    manifest=load_json(manifest_path)
    if manifest.get('style_scores_omitted_by_user'):raise ValueError('Style calculation is excluded by the current user scope')
    if manifest.get('status')!='ready':raise ValueError('assemble corrected factors first')
    classification_path=OUT/'inputs'/'issuer_classification.parquet'
    classification=pd.read_parquet(classification_path).drop_duplicates('security_id').fillna('')
    classification['is_financial']=(classification.industry_group_name.str.contains('금융')
        |classification.sector_code.isin(['FINANCIALS','40'])|classification.industry_group_code.isin(['FINANCIALS','40']))
    qa=load_json(OUT/'factor_source_qa.json');raw_withheld=set(qa['withheld_factors'])
    dependency_issues={}
    for style,weights in style_weights_for_country(label).items():
        missing=sorted(set(weights)&raw_withheld)
        factor='style_'+STYLE_SCORE_COLUMNS[style]
        if missing and factor in STYLE_SCORE_FACTORS:dependency_issues[factor]=missing
    if dependency_issues:dependency_issues['style_total_score']=sorted({x for xs in dependency_issues.values() for x in xs})
    for factor,dependencies in dependency_issues.items():
        qa['withheld_factors'][factor]='native style includes inputs with unverified share units or denominators: '+','.join(dependencies)
    save_json(OUT/'factor_source_qa.json',qa)
    print(label,'style source dependencies saved',flush=True)
    records=[]
    tasks=[(path,label,classification) for path in sorted(OUT.glob(f'{label}_factors_*.parquet'))]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for path,result in pool.map(calculate_style_year,tasks):
            save_frame(path,result)
            manifest.setdefault('factor_files',{})[path.name]=digest(path)
            records.append({'year':int(path.stem[-4:]),'sha256':digest(path),'style_coverage':result[list(STYLE_SCORE_FACTORS)].notna().sum().to_dict()})
            print(label,'styles',path.stem[-4:],flush=True)
    manifest['style_factors']={'profile':'DEFAULT','classification_basis':'current snapshot',
        'classification_sha256':digest(classification_path),'source_qa_dependencies':dependency_issues,
        'normalization_population':'top70 before factor availability; native financial-sector exclusion',
        'code_sha256':digest(Path(__file__)),'files':records}
    save_json(manifest_path,manifest)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--market',required=True,choices=['kr','us'])
    parser.add_argument('--workers',type=int,default=3)
    args=parser.parse_args();build_styles(args.market,args.workers)
