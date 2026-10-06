'use strict';

const MAX_PROMPT_CHARS = 4000;
const MAX_RESULTS = 10;
const MAX_CONTENT_CHARS = 500000;
const INSERTION_START_CUTOFF_MS = 20000;

function validatePrompt(input) {
  const body = input?.body ?? input;
  const prompt = body?.prompt;
  if (typeof prompt !== 'string' || !prompt.trim()) throw new Error('Request body must include a non-empty prompt string.');
  if (prompt.trim().length > MAX_PROMPT_CHARS) throw new Error('prompt must be at most 4000 characters.');
  const maxResults = body.maxResults === undefined ? 5 : body.maxResults;
  if (typeof maxResults !== 'number' || !Number.isInteger(maxResults) || maxResults < 1 || maxResults > MAX_RESULTS) {
    throw new Error('maxResults must be an integer from 1 to 10.');
  }
  return { prompt: prompt.trim(), maxResults };
}

function validHttpUrl(value) {
  const source = value.trim().split('#', 1)[0];
  if (!/^https?:\/\/[^\s/?#@]+(?:[/?#]|$)/i.test(source) || /[\u0000-\u0020\u007f]/u.test(source)) return null;
  return source;
}

function validateSearchResponse(response) {
  if (!response || typeof response !== 'object' || Array.isArray(response)) throw new Error('Tavily returned a malformed response object.');
  if (response.error !== undefined && response.error !== null && response.error !== '') throw new Error('Tavily search failed.');
  if (!Array.isArray(response.results)) throw new Error('Tavily response must contain a results array.');
  if (response.results.length > MAX_RESULTS) throw new Error('Tavily returned too many results.');
  const results = [];
  const reasons = {};
  const reject = (reason) => { reasons[reason] = (reasons[reason] ?? 0) + 1; };
  const hasLetterOrNumber = /[\p{L}\p{N}]/u;
  const denialOnly = /^(?:access denied|access forbidden|403 forbidden|you have been blocked|request blocked|website unavailable|page unavailable|service unavailable|404 not found|page not found|this site can(?:not|\x27t) be reached|the site can(?:not|\x27t) be reached|robot check|captcha required|enable javascript and cookies to continue)[.!\s]*$/i;
  for (const result of response.results) {
    if (!result || typeof result !== 'object' || Array.isArray(result)) { reject('malformed_result'); continue; }
    if (typeof result.raw_content !== 'string' || !result.raw_content.trim()) { reject('missing_raw_content'); continue; }
    if (result.raw_content.length > MAX_CONTENT_CHARS) { reject('raw_content_too_large'); continue; }
    if (typeof result.url !== 'string' || !validHttpUrl(result.url)) { reject('invalid_url'); continue; }
    if (/^(?:<!doctype\s+html\b|<html\b)/i.test(result.raw_content.trimStart())) { reject('html_page'); continue; }
    const trimmed = result.raw_content.trim();
    if (!hasLetterOrNumber.test(trimmed)) { reject('no_alphanumeric_content'); continue; }
    if (trimmed.length <= 300 && denialOnly.test(trimmed)) { reject('access_denied_or_error_page'); continue; }
    results.push(result);
  }
  const rejected = Object.values(reasons).reduce((sum, count) => sum + count, 0);
  return { ...response, results, validation: { received: response.results.length, accepted: results.length, rejected, reasons } };
}

function deduplicateResults(results, prompt) {
  if (!Array.isArray(results)) throw new Error('Validated search results must be an array.');
  const seen = new Set();
  const seenUrls = new Set();
  const documents = [];
  for (const result of results) {
    if (!result || typeof result.raw_content !== 'string' || !result.raw_content.trim() || result.raw_content.length > MAX_CONTENT_CHARS || typeof result.url !== 'string') continue;
    const source = validHttpUrl(result.url);
    if (!source || seenUrls.has(source)) continue;
    const text = result.raw_content.normalize('NFKC').replace(/\s+/gu, ' ').trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    seenUrls.add(source);
    documents.push({ text, file_source: source });
  }
  return { documents, prompt, found: documents.length };
}

function prepareGraphDocuments(bundle, validatedSearch) {
  if (!bundle || typeof bundle !== 'object' || Array.isArray(bundle)) throw new Error('Deduplication step returned a malformed document bundle.');
  if (!Array.isArray(bundle.documents)) throw new Error('Deduplication step must return a documents array.');
  if (!validatedSearch || !Array.isArray(validatedSearch.results)) throw new Error('Validated search results are unavailable for metadata mapping.');
  const metadataBySource = new Map();
  for (const result of validatedSearch.results) {
    if (!result || typeof result.url !== 'string') continue;
    const source = result.url.trim().split('#', 1)[0];
    if (!metadataBySource.has(source)) metadataBySource.set(source, { title: typeof result.title === 'string' && result.title.trim() ? result.title : undefined });
  }
  const seenSources = new Set();
  const documents = bundle.documents.map((doc, index) => {
    if (!doc || typeof doc !== 'object' || typeof doc.text !== 'string' || !doc.text.trim() || typeof doc.file_source !== 'string' || !doc.file_source.trim()) {
      throw new Error(`Deduplicated document ${index} has invalid text/source alignment.`);
    }
    if (seenSources.has(doc.file_source)) throw new Error(`Deduplicated documents contain a repeated source at index ${index}.`);
    seenSources.add(doc.file_source);
    const match = metadataBySource.get(doc.file_source.split('#', 1)[0]);
    return {
      ...doc,
      metadata: {
        source_url: doc.file_source,
        ...(match?.title ? { title: match.title } : {}),
        query: bundle.prompt,
        content_type: 'text/plain',
      },
    };
  });
  return { ...bundle, found: documents.length, documents };
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
  const detail = typeof body.detail === 'string' ? body.detail : '';
  if (code >= 200 && code < 300) {
    if (body.status === 'success' && typeof body.track_id === 'string' && body.track_id) return { source, status: 'queued', track_id: body.track_id };
    return { source, status: 'failed' };
  }
  if (code === 409 && detail.startsWith('Document storage already contains')) return { source, status: 'already_present' };
  if (isDuplicateFailure(errorMessage)) return { source, status: 'already_present' };
  return { source, status: 'failed' };
}

function summarizeInsertions(outcomes, { deadlineExceeded = false } = {}) {
  if (!Array.isArray(outcomes)) throw new Error('Insertion outcomes must be an array.');
  let queued = 0;
  let alreadyPresent = 0;
  const trackIds = [];
  for (const outcome of outcomes) {
    if (outcome?.status === 'queued' && typeof outcome.track_id === 'string' && outcome.track_id) {
      queued++;
      trackIds.push(outcome.track_id);
    } else if (outcome?.status === 'already_present') alreadyPresent++;
  }
  const failed = outcomes.some((outcome) => outcome?.status === 'failed' || outcome?.status === 'not_attempted');
  if (failed || deadlineExceeded) {
    return {
      httpStatus: 502,
      status: 'error',
      error: {
        code: 'upstream_failure',
        message: 'An upstream service failed during ingestion.',
      },
      queued,
      alreadyPresent,
      trackIds,
    };
  }
  const hasNewWork = queued > 0;
  return {
    httpStatus: hasNewWork ? 202 : 200,
    status: hasNewWork ? 'queued' : 'skipped',
    message: hasNewWork
      ? 'LightRAG accepted documents for background processing; ingestion is not complete yet.'
      : 'All usable sources were already present in LightRAG; no new processing was queued.',
    queued,
    alreadyPresent,
    trackIds,
  };
}

function hasInsertionBudget(requestStart, now = Date.now()) {
  const startSeconds = typeof requestStart === 'number' || typeof requestStart === 'string' ? Number(requestStart) : NaN;
  if (!Number.isFinite(startSeconds) || startSeconds <= 0) return false;
  const startMs = startSeconds * 1000;
  if (startMs > now + 1000) return false;
  return now < startMs + INSERTION_START_CUTOFF_MS;
}

module.exports = {
  deduplicateResults,
  hasInsertionBudget,
  isDuplicateFailure,
  insertionOutcome,
  prepareGraphDocuments,
  summarizeInsertions,
  validatePrompt,
  validateSearchResponse,
};
