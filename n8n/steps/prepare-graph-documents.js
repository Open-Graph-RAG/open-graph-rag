// n8n Code node body: place after the existing dedup node and before its IF node.
// Text and source values are copied verbatim because LightRAG uses them for identity.
const input = $input.first()?.json;
if (!input || typeof input !== 'object' || Array.isArray(input)) {
  throw new Error('Deduplication step returned a malformed document bundle.');
}
if (!Array.isArray(input.documents)) {
  throw new Error('Deduplication step must return a documents array.');
}
const validated = $('Validate search results').first()?.json;
if (!validated || !Array.isArray(validated.results)) {
  throw new Error('Validated search results are unavailable for metadata mapping.');
}
const stripFragment = (url) => url.split('#', 1)[0];
const metadataBySource = new Map();
for (const result of validated.results) {
  if (!result || typeof result.url !== 'string') continue;
  const source = stripFragment(result.url.trim());
  if (!metadataBySource.has(source)) {
    metadataBySource.set(source, {
      title: typeof result.title === 'string' && result.title.trim() ? result.title : undefined,
    });
  }
}
const seenSources = new Set();
const documents = input.documents.map((doc, index) => {
  if (!doc || typeof doc !== 'object' || typeof doc.text !== 'string' || !doc.text.trim() || typeof doc.file_source !== 'string' || !doc.file_source.trim()) {
    throw new Error(`Deduplicated document ${index} has invalid text/source alignment.`);
  }
  if (seenSources.has(doc.file_source)) {
    throw new Error(`Deduplicated documents contain a repeated source at index ${index}.`);
  }
  seenSources.add(doc.file_source);
  const match = metadataBySource.get(stripFragment(doc.file_source));
  return {
    ...doc,
    metadata: {
      source_url: doc.file_source,
      ...(match?.title ? { title: match.title } : {}),
      query: input.prompt,
      content_type: 'text/plain',
    },
  };
});
return [{ json: { ...input, found: documents.length, documents } }];
