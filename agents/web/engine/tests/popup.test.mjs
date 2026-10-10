import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";

test("Chrome Bridge popup polls backend status so authorization requests appear while popup stays open", async () => {
  const source = await fs.readFile(new URL("../extension/popup.js", import.meta.url), "utf8");
  assert.match(source, /async function refreshSilently\(\)/);
  assert.match(source, /chrome\.runtime\.sendMessage\(\{ action: "status" \}\)/);
  assert.match(source, /setInterval\(\(\) => void refreshSilently\(\), 1500\)/);
});
