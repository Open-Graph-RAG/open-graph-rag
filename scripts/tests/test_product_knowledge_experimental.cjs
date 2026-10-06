const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const repo = path.resolve(__dirname, '../..');
const experimental = JSON.parse(fs.readFileSync(path.join(repo, 'agents/product-knowledge-decision-experimental.json'), 'utf8'));
const original = JSON.parse(fs.readFileSync(path.join(repo, 'agents/product-knowledge.json'), 'utf8'));
const instructions = fs.readFileSync(path.join(repo, 'agents/product-knowledge-decision-experimental.instructions.md'), 'utf8');
const provisioner = require('../provision_product_agent.cjs');

// This ledger is test-only: it exercises the recipe's stated accounting with
// synthetic tool outcomes. It does not claim that the Luna model obeys prompts.
function simulateInvocations(plans, fakeDecisionEvaluate) {
  const calls = [];
  const seenInputs = new Set();
  const seenFingerprints = new Set();
  let stopForNoNovelEvidence = false;
  for (const plan of plans) {
    if (calls.length >= 3 || stopForNoNovelEvidence) break;
    const key = JSON.stringify(plan.input);
    if (seenInputs.has(key)) continue;
    seenInputs.add(key);
    const request = { ...plan.input, seen_fingerprints: [...seenFingerprints] };
    calls.push(request); // Count before the call so thrown errors consume a slot.
    let result;
    try {
      result = fakeDecisionEvaluate(request);
    } catch (error) {
      calls[calls.length - 1].error = error.message;
      continue;
    }
    for (const fingerprint of result.novelty_fingerprints || []) seenFingerprints.add(fingerprint);
    calls[calls.length - 1].result = result;
    if (result.material_context_gap && !result.relevant_new_evidence) stopForNoNovelEvidence = true;
  }
  return calls;
}

test('experimental manifest is separate, Luna-pinned, and keeps the original tools and policies', () => {
  assert.equal(experimental.name, 'Product Knowledge & Decision (Experimental)');
  assert.equal(experimental.model, 'gpt-6-luna');
  assert.equal(experimental.tools.includes('decision_evaluate_mcp_lightrag'), true);
  for (const tool of original.tools) assert.equal(experimental.tools.includes(tool), true);
  assert.equal(experimental.instructions, instructions);
  assert.match(instructions, /at most three decision_evaluate invocations total/i);
  assert.match(instructions, /including tool errors, timeouts, retrieval failures, unavailable results, and insufficient-context results/i);
  assert.match(instructions, /Never retry an identical request/i);
  assert.match(instructions, /cumulative union of all returned novelty fingerprints/i);
  assert.match(instructions, /source locator, normalized original content, and available version\/status/i);
  assert.match(instructions, /request-local IDs and retrieval timestamps do not define novelty/i);
  assert.match(instructions, /exactly: “Decision evaluation was unavailable; Kev did not assess this case\.”/);
  assert.match(instructions, /`conflict`, `compatible`, and `insufficient`/);
  assert.match(instructions, /`scope_time_sufficient`/);
  assert.match(instructions, /both sides of a suspected disagreement/);
  assert.match(instructions, /not calibrated business confidence/i);
});

test('legacy provisioning defaults to the existing ID and original model precedence', () => {
  const selected = provisioner.resolveAgentSelection(original, [], { OPENAI_MODELS: 'gpt-6-luna,gpt-4.1-mini' });
  assert.equal(selected.agentId, 'agent_product_knowledge_planner');
  assert.equal(selected.manifest.model, 'gpt-6-luna');
  assert.deepEqual(selected.manifest.tools, original.tools);
  assert.equal(original.model, 'gpt-4.1-mini');

  const withoutConfiguredModels = provisioner.resolveAgentSelection(original, [], {});
  assert.equal(withoutConfiguredModels.manifest.model, original.model);
});

test('explicit experimental ID and model override defaults without mutating the manifest', () => {
  const selected = provisioner.resolveAgentSelection(
    experimental,
    ['--agent-id', 'agent_product_knowledge_decision_experimental', '--model', 'gpt-6-luna'],
    { OPENAI_MODELS: 'gpt-4.1-mini' },
  );
  assert.equal(selected.agentId, 'agent_product_knowledge_decision_experimental');
  assert.equal(selected.manifest.model, 'gpt-6-luna');
  assert.equal(experimental.model, 'gpt-6-luna');
  assert.throws(() => provisioner.resolveAgentSelection(experimental, ['--model'], {}), /Missing --model/);
  assert.throws(() => provisioner.resolveAgentSelection({ ...experimental, model: '' }, [], {}), /Model must not be empty/);
});

test('provisioning still refuses a different owner and accepts the current owner', () => {
  const existing = { id: 'agent_product_knowledge_decision_experimental', author: 'user-1' };
  assert.doesNotThrow(() => provisioner.assertAgentOwner(existing, 'user-1'));
  assert.throws(() => provisioner.assertAgentOwner(existing, 'user-2'), /refusing to overwrite/);
  assert.doesNotThrow(() => provisioner.assertAgentOwner(null, 'user-2'));
});

test('simulated calls count errors, skip identical retries, carry cumulative fingerprints, and stop at three', () => {
  const initial = { objective: 'Compare claims', query: 'plan change approval', supplied_evidence: [] };
  const plans = [
    { input: initial },
    { input: initial }, // The identical failed request is not retried.
    { input: { ...initial, query: 'approval effective time and workflow scope' } },
    { input: { ...initial, query: 'verify the second source status and version' } },
    { input: { ...initial, query: 'a fourth query must never run' } },
  ];
  const calls = simulateInvocations(plans, request => {
    if (request.query === initial.query) throw new Error('synthetic runtime error');
    if (request.query.startsWith('approval')) {
      return { status: 'insufficient_context', novelty_fingerprints: ['sha256:first'] };
    }
    return { status: 'unavailable', novelty_fingerprints: ['sha256:second'] };
  });
  assert.equal(calls.length, 3);
  assert.equal(calls[0].error, 'synthetic runtime error');
  assert.deepEqual(calls[1].seen_fingerprints, []);
  assert.deepEqual(calls[2].seen_fingerprints, ['sha256:first']);
  assert.notEqual(calls.some(call => call.query === 'a fourth query must never run'), true);
});

test('simulated recipe stops when a material gap yields no relevant new evidence', () => {
  const base = { objective: 'Compare claims', supplied_evidence: [] };
  let attempted = 0;
  const calls = simulateInvocations([
    { input: { ...base, query: 'initial evidence' } },
    { input: { ...base, query: 'targeted scope follow-up' } },
  ], () => {
    attempted += 1;
    return { status: 'insufficient_context', material_context_gap: true, relevant_new_evidence: false };
  });
  assert.equal(attempted, 1);
  assert.equal(calls.length, 1);
});
