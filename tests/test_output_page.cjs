"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../pages/陪伴小屋/output-page.js"), "utf8");
const selectors = [
  '[data-output-content-stat="works"]',
  '[data-output-content-stat="candidates"]',
  '[data-output-content-stat="updated"]',
  "[data-output-content-preview]",
  "[data-output-content-preview-count]",
  "[data-output-content-preview-updated]",
  "[data-output-native-notice]",
];

function fixture() {
  const nodes = new Map(selectors.map(selector => [selector, [{textContent: "初始值", hidden: false,
    classList: {toggle() {}}}]]));
  const single = new Map([
    ["#outputAlbumWorkspace", {innerHTML: "", hidden: false}],
    ["#outputAlbumLightbox", {innerHTML: "", hidden: true}],
    ["[data-output-preview-image]", {hidden: true, src: "", alt: "", removeAttribute() {}}],
    [".output-image-frame", {dataset: {}}],
    [".output-image-status span", {textContent: ""}],
  ]);
  const requests = [];
  let observabilityFailure = false;
  let reconnectFailureCode = "";
  const listeners = new Map();
  const role = {model_api: "model", image_material_api: "image-materials"};
  const window = {
    CompanionProduct: {
      scopeKey: () => "role-a",
      role: () => role,
      relative: value => value,
      async request(method, endpoint) {
        requests.push({method, endpoint});
        if (endpoint === "model/image/observability") {
          if (observabilityFailure) throw Object.assign(new Error("network failed"), {code: "network_error"});
          return {images: {total: 1, items: []}, content: {available: true, works_count: 2,
            candidate_count: 1, updated_at: "2026-10-12T04:05:00Z"}};
        }
        if (endpoint === "model/image/output") return {settings: {backend: "auto", enabled: false,
          endpoints: [], comfyui: {}, sd_webui: {}}, revision: "r1", host_registered: false};
        if (endpoint === "model/image/output/reconnect") {
          if (reconnectFailureCode) throw Object.assign(new Error(reconnectFailureCode), {code: reconnectFailureCode});
          return {reconnected: true, host_registered: true};
        }
        if (endpoint === "model/image/review/providers") return {providers: []};
        if (endpoint === "image-materials/album") return {items: [{image_id: "img-1",
          thumbnail: "data:image/png;base64,AAA", width: 512, height: 512,
          created_at: "2026-10-12T04:05:00Z", action: "image.generate"}], next_cursor: null};
        if (endpoint === "image-materials/album/export") return {filename: "album.zip",
          data_url: "data:application/zip;base64,AAA", count: 1};
        if (endpoint === "image-materials/album/hide") return {hidden: 1, hidden_total: 1};
        if (endpoint === "image-materials/album/open-folder") return {path: "C:\\data", opened: true};
        throw new Error(`unexpected request: ${method} ${endpoint}`);
      },
    },
    dispatchEvent() {},
    document: {createElement: () => ({click() {}, remove() {}}), body: {appendChild() {}}},
  };
  const root = {
    isConnected: true,
    addEventListener(type, listener) {listeners.set(type, listener);},
    removeEventListener() {},
    contains() {return true;},
    querySelector(selector) {return single.get(selector) || null;},
    querySelectorAll(selector) {return nodes.get(selector) || [];},
  };
  const context = {window, console, AbortController, Event, setTimeout, clearTimeout, Promise, Math, Date, JSON,
    Object, String, Array, Map, Set, Error};
  vm.runInNewContext(source, context, {filename: "output-page.js"});
  return {page: window.CompanionOutputPage, nodes, single, requests, root,
    failObservability(value) {observabilityFailure = value;},
    failReconnect(code) {reconnectFailureCode = code;},
    clickReconnect() {
      const target = {disabled: false, dataset: {outputNativeAction: "reconnect"}};
      listeners.get("click")?.({target: {closest: () => target}});
    },
    clickAlbum(index) {
      const target = {disabled: false, dataset: {outputNativeAction: "album-open",
        outputImageIndex: String(index)}};
      listeners.get("click")?.({target: {closest: () => target}});
    },
    clickAction(action, dataset = {}) {
      const target = {disabled: false, dataset: {outputNativeAction: action, ...dataset}};
      listeners.get("click")?.({target: {closest: () => target}});
    },
    toggleAlbum(imageId, checked = true) {
      const target = {matches: selector => selector === "[data-output-album-select]",
        dataset: {outputAlbumSelect: imageId}, checked};
      listeners.get("change")?.({target});
    }};
}

test("Content summary renders real counts and clears stale values after a failed reread", async () => {
  const state = fixture();
  state.page.mount(state.root);
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(state.nodes.get('[data-output-content-stat="works"]')[0].textContent, "2");
  assert.equal(state.nodes.get('[data-output-content-stat="candidates"]')[0].textContent, "1");
  assert.match(state.nodes.get("[data-output-content-preview]")[0].textContent, /2 部已关联作品 · 1 份续写候选稿/);
  assert.ok(state.requests.some(request => request.endpoint === "model/image/observability"));

  state.failObservability(true);
  await state.page.refreshObservability();
  assert.equal(state.nodes.get('[data-output-content-stat="works"]')[0].textContent, "—");
  assert.equal(state.nodes.get('[data-output-content-stat="candidates"]')[0].textContent, "—");
  assert.equal(state.nodes.get("[data-output-content-preview]")[0].textContent, "Content 作品数据暂不可用");

  state.page.unmount();
});

test("Reconnect failures show a specific V2 session recovery hint", async () => {
  const state = fixture();
  state.page.mount(state.root);
  await new Promise(resolve => setImmediate(resolve));
  state.failReconnect("staging_session_still_stopping");
  state.clickReconnect();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(state.nodes.get("[data-output-native-notice]")[0].textContent,
    "V2 当前会话仍在停止，请稍等片刻后再重连。");
  state.page.unmount();
});

test("output page renders the generated image gallery and opens a preview", async () => {
  const state = fixture();
  state.page.mount(state.root);
  await new Promise(resolve => setImmediate(resolve));

  const gallery = state.single.get("#outputAlbumWorkspace");
  assert.match(gallery.innerHTML, /output-album-card/);
  assert.match(gallery.innerHTML, /data-output-native-action="album-open"/);
  assert.match(gallery.innerHTML, /data:image\/png;base64,AAA/);

  state.clickAlbum(0);
  const lightbox = state.single.get("#outputAlbumLightbox");
  assert.equal(lightbox.hidden, false);
  assert.match(lightbox.innerHTML, /output-album-lightbox-card/);
  assert.match(lightbox.innerHTML, /1 \/ 1/);
  state.page.unmount();
});

test("gallery supports selection, export, delete and open-folder actions", async () => {
  const state = fixture();
  state.page.mount(state.root);
  await new Promise(resolve => setImmediate(resolve));

  const gallery = state.single.get("#outputAlbumWorkspace");
  assert.match(gallery.innerHTML, /导出选中/);
  assert.match(gallery.innerHTML, /删除选中/);
  assert.match(gallery.innerHTML, /打开数据目录/);

  state.toggleAlbum("img-1", true);
  assert.match(gallery.innerHTML, /已选 <b>1<\/b> 张/);

  state.clickAction("album-export");
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(state.requests.some(request => request.method === "POST" &&
    request.endpoint === "image-materials/album/export"));

  state.clickAction("album-hide");
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(state.requests.some(request => request.method === "POST" &&
    request.endpoint === "image-materials/album/hide"));
  assert.doesNotMatch(gallery.innerHTML, /output-album-card/);

  state.clickAction("album-open-folder");
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(state.requests.some(request => request.method === "POST" &&
    request.endpoint === "image-materials/album/open-folder"));
  state.page.unmount();
});
