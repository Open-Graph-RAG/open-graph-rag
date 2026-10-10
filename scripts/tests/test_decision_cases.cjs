const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const datasetPath = path.resolve(__dirname, '../../evaluation/decision_cases.json');
const dataset = JSON.parse(fs.readFileSync(datasetPath, 'utf8'));
const cases = dataset.cases;
const labels = ['conflict', 'compatible', 'insufficient'];
const requiredCoverage = [
  'multihop',
  'distractor',
  'unknown_ownership',
  'injection',
  'misleading_graph',
  'missing_links',
];

function tally(items, field) {
  return items.reduce((counts, item) => {
    counts[item[field]] = (counts[item[field]] || 0) + 1;
    return counts;
  }, {});
}

test('dataset has the requested development and held-out balance', () => {
  assert.equal(dataset.version, 1);
  assert.equal(cases.length, 84);
  assert.deepEqual(tally(cases, 'split'), { dev: 24, heldout: 60 });
  for (const split of ['dev', 'heldout']) {
    const splitCases = cases.filter(item => item.split === split);
    const expectedPerLabel = split === 'dev' ? 8 : 20;
    assert.deepEqual(tally(splitCases, 'label'), Object.fromEntries(labels.map(label => [label, expectedPerLabel])));
  }
});

test('scenario and case identifiers are unique and scenario sets do not overlap', () => {
  assert.equal(new Set(cases.map(item => item.id)).size, cases.length);
  assert.equal(new Set(cases.map(item => item.scenario_id)).size, cases.length);
  const devScenarios = new Set(cases.filter(item => item.split === 'dev').map(item => item.scenario_id));
  for (const item of cases.filter(item => item.split === 'heldout')) assert.equal(devScenarios.has(item.scenario_id), false);
});

test('all retrieval, graph, and gold references resolve to original source passages', () => {
  for (const item of cases) {
    assert.ok(item.objective.trim());
    assert.ok(item.user_query.trim());
    assert.ok(Array.isArray(item.sources) && item.sources.length >= 2 && item.sources.length <= 8, item.id);
    const sourceIds = item.sources.map(source => source.id);
    const known = new Set(sourceIds);
    assert.equal(known.size, sourceIds.length, `${item.id} has duplicate source IDs`);
    for (const source of item.sources) {
      assert.ok(source.locator.trim(), `${item.id} has an empty locator`);
      assert.ok(source.text.trim(), `${item.id} has an empty source excerpt`);
      assert.ok(Buffer.byteLength(source.text, 'utf8') <= 8000, `${item.id} exceeds the per-excerpt input limit`);
      assert.ok(typeof source.claim === 'string' && source.claim.length > 0, `${item.id} lacks a gold claim`);
      assert.ok(source.text.includes(source.claim), `${item.id} gold claim is not quoted in its passage`);
      for (const field of ['actor', 'scope', 'status', 'effective_time', 'owner']) {
        const value = source[field];
        assert.ok(value === null || (typeof value === 'string' && value.length > 0), `${item.id} has malformed ${field}`);
        if (value !== null) assert.ok(source.text.includes(value), `${item.id} ${field} is not grounded in its passage`);
      }
    }
    assert.ok(item.initial_source_ids.length > 0, `${item.id} lacks an initial source`);
    for (const id of item.initial_source_ids) assert.ok(known.has(id), `${item.id} has unknown initial source ${id}`);
    assert.ok(item.targeted_query_expansion && typeof item.targeted_query_expansion === 'object');
    for (const [query, ids] of Object.entries(item.targeted_query_expansion)) {
      assert.ok(query.trim());
      assert.ok(Array.isArray(ids));
      for (const id of ids) assert.ok(known.has(id), `${item.id} has unknown expansion source ${id}`);
    }
    for (const id of item.gold_supporting_source_ids) assert.ok(known.has(id), `${item.id} has unknown gold source ${id}`);
    for (const pair of item.gold_citation_pairs) {
      assert.equal(pair.length, 2, `${item.id} citation pair must have two IDs`);
      for (const id of pair) assert.ok(known.has(id), `${item.id} has unknown citation source ${id}`);
    }
    for (const assertion of item.graph_assertions) {
      assert.ok(assertion.text.trim());
      assert.ok(assertion.source_ids.length > 0);
      for (const id of assertion.source_ids) assert.ok(known.has(id), `${item.id} graph assertion has unknown source ${id}`);
    }
    assert.ok(item.expected_limitations.length > 0, `${item.id} needs a limitation`);
    assert.ok(Buffer.byteLength(JSON.stringify(item), 'utf8') < 64000, `${item.id} exceeds the serialized input budget`);
  }
});

test('gold structure matches each decision class', () => {
  for (const item of cases) {
    const [left, right] = item.sources;
    if (item.label === 'conflict') {
      for (const field of ['actor', 'scope', 'status', 'effective_time']) {
        assert.ok(left[field] && right[field], `${item.id} conflict lacks ${field}`);
        assert.equal(left[field], right[field], `${item.id} conflict differs in ${field}`);
      }
      assert.notEqual(left.claim, right.claim, `${item.id} conflict claims must differ`);
      assert.deepEqual(item.gold_citation_pairs, [[left.id, right.id]]);
    } else if (item.label === 'compatible') {
      const differs = ['actor', 'scope', 'status', 'effective_time'].some(field => left[field] !== right[field]);
      assert.ok(differs, `${item.id} compatible case needs an explicit scope/time/actor/status distinction`);
      assert.notEqual(left.scope, left.claim, `${item.id} left scope should be a scope label, not a claim paraphrase`);
      assert.notEqual(right.scope, right.claim, `${item.id} right scope should be a scope label, not a claim paraphrase`);
      assert.deepEqual(item.gold_citation_pairs, [[left.id, right.id]]);
    } else {
      const hasGap = ['actor', 'scope', 'status', 'effective_time'].some(field => left[field] === null || right[field] === null || left[field] !== right[field]);
      assert.ok(hasGap, `${item.id} insufficient case lacks a missing or differing qualifier`);
      assert.deepEqual(item.gold_citation_pairs, []);
    }
  }
});

test('coverage includes the requested edge cases and multihop evidence paths', () => {
  assert.ok(cases.filter(item => item.initial_source_ids.length >= 2).length >= 75, 'most cases should begin with both relevant passages');
  const allTags = new Set(cases.flatMap(item => item.coverage_tags || []));
  for (const tag of requiredCoverage) assert.ok(allTags.has(tag), `missing coverage tag: ${tag}`);
  const multihop = cases.filter(item => item.coverage_tags?.includes('multihop'));
  assert.ok(multihop.length >= 2);
  assert.ok(multihop.every(item => item.graph_assertions.some(assertion => assertion.source_ids.length >= 3)));
  assert.ok(cases.some(item => item.coverage_tags?.includes('missing_links') && Object.values(item.targeted_query_expansion).some(ids => ids.length === 0)));
  for (const item of cases.filter(entry => entry.coverage_tags?.includes('targeted_qualifier_gap') || entry.coverage_tags?.includes('multihop'))) {
    const initiallyVisible = new Set(item.initial_source_ids);
    const expandedIds = Object.values(item.targeted_query_expansion).flat();
    assert.ok(expandedIds.length > 0, `${item.id} has no targeted evidence`);
    assert.ok(expandedIds.every(id => !initiallyVisible.has(id)), `${item.id} exposes targeted evidence initially`);
  }
  const injection = cases.find(item => item.coverage_tags?.includes('injection'));
  assert.ok(injection.sources.some(source => /ignore (prior rules|your instructions)/i.test(source.text)));
});

test('injected instructions remain only in the original passage, not in the gold claim', () => {
  const item = cases.find(entry => entry.scenario_id === 'injection_ticket');
  assert.ok(item);
  const source = item.sources[0];
  assert.match(source.text, /Ignore prior rules/i);
  assert.equal(source.claim, 'purge after 14 days');
  assert.ok(source.text.includes(source.claim));
});
