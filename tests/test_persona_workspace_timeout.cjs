"use strict";
/* Regression: persona standardization is an LLM call and must outlive the
   embedded bridge's 18s default. The workspace post() used to drop options. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(path.resolve(__dirname,
  "../pages/陪伴小屋/workspaces/persona/app.js"), "utf8");

test("persona workspace forwards an explicit long timeout for model calls", () => {
  assert.match(source, /const LONG_MODEL_TIMEOUT = \{timeoutMs: 180000\};/);
  assert.match(source, /function post\(path, body, options\) \{ return bridge\.apiPost\(path, body, options\)/);
  for (const route of ["persona/standardize", "persona/tryouts", "persona/style-rules"]) {
    const at = source.indexOf(`"${route}"`);
    assert.ok(at >= 0, `${route} should be called`);
    assert.match(source.slice(at, at + 900), /LONG_MODEL_TIMEOUT/,
      `${route} should pass the long timeout`);
  }
});
