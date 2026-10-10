"""Source-only product fixtures must pass the actual ontology acceptance contract."""
import json
import unittest
from pathlib import Path
from services.ontology.store import MemoryStore
from services.ontology.validation import load_definition
from validation.product.canonical import prepare_candidates

class ProductCandidateContracts(unittest.TestCase):
    def test_prepare_accept_version_and_outbox_without_duplicates(self):
        root = Path(__file__).resolve().parents[2]
        corpus = [json.loads(s) for s in (root/'validation/product/dataset/corpus.jsonl').read_text().splitlines()]
        facts = prepare_candidates(corpus)
        store = MemoryStore()
        definition = load_definition((root/'ontology/definitions/ogr-core/1.0.0.yaml').read_text())
        store.save_draft('validation_governed',definition,'fixture-reviewer')
        store.publish('validation_governed','ogr-core','1.0.0','fixture-reviewer')
        for fact in facts:
            first = store.write_fact('validation_governed',fact)
            self.assertEqual(first['status'],'accepted')
            self.assertEqual(first,store.write_fact('validation_governed',fact))
        self.assertEqual(len(store.list_facts('validation_governed')),len(facts))
        self.assertEqual(store.list_facts('baseline'),[])
        self.assertEqual(len(store.list_sync('validation_governed')),len(facts))
