'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { probeCli, ensureBaselineProviders } = require('./providers');

function sandbox(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'takkub providers '));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const prefix = path.join(dir, 'global prefix');
  const npmCli = path.join(dir, 'npm-cli.js');
  const requests = path.join(dir, 'requests.jsonl');
  fs.mkdirSync(prefix);
  fs.writeFileSync(npmCli, `
    const fs = require('fs');
    const path = require('path');
    const args = process.argv.slice(2);
    fs.appendFileSync(process.env.TAKKUB_TEST_REQUESTS, JSON.stringify(args) + '\\n');
    const name = args[2] === '@anthropic-ai/claude-code' ? 'claude' : 'codex';
    if (process.env.TAKKUB_TEST_FAIL === name) process.exit(7);
    if (process.env.TAKKUB_TEST_NO_BINARY === name) process.exit(0);
    const prefix = args[args.indexOf('--prefix') + 1];
    const bin = process.platform === 'win32' ? prefix : path.join(prefix, 'bin');
    fs.mkdirSync(bin, {recursive: true});
    const script = path.join(bin, name + '-fixture.js');
    fs.writeFileSync(script, 'console.log(' + JSON.stringify(name + ' fixture 1.0') + ');');
    if (process.platform === 'win32') {
      fs.writeFileSync(path.join(bin, name + '.cmd'), '@echo off\\r\\n"' + process.execPath + '" "' + script + '" %*\\r\\n');
    } else {
      fs.writeFileSync(path.join(bin, name), '#!' + process.execPath + '\\n' + fs.readFileSync(script), {mode: 0o755});
    }
  `);
  const messages = [];
  const options = {
    env: { ...process.env, npm_execpath: npmCli, npm_config_prefix: prefix, TAKKUB_TEST_REQUESTS: requests },
    log: (text) => messages.push(text), warn: (text) => messages.push(text),
  };
  return {
    options, messages, prefix,
    requests: () => fs.existsSync(requests)
      ? fs.readFileSync(requests, 'utf8').trim().split('\n').map(JSON.parse) : [],
  };
}

test('first install provisions both CLIs through npm in the requested prefix', (t) => {
  const s = sandbox(t);
  assert.deepEqual(ensureBaselineProviders({}, s.options), { claude: true, codex: true });
  assert.deepEqual(s.requests(), [
    ['install', '-g', '@anthropic-ai/claude-code', '--prefix', s.prefix],
    ['install', '-g', '@openai/codex', '--prefix', s.prefix],
  ]);
  assert.equal(probeCli('claude', s.options.env), 'claude fixture 1.0');
  assert.equal(probeCli('codex', s.options.env), 'codex fixture 1.0');
});

test('an existing provider is reused while the missing provider is installed', (t) => {
  const s = sandbox(t);
  assert.deepEqual(ensureBaselineProviders({ claudeCli: { present: true } }, s.options), {
    claude: true, codex: true,
  });
  assert.equal(s.requests().length, 1);
  assert.equal(s.requests()[0][2], '@openai/codex');
});

test('reinstall with both providers present makes no npm install calls', (t) => {
  const s = sandbox(t);
  assert.deepEqual(ensureBaselineProviders({
    claudeCli: { present: true }, codexCli: { present: true },
  }, s.options), { claude: true, codex: true });
  assert.deepEqual(s.requests(), []);
});

test('one install failure still attempts the other provider and reports recovery', (t) => {
  const s = sandbox(t);
  s.options.env.TAKKUB_TEST_FAIL = 'claude';
  assert.deepEqual(ensureBaselineProviders({}, s.options), { claude: false, codex: true });
  assert.equal(s.requests().length, 2);
  assert.ok(s.messages.some((text) => text.includes('status 7') && text.includes('@anthropic-ai/claude-code')));
});

test('npm success without a runnable CLI is not reported as an installed provider', (t) => {
  const s = sandbox(t);
  s.options.env.TAKKUB_TEST_NO_BINARY = 'codex';
  // Mask globally installed CLIs so a developer's Codex cannot satisfy the probe.
  const pathKey = Object.keys(s.options.env).find((key) => key.toUpperCase() === 'PATH');
  s.options.env[pathKey] = process.platform === 'win32'
    ? path.join(process.env.SystemRoot, 'System32') : '/usr/bin:/bin';
  assert.deepEqual(ensureBaselineProviders({}, s.options), { claude: true, codex: false });
  assert.ok(s.messages.some((text) => text.includes('could not be started') && text.includes('@openai/codex')));
});
