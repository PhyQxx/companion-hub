import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/ChatComposer.vue', import.meta.url), 'utf8');
const handler = source.slice(source.indexOf('let composing = false;'), source.indexOf('</script>'));
function setup() {
  const sent = [];
  const context = { emit: (event) => sent.push(event) };
  runInNewContext(ts.transpile(handler), context);
  return {
    sent,
    compose(value) { runInNewContext(`composing = ${value}`, context); },
    key(overrides = {}) {
      let prevented = false;
      context.onDraftKeydown({ key: 'Enter', preventDefault() { prevented = true; }, ...overrides });
      return prevented;
    },
  };
}
test('IME confirmation leaves default action intact and sends only on the next Enter', () => {
  const state = setup();
  state.compose(true);
  assert.equal(state.key(), false);
  assert.deepEqual(state.sent, []);
  state.compose(false);
  assert.equal(state.key(), true);
  assert.deepEqual(state.sent, ['send']);
});
test('native composing flag and legacy 229 do not submit an old draft', () => {
  const state = setup();
  assert.equal(state.key({ isComposing: true }), false);
  assert.equal(state.key({ keyCode: 229 }), false);
  assert.deepEqual(state.sent, []);
});
test('modified Enter and other keys retain normal editing behavior', () => {
  const state = setup();
  for (const modifier of ['shiftKey', 'ctrlKey', 'altKey', 'metaKey']) {
    assert.equal(state.key({ [modifier]: true }), false);
  }
  assert.equal(state.key({ key: 'a' }), false);
  assert.deepEqual(state.sent, []);
});
