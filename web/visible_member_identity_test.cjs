"use strict";

const assert = require("node:assert/strict");
const { createVisibleMemberIdentityBinding } = require("./app.js");

const expectedMember = (overrides = {}) => ({
  member_id: "stylist",
  persona_id: "stylist",
  name: "Stylist",
  status: "available",
  boundary: "只处理穿搭任务。",
  capabilities: ["场景理解"],
  ...overrides
});

async function bindResolvedTeam(team) {
  const payload = await Promise.resolve(team);
  return createVisibleMemberIdentityBinding(payload);
}

async function run() {
  const correct = await bindResolvedTeam({ members: [expectedMember()] });
  assert.equal(correct.visibleLabel, "私人 Stylist");
  assert.equal(correct.trusted, true);
  assert.equal(correct.member.member_id, "stylist");
  assert.equal(correct.member.persona_id, "stylist");
  assert.equal(correct.member.name, "Stylist", "the authority value remains intact internally");
  assert.equal(Object.isFrozen(correct), true);

  const renamedByServer = await bindResolvedTeam({
    members: [expectedMember({ name: "任意外部角色名" })]
  });
  assert.equal(renamedByServer.trusted, true, "identity is bound by the closed internal IDs");
  assert.equal(renamedByServer.visibleLabel, "私人 Stylist");
  assert.notEqual(renamedByServer.visibleLabel, renamedByServer.member.name, "raw server name must not become the visible role label");

  for (const [name, team] of [
    ["missing", { members: [] }],
    ["wrong member ID", { members: [expectedMember({ member_id: "other" })] }],
    ["wrong persona ID", { members: [expectedMember({ persona_id: "other" })] }],
    ["display-name spoof only", { members: [expectedMember({ member_id: "other", persona_id: "other", name: "私人 Stylist" })] }],
    ["ambiguous duplicate", { members: [expectedMember(), expectedMember({ name: "duplicate" })] }],
    ["malformed members", { members: {} }]
  ]) {
    const binding = await bindResolvedTeam(team);
    assert.equal(binding.visibleLabel, "私人 Stylist", `${name}: fallback must remain the approved visible label`);
    assert.equal(binding.trusted, false, `${name}: untrusted identity must fail closed`);
    assert.equal(binding.member, null, `${name}: arbitrary server member data must not be rendered`);
  }

  console.log("web visible private-Stylist API identity binding/fail-safe runtime contract: PASS");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
