const {test} = require('node:test');
const assert = require('node:assert/strict');
const core = require('../src/stock_crawler/browser/core.js');
test('Retry-After dates and seconds are persisted with at least an hour cooldown', () => {
  assert.equal(core.retryUntil('7200', 0), 7200000);
  assert.equal(core.retryUntil('10', 0), 3600000);
  assert.equal(core.retryUntil('invalid', 0), 3600000);
  assert.equal(core.retryUntil('Thu, 01 Jan 1970 02:00:00 GMT', 0), 7200000);
});
test('HTML error body is not an XLSX success', () => {
  assert.equal(core.isXlsx(new TextEncoder().encode('<html>Blocked</html>'.repeat(100))), false);
  const bytes = new Uint8Array(100); bytes.set([0x50,0x4b,3,4]);
  assert.equal(core.isXlsx(bytes), true);
});
test('batch boundaries are deterministic and bounded', () => {
  assert.deepEqual(core.chunk([1,2,3,4,5], 2), [[1,2],[3,4],[5]]);
  assert.throws(() => core.chunk([1,2], 0));
});
