import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from validation.product.adapters.live import LiveAdapter, LiveRunError, bounded_post


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'corpus.jsonl').write_text('{}\n')
        self.cfg = {'_config_base': str(self.root), 'arms': ['baseline', 'candidate'],
            'dataset': {'corpus': 'corpus.jsonl'},
            'budget': {'max_requests': 8, 'max_total_input_tokens': 100000,
                'max_total_output_tokens': 400, 'max_input_tokens': 12000,
                'max_output_tokens': 100, 'max_request_bytes': 65536,
                'max_response_bytes': 65536,
                'max_cost_usd': 1, 'max_duration_seconds': 3600,
                'request_timeout_seconds': 1, 'input_usd_per_million': 1,
                'output_usd_per_million': 2},
            'live': {'model': 'frozen-model', 'retrieval': {'mode': 'mix', 'top_k': 12},
                'equivalence_manifest': 'equivalence.json',
                'baseline_url_env': 'TEST_BASE', 'candidate_url_env': 'TEST_CAND',
                'baseline_key_env': 'TEST_BASE_KEY', 'candidate_key_env': 'TEST_CAND_KEY',
                'provider_url_env': 'TEST_PROVIDER', 'provider_key_env': 'TEST_PROVIDER_KEY',
                'allowed_retrieval_hosts': ['127.0.0.1'],
                'allowed_provider_hosts': ['provider.example']}}
        self.attest = {'shared_snapshot_sha256': 'a' * 64,
            'corpus_sha256': hashlib.sha256((self.root/'corpus.jsonl').read_bytes()).hexdigest(),
            'model_revision': 'frozen-model', 'embedding_revision': 'local-model',
            'embedding_dimensions': 768, 'retrieval': self.cfg['live']['retrieval'],
            'only_candidate_projection': True, 'baseline_preserved': True,
            'synthetic_or_authorized_sources': True, 'retrieval_uses_only_local_models': True}
        self.write_attest()
        self.env = patch.dict(os.environ, {'TEST_BASE': 'http://127.0.0.1:9621',
            'TEST_CAND': 'http://127.0.0.1:9622', 'TEST_BASE_KEY': 'fake-base',
            'TEST_CAND_KEY': 'fake-cand', 'TEST_PROVIDER': 'https://provider.example/v1',
            'TEST_PROVIDER_KEY': 'fake-provider'})
        self.env.start()
        self.calls = []
        self.case = {'id': 'one', 'question': 'Which systems are affected?', 'expected': 'SECRET_GOLD'}

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def write_attest(self):
        (self.root/'equivalence.json').write_text(json.dumps(self.attest))

    def transport(self, url, payload, headers, timeout, request_limit, response_limit):
        self.calls.append((url, copy.deepcopy(payload)))
        if url.endswith('/query/data'):
            return {'data': {'chunks': [{'source_id': 'source.md', 'content': 'original text'}]}}
        return {'choices': [{'message': {'content': json.dumps({'response': 'answer', 'citations': ['source.md']})}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10}}

    def adapter(self):
        return LiveAdapter(self.cfg, 'same prompt', True, self.transport)

    def test_real_retrieval_both_arms_identical_prompt_gold_not_sent(self):
        adapter = self.adapter()
        for arm in self.cfg['arms']:
            result = adapter.answer(self.case, [{'text': 'SECRET_CORPUS_OR_GOLD'}], arm)
            self.assertEqual(result['adapter'], 'live_lightrag')
            self.assertEqual(len(result['tool_calls']), 1)
        self.assertNotEqual(self.calls[0][0], self.calls[2][0])
        self.assertEqual(self.calls[0][1], self.calls[2][1])
        self.assertEqual(self.calls[1][1], self.calls[3][1])
        self.assertNotIn('SECRET', json.dumps(self.calls))
        self.assertEqual(adapter.calls, 4)
        self.assertEqual(adapter.input_tokens, 40)

    def test_no_paid_authorization_no_requests(self):
        with self.assertRaises(ValueError):
            LiveAdapter(self.cfg, 'same', False, self.transport)
        self.assertEqual(self.calls, [])

    def test_zero_prices_and_unattested_remote_retrieval_fail_closed(self):
        self.cfg['budget']['input_usd_per_million'] = 0
        with self.assertRaises(ValueError): self.adapter()
        self.cfg['budget']['input_usd_per_million'] = 1
        self.attest['retrieval_uses_only_local_models'] = False
        self.write_attest()
        with self.assertRaises(ValueError): self.adapter()

    def test_failed_provider_keeps_reservation_and_retrieval_trace(self):
        adapter = self.adapter()
        def fail(url, payload, headers, timeout, request_limit, response_limit):
            if url.endswith('/query/data'): return self.transport(url,payload,headers,timeout,request_limit,response_limit)
            raise LiveRunError('provider failed')
        adapter.transport = fail
        with self.assertRaises(LiveRunError): adapter.answer(self.case, [], 'baseline')
        self.assertGreater(adapter.cost, 0)
        self.assertGreater(adapter.input_tokens, 0)
        self.assertTrue(adapter.last_attempt['tool_calls'])
        self.assertEqual(adapter.calls, 2)

    def test_request_and_input_budget_stop_before_network(self):
        self.cfg['budget']['max_requests'] = 1
        adapter = self.adapter()
        with self.assertRaises(LiveRunError): adapter.answer(self.case, [], 'baseline')
        self.assertEqual(len(self.calls), 1)
        self.calls.clear(); self.cfg['budget']['max_requests'] = 8
        self.cfg['budget']['max_input_tokens'] = 1
        adapter = self.adapter()
        with self.assertRaises(LiveRunError): adapter.answer(self.case, [], 'baseline')
        self.assertEqual(len(self.calls), 1)

    def test_provider_token_overrun_permanently_stops_following_calls(self):
        adapter = self.adapter()
        def overrun(url, payload, headers, timeout, request_limit, response_limit):
            result = self.transport(url,payload,headers,timeout,request_limit,response_limit)
            if url.endswith('/chat/completions'): result['usage']['completion_tokens'] = 101
            return result
        adapter.transport = overrun
        with self.assertRaises(LiveRunError): adapter.answer(self.case, [], 'baseline')
        count = len(self.calls)
        with self.assertRaises(LiveRunError): adapter.answer(self.case, [], 'candidate')
        self.assertEqual(len(self.calls), count)

    def test_request_and_response_byte_limits_are_separate(self):
        adapter=self.adapter(); seen=[]
        def limited(url,payload,headers,timeout,request_limit,response_limit):
            seen.append((request_limit,response_limit)); return {}
        adapter.transport=limited
        adapter._request('http://127.0.0.1/test',{'query':'x'},{})
        self.assertEqual(seen,[(65536,65536)])

    def test_bounded_post_reads_only_response_limit_plus_one(self):
        class Response:
            def __init__(self,body): self.body=body; self.read_size=None
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,size): self.read_size=size; return self.body
        class Opener:
            def __init__(self,response): self.response=response
            def open(self,request,timeout): return self.response
        good=Response(b'{"ok":true}')
        with patch('urllib.request.build_opener',return_value=Opener(good)):
            self.assertEqual(bounded_post('https://example.test',{}, {},1,100,20),{'ok':True})
        self.assertEqual(good.read_size,21)
        too_large=Response(b'123456')
        with patch('urllib.request.build_opener',return_value=Opener(too_large)):
            with self.assertRaisesRegex(LiveRunError,'response byte limit'):
                bounded_post('https://example.test',{}, {},1,100,5)

    def test_credentials_in_urls_or_identical_endpoints_rejected(self):
        with patch.dict(os.environ, {'TEST_BASE': 'http://secret@127.0.0.1:9621'}):
            with self.assertRaises(ValueError): self.adapter()
        with patch.dict(os.environ, {'TEST_CAND': 'http://127.0.0.1:9621'}):
            with self.assertRaises(ValueError): self.adapter()
