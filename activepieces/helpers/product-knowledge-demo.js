'use strict';

function prepareDemoDocuments(dataset) {
  if (!dataset || dataset.fictional !== true || dataset.demo_id !== 'product-knowledge-demo-v1' ||
      !Array.isArray(dataset.documents) || dataset.documents.length !== 11) {
    throw new Error('The product knowledge demo dataset is invalid.');
  }
  return dataset.documents.map((document) => ({ ...document, demo_id: dataset.demo_id, fictional: dataset.fictional }));
}

function isDuplicateFailure(errorMessage) {
  if (typeof errorMessage !== 'string') return false;
  let failure;
  try { failure = JSON.parse(errorMessage); } catch { return false; }
  const statusCode = Number(failure?.status);
  let body = failure?.responseBody;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch { return false; }
  }
  return statusCode === 409 && typeof body?.detail === 'string' &&
    body.detail.startsWith('Document storage already contains');
}

function insertionOutcome(response, source, errorMessage) {
  const code = Number(response?.statusCode ?? response?.status ?? 0);
  const body = response?.body && typeof response.body === 'object' ? response.body : {};
  if (code >= 200 && code < 300 && body.status === 'success' && typeof body.track_id === 'string' && body.track_id) {
    return { source, status: 'queued', track_id: body.track_id };
  }
  if (code === 409 && typeof body.detail === 'string' && body.detail.startsWith('Document storage already contains')) {
    return { source, status: 'already_present' };
  }
  if (isDuplicateFailure(errorMessage)) return { source, status: 'already_present' };
  return { source, status: 'failed' };
}

function summarizeDemoInsertions(outcomes) {
  if (!Array.isArray(outcomes)) throw new Error('Demo insertion outcomes must be an array.');
  let queued = 0;
  let alreadyPresent = 0;
  const trackIds = [];
  let failed = false;
  for (const outcome of outcomes) {
    if (outcome?.status === 'queued' && typeof outcome.track_id === 'string' && outcome.track_id) {
      queued++;
      trackIds.push(outcome.track_id);
    } else if (outcome?.status === 'already_present') alreadyPresent++;
    else failed = true;
  }
  if (failed) {
    return {
      demo_id: 'product-knowledge-demo-v1',
      fictional: true,
      httpStatus: 502,
      status: 'error',
      error: { code: 'upstream_failure', message: 'An upstream service failed during ingestion.' },
      queued,
      alreadyPresent,
      trackIds,
    };
  }
  return {
    demo_id: 'product-knowledge-demo-v1',
    fictional: true,
    httpStatus: queued > 0 ? 202 : 200,
    status: queued > 0 ? 'queued' : 'skipped',
    message: queued > 0
      ? 'LightRAG accepted documents for background processing; ingestion is not complete yet.'
      : 'All demo sources were already present in LightRAG; no new processing was queued.',
    queued,
    alreadyPresent,
    trackIds,
  };
}

module.exports = { insertionOutcome, isDuplicateFailure, prepareDemoDocuments, summarizeDemoInsertions };
