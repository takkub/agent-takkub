'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASELINE_PROVIDERS = {
  claude: '@anthropic-ai/claude-code',
  codex: '@openai/codex',
};

function providerEnv(env) {
  const prefix = env.npm_config_prefix;
  if (!prefix) return env;
  const bin = process.platform === 'win32' ? prefix : path.join(prefix, 'bin');
  const pathKey = Object.keys(env).find((key) => key.toUpperCase() === 'PATH') || 'PATH';
  return { ...env, [pathKey]: bin + path.delimiter + (env[pathKey] || '') };
}

function probeCli(name, env = process.env) {
  if (!Object.hasOwn(BASELINE_PROVIDERS, name)) throw new Error(`Unknown baseline provider: ${name}`);
  // Node rejects direct .cmd execution on Windows (EINVAL). These commands
  // are fixed provider names, with no user input interpolated into the shell.
  const command = process.platform === 'win32' ? 'cmd.exe' : name;
  const args = process.platform === 'win32' ? ['/d', '/s', '/c', `${name} --version`] : ['--version'];
  const result = spawnSync(command, args, {
    env: providerEnv(env), encoding: 'utf8', timeout: 30_000, windowsHide: true,
  });
  if (result.status !== 0) return null;
  return ((result.stdout || '') + (result.stderr || '')).trim() || null;
}

function npmInvocation(env) {
  // Use the npm CLI that started this lifecycle, through Node rather than a
  // .cmd shim. This also preserves custom prefixes and paths with spaces.
  if (env.npm_execpath && fs.existsSync(env.npm_execpath) && /\.[cm]?js$/i.test(env.npm_execpath)) {
    return [process.execPath, [env.npm_execpath]];
  }
  if (process.platform !== 'win32') return ['npm', []];
  // The cockpit's repair/update flow can run postinstall directly, outside
  // npm. Find its JS entry point without executing a batch shim.
  const pathValue = Object.entries(env).find(([key]) => key.toUpperCase() === 'PATH')?.[1] || '';
  for (const dir of [path.dirname(process.execPath), ...pathValue.split(path.delimiter)]) {
    if (!dir) continue;
    const cli = path.join(dir, 'node_modules', 'npm', 'bin', 'npm-cli.js');
    if (fs.existsSync(cli)) return [process.execPath, [cli]];
  }
  throw new Error('npm CLI not found');
}

function ensureBaselineProviders(detected, { env = process.env, log = console.log, warn = console.warn } = {}) {
  const available = {};
  for (const [name, packageName] of Object.entries(BASELINE_PROVIDERS)) {
    if (detected[`${name}Cli`]?.present) {
      available[name] = true;
      continue;
    }
    log(`[agent-takkub] ${name} CLI not found — installing ${packageName}…`);
    let failure;
    try {
      const [command, npmArgs] = npmInvocation(env);
      const args = [...npmArgs, 'install', '-g', packageName];
      if (env.npm_config_prefix) args.push('--prefix', env.npm_config_prefix);
      const result = spawnSync(command, args, {
        env: providerEnv(env), stdio: 'inherit', timeout: 300_000, windowsHide: true,
      });
      failure = result.error?.message || (result.status !== 0 ? `npm exited with status ${result.status}` : null);
      available[name] = !failure && Boolean(probeCli(name, env));
      if (!failure && !available[name]) failure = 'installed CLI could not be started';
    } catch (error) {
      failure = error.message;
      available[name] = false;
    }
    if (available[name]) log(`[agent-takkub] ✓ ${name} CLI ready.`);
    else warn(`[agent-takkub] ${name} installation failed: ${failure}. Retry: npm install -g ${packageName}`);
  }
  return available;
}

module.exports = { probeCli, ensureBaselineProviders };
