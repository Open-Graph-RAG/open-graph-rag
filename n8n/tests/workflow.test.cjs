const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const workflow = require('../workflows/internet-to-lightrag.json');
const node = name => workflow.nodes.find(n => n.name === name);
function run(name, items, refs = {}) {
  const context = { $json: items[0]?.json, $input: { first: () => items[0], all: () => items },
    $: key => ({ first: () => refs[key]?.[0], all: () => refs[key] }) };
  return JSON.parse(JSON.stringify(vm.runInNewContext(`(function(){${node(name).parameters.jsCode}\n})()`, context)));
}
const promptRefs = { 'Validate prompt': [{ json: { prompt: 'topic' } }] };
test('authenticated, inactive workflow has valid connections and no embedded keys', () => {
  assert.equal(workflow.active, false);
  assert.equal(node('Authenticated prompt webhook').parameters.authentication, 'headerAuth');
  const names = new Set(workflow.nodes.map(n => n.name));
  for (const [source, outputs] of Object.entries(workflow.connections)) {
    assert.ok(names.has(source));
    for (const branch of outputs.main) for (const edge of branch) assert.ok(names.has(edge.node));
  }
  for (const name of ['Search web with Tavily', 'Queue page in LightRAG']) {
    const auth = name === 'Search web with Tavily' ? 'httpBearerAuth' : 'httpHeaderAuth';
    assert.equal(node(name).parameters.genericAuthType, auth);
    assert.ok(node(name).credentials[auth]);
  }
});
test('prompt and result bounds are validated', () => {
  assert.equal(run('Validate prompt', [{json:{body:{prompt:' topic '}}}])[0].json.maxResults, 5);
  for (const body of [{}, {prompt:' '}, {prompt:'a',maxResults:0}, {prompt:'a',maxResults:11}, {prompt:'a',maxResults:true}])
    assert.throws(() => run('Validate prompt', [{json:{body}}]));
});
test('exact normalized content and URLs dedup; snippets and invalid URLs skipped', () => {
  const results = [
    {url:'https://one.example/page',raw_content:'Ａ  useful\npage'},
    {url:'https://two.example/page',raw_content:'A useful page'},
    {url:'https://one.example/page',raw_content:'different text'},
    {url:'https://three.example',content:'snippet only'},
    {url:'file:///etc/passwd',raw_content:'private'},
    {url:'https://user:pass@four.example',raw_content:'userinfo'},
    {url:'https://five.example/page',raw_content:'Another page'},
  ];
  const docs = run('Deduplicate raw page content', [{json:{results}}], promptRefs)[0].json.documents;
  assert.equal(docs.length, 2);
  assert.equal(docs[0].text, 'A useful page');
  assert.equal(docs[0].file_source, 'https://one.example/page');
});
test('empty search yields no ingestion items', () => {
  const result = run('Deduplicate raw page content', [{json:{results:[]}}], promptRefs);
  assert.equal(result[0].json.found, 0);
  assert.deepEqual(run('Expand unique pages', result), []);
});
test('queue summary distinguishes accepted, existing source, and errors', () => {
  const refs = {'Expand unique pages':[{json:{file_source:'https://one.example'}},{json:{file_source:'https://two.example'}}]};
  const success = {json:{statusCode:200,body:{status:'success',track_id:'insert_123'}}};
  const duplicate = {json:{statusCode:409,body:{detail:"Document storage already contains 'https://two.example' (Status: processed)."}}};
  const result = run('Summarize queue results', [success,duplicate], refs)[0].json;
  assert.equal(result.queued, 1);
  assert.equal(result.alreadyPresent, 1);
  assert.deepEqual(result.trackIds, ['insert_123']);
  for (const response of [
    {json:{statusCode:409,body:{detail:'Pipeline is busy'}}},
    {json:{statusCode:401,body:{detail:'Unauthorized'}}},
    {json:{statusCode:200,body:{status:'failure'}}},
  ]) assert.throws(() => run('Summarize queue results', [response], refs));
});
test('search validation rejects malformed provider responses and unusable pages', () => {
  for (const json of [{}, {results:null}, {results:[],error:'quota exceeded'}])
    assert.throws(() => run('Validate search results', [{json}]));
  const valid = {url:'https://example.com/docs',title:'Docs',raw_content:'A short but valid fact.'};
  const result = run('Validate search results', [{json:{results:[valid,null,{url:'https://example.com/empty',raw_content:'  '},{url:'https://example.com/error',raw_content:'Access denied'},{url:'https://example.com/html',raw_content:'<!DOCTYPE html><html>blocked</html>'},{url:'javascript:alert(1)',raw_content:'a fact'}]}}])[0].json;
  assert.deepEqual(result.results, [valid]);
  assert.equal(result.validation.received, 6);
  assert.equal(result.validation.accepted, 1);
  assert.equal(result.validation.rejected, 5);
});
test('preparation preserves document identities and attaches provenance separately', () => {
  const doc = {text:'A useful page',file_source:'https://example.com/docs'};
  const refs = {'Validate search results':[{json:{results:[{url:doc.file_source,title:'Documentation'}],validation:{received:1,accepted:1,rejected:0}}}]};
  const prepared = run('Prepare graph documents', [{json:{documents:[doc],found:1,prompt:'topic'}}], refs)[0].json;
  assert.equal(prepared.documents[0].text, doc.text);
  assert.equal(prepared.documents[0].file_source, doc.file_source);
  assert.equal(prepared.documents[0].metadata.source_url, doc.file_source);
  assert.equal(prepared.documents[0].metadata.query, 'topic');
  const expanded = run('Expand unique pages', [{json:prepared}]);
  assert.equal(expanded[0].json.text, doc.text);
  assert.equal(expanded[0].json.file_source, doc.file_source);
});
test('new steps precede ingestion and empty results still use existing reply branch', () => {
  const next = name => workflow.connections[name].main[0][0].node;
  assert.equal(next('Search web with Tavily'), 'Validate search results');
  assert.equal(next('Validate search results'), 'Deduplicate raw page content');
  assert.equal(next('Deduplicate raw page content'), 'Prepare graph documents');
  assert.equal(next('Prepare graph documents'), 'Any usable pages?');
  assert.equal(workflow.connections['Any usable pages?'].main[1][0].node,'Reply no usable pages');
});
test('full validation-to-ingestion preparation retains existing dedup outputs', () => {
  const results = [{url:'https://example.com/a',raw_content:'Ａ useful\npage',title:'Article'},
    {url:'https://example.com/b',raw_content:'A useful page',title:'Mirror'},
    {url:'https://example.com/c',raw_content:'Second useful page',title:'Other'}];
  const before = run('Deduplicate raw page content', [{json:{results}}], promptRefs)[0].json;
  const validated = run('Validate search results', [{json:{results}}]);
  const after = run('Deduplicate raw page content', validated, promptRefs);
  const prepared = run('Prepare graph documents', after, {'Validate search results':validated})[0].json;
  assert.deepEqual(prepared.documents.map(({text,file_source}) => ({text,file_source})),before.documents);
  assert.equal(prepared.found,before.found);
});
