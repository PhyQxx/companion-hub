import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
import ts from 'typescript';
const source = readFileSync(new URL('../src/admin/views/MemoryView.vue', import.meta.url), 'utf8');
function setup({ cancel = false, fail = false } = {}) {
  const calls = [], messages = [];
  let loads = 0;
  const context = {
    deleting: { value: false }, detail: { value: { id: 2 } }, editing: { value: null },
    ElMessageBox: { prompt: async () => { if (cancel) throw Error(); return { value: '测试原因' }; } },
    api: { request: async (url) => { calls.push(url); if (fail && url.includes('/3?')) throw Error(); return { deleted_ids: [1, 2] }; } },
    emit: (...args) => messages.push(args), load: async () => { loads++; },
  };
  runInNewContext(ts.transpile(source.slice(source.indexOf('async function removeMemories('), source.indexOf('async function runQuery()'))), context);
  return { context, calls, messages, loads: () => loads };
}
test('deletes shared lineage once and clears deleted detail', async () => {
  const s = setup(); await s.context.removeMemories([1, 2]);
  assert.equal(s.calls.length, 1); assert.equal(s.context.detail.value, null);
  assert.equal(s.loads(), 1); assert.equal(s.context.deleting.value, false);
});
test('cancel and empty selection issue no mutations', async () => {
  const s = setup({ cancel: true });
  await s.context.removeMemories([1]); await s.context.removeMemories([]);
  assert.equal(s.calls.length, 0); assert.equal(s.loads(), 0); assert.equal(s.context.deleting.value, false);
});
test('partial failures report unresolved IDs and refresh', async () => {
  const s = setup({ fail: true }); await s.context.removeMemories([1, 3]);
  assert.equal(s.calls.length, 2); assert.match(s.messages[0][1], /#3/);
  assert.equal(s.messages[0][2], true); assert.equal(s.loads(), 1);
});
test('busy guard prevents duplicate submission', async () => {
  const s = setup(); s.context.deleting.value = true;
  await s.context.removeMemories([1]); assert.equal(s.calls.length, 0);
});
