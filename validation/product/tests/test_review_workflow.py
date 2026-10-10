import json
import tempfile
import unittest
from pathlib import Path
from validation.product.review.workflow import export_review

class ReviewWorkflowTests(unittest.TestCase):
    def test_packet_has_no_variant_labels_and_key_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{'response_id':'r1','case_id':'c1','arm':'candidate','repeat':1,
                'status':'completed','answer':{'response':'answer'},'evidence':{'chunks':[]}}]
            cases = [{'id':'c1','question':'question','category':'multi_hop'}]
            gold = [{'case_id':'c1','expected':{'known_unknowns':[]},'evidence':[]}]
            out = Path(tmp)/'review'
            export_review(rows,cases,gold,out)
            packet = json.loads((out/'blind-packet.jsonl').read_text())
            self.assertNotIn('arm',packet)
            self.assertNotIn('candidate',json.dumps(packet))
            self.assertEqual(json.loads((out/'unblinding-key.jsonl').read_text())['arm'],'candidate')
            self.assertEqual((out/'unblinding-key.jsonl').stat().st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError): export_review(rows,cases,gold,out)
