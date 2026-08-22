'use strict';
// node --test npm/scripts/lib.test.js — no framework, Node's built-in runner
// (engines floor is node >=18, which ships node:test).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { findWheelForVersion } = require('./lib');

function tmpDist(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-takkub-wheel-'));
  for (const f of files) fs.writeFileSync(path.join(dir, f), '');
  return dir;
}

test('picks the wheel matching the exact version', () => {
  const dir = tmpDist(['agent_takkub-1.0.82-py3-none-any.whl', 'agent_takkub-1.0.84-py3-none-any.whl']);
  const found = findWheelForVersion(dir, '1.0.84');
  assert.equal(path.basename(found), 'agent_takkub-1.0.84-py3-none-any.whl');
});

test('does not let string sort pick 1.0.9 over 1.0.10', () => {
  const dir = tmpDist(['agent_takkub-1.0.10-py3-none-any.whl', 'agent_takkub-1.0.9-py3-none-any.whl']);
  const found = findWheelForVersion(dir, '1.0.9');
  assert.equal(path.basename(found), 'agent_takkub-1.0.9-py3-none-any.whl');
});

test('returns null when no wheel matches the version', () => {
  const dir = tmpDist(['agent_takkub-1.0.82-py3-none-any.whl']);
  assert.equal(findWheelForVersion(dir, '1.0.84'), null);
});

test('returns null when dist dir does not exist', () => {
  assert.equal(findWheelForVersion(path.join(os.tmpdir(), 'no-such-dir-xyz'), '1.0.84'), null);
});
