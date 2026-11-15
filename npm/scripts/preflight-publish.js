'use strict';
// `npm publish` preflight guard (#340) — wired as `prepublishOnly` so it runs
// automatically. dist/ is gitignored, so a wheel left over from a previous
// `takkub release` sits there unnoticed; without this check `npm publish`
// happily tars up that stale wheel under the NEW version number (v1.0.84
// shipped v1.0.82's code, see issue #340). Dies loudly instead of warning —
// a silent old-code publish is worse than a blocked one.

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { findWheelForVersion } = require('./lib');

function checkDistWheelMatchesVersion(distDir, version) {
  if (!fs.existsSync(distDir)) {
    return {
      ok: false,
      reason: `dist/ not found — build first: python -m build --wheel`,
    };
  }
  const whls = fs.readdirSync(distDir).filter((f) => f.endsWith('.whl'));
  if (whls.length === 0) {
    return {
      ok: false,
      reason: `dist/ has no wheel — build first: python -m build --wheel`,
    };
  }
  if (whls.length > 1) {
    return {
      ok: false,
      reason:
        `dist/ has ${whls.length} wheels (expected exactly one): ${whls.join(', ')} — ` +
        `rebuild clean: rm -f dist/*.whl && python -m build --wheel`,
    };
  }
  const wheel = findWheelForVersion(distDir, version);
  if (!wheel) {
    return {
      ok: false,
      reason:
        `dist/${whls[0]} does not match package.json version ${version} — ` +
        `rebuild: rm -f dist/*.whl && python -m build --wheel`,
    };
  }
  return { ok: true, wheel: path.basename(wheel) };
}

function checkPackedWheel(files, wheel) {
  return files.some((file) => file.path === `dist/${wheel}`)
    ? { ok: true }
    : { ok: false, reason: `npm package omits dist/${wheel} — check dist/.npmignore` };
}

function main() {
  const pkg = require('../../package.json');
  const dist = path.join(__dirname, '..', '..', 'dist');
  const result = checkDistWheelMatchesVersion(dist, pkg.version);
  if (!result.ok) {
    console.error(`[agent-takkub] publish preflight FAILED: ${result.reason}`);
    process.exit(1);
  }
  const packed = spawnSync(process.execPath, [process.env.npm_execpath, 'pack', '--dry-run', '--json', '--ignore-scripts'], {
    cwd: path.join(__dirname, '..', '..'), encoding: 'utf8',
  });
  if (packed.status !== 0) {
    console.error(`[agent-takkub] publish preflight FAILED: npm pack: ${packed.stderr || packed.error}`);
    process.exit(1);
  }
  const payload = JSON.parse(packed.stdout)[0];
  const included = checkPackedWheel(payload.files, result.wheel);
  if (!included.ok) {
    console.error(`[agent-takkub] publish preflight FAILED: ${included.reason}`);
    process.exit(1);
  }
  console.log(`[agent-takkub] publish preflight OK — dist/${result.wheel} matches package.json ${pkg.version}`);
}

module.exports = { checkDistWheelMatchesVersion, checkPackedWheel };

if (require.main === module) {
  main();
}
