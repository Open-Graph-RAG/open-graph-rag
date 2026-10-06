const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const dir = path.join(__dirname, 'workflows');
const generator = path.join(__dirname, 'generate_workflows.py');

function visit(step, output = []) {
  if (!step || typeof step !== 'object') return output;
  output.push(step);
  if (step.nextAction) visit(step.nextAction, output);
  if (step.firstLoopAction) visit(step.firstLoopAction, output);
  for (const branch of Object.values(step.continueOnFailureBranches || {})) visit(branch, output);
  for (const branch of step.children || []) visit(branch, output);
  return output;
}

test('generated Activepieces exports are current, sanitized schema-27 templates', () => {
  execFileSync('python3', [generator, '--check']);
  const files = fs.readdirSync(dir).filter((name) => name.endsWith('.json'));
  assert.deepEqual(files.sort(), [
    'google-notebooks-to-lightrag.json',
    'internet-to-lightrag.json',
    'product-knowledge-demo.json',
  ]);
  for (const file of files) {
    const template = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'));
    assert.equal(template.schemaVersion, '27');
    assert.equal(template.flows.length, 1);
    assert.equal(template.flows[0].schemaVersion, '27');
    assert.equal(template.flows[0].valid, false);
    assert.deepEqual(template.flows[0].agentIds, []);
    assert.deepEqual(template.flows[0].connectionIds, []);
    const serialized = JSON.stringify(template);
    assert.doesNotMatch(serialized, /REPLACE_WITH|Bearer [A-Za-z0-9._-]{20,}|"(apiKey|access_token|client_secret)"\s*:/i);
  }
});

test('API IMPORT_FLOW extraction uses the same template trigger and includes nested actions', () => {
  for (const slug of ['internet-to-lightrag', 'google-notebooks-to-lightrag', 'product-knowledge-demo']) {
    const operation = JSON.parse(execFileSync('python3', [generator, '--import-request', slug], { encoding: 'utf8' }));
    const flow = JSON.parse(fs.readFileSync(path.join(dir, `${slug}.json`), 'utf8')).flows[0];
    assert.equal(operation.type, 'IMPORT_FLOW');
    assert.equal(operation.request.displayName, flow.displayName);
    assert.equal(operation.request.schemaVersion, '27');
    assert.deepEqual(operation.request.trigger, flow.trigger);
  }
});

test('webhook templates require literal native auth config and return only public response shape', () => {
  for (const slug of ['internet-to-lightrag', 'google-notebooks-to-lightrag']) {
    const flow = JSON.parse(fs.readFileSync(path.join(dir, `${slug}.json`), 'utf8')).flows[0];
    assert.equal(flow.trigger.settings.input.authType, 'header');
    assert.equal(flow.trigger.settings.input.authFields.headerName, 'X-Ingest-Token');
    assert.equal(flow.trigger.settings.input.authFields.headerValue, 'CONFIGURE_INCOMING_TOKEN');
    assert.equal(flow.trigger.settings.input.auth, undefined);
    const steps = visit(flow.trigger);
    const reply = steps.find((step) => step.settings?.actionName === 'return_response');
    assert.ok(reply, `${slug} must end with a synchronous webhook response`);
    assert.equal(reply.settings.pieceName, '@activepieces/piece-webhook');
    assert.equal(reply.settings.input.respond, 'stop');
    const projections = steps.filter((step) => step.name?.startsWith('public_response'));
    assert.ok(projections.length > 0);
    for (const projection of projections) {
      assert.match(projection.settings.sourceCode?.code ?? '', /httpStatus, \.\.\.body/);
    }
  }
});

test('generated webhook and HTTP piece versions are exact tested versions', () => {
  for (const file of fs.readdirSync(dir).filter((name) => name.endsWith('.json'))) {
    const flow = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8')).flows[0];
    for (const step of visit(flow.trigger)) {
      if (step.settings?.pieceName === '@activepieces/piece-webhook') assert.equal(step.settings.pieceVersion, '0.1.42');
      if (step.settings?.pieceName === '@activepieces/piece-http') assert.equal(step.settings.pieceVersion, '0.12.1');
      if (step.settings?.pieceName === '@activepieces/piece-http-oauth2') assert.equal(step.settings.pieceVersion, '0.3.0');
    }
  }
});

test('HTTP bearer authentication consumes encrypted connection fields while LightRAG keeps its API-key header', () => {
  const internet = JSON.parse(fs.readFileSync(path.join(dir, 'internet-to-lightrag.json'), 'utf8')).flows[0];
  const google = JSON.parse(fs.readFileSync(path.join(dir, 'google-notebooks-to-lightrag.json'), 'utf8')).flows[0];
  const internetSteps = visit(internet.trigger);
  const search = internetSteps.find((step) => step.name === 'search_web');
  assert.equal(search.settings.input.authType, 'BEARER_TOKEN');
  assert.equal(search.settings.input.authFields.token, "{{connections.tavily_bearer_api_key.secret_text}}");
  assert.equal(search.settings.input.headers.Authorization, undefined);
  const insert = internetSteps.find((step) => step.name === 'queue_page');
  assert.equal(insert.settings.input.authType, 'NONE');
  assert.equal(insert.settings.input.headers['X-API-Key'], "{{connections.lightrag_api_key.secret_text}}");
  const exportDoc = visit(google.trigger).find((step) => step.name === 'export_google_doc');
  assert.equal(exportDoc.settings.pieceName, '@activepieces/piece-http-oauth2');
  assert.equal(exportDoc.settings.pieceVersion, '0.3.0');
  assert.equal(exportDoc.settings.actionName, 'send-oauth2-request');
  assert.equal(exportDoc.settings.input.auth, '{{connections.google_drive_oauth2}}');
  assert.equal(exportDoc.settings.input.authFields, undefined);
});

test('public ingestion flows map method, input, and upstream failures to stable HTTP contracts', () => {
  for (const [slug, expectedErrors] of [
    ['internet-to-lightrag', ['validate_method', 'invalid_input', 'search_failed', 'search_response_failed']],
    ['google-notebooks-to-lightrag', ['validate_method', 'invalid_input', 'google_export_failed', 'google_ingestion_failed']],
  ]) {
    const flow = JSON.parse(fs.readFileSync(path.join(dir, `${slug}.json`), 'utf8')).flows[0];
    const steps = visit(flow.trigger);
    for (const name of expectedErrors) assert.ok(steps.some((step) => step.name === name), `${slug} missing ${name}`);
    const method = steps.find((step) => step.name === 'validate_method');
    assert.equal(method.settings.errorHandlingOptions.continueOnFailure.value, true);
    assert.equal(method.continueOnFailureBranches.onFailure.name, 'method_not_allowed');
    const invalid = steps.find((step) => step.name === 'invalid_input');
    assert.match(invalid.settings.sourceCode.code, /httpStatus: 400/);
    assert.match(invalid.settings.sourceCode.code, /code: 'invalid_input'/);
    for (const failure of expectedErrors.slice(2).map((name) => steps.find((step) => step.name === name))) {
      assert.match(failure.settings.sourceCode.code, /httpStatus: 502/);
      assert.match(failure.settings.sourceCode.code, /code: 'upstream_failure'/);
    }
    assert.ok(steps.filter((step) => step.settings?.actionName === 'return_response').length >= expectedErrors.length);
  }
});

test('generated ingestion actions project output references once and preserve internet provenance', () => {
  const internet = JSON.parse(fs.readFileSync(path.join(dir, 'internet-to-lightrag.json'), 'utf8')).flows[0];
  const steps = visit(internet.trigger);
  for (const projection of steps.filter((step) => step.name?.startsWith('public_response'))) {
    const reference = projection.settings.input.result;
    assert.match(reference, /^{{[a-z_]+\['output'\]}}$/);
    assert.doesNotMatch(reference, /\['output'\].*\['output'\]/);
  }
  const prepare = steps.find((step) => step.name === 'prepare_pages');
  assert.match(prepare.settings.sourceCode.code, /prepareGraphDocuments\(bundle, checked\)/);
  const insert = steps.find((step) => step.name === 'queue_page');
  assert.ok(insert.settings.input.body.data.metadata);
  assert.equal(insert.continueOnFailureBranches.onFailure.settings.input.errorMessage, "{{queue_page['error']['message']}}");

  const google = JSON.parse(fs.readFileSync(path.join(dir, 'google-notebooks-to-lightrag.json'), 'utf8')).flows[0];
  const googleInsert = visit(google.trigger).find((step) => step.name === 'queue_google_doc');
  assert.equal(googleInsert.continueOnFailureBranches.onFailure.settings.input.errorMessage,
    "{{queue_google_doc['error']['message']}}");

  const demoRequest = JSON.parse(execFileSync('python3', [generator, '--import-request', 'product-knowledge-demo'], { encoding: 'utf8' }));
  const demoInsert = visit(demoRequest.request.trigger).find((step) => step.name === 'queue_demo_doc');
  assert.equal(demoInsert.continueOnFailureBranches.onFailure.settings.input.errorMessage,
    "{{queue_demo_doc['error']['message']}}");
});

test('demo acceptance webhook is temporary and the checked-in template stays manual', () => {
  const flow = JSON.parse(fs.readFileSync(path.join(dir, 'product-knowledge-demo.json'), 'utf8')).flows[0];
  assert.equal(flow.trigger.type, 'EMPTY');
  const acceptance = JSON.parse(execFileSync('python3', [generator, '--import-request', 'product-knowledge-demo', '--acceptance-webhook'], { encoding: 'utf8' }));
  assert.equal(acceptance.request.trigger.type, 'PIECE_TRIGGER');
  assert.equal(acceptance.request.trigger.settings.triggerName, 'catch_webhook');
  assert.equal(acceptance.request.trigger.settings.input.authFields.headerName, 'X-Ingest-Token');
  assert.equal(acceptance.request.trigger.settings.input.authFields.headerValue, 'CONFIGURE_INCOMING_TOKEN');
  assert.equal(visit(acceptance.request.trigger).filter((step) => step.settings?.actionName === 'return_response').length, 1);
});

test('ingestion imports use bounded no-retry HTTP and explicit per-item failure branches', () => {
  for (const slug of ['internet-to-lightrag', 'google-notebooks-to-lightrag', 'product-knowledge-demo']) {
    const flow = JSON.parse(fs.readFileSync(path.join(dir, `${slug}.json`), 'utf8')).flows[0];
    const steps = visit(flow.trigger);
    const inserts = steps.filter((step) => step.type === 'PIECE' && step.settings.actionName === 'send_request' &&
      step.settings.input.url === 'http://lightrag:9621/documents/text');
    assert.ok(inserts.length > 0, `${slug} should insert documents into LightRAG`);
    for (const insert of inserts) {
      assert.equal(insert.settings.input.timeout, 5);
      assert.equal(insert.settings.input.failureMode, 'retry_none');
      assert.equal(insert.settings.errorHandlingOptions.retryOnFailure.value, false);
      assert.ok(insert.continueOnFailureBranches.onSuccess);
      assert.ok(insert.continueOnFailureBranches.onFailure);
    }
    if (slug !== 'google-notebooks-to-lightrag') {
      assert.ok(steps.some((step) => step.type === 'LOOP_ON_ITEMS'));
    }
  }
  const internet = JSON.parse(fs.readFileSync(path.join(dir, 'internet-to-lightrag.json'), 'utf8')).flows[0];
  const deadline = visit(internet.trigger).find((step) => step.name === 'deadline_gate');
  assert.equal(deadline.settings.errorHandlingOptions.continueOnFailure.value, true);
  assert.ok(deadline.continueOnFailureBranches.onSuccess);
  assert.ok(deadline.continueOnFailureBranches.onFailure);
  const google = JSON.parse(fs.readFileSync(path.join(dir, 'google-notebooks-to-lightrag.json'), 'utf8')).flows[0];
  const googleDeadline = visit(google.trigger).find((step) => step.name === 'google_deadline_gate');
  assert.ok(googleDeadline, 'Google insertion must honor the gateway insertion cutoff');
  assert.ok(googleDeadline.continueOnFailureBranches.onSuccess);
  assert.ok(googleDeadline.continueOnFailureBranches.onFailure);
});
