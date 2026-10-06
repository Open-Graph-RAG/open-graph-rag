const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const internet = require('./helpers/internet');
const google = require('./helpers/google-notebooks');
const demo = require('./helpers/product-knowledge-demo');

test('internet request validation and provider filtering preserve accepted result objects', () => {
  assert.deepEqual(internet.validatePrompt({ prompt: ' topic ', maxResults: 3 }), { prompt: 'topic', maxResults: 3 });
  for (const body of [{}, { prompt: ' ' }, { prompt: 'x'.repeat(4001) }, { prompt: 'x', maxResults: 0 }]) {
    assert.throws(() => internet.validatePrompt(body));
  }
  const accepted = { url: 'https://example.test/a', title: 'A', raw_content: 'A useful fact.' };
  const result = internet.validateSearchResponse({ results: [accepted, null, { url: 'https://example.test/b', raw_content: 'Access denied' }] });
  assert.deepEqual(result.results, [accepted]);
  assert.deepEqual(result.validation, {
    received: 3, accepted: 1, rejected: 2,
    reasons: { malformed_result: 1, access_denied_or_error_page: 1 },
  });
  assert.throws(() => internet.validateSearchResponse({ results: [], error: 'quota' }), /Tavily/);
});

test('internet search rejects provider result arrays larger than the requested maximum', () => {
  const oversized = Array.from({ length: 11 }, (_, index) => ({
    url: `https://example.test/${index}`, raw_content: 'A useful fact.',
  }));
  assert.throws(() => internet.validateSearchResponse({ results: oversized }), /Tavily.*too many results/);
});

test('internet deduplication preserves identity text and builds separately mapped provenance', () => {
  const search = {
    results: [
      { url: 'https://example.test/a#first', title: 'Article', raw_content: 'Ａ useful\npage' },
      { url: 'https://example.test/b', title: 'Mirror', raw_content: 'A useful page' },
    ],
  };
  const bundle = internet.deduplicateResults(search.results, 'topic');
  assert.equal(bundle.found, 1);
  assert.equal(bundle.documents[0].text, 'A useful page');
  assert.equal(bundle.documents[0].file_source, 'https://example.test/a');
  const prepared = internet.prepareGraphDocuments(bundle, search);
  assert.equal(prepared.documents[0].text, 'A useful page');
  assert.equal(prepared.documents[0].file_source, 'https://example.test/a');
  assert.deepEqual(prepared.documents[0].metadata, {
    source_url: 'https://example.test/a', title: 'Article', query: 'topic', content_type: 'text/plain',
  });
});

test('Google document requests and exports enforce the source workflow limits', () => {
  const request = google.validateDocumentRequest({ driveFileId: '1Abcdefghijklmnopqrstuvwxyz_123', notebook: ' Research ' });
  assert.deepEqual(request, {
    driveFileId: '1Abcdefghijklmnopqrstuvwxyz_123', notebook: 'Research',
    file_source: 'https://docs.google.com/document/d/1Abcdefghijklmnopqrstuvwxyz_123/edit',
  });
  for (const body of [{}, { driveFileId: 'short' }, { driveFileId: '1Abcdefghij', notebook: 42 }]) {
    assert.throws(() => google.validateDocumentRequest(body));
  }
  assert.deepEqual(google.validateExport({ statusCode: 200, body: 'Notes' }, request), {
    text: 'Notes', file_source: request.file_source, notebook: 'Research',
  });
  assert.equal(google.validateExport({ status: 200, body: 'Notes' }, request).text, 'Notes');
  for (const response of [
    { statusCode: 403, body: 'Forbidden' }, { statusCode: 200, body: ' ' },
    { body: 'Notes' }, { statusCode: 'invalid', body: 'Notes' },
  ]) {
    assert.throws(() => google.validateExport(response, request));
  }
});

test('insertion outcomes distinguish accepted, exact duplicate, and partial failure without retry claims', () => {
  const outcomes = [
    internet.insertionOutcome({ statusCode: 200, body: { status: 'success', track_id: 'insert_1' } }, 'source-a'),
    internet.insertionOutcome({ statusCode: 409, body: { detail: "Document storage already contains 'source-b' (Status: processed)." } }, 'source-b'),
    internet.insertionOutcome({ statusCode: 503, body: { detail: 'busy' } }, 'source-c'),
  ];
  const summary = internet.summarizeInsertions(outcomes, { deadlineExceeded: true });
  assert.equal(summary.httpStatus, 502);
  assert.deepEqual(Object.keys(summary).sort(), ['alreadyPresent', 'error', 'httpStatus', 'queued', 'status', 'trackIds'].sort());
  assert.equal(summary.status, 'error');
  assert.equal(summary.error.code, 'upstream_failure');
  assert.equal(summary.queued, 1);
  assert.equal(summary.alreadyPresent, 1);
  assert.deepEqual(summary.trackIds, ['insert_1']);
  assert.equal(summary.error.message.includes('busy'), false);
});

test('HTTP piece status field is accepted for successful LightRAG and Google responses', () => {
  assert.deepEqual(internet.insertionOutcome({ status: 202, body: { status: 'success', track_id: 'ap_track' } }, 'source'), {
    source: 'source', status: 'queued', track_id: 'ap_track',
  });
  assert.deepEqual(demo.insertionOutcome({ status: 202, body: { status: 'success', track_id: 'ap_demo' } }, 'source'), {
    source: 'source', status: 'queued', track_id: 'ap_demo',
  });
  assert.equal(google.summarizeIngestion({ status: 200, body: { status: 'success', track_id: 'ap_google' } }, 'source').queued, 1);
});

test('Activepieces errorMessage only classifies the exact LightRAG duplicate 409 as already present', () => {
  const errorMessage = JSON.stringify({
    status: 409,
    responseBody: { detail: "Document storage already contains 'source-a' (Status: processed)." },
  });
  const nestedBody = JSON.stringify({
    status: '409',
    responseBody: JSON.stringify({ detail: 'Document storage already contains source-b' }),
  });
  assert.deepEqual(internet.insertionOutcome(null, 'source-a', errorMessage), { source: 'source-a', status: 'already_present' });
  assert.deepEqual(google.summarizeIngestion(null, 'source-a', errorMessage), {
    httpStatus: 200, status: 'already_present',
    message: 'This source is already present in LightRAG.', file_source: 'source-a', queued: 0, alreadyPresent: 1,
  });
  assert.deepEqual(demo.insertionOutcome(null, 'source-a', nestedBody), { source: 'source-a', status: 'already_present' });
  for (const error of [
    JSON.stringify({ status: 409, responseBody: { detail: 'different conflict' } }),
    JSON.stringify({ status: 503, responseBody: { detail: 'Document storage already contains source' } }),
    '{malformed',
  ]) {
    assert.deepEqual(internet.insertionOutcome(null, 'source-a', error), { source: 'source-a', status: 'failed' });
    assert.deepEqual(demo.insertionOutcome(null, 'source-a', error), { source: 'source-a', status: 'failed' });
  }
});

test('native Activepieces HTTP error shape requires the exact duplicate detail', () => {
  const errorMessage = JSON.stringify({
    __apErrorVersion: 1,
    apiMessage: 'Request failed with status code 409',
    errorName: 'HttpError',
    message: 'Request failed with status code 409',
    responseBody: { detail: 'Document storage already contains source-a' },
    status: 409,
  });
  assert.deepEqual(internet.insertionOutcome(null, 'source-a', errorMessage), {
    source: 'source-a', status: 'already_present',
  });
  assert.deepEqual(internet.insertionOutcome(null, 'source-a', JSON.stringify({
    errorName: 'HttpError', status: 409,
    responseBody: JSON.stringify({ detail: 'request conflicted for another reason' }),
  })), { source: 'source-a', status: 'failed' });
});

test('gateway-start deadlines include queue delay and leave failed runs out of retried state', () => {
  const requestStart = '1760000000.000';
  assert.equal(internet.hasInsertionBudget(requestStart, 1760000019000), true);
  assert.equal(internet.hasInsertionBudget(requestStart, 1760000020000), false);
  assert.equal(internet.hasInsertionBudget(undefined, 1760000000000), false);
  assert.equal(internet.hasInsertionBudget('bad', 1760000000000), false);
});

test('demo preparation is byte-for-byte aligned with the fictional dataset contract', () => {
  const dataset = JSON.parse(fs.readFileSync(path.join(__dirname, '../sample-data/product-knowledge-demo/dataset.json'), 'utf8'));
  const prepared = demo.prepareDemoDocuments(dataset);
  assert.equal(prepared.length, dataset.documents.length);
  assert.deepEqual(prepared.map(({ demo_id, fictional, ...doc }) => doc), dataset.documents);
  assert.ok(prepared.every((doc) => doc.demo_id === dataset.demo_id && doc.fictional === true));
  assert.throws(() => demo.prepareDemoDocuments({ ...dataset, demo_id: 'different-demo' }));
  assert.throws(() => demo.prepareDemoDocuments({ ...dataset, documents: dataset.documents.slice(1) }));
  const summary = demo.summarizeDemoInsertions([
    { source: 'source-a', status: 'queued', track_id: 'insert_a' },
    { source: 'source-b', status: 'already_present' },
  ]);
  assert.equal(summary.status, 'queued');
  assert.equal(summary.httpStatus, 202);
  assert.equal(summary.queued, 1);
  assert.equal(summary.alreadyPresent, 1);
  assert.match(summary.message, /not complete yet/i);
  const googleSummary = google.summarizeIngestion({ statusCode: 200, body: { status: 'success', track_id: 'insert_2' } }, 'stable-source');
  assert.equal(internet.summarizeInsertions([{ status: 'queued', track_id: 'insert_3' }]).httpStatus, 202);
  assert.deepEqual(googleSummary, {
    httpStatus: 202, status: 'queued',
    message: 'LightRAG accepted the document for background processing; ingestion is not complete yet.',
    file_source: 'stable-source', queued: 1, alreadyPresent: 0, track_id: 'insert_2',
  });
});
