"""Blinded semantic-review packets; labels and private mapping never mix."""
from __future__ import annotations

import json
import random
from pathlib import Path


def export_review(rows, cases, gold, output_dir, seed=20261010):
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError('review export destination exists; refusing overwrite')
    case_by = {c['id']: c for c in cases}
    gold_by = {g['case_id']: g for g in gold}
    packet, key = [], []
    seen = set()
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    for row in shuffled:
        rid = row.get('response_id')
        if not rid or rid in seen:
            raise ValueError('missing or duplicate blind response ID')
        seen.add(rid)
        cid = row['case_id']
        if cid not in case_by or cid not in gold_by:
            raise ValueError('review case/gold not found')
        item = {'blind_response_id': rid, 'question': case_by[cid]['question'],
            'category': case_by[cid]['category'], 'status': row['status'],
            'answer': row.get('answer'), 'original_evidence': row.get('evidence'),
            'rubric': gold_by[cid]['expected'], 'gold_evidence': gold_by[cid]['evidence']}
        packet.append(item)
        key.append({'blind_response_id': rid, 'case_id': cid, 'arm': row['arm'], 'repeat': row['repeat']})
    output_dir.mkdir(parents=True, mode=0o700)
    for name, data in [('blind-packet.jsonl', packet), ('unblinding-key.jsonl', key)]:
        path = output_dir / name
        with path.open('x', encoding='utf-8') as f:
            f.write(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in data))
        path.chmod(0o600)
    return {'review_packet': str(output_dir / 'blind-packet.jsonl'),
        'private_unblinding_key': str(output_dir / 'unblinding-key.jsonl'),
        'response_count': len(rows), 'note': 'Send only blind-packet to reviewers, never unblinding-key.'}
