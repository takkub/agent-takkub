'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { checkDistWheelMatchesVersion } = require('./preflight-publish');

function tmpDist(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-takkub-preflight-'));
  for (const f of files) fs.writeFileSync(path.join(dir, f), '');
  return dir;
}

test('ok when dist/ has exactly one wheel matching package.json version', () => {
  const dir = tmpDist(['agent_takkub-1.0.84-py3-none-any.whl']);
  const res = checkDistWheelMatchesVersion(dir, '1.0.84');
  assert.equal(res.ok, true);
  assert.equal(res.wheel, 'agent_takkub-1.0.84-py3-none-any.whl');
});

test('fails when dist/ wheel is a stale older version', () => {
  const dir = tmpDist(['agent_takkub-1.0.82-py3-none-any.whl']);
  const res = checkDistWheelMatchesVersion(dir, '1.0.84');
  assert.equal(res.ok, false);
  assert.match(res.reason, /1\.0\.84/);
});

test('fails when dist/ does not exist', () => {
  const res = checkDistWheelMatchesVersion(path.join(os.tmpdir(), 'no-such-dist-xyz'), '1.0.84');
  assert.equal(res.ok, false);
  assert.match(res.reason, /dist\//);
});

test('fails when dist/ has no wheel at all', () => {
  const dir = tmpDist([]);
  const res = checkDistWheelMatchesVersion(dir, '1.0.84');
  assert.equal(res.ok, false);
});

test('fails when dist/ has more than one wheel', () => {
  const dir = tmpDist(['agent_takkub-1.0.84-py3-none-any.whl', 'agent_takkub-1.0.82-py3-none-any.whl']);
  const res = checkDistWheelMatchesVersion(dir, '1.0.84');
  assert.equal(res.ok, false);
  assert.match(res.reason, /expected exactly one/);
});
