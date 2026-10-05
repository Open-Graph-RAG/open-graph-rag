'use strict';

function validateDocumentRequest(input) {
  const body = input?.body ?? input;
  const driveFileId = body?.driveFileId;
  if (typeof driveFileId !== 'string' || !/^[A-Za-z0-9_-]{10,200}$/.test(driveFileId)) throw new Error('driveFileId must be a valid Google Drive file ID.');
  const notebook = body.notebook === undefined ? '' : body.notebook;
  if (typeof notebook !== 'string' || notebook.length > 500) throw new Error('notebook must be a string of at most 500 characters.');
  return {
    driveFileId,
    notebook: notebook.trim(),
    file_source: `https://docs.google.com/document/d/${driveFileId}/edit`,
  };
}

function validateExport(response, request) {
  if (!response || !request) throw new Error('Google Drive export response or request is unavailable.');
  const code = Number(response.statusCode ?? response.status);
  if (!Number.isInteger(code) || code < 200 || code >= 300) throw new Error('Google Drive export failed.');
  if (typeof response.body !== 'string' || !response.body.trim()) throw new Error('Google Drive returned an empty or non-text export. Ensure the file is a Google Doc exported from NotebookLM.');
  if (response.body.length > 500000) throw new Error('Exported text exceeds the 500000-character limit.');
  return { text: response.body, file_source: request.file_source, notebook: request.notebook };
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

function summarizeIngestion(response, fileSource, errorMessage) {
  const code = Number(response?.statusCode ?? response?.status ?? 0);
  const body = response?.body && typeof response.body === 'object' ? response.body : {};
  const detail = typeof body.detail === 'string' ? body.detail : '';
  if (code >= 200 && code < 300) {
    if (body.status !== 'success' || typeof body.track_id !== 'string' || !body.track_id) {
      return {
        httpStatus: 502,
        status: 'error',
        error: { code: 'upstream_failure', message: 'An upstream service failed during ingestion.' },
        queued: 0,
        alreadyPresent: 0,
        trackIds: [],
      };
    }
    return {
      httpStatus: 202,
      status: 'queued',
      message: 'LightRAG accepted the document for background processing; ingestion is not complete yet.',
      file_source: fileSource,
      queued: 1,
      alreadyPresent: 0,
      track_id: body.track_id,
    };
  }
  if (code === 409 && detail.startsWith('Document storage already contains')) {
    return {
      httpStatus: 200,
      status: 'already_present',
      message: 'This source is already present in LightRAG.',
      file_source: fileSource,
      queued: 0,
      alreadyPresent: 1,
    };
  }
  if (isDuplicateFailure(errorMessage)) {
    return {
      httpStatus: 200,
      status: 'already_present',
      message: 'This source is already present in LightRAG.',
      file_source: fileSource,
      queued: 0,
      alreadyPresent: 1,
    };
  }
  return {
    httpStatus: 502,
    status: 'error',
    error: { code: 'upstream_failure', message: 'An upstream service failed during ingestion.' },
    queued: 0,
    alreadyPresent: 0,
    trackIds: [],
  };
}

module.exports = { isDuplicateFailure, summarizeIngestion, validateDocumentRequest, validateExport };
