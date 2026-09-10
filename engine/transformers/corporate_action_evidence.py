"""Reproducible, reviewed links between exact ratios and official trading dates.

The manifest contains factual assertions, source hashes and exact text markers.
It supplements strict parsers when two different official documents are needed;
it never supplies ratios inferred from prices or rounded share capital.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from engine.transformers.stock_splits import SplitEvent, text_of_html

MANIFEST = Path(__file__).with_name('corporate_action_evidence.json')


def evidence_records(path=None):
    return json.loads(Path(path or MANIFEST).read_text(encoding='utf-8'))['records']


def validate_evidence(root, *, as_of, manifest_path=None):
    events, episodes, reviews = [], [], []
    for record in evidence_records(manifest_path):
        if record['effective_date'] > as_of:
            continue
        sid = record['security_id']
        sources = []
        try:
            for source in record['sources']:
                if source['published_date'] > as_of:
                    raise ValueError('supporting evidence is not yet published')
                filename = f"kind_{source['source_id']}_{source['kind_doc_no']}.html"
                path = Path(root) / 'disclosures' / sid.removeprefix('SEC_KR_') / filename
                raw = path.read_bytes()
                meta = json.loads(path.with_suffix('.html.metadata.json').read_text(encoding='utf-8'))
                if (hashlib.sha256(raw).hexdigest() != source['source_sha256']
                        or any(meta.get(k) != source[k] for k in
                               ('source_url', 'source_id', 'published_date', 'kind_doc_no'))
                        or meta.get('security_id') != sid):
                    raise ValueError('reviewed official source identity/hash changed')
                compact = re.sub(r'\s+', '', text_of_html(raw))
                if not source['markers'] or any(re.sub(r'\s+', '', marker) not in compact
                                                for marker in source['markers']):
                    raise ValueError('reviewed evidence text is missing')
                sources.append(source)
            if record['kind'] == 'new_listing':
                episodes.append({'security_id': sid, 'effective_date': record['effective_date'],
                                 'evidence': sources})
            elif record['kind'] == 'share_consolidation':
                ratio_source = next(s for s in sources if s['role'] == 'exact_ratio')
                actual_source = next(s for s in sources if s['role'] == 'actual_listing')
                if actual_source['source_id'] == ratio_source['source_id']:
                    raise ValueError('independent actual listing evidence is required')
                if not any(record['effective_date'] in m for m in actual_source['markers']):
                    raise ValueError('trading date assertion lacks an actual listing marker')
                if record['ratio_marker'] not in ratio_source['markers']:
                    raise ValueError('exact ratio assertion has no linked source marker')
                # The reviewed marker states old:new (Korean consolidation convention).
                marker = f"({record['old_shares']}:{record['new_shares']})"
                if marker not in record['ratio_marker']:
                    raise ValueError('manifest ratio does not match its explicit source text')
                events.append(SplitEvent(
                    security_id=sid, effective_date=record['effective_date'],
                    new_shares=record['new_shares'], old_shares=record['old_shares'],
                    source='KIND', source_id=ratio_source['source_id'],
                    source_url=ratio_source['source_url'], source_sha256=ratio_source['source_sha256'],
                    published_date=max(s['published_date'] for s in sources),
                    action_type='reverse_split', evidence=json.dumps(sources, ensure_ascii=False)))
            else:
                raise ValueError('unsupported reviewed corporate action')
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            reviews.append({'security_id': sid, 'effective_date': record['effective_date'],
                            'reason': 'reviewed_evidence_unavailable', 'detail': str(exc)})
    return events, episodes, reviews
