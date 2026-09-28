'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { createMac } = require('./shortcut');

test('macOS launcher update keeps the pinned Applications bundle in place', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'takkub-mac-shortcut-'));
  const originalHome = os.homedir;
  const originalDesktop = process.env.AGENT_TAKKUB_DESKTOP;
  os.homedir = () => root;
  process.env.AGENT_TAKKUB_DESKTOP = path.join(root, 'Desktop');
  try {
    createMac();
    const app = path.join(root, 'Applications', 'Takkub Cockpit.app');
    const marker = path.join(app, 'dock-pin-marker');
    fs.writeFileSync(marker, 'pinned');

    createMac();

    assert.equal(fs.readFileSync(marker, 'utf8'), 'pinned');
    assert.match(fs.readFileSync(path.join(app, 'Contents', 'MacOS', 'launch'), 'utf8'), /agent_takkub/);
  } finally {
    os.homedir = originalHome;
    if (originalDesktop === undefined) delete process.env.AGENT_TAKKUB_DESKTOP;
    else process.env.AGENT_TAKKUB_DESKTOP = originalDesktop;
    fs.rmSync(root, { recursive: true, force: true });
  }
});
