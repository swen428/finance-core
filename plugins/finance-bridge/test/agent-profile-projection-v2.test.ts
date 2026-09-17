import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { projectFinanceAgentConfigV2 } from "../src/agent-profile-projection-v2.js";

const GOLDEN = new URL(
  "../../../../tests/fixtures/finance_ai/agent_projection_v2_golden.json",
  import.meta.url,
);

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (typeof value === "object" && value !== null) {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map(
      (key) => `${JSON.stringify(key)}:${canonical(record[key])}`,
    ).join(",")}}`;
  }
  return JSON.stringify(value);
}

test("v2 projector matches the shared golden projection and canonical hash", async () => {
  const fixture = JSON.parse(await readFile(GOLDEN, "utf8")) as {
    source: unknown;
    projection: unknown;
    projection_sha256: string;
  };
  const projection = projectFinanceAgentConfigV2(fixture.source);
  assert.deepEqual(projection, fixture.projection);
  assert.equal((fixture.source as { openclawVersion: string }).openclawVersion, "2026.7.1");
  assert.equal(
    (projection as { openclaw_version: string }).openclaw_version,
    (fixture.source as { openclawVersion: string }).openclawVersion,
  );
  assert.notEqual((projection as { openclaw_version: string }).openclaw_version, "2026.7.1-2");
  assert.equal(
    createHash("sha256").update(canonical(projection)).digest("hex"),
    fixture.projection_sha256,
  );
});

test("v2 projector rejects fallbacks retry drift secrets and raw content", async () => {
  const fixture = JSON.parse(await readFile(GOLDEN, "utf8")) as {
    source: Record<string, unknown>;
  };
  for (const mutation of [
    (value: any) => { value.policies.effectiveMaxRetries = 1; },
    (value: any) => { value.agents.list[0].model.fallbacks = ["other/model"]; },
    (value: any) => { value.apiKey = "secret"; },
    (value: any) => { value.telegramMessage = "raw receipt"; },
  ]) {
    const source = structuredClone(fixture.source);
    mutation(source);
    assert.throws(() => projectFinanceAgentConfigV2(source));
  }
});

test("v2 projector preserves Unicode aliases but rejects control characters", async () => {
  const fixture = JSON.parse(await readFile(GOLDEN, "utf8")) as {
    source: any;
  };
  fixture.source.agents.list[0].displayAlias = "本地模型";
  assert.equal(projectFinanceAgentConfigV2(fixture.source).display_alias, "本地模型");
  fixture.source.agents.list[0].displayAlias = "bad\nname";
  assert.throws(() => projectFinanceAgentConfigV2(fixture.source));
  fixture.source.agents.list[0].displayAlias = "bad\ud800name";
  assert.throws(() => projectFinanceAgentConfigV2(fixture.source));
  fixture.source.agents.list[0].displayAlias = "bad\u202ename";
  assert.throws(() => projectFinanceAgentConfigV2(fixture.source));
});

test("v2 projector preserves provider model ids containing slashes", async () => {
  const fixture = JSON.parse(await readFile(GOLDEN, "utf8")) as { source: any };
  fixture.source.agents.list[0].model.primary = "openrouter/anthropic/example-model";
  const projection = projectFinanceAgentConfigV2(fixture.source);
  assert.equal(projection.canonical_provider, "openrouter");
  assert.equal(projection.canonical_model, "anthropic/example-model");
});

test("v2 projector rejects missing null duplicate and nested unknown fields", async () => {
  const fixture = JSON.parse(await readFile(GOLDEN, "utf8")) as { source: any };
  for (const mutation of [
    (value: any) => { delete value.policies.promptSha256; },
    (value: any) => { value.policies.promptSha256 = null; },
    (value: any) => { value.agents.list[0].unexpected = "value"; },
    (value: any) => { value.agents.list.push(structuredClone(value.agents.list[0])); },
    (value: any) => { value.agents.list.push({
      id: "other", apiKey: "secret", history: ["raw"], tools: ["all"], attachment: "bytes",
    }); },
    (value: any) => { value.agents.list[0].id = "other"; },
    (value: any) => { value.agents.list = []; },
  ]) {
    const source = structuredClone(fixture.source);
    mutation(source);
    assert.throws(() => projectFinanceAgentConfigV2(source));
  }
});

test("v2 projector is order-independent and enforces byte and model bounds", async () => {
  const fixture = JSON.parse(await readFile(GOLDEN, "utf8")) as { source: any };
  const reversed = Object.fromEntries(Object.entries(fixture.source).reverse());
  assert.deepEqual(
    projectFinanceAgentConfigV2(reversed),
    projectFinanceAgentConfigV2(fixture.source),
  );
  for (const mutation of [
    (value: any) => { value.agents.list[0].displayAlias = "名".repeat(22); },
    (value: any) => { value.agents.list[0].model.primary = "missing-slash"; },
    (value: any) => { value.agents.list[0].model.primary = "/missing-provider"; },
    (value: any) => { value.agents.list[0].model.primary = "provider/"; },
  ]) {
    const source = structuredClone(fixture.source);
    mutation(source);
    assert.throws(() => projectFinanceAgentConfigV2(source));
  }
});
