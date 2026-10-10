import asyncio, json, os, sys
from pathlib import Path
import httpx

repo = Path(__file__).resolve().parents[1]
source = Path(os.environ.get('KEV_SOURCE', '/opt/kev'))
artifacts = Path(os.environ.get('KEV_ARTIFACT_ROOT', '/model-cache/artifacts'))
sys.path.insert(0, str(repo / 'mcp'))
sys.path.insert(0, str(source))
os.environ.update({
    'KEV_SOURCE': str(source), 'HF_HOME': os.environ.get('HF_HOME', '/tmp/kev-empty-hf'),
    'KEV_ARTIFACT_ROOT': str(artifacts), 'KEV_CHECKPOINT': str(artifacts / 'kev'),
    'KEV_BASE': str(artifacts / 'base'), 'KEV_DEVICE': 'cuda',
    'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
})
import torch
from kev.api import SystemOneRequest, to_record, to_answers
from kev_adapter import load_adapter
from decision_contract import validate_decision_request
from decision_retrieval import normalize_retrieval_response
from decision_runtime import DecisionRuntime
from decision_context import build_decision_context

adapter = load_adapter(source=str(source), artifact_root=str(artifacts),
    checkpoint_path=str(artifacts/'kev'), base_path=str(artifacts/'base'),
    manifest_path=str(artifacts/'manifest.json'), device='cuda')
assert adapter.metadata.execution_device == 'cuda'
assert adapter.metadata.device_name == torch.cuda.get_device_name()

input_data = {
  'objective': 'Compare documented approval and effective-date rules.',
  'query': 'approval and effective date rules',
  'questions': {
    'classification': {'type':'choice','instructions':'Which outcome is supported?', 'options':[
      {'id':'conflict','description':'Rules conflict in the same scope.'},
      {'id':'compatible','description':'Rules can both hold.'},
      {'id':'unknown','description':'Evidence is insufficient.'}]},
    'sufficient': {'type':'yes_no','instructions':'Does the evidence cover the same workflow?',
      'true_description':'Same workflow.','false_description':'Different or unknown workflow.'},
    'impact': {'type':'score','instructions':'Rate direct support.', 'levels':['low','medium','high']},
  }
}
request = validate_decision_request(input_data)
state = 'Escaped fixture: quotes " survive; newline follows.\nCafé and 日本語 stay intact.'
payload = [adapter._question_payload(qid, q.kev_question()) for qid, q in request.questions.items()]
upstream_request = SystemOneRequest.model_validate({'state':state,'questions':adapter._upstream_questions(payload)})
record, meta = to_record(upstream_request)
assert adapter._record(state,payload) == record
encoded_direct = adapter.model.encode(adapter.tokenizer, record, max_state=4096, max_branch=5120, strict=True)
encoded_adapter = adapter._encode(state,payload)
assert encoded_direct['ids'] == encoded_adapter['ids']
assert encoded_direct['state_tokens'] == encoded_adapter['state_tokens']
with torch.inference_mode():
    direct_probs = adapter.model.probs(encoded_direct)
direct = to_answers([p.tolist() for p in direct_probs], meta)
answers, prep_ms, inference_ms = adapter.infer(state, request.questions)
for qid, question in request.questions.items():
    result = answers[qid]
    expected = direct[qid]
    if question.type == 'choice':
        assert result.selected == expected['choice'], (qid, result, expected)
        assert result.confidence == expected['confidence'], (qid, result, expected)
        assert result.probabilities == expected['probabilities'], (qid, result, expected)
    elif question.type == 'yes_no':
        assert result.probability_true == expected['noul'], (qid, result, expected)
        assert abs(sum(result.probabilities.values()) - 1) <= 1e-5
    else:
        assert result.expected_score == expected['score'], (qid, result, expected)
        assert result.confidence == expected['confidence'], (qid, result, expected)
        assert result.probabilities == expected['probabilities'], (qid, result, expected)

retrieval_payload = {'status':'success','data': {
  'chunks':[{'chunk_id':'host-c1','content':'Approval is required before a change takes effect.','file_path':'fixture.md'},
            {'chunk_id':'host-c2','content':'Changes take effect on approval.','file_path':'fixture-2.md'}],
  'references':[], 'entities':[{'entity_name':'Approval','description':'Required before effective change.','source_id':'host-c1<SEP>host-c2'}],
  'relationships':[]}}
class Retriever:
 async def retrieve(self, query): return normalize_retrieval_response(retrieval_payload)

async def run_mcp():
    runtime = DecisionRuntime(Retriever(), lambda: adapter)
    await asyncio.wrap_future(runtime._load_future)
    # Let the actual enabled MCP tool invoke the real runtime, without starting a duplicate GPU load.
    import decision_runtime
    original_runtime = decision_runtime.DecisionRuntime
    decision_runtime.DecisionRuntime = lambda retriever, adapter_loader: runtime
    os.environ['MCP_DECISION_ENABLED'] = '1'
    os.environ['MCP_TOKEN'] = 'host-parity-token-' + 'x'*40
    os.environ['LIGHTRAG_API_KEY'] = 'host-parity-key'
    sys.modules.pop('server', None)
    import server
    diagnostic_retrieval = normalize_retrieval_response(retrieval_payload)
    diagnostic_context = build_decision_context(request, diagnostic_retrieval, adapter)
    print('CONTEXT_DIAGNOSTIC', diagnostic_context.status, diagnostic_context.limitations.items, diagnostic_context.token_counts, flush=True)
    transport = httpx.ASGITransport(app=server.app)
    async with server._mcp_http_app.router.lifespan_context(server._mcp_http_app):
      async with httpx.AsyncClient(transport=transport, base_url='http://mcp:8000') as client:
        client.headers.update({'Authorization':'Bearer '+server.MCP_TOKEN,'Accept':'application/json, text/event-stream'})
        init = await client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'host-parity','version':'1'}}})
        assert init.status_code == 200, init.text
        print('RUNTIME_BEFORE_DECISION', runtime.enabled, runtime._closed, runtime.state, flush=True)
        response = await client.post('/mcp', json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'decision_evaluate','arguments':input_data}})
        assert response.status_code == 200, response.text
        content = response.json()['result']
        print('MCP_RESULT', repr(content), flush=True)
        if content.get('isError'): Path('/tmp/host-parity-mcp-error.txt').write_text(content['content'][0]['text'])
        result = content.get('structuredContent') or json.loads(content['content'][0]['text'])
        assert result['status'] == 'evaluated', result
        assert set(result['answers']) == {'classification','sufficient','impact'}
        assert result['model']['execution_device'] == 'cuda'
        print(json.dumps({'status':result['status'],'model':result['model'],'timings':result['timings'],
            'answers':result['answers'],'direct_adapter_prep_ms':prep_ms,'direct_adapter_inference_ms':inference_ms,
            'strict_record_ids_equal':True,'offline_hf_cache_empty':not Path('/tmp/kev-empty-hf').exists()}, indent=2))
    assert runtime._closed
    decision_runtime.DecisionRuntime = original_runtime

asyncio.run(run_mcp())
