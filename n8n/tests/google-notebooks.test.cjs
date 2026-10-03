const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const workflow = require('../workflows/google-notebooks-to-lightrag.json');

function node(name) {
  const found = workflow.nodes.find((item) => item.name === name);
  assert.ok(found, `missing node: ${name}`);
  return found;
}

function run(name, items, refs = {}) {
  const context = {
    $json: items[0]?.json,
    $input: { first: () => items[0], all: () => items },
    $: (key) => ({ first: () => refs[key]?.[0], all: () => refs[key] }),
  };
  const code = `(function(){${node(name).parameters.jsCode}\n})()`;
  return JSON.parse(JSON.stringify(vm.runInNewContext(code, context)));
}

test('workflow is inactive, authenticated, connected, and contains no secrets', () => {
  assert.equal(workflow.active, false);
  assert.equal(node('Authenticated NotebookLM webhook').parameters.authentication, 'headerAuth');
  assert.equal(node('Export Google Doc as text').parameters.nodeCredentialType, 'googleDriveOAuth2Api');
  assert.equal(node('Queue exported text in LightRAG').parameters.genericAuthType, 'httpHeaderAuth');
  for (const item of workflow.nodes) {
    for (const credential of Object.values(item.credentials ?? {})) {
      assert.equal(credential.id, 'REPLACE_WITH_N8N_CREDENTIAL_ID');
    }
  }
  const names = new Set(workflow.nodes.map((item) => item.name));
  for (const [source, outputs] of Object.entries(workflow.connections)) {
    assert.ok(names.has(source));
    for (const branch of outputs.main) for (const edge of branch) assert.ok(names.has(edge.node));
  }
});

test('validates the Google Doc ID and optional notebook label', () => {
  const result = run('Validate exported document request', [{ json: { body: {
    driveFileId: '1Abcdefghijklmnopqrstuvwxyz_123', notebook: ' Research ',
  } } }])[0].json;
  assert.deepEqual(result, {
    driveFileId: '1Abcdefghijklmnopqrstuvwxyz_123',
    notebook: 'Research',
    file_source: 'https://docs.google.com/document/d/1Abcdefghijklmnopqrstuvwxyz_123/edit',
  });
  for (const body of [
    {}, { driveFileId: '../files?alt=media' }, { driveFileId: 'short' },
    { driveFileId: '1Abcdefghij', notebook: 42 },
    { driveFileId: '1Abcdefghij', notebook: 'x'.repeat(501) },
  ]) assert.throws(() => run('Validate exported document request', [{ json: { body } }]));
});

test('validates a successful Google Docs text export', () => {
  const refs = { 'Validate exported document request': [{ json: {
    file_source: 'https://docs.google.com/document/d/1234567890/edit', notebook: 'Research',
  } }] };
  const result = run('Validate exported text', [{ json: { statusCode: 200, body: 'Notes from NotebookLM' } }], refs)[0].json;
  assert.deepEqual(result, {
    text: 'Notes from NotebookLM',
    file_source: 'https://docs.google.com/document/d/1234567890/edit',
    notebook: 'Research',
  });
  for (const response of [
    { statusCode: 403, body: 'Forbidden' },
    { statusCode: 200, body: '  ' },
    { statusCode: 200, body: 'x'.repeat(500001) },
    { statusCode: 200, body: { unexpected: true } },
  ]) assert.throws(() => run('Validate exported text', [{ json: response }], refs));
});

test('queues accepted documents and identifies an existing source', () => {
  const refs = { 'Validate exported document request': [{ json: { file_source: 'https://docs.google.com/document/d/123/edit' } }] };
  const queued = run('Summarize ingestion result', [{ json: { statusCode: 200, body: { status: 'success', track_id: 'insert_123' } } }], refs)[0].json;
  assert.equal(queued.status, 'queued');
  assert.equal(queued.track_id, 'insert_123');
  const duplicate = run('Summarize ingestion result', [{ json: { statusCode: 409, body: { detail: "Document storage already contains 'source' (Status: processed)." } } }], refs)[0].json;
  assert.equal(duplicate.status, 'already_present');
  for (const response of [
    { statusCode: 409, body: { detail: 'Pipeline is busy' } },
    { statusCode: 401, body: { detail: 'Unauthorized' } },
    { statusCode: 200, body: { status: 'failure' } },
  ]) assert.throws(() => run('Summarize ingestion result', [{ json: response }], refs));
});
