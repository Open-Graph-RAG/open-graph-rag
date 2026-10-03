// n8n Code node body: place directly after the Tavily HTTP Request node.
// Keeps accepted Tavily result objects unchanged for the existing dedup node.
const response = $input.first()?.json;
if (!response || typeof response !== 'object' || Array.isArray(response)) {
  throw new Error('Tavily returned a malformed response object.');
}
if (response.error !== undefined && response.error !== null && response.error !== '') {
  throw new Error(`Tavily search failed: ${String(response.error)}`);
}
if (!Array.isArray(response.results)) {
  throw new Error('Tavily response must contain a results array.');
}

const results = [];
const reasons = {};
const reject = (reason) => { reasons[reason] = (reasons[reason] ?? 0) + 1; };
const hasLetterOrNumber = /[\p{L}\p{N}]/u;
// These patterns only apply to short, stand-alone denial/error pages. They avoid
// rejecting useful articles that happen to discuss access errors.
const denialOnly = /^(?:access denied|access forbidden|403 forbidden|you have been blocked|request blocked|website unavailable|page unavailable|service unavailable|404 not found|page not found|this site can(?:not|\x27t) be reached|the site can(?:not|\x27t) be reached|robot check|captcha required|enable javascript and cookies to continue)[.!\s]*$/i;
for (const result of response.results) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) { reject('malformed_result'); continue; }
  if (typeof result.raw_content !== 'string' || !result.raw_content.trim()) { reject('missing_raw_content'); continue; }
  if (result.raw_content.length > 500000) { reject('raw_content_too_large'); continue; }
  if (typeof result.url !== 'string') { reject('invalid_url'); continue; }
  const source = result.url.trim().split('#', 1)[0];
  if (!/^https?:\/\/[^\s/?#@]+(?:[/?#]|$)/i.test(source) || /[\u0000-\u0020\u007f]/u.test(source)) { reject('invalid_url'); continue; }
  const contentStart = result.raw_content.trimStart();
  if (/^(?:<!doctype\s+html\b|<html\b)/i.test(contentStart)) { reject('html_page'); continue; }
  const trimmed = result.raw_content.trim();
  if (!hasLetterOrNumber.test(trimmed)) { reject('no_alphanumeric_content'); continue; }
  if (trimmed.length <= 300 && denialOnly.test(trimmed)) { reject('access_denied_or_error_page'); continue; }
  results.push(result);
}
const rejected = Object.values(reasons).reduce((sum, count) => sum + count, 0);
return [{ json: { ...response, results, validation: { received: response.results.length, accepted: results.length, rejected, reasons } } }];
