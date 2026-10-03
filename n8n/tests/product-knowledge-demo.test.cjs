const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '../..');
const dataset = JSON.parse(fs.readFileSync(path.join(root, 'sample-data/product-knowledge-demo/dataset.json'), 'utf8'));
const evidence = JSON.parse(fs.readFileSync(path.join(root, 'sample-data/product-knowledge-demo/evidence.json'), 'utf8'));
const workflow = JSON.parse(fs.readFileSync(path.join(root, 'n8n/workflows/product-knowledge-demo.json'), 'utf8'));
const byName = Object.fromEntries(workflow.nodes.map((node) => [node.name, node]));
const runCode = (name, input, named = {}) => {
  const code = byName[name].parameters.jsCode;
  const first = (items) => items[0];
  const inputApi = {
    first: () => first(input),
    all: () => input,
  };
  const lookup = (nodeName) => ({ all: () => named[nodeName] ?? [], first: () => first(named[nodeName] ?? []) });
  return new Function('$input', '$', code)(inputApi, lookup);
};

test('workflow embeds the exact fictional dataset and has a valid connected graph', () => {
  assert.equal(workflow.active, false);
  assert.equal(byName['Manual Trigger'].type, 'n8n-nodes-base.manualTrigger');
  const prepared = runCode('Prepare demo documents', []);
  assert.equal(prepared.length, dataset.documents.length);
  assert.deepEqual(prepared.map((item) => {
    const { demo_id, fictional, ...doc } = item.json;
    assert.equal(demo_id, dataset.demo_id);
    assert.equal(fictional, true);
    return doc;
  }), dataset.documents);
  assert.equal(new Set(dataset.documents.map((doc) => doc.id)).size, dataset.documents.length);
  assert.equal(new Set(dataset.documents.map((doc) => doc.file_source)).size, dataset.documents.length);
  for (const doc of dataset.documents) {
    assert.match(doc.file_source, /^demo-product-knowledge-v1-[A-Za-z0-9-]+\.txt$/);
    assert.equal(doc.text.startsWith('DEMO FICTIONAL'), true);
  }
  const documentIds = new Set(dataset.documents.map((doc) => doc.id));
  for (const useCase of evidence.use_cases) {
    for (const sourceId of useCase.expected_source_ids) assert.ok(documentIds.has(sourceId), `missing evidence source ${sourceId}`);
  }
  const conflict = evidence.use_cases.find((useCase) => useCase.conflict);
  assert.ok(conflict);
  assert.deepEqual(conflict.conflict.claims.map((claim) => claim.source_id).sort(), ['figma-FLOW-02', 'jira-CP-248']);
  assert.equal(conflict.conflict.expected_resolution, 'unresolved');
  for (const [from, groups] of Object.entries(workflow.connections)) {
    assert.ok(byName[from], `missing source node ${from}`);
    for (const group of groups.main) for (const edge of group) assert.ok(byName[edge.node], `missing target node ${edge.node}`);
  }
});

test('queue node uses serial authenticated per-document LightRAG text insertion', () => {
  const node = byName['Queue demo document in LightRAG'];
  assert.equal(node.parameters.url, 'http://lightrag:9621/documents/text');
  assert.equal(node.parameters.genericAuthType, 'httpHeaderAuth');
  assert.equal(node.parameters.options.batching.batch.batchSize, 1);
  assert.equal(node.parameters.options.batching.batch.batchInterval, 1000);
  assert.equal(node.parameters.options.response.response.fullResponse, true);
  assert.equal(node.parameters.options.response.response.neverError, true);
  assert.equal(node.credentials.httpHeaderAuth.name, 'LightRAG X-API-Key');
  assert.equal(node.credentials.httpHeaderAuth.id, 'REPLACE_WITH_N8N_CREDENTIAL_ID');
});

test('summary reports accepted and already-present documents without claiming completion', () => {
  const docs = [{ json: { file_source: 'source-a' } }, { json: { file_source: 'source-b' } }];
  const result = runCode('Summarize demo ingestion', [
    { json: { statusCode: 200, body: { status: 'success', track_id: 'insert_a' } } },
    { json: { statusCode: 409, body: { detail: "Document storage already contains 'source-b' (Status: processed)" } } },
  ], { 'Prepare demo documents': docs })[0].json;
  assert.equal(result.demo_id, dataset.demo_id);
  assert.equal(result.fictional, true);
  assert.equal(result.status, 'queued');
  assert.match(result.message, /not complete yet/i);
  assert.equal(result.queued, 1);
  assert.equal(result.alreadyPresent, 1);
  assert.deepEqual(result.trackIds, ['insert_a']);
});

test('summary rejects unrelated conflicts, authorization errors, and incomplete successes', () => {
  const source = [{ json: { file_source: 'demo-source' } }];
  const failures = [
    { statusCode: 409, body: { detail: 'some other conflict' } },
    { statusCode: 401, body: { detail: 'unauthorized' } },
    { statusCode: 200, body: { status: 'failure', message: 'not queued' } },
    { statusCode: 200, body: { status: 'success', track_id: '' } },
  ];
  for (const response of failures) {
    assert.throws(() => runCode('Summarize demo ingestion', [{ json: response }], { 'Prepare demo documents': source }), /LightRAG/);
  }
});
