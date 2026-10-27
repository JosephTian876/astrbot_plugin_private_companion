/* Performance mode keeps the heavy cottage assets inert until checked. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const PAGE = path.join(__dirname, "..", "pages", "陪伴小屋");
const INDEX = fs.readFileSync(path.join(PAGE, "index.html"), "utf8");
const GATE = fs.readFileSync(path.join(PAGE, "performance-gate.js"), "utf8");
const SCHEMA = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "_conf_schema.json"), "utf8"));

test("官方配置页第一组就是性能模式开关", () => {
  assert.equal(Object.keys(SCHEMA)[0], "performance");
  assert.equal(SCHEMA.performance.items.enabled.type, "bool");
  assert.equal(SCHEMA.performance.items.enabled.default, false);
});

test("完整小屋资源位于惰性 template，首屏不直接下载", () => {
  const start = INDEX.indexOf('<template id="companion-full-assets">');
  const end = INDEX.indexOf("</template>", start);
  assert.ok(start > 0 && end > start, "缺少完整资源 template");
  const inert = INDEX.slice(start, end);
  for (const asset of ["tokens.css", "assets/voxel-room.js", "app.js", "models-page.js"]) {
    assert.match(inert, new RegExp(asset.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }
  const before = INDEX.slice(0, start);
  assert.doesNotMatch(before, /<script[^>]+src="app\.js/);
  assert.doesNotMatch(before, /<link[^>]+href="tokens\.css/);
  assert.match(INDEX, /<script defer src="performance-gate\.js\?v=/);
  assert.match(INDEX, /data-companion-booting/);
});

test("性能门先询问官方设置，关闭时按原顺序激活完整资源", () => {
  assert.match(GATE, /host\/performance/);
  assert.match(GATE, /apiGet\(ENDPOINT/);
  assert.match(GATE, /showLite/);
  assert.match(GATE, /activateFull/);
  assert.match(GATE, /node\.async = false/);
  assert.match(GATE, /removeAttribute\("data-companion-booting"\)/);
  assert.match(GATE, /replaceChildren\(page\)/);
});
