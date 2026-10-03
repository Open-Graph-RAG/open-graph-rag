# Product knowledge demo

Use the [Product Knowledge agent](../../docs/agent-mcp-setup.md) in LibreChat
after ingesting the simulated sources. The agent uses LightRAG for this
fictional scenario; Jira and Figma OAuth are only needed for real references.

This folder contains a small, simulated knowledge set for the Northstar
Workspace Customer Portal and Operations Console. It represents fictional Jira and Figma
snapshots; it is not made from real Jira/Figma exports and does not connect to
those products.

Use the companion n8n workflow at
`n8n/workflows/product-knowledge-demo.json` to submit the bundled document
texts to LightRAG. It uses a Manual Trigger and carries the source data inside
the workflow, so it does not need Jira, Figma, Tavily, or other source
credentials. You do need a LightRAG Header Auth credential for `X-API-Key`.
The JSON and evidence manifest in this folder are reference material; do not
upload them as documents, since the workflow already sends each source text
with its stable filename and the evidence manifest is an evaluation aid.

## Import and ingest

1. In n8n, import `n8n/workflows/product-knowledge-demo.json` from the
   repository root.
2. In the `Queue demo document in LightRAG` node, create or select a Header
   Auth credential with header `X-API-Key` and the `LIGHTRAG_API_KEY` value
   from `.env`.
3. Save the workflow, then select **Test workflow** to run its Manual Trigger.
4. Wait for the LightRAG requests to finish. A successful queue response means
   background processing was accepted; wait until the documents show
   **processed** in LightRAG before asking questions.

The dataset is embedded as fixed snapshots in the workflow. Re-running it can
encounter a source already present in LightRAG. An `alreadyPresent` count means
the same source identifier exists; it does not guarantee that its earlier
ingestion completed successfully. Check each document's status in LightRAG and
resolve pending or failed items before evaluating retrieval. Use a clean,
isolated demo workspace when possible. The default stack uses a shared
workspace and has no project-level separation, so only ingest this fictional
dataset into a workspace where its visibility is appropriate.

## Try these prompts

Select LibreChat's configured model and enable the `lightrag` tool. Prompts are
starting points; wording of model responses is not deterministic.

| Prompt | Evidence to look for in a good answer |
|---|---|
| What is the Northstar Workspace initiative trying to achieve? | Explain the move toward customer self-service plan changes, current support workflow, and need for request status/audit visibility. Cite `demo-product-knowledge-v1-jira-OPS-205.txt`, `demo-product-knowledge-v1-jira-PLAT-77.txt`, and `demo-product-knowledge-v1-jira-CP-271.txt`; keep implementation and proposal status clear. |
| Can customers change plans immediately, or is approval required? | Surface the disagreement: `demo-product-knowledge-v1-figma-FLOW-02.txt` is a draft that proposes immediate application, while `demo-product-knowledge-v1-jira-CP-248.txt` proposes account-owner approval. Cite both and do not present either as resolved current policy. |
| Who owns the eligibility policy? | Say the owner is unassigned/unknown in `demo-product-knowledge-v1-jira-CP-263.txt`. Do not guess a team or person. |
| Which artifacts are done, in progress, proposed, open, or drafts? | Keep Jira statuses in `demo-product-knowledge-v1-jira-CP-101.txt`, `demo-product-knowledge-v1-jira-OPS-205.txt`, `demo-product-knowledge-v1-jira-CP-248.txt`, `demo-product-knowledge-v1-jira-PLAT-77.txt`, `demo-product-knowledge-v1-jira-CP-263.txt`, and `demo-product-knowledge-v1-jira-CP-271.txt` distinct from Figma draft versions in `demo-product-knowledge-v1-figma-FLOW-01.txt` and `demo-product-knowledge-v1-figma-FLOW-02.txt`. |
| What should the team do next, and what Jira tickets could be drafted? | Offer a clearly labeled proposal based on evidence, call out the unresolved approval conflict and unknown policy owner, and provide ticket drafts for human review. No Jira/Figma records are changed. |
| Which artifacts are related? | Cite the explicitly linked CP-248 / FLOW-01 / FLOW-02 and PLAT-77 relationships where documented. Label any proposed connection to the existing support workflow as a suggestion, not a source fact. |

## Human review rubric

Review the response against the source snapshots, not against a preferred
wording. A useful answer cites stable filenames near the claims they support,
preserves each artifact's status, presents the plan-change disagreement, keeps
the eligibility owner unknown, and labels inferred links as suggestions. Any
recommendations or ticket descriptions should remain drafts for a person to
review. The assistant must not claim it wrote to Jira or Figma.

An answer that sounds confident but lacks evidence, silently resolves the
conflict, invents an owner, or presents a suggested relationship as fact does
not meet the demo goal. Model phrasing and retrieval can vary between runs;
judge the evidence and distinctions rather than expecting a fixed response.
