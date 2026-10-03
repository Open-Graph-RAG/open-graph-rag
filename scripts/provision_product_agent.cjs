#!/usr/bin/env node
// Run inside the LibreChat container. Uses the application's model and ACL helpers.
const fs = require('node:fs');
const { createRequire } = require('node:module');
const appRequire = createRequire('/app/package.json');
const args = process.argv.slice(2);
function value(flag) {
  const index = args.indexOf(flag);
  if (index < 0 || !args[index + 1] || args[index + 1].startsWith('--')) throw new Error(`Missing ${flag}`);
  return args[index + 1];
}
async function main() {
  const email = value('--email');
  const manifestPath = value('--manifest');
  const apply = args.includes('--apply');
  const { agentCreateSchema } = appRequire('@librechat/api');
  const { ResourceType, PrincipalType, AccessRoleIds } = appRequire('librechat-data-provider');
  const { load } = appRequire('js-yaml');
  const manifest = agentCreateSchema.parse(JSON.parse(fs.readFileSync(manifestPath, 'utf8')));
  const config = load(fs.readFileSync(process.env.CONFIG_PATH || '/app/librechat.yaml', 'utf8'));
  const serverNames = ['lightrag', 'jira', 'figma'];
  for (const name of serverNames) {
    if (!config.mcpServers?.[name]) throw new Error(`Configure the ${name} MCP server first.`);
  }
  const configuredModels = (process.env.OPENAI_MODELS || '').split(',').map(s => s.trim()).filter(Boolean);
  if (configuredModels.length) manifest.model = configuredModels[0];
  const mongoose = appRequire('mongoose');
  appRequire('@librechat/data-schemas').createModels(mongoose);
  try {
    await appRequire('./config/connect')();
    const db = appRequire('./api/models');
    const user = await db.findUser({ email });
    if (!user) throw new Error('No existing LibreChat user matches the supplied email.');
    const agentId = 'agent_product_knowledge_planner';
    const existing = await db.getAgent({ id: agentId });
    if (existing && String(existing.author) !== String(user._id)) throw new Error('Agent ID belongs to another user; refusing to overwrite.');
    if (!apply) {
      console.log(JSON.stringify({ mode: 'check', owner: user.email, agentId, model: manifest.model, tools: manifest.tools, existing: Boolean(existing) }));
      return;
    }
    const agent = existing
      ? await db.updateAgent({ id: agentId }, { ...manifest, mcpServerNames: serverNames }, { updatingUserId: String(user._id) })
      : await db.createAgent({ ...manifest, id: agentId, author: user._id, mcpServerNames: serverNames });
    const { grantPermission } = appRequire('./api/server/services/PermissionService');
    for (const [resourceType, accessRoleId] of [
      [ResourceType.AGENT, AccessRoleIds.AGENT_OWNER],
      [ResourceType.REMOTE_AGENT, AccessRoleIds.REMOTE_AGENT_OWNER],
    ]) {
      await grantPermission({ principalType: PrincipalType.USER, principalId: String(user._id), resourceType,
        resourceId: agent._id, accessRoleId, grantedBy: String(user._id) });
    }
    console.log(JSON.stringify({ mode: 'applied', owner: user.email, agentId: agent.id, name: agent.name,
      model: agent.model, tools: agent.tools, mcpServerNames: agent.mcpServerNames }));
  } finally {
    await mongoose.disconnect();
  }
}
main().then(() => process.exit(0)).catch(error => { console.error(error.message); process.exit(1); });
