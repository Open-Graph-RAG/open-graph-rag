# Product Knowledge agent: Jira and Figma connections

LibreChat includes a **Product Knowledge & Initiative Planner** agent for
evidence-based answers and cross-team initiative drafts. It can search the
shared LightRAG knowledge base and, after each user connects their account,
read Jira and Figma through the official remote MCP servers. Jira and Figma
access uses the signed-in user's permissions; a configured server does not
grant access to projects or files that user cannot already open.

## Local setup status

The agent has been provisioned in the local LibreChat instance with ID
`agent_product_knowledge_planner`. Its configured runtime model is `gpt-6-luna`;
the portable manifest defaults to `gpt-4.1-mini`, and the provisioning script
selects the first model in `OPENAI_MODELS`.

LightRAG's MCP initialization and tool discovery have been verified. Jira and
Figma are configured and attached, but each user must complete OAuth before
their live records can be queried. Live Jira/Figma calls and a full agent
conversation have not been verified.

The fictional demo workflow and its eleven source documents are ready in this
repository. They have not been imported into n8n or ingested into LightRAG by
the setup process. Follow the [demo guide](../sample-data/product-knowledge-demo/README.md)
to import, execute, and wait for indexing before evaluating answers.

The default LightRAG workspace is shared across users and has no document-level
access controls. Jira/Figma user permissions do not extend to indexed LightRAG
snapshots; use an isolated workspace when demonstrating separate access scopes.

## Selected tools

The agent binds the LightRAG `knowledge_search` tool and a selected read-only
subset of the Jira and Figma server tools:

| Provider | Bound tools |
|---|---|
| LightRAG | `knowledge_search_mcp_lightrag` |
| Jira | `getAccessibleAtlassianResources_mcp_jira`, `listJiraProjects_mcp_jira`, `searchJiraIssuesUsingJql_mcp_jira`, `getJiraIssue_mcp_jira` |
| Figma | `get_design_context_mcp_figma`, `get_metadata_mcp_figma`, `get_screenshot_mcp_figma`, `get_variable_defs_mcp_figma` |

Atlassian and Figma expose additional tools, including tools that can modify
external records. This agent's bindings select only the read tools listed
above. Its instructions also prohibit external writes. Instructions are model
guidance, not a security boundary; keep the agent's tool bindings restricted to
the listed read-only tools when editing its configuration.

## Connect your accounts

The remote MCP server configuration is in the root `librechat.yaml` file:

- Jira uses Atlassian's current MCP v2 endpoint,
  `https://mcp.atlassian.com/v2/mcp`.
- Figma uses `https://mcp.figma.com/mcp`.
- Both servers have OAuth required and `startup: false`, so LibreChat can start
  before users connect their accounts and initializes each connection when it
  is needed.

Restart or recreate LibreChat after an administrator changes `librechat.yaml`:

```bash
docker compose up -d --no-deps --force-recreate librechat
```

Open the **Product Knowledge & Initiative Planner** agent. When prompted,
complete the Atlassian or Figma OAuth flow and approve the requested access.
Each user authenticates their own account. Jira access depends on the user's
Atlassian site and project permissions; Figma access depends on files the
user can access. If an MCP tool is unavailable, reconnect that provider and
check the user's access to the requested project or file.

The repository config does not contain service credentials. LibreChat stores
per-user OAuth authorization through its MCP OAuth support. The OAuth callback
must be reachable at the URL users use to access LibreChat; when using a public
host or reverse proxy, configure the public LibreChat URL and HTTPS correctly.

## Provision the agent

The reproducible agent definition is in `agents/product-knowledge.json`; its
instructions are also readable in `agents/product-knowledge.instructions.md`.
The administrative script uses LibreChat's own agent model and permission
helpers. It requires an existing account and grants ownership only to that
account. It does not create a user, change passwords, or complete OAuth.

After starting the stack and loading the three configured MCP servers:

```bash
docker compose cp agents/product-knowledge.json librechat:/tmp/product-knowledge-agent.json
docker compose cp scripts/provision_product_agent.cjs librechat:/tmp/provision_product_agent.cjs
docker compose exec -T librechat node /tmp/provision_product_agent.cjs --email you@example.com --manifest /tmp/product-knowledge-agent.json
# Review the check output, then provision for the same account:
docker compose exec -T librechat node /tmp/provision_product_agent.cjs --email you@example.com --manifest /tmp/product-knowledge-agent.json --apply
```

Replace `you@example.com` with the existing LibreChat account. The script uses
the first model in the container's configured `OPENAI_MODELS`, preserves
version history on updates, and refuses to overwrite an agent owned by a
different user. Reapplying updates this specific agent's managed definition;
export any customizations before replacing them.

## Experimental decision agent

The experimental recipe is kept separately in
`agents/product-knowledge-decision-experimental.json`, with a readable mirror
in `agents/product-knowledge-decision-experimental.instructions.md`. It has a
separate ID, `agent_product_knowledge_decision_experimental`, and explicitly
uses `gpt-6-luna`; it does not replace or update the planner above. The recipe
is advisory and asks for no more than three decision-tool invocations. This is
a prompt policy, not a deterministic limit enforced by the model runtime; the
offline test simulates attempts but does not prove Luna will obey it.
The tool appears only when the optional decision deployment is enabled; prepare
its local model cache as described in [the Kev setup guide](kev-decision-setup.md).

Before provisioning, configure `.env` with `gpt-6-luna` in `CHAT_MODELS` and
valid `CHAT_API_KEY` and `CHAT_API_BASE` values for the intended
OpenAI-compatible provider. The Compose configuration passes `CHAT_MODELS` to
LibreChat as `OPENAI_MODELS`; the explicit `--model` flag below selects Luna
for this agent even when another model appears first. Keep the ordinary
`gpt-4.1-mini` default in `.env.example` for the original setup.

Copy the separate definition and provisioner into LibreChat. First run the
check mode for the existing account, then apply the same explicit selection:

```bash
docker compose cp agents/product-knowledge-decision-experimental.json librechat:/tmp/product-knowledge-decision-experimental.json
docker compose cp scripts/provision_product_agent.cjs librechat:/tmp/provision_product_agent.cjs
docker compose exec -T librechat node /tmp/provision_product_agent.cjs --email you@example.com --manifest /tmp/product-knowledge-decision-experimental.json --agent-id agent_product_knowledge_decision_experimental --model gpt-6-luna
# Review the check output, then provision for the same account:
docker compose exec -T librechat node /tmp/provision_product_agent.cjs --email you@example.com --manifest /tmp/product-knowledge-decision-experimental.json --agent-id agent_product_knowledge_decision_experimental --model gpt-6-luna --apply
```

The provisioner retains its original ID and model-selection behavior when
`--agent-id` and `--model` are omitted. It checks ownership for whichever ID
is selected and refuses to overwrite an agent owned by another user. The
provisioner tests are offline and do not connect to MongoDB, provision a live
agent, or call a model provider:

```bash
node scripts/tests/test_product_knowledge_experimental.cjs
```

Those tests check manifest separation, the recipe's prompt invariants, model
and ID selection, and owner refusal. Their synthetic three-attempt ledger
checks that errors consume attempts, identical inputs are skipped, cumulative
fingerprints are carried, and follow-up stops occur; it is not an evaluation
of model behavior.

Select **Product Knowledge & Initiative Planner** in LibreChat. For the
[fictional demo](../sample-data/product-knowledge-demo/README.md), ingest the
simulated sources first and use LightRAG; Jira/Figma authorization is only
needed for real project and design references.

## Evidence and safety

The agent searches LightRAG for company-knowledge questions and uses Jira and
Figma for current issue and design evidence. It cites actual ticket or design
references, distinguishes live records from indexed snapshots, and identifies
contradictory sources instead of choosing a winner without evidence. The
fictional Northstar demo uses only local LightRAG snapshots; its `.invalid`
Jira and Figma URLs are examples and must not be sent to the live providers.

The current agent binds only the documented read tools above, so it cannot
create, edit, transition, assign, comment on, or delete Jira work items through
its selected MCP tools. It cannot create or edit Figma files through its
selected MCP tools. The remote providers themselves expose more capabilities;
do not add write tools to the agent unless the workflow is deliberately
changed, reviewed, and authorized.

Retrieved ticket text, comments, and design content are untrusted data. The
agent does not follow instructions embedded in them. It prepares drafts in the
conversation and does not send external messages or create records.

## Official references

- [LibreChat MCP server configuration](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_servers)
- [Atlassian remote MCP server setup](https://support.atlassian.com/atlassian-ai-gateway/docs/get-started-with-the-atlassian-remote-mcp-server/)
- [Atlassian MCP v2 supported tools](https://support.atlassian.com/atlassian-ai-gateway/docs/supported-tools/)
- [Figma remote MCP server setup](https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/)
- [Figma MCP tools and prompts](https://developers.figma.com/docs/figma-mcp-server/tools-and-prompts/)

The experimental manifest sets `model_parameters.reasoning_effort` to `none` and temperature to zero. [OpenAI’s Luna model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna) requires `none` for Chat Completions function calling; the installed LibreChat schema and OpenAI configuration mapping accept this setting. The original manifest remains unchanged.
