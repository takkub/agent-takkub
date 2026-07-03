'use strict';
// Best-effort Desktop launcher creation (win32 + darwin). NEVER throws — a
// failure just means no icon; the `agent-takkub` command still works. The
// Desktop location is overridable via AGENT_TAKKUB_DESKTOP so an isolated test
// (or a user who wants it elsewhere) doesn't touch the real Desktop.

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { venvPythonw, venvPython } = require('./lib');

function desktopDir() {
  return process.env.AGENT_TAKKUB_DESKTOP || path.join(os.homedir(), 'Desktop');
}

function iconAsset(name) {
  return path.join(__dirname, '..', '..', 'assets', name);
}

// Single-quote a string for a PowerShell literal (double any embedded quote).
function psStr(s) {
  return "'" + String(s).replace(/'/g, "''") + "'";
}

function createWindows() {
  const lnk = path.join(desktopDir(), 'Takkub Cockpit.lnk');
  const target = venvPythonw();
  const ico = iconAsset('icon.ico');
  // WorkingDirectory = the venv root (…/venv), two levels up from Scripts\pythonw.exe.
  const workdir = path.dirname(path.dirname(target));
  const lines = [
    '$ws = New-Object -ComObject WScript.Shell',
    `$sc = $ws.CreateShortcut(${psStr(lnk)})`,
    `$sc.TargetPath = ${psStr(target)}`,
    "$sc.Arguments = '-m agent_takkub'",
    `$sc.WorkingDirectory = ${psStr(workdir)}`,
  ];
  if (fs.existsSync(ico)) lines.push(`$sc.IconLocation = ${psStr(ico)}`);
  lines.push("$sc.Description = 'agent-takkub dev team cockpit'");
  lines.push('$sc.Save()');

  fs.mkdirSync(desktopDir(), { recursive: true });
  // Run from a temp .ps1 FILE, not an inline -Command: passing a long quoted
  // command through Node→Windows arg-escaping mangles the quotes and produces
  // an empty shortcut. A -File avoids all of that.
  const ps1 = path.join(os.tmpdir(), `att-shortcut-${process.pid}.ps1`);
  fs.writeFileSync(ps1, lines.join('\r\n') + '\r\n', 'utf8');
  try {
    const r = spawnSync(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ps1],
      { stdio: 'ignore' }
    );
    return r.status === 0 && fs.existsSync(lnk) ? lnk : null;
  } finally {
    try {
      fs.unlinkSync(ps1);
    } catch (_e) {
      /* ignore */
    }
  }
}

function createMac() {
  const appDir = path.join(desktopDir(), 'Takkub Cockpit.app');
  const macos = path.join(appDir, 'Contents', 'MacOS');
  const res = path.join(appDir, 'Contents', 'Resources');
  fs.mkdirSync(macos, { recursive: true });
  fs.mkdirSync(res, { recursive: true });

  const launch = `#!/bin/sh\nexec ${JSON.stringify(venvPython())} -m agent_takkub "$@"\n`;
  fs.writeFileSync(path.join(macos, 'launch'), launch);
  fs.chmodSync(path.join(macos, 'launch'), 0o755);

  const plist =
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" ' +
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n' +
    '<plist version="1.0"><dict>\n' +
    '  <key>CFBundleName</key><string>Takkub Cockpit</string>\n' +
    '  <key>CFBundleExecutable</key><string>launch</string>\n' +
    '  <key>CFBundleIdentifier</key><string>com.takkub.cockpit</string>\n' +
    '  <key>CFBundleIconFile</key><string>icon</string>\n' +
    '  <key>CFBundlePackageType</key><string>APPL</string>\n' +
    '</dict></plist>\n';
  fs.writeFileSync(path.join(appDir, 'Contents', 'Info.plist'), plist);

  // Convert the bundled png → icns with sips (macOS built-in). Best-effort.
  const png = iconAsset('icon.png');
  if (fs.existsSync(png)) {
    spawnSync('sips', ['-s', 'format', 'icns', png, '--out', path.join(res, 'icon.icns')], {
      stdio: 'ignore',
    });
  }
  return fs.existsSync(path.join(macos, 'launch')) ? appDir : null;
}

// Returns the created launcher path, or null if unsupported/failed.
function create() {
  try {
    if (process.platform === 'win32') return createWindows();
    if (process.platform === 'darwin') return createMac();
  } catch (_e) {
    /* best-effort — never block the install over a shortcut */
  }
  return null;
}

module.exports = { create };
