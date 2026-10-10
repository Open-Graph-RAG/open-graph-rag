import copy
import json
import unittest
from pathlib import Path
from validation.product.canonical import prepare_candidates, validate_provenance


class CanonicalTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.corpus = [json.loads(line) for line in (root / 'dataset/corpus.jsonl').read_text().splitlines()]

    def test_source_only_preparation_direction_supersession_and_repeatability(self):
        facts = prepare_candidates(self.corpus)
        self.assertEqual(facts, prepare_candidates(self.corpus))
        self.assertEqual(len(facts), 285)
        self.assertTrue(validate_provenance(facts, self.corpus))
        entities = {f['id']: f for f in facts if f['kind'] == 'entity'}
        for f in facts:
            if f.get('predicate') == 'DEPENDS_ON':
                self.assertTrue(entities[f['object_id']]['properties']['name'].endswith('Service'))
                self.assertFalse(entities[f['subject_id']]['properties']['name'].endswith('Service'))
            if f.get('predicate') == 'SUPERSEDES':
                self.assertEqual(entities[f['subject_id']]['properties']['status'], 'accepted')
                self.assertEqual(entities[f['object_id']]['properties']['status'], 'superseded')
        demo_source='development-01/demo-product-knowledge-v1-context-product-overview.txt'
        demo_edges={(entities[f['subject_id']]['properties']['name'],entities[f['object_id']]['properties']['name'])
                    for f in facts if f.get('predicate')=='DEPENDS_ON' and f['provenance'][0]['source_id']==demo_source}
        self.assertEqual(demo_edges,{('Customer Portal','Subscription Service'),('Operations Console','Subscription Service')})

    def test_missing_source_and_wrong_version_never_resolve(self):
        facts = prepare_candidates(self.corpus)
        for field, value in [('source_id', 'absent'), ('source_version', '999'), ('chunk_id', 'absent')]:
            bad = copy.deepcopy(facts)
            bad[0]['provenance'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_provenance(bad, self.corpus)
