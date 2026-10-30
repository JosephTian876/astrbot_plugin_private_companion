const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'pages/陪伴小屋/app.js'), 'utf8');
const index = fs.readFileSync(path.join(root, 'pages/陪伴小屋/index.html'), 'utf8');

test('the early app shell does not dereference later room-editor globals while mounting voxels', () => {
  assert.doesNotMatch(app, /onEditorChange\s*:\s*updateRoomEditor\b/);
  assert.match(app, /onEditorChange:editor=>\{/);
  assert.match(app, /typeof updateRoomEditor==='function'/);
  assert.match(app, /typeof finishRoomEditor==='function'/);
});

test('app.js intentionally runs before room-editor.js so those callbacks stay lazy', () => {
  const appAt = index.indexOf('app.js?v=');
  const editorAt = index.indexOf('room-editor.js?v=');
  assert.ok(appAt >= 0);
  assert.ok(editorAt > appAt);
});

test('the picture-in-picture room label stays readable Chinese', () => {
  const shell = fs.readFileSync(path.join(root, 'pages/陪伴小屋/shell.css'), 'utf8');
  const bundle = fs.readFileSync(path.join(root, 'pages/陪伴小屋/bundle.css'), 'utf8');
  assert.match(shell, /content:\s*"回到小屋"/);
  assert.doesNotMatch(shell, /鍥炲埌灏忓眿/);
  assert.match(bundle, /content:\s*"回到小屋"/);
});
