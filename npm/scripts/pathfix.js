'use strict';
// Ensure the npm global bin dir stays on the user's persistent PATH.
//
// Field incident 2026-07-04: a Node update dropped %APPDATA%\npm from the user
// PATH — `claude`, `takkub`, and `agent-takkub` all became "command not found"
// and the user had to hand-edit the registry. This module makes the install
// self-healing:
//   • win32  — appends the dir to HKCU\Environment Path via .NET RegistryKey,
//              PRESERVING the value kind (REG_SZ vs REG_EXPAND_SZ, read with
//              DoNotExpandEnvironmentNames so %VAR% entries survive verbatim),
//              then broadcasts WM_SETTINGCHANGE so new shells see it.
//   • darwin/linux — appends a marker-guarded export block to ~/.zshrc (and
//              ~/.bashrc when present). Idempotent via the marker.
// Best-effort by design: any failure only prints a manual-fix hint — it never
// fails the install.

const { execFileSync, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const MARKER = '# >>> agent-takkub PATH >>>';

function npmGlobalBinDir() {
  // Node ≥18 refuses to spawn .cmd shims without a shell (CVE-2024-27980
  // hardening) — route through cmd.exe explicitly on Windows.
  const r =
    process.platform === 'win32'
      ? spawnSync('cmd.exe', ['/d', '/s', '/c', 'npm prefix -g'], {
          encoding: 'utf8',
          timeout: 30000,
          windowsHide: true,
        })
      : spawnSync('npm', ['prefix', '-g'], { encoding: 'utf8', timeout: 30000 });
  const prefix = (r.stdout || '').trim();
  if (r.status !== 0 || !prefix) return null;
  return process.platform === 'win32' ? prefix : path.join(prefix, 'bin');
}

function normalize(p) {
  return path.normalize(p.trim()).replace(/[\\/]+$/, '').toLowerCase();
}

function dirOnPath(dir, pathValue) {
  const want = normalize(dir);
  return pathValue
    .split(path.delimiter)
    .filter(Boolean)
    .some((p) => normalize(p) === want);
}

// PowerShell one-shot: read raw user Path (unexpanded), append if missing with
// the same value kind, broadcast WM_SETTINGCHANGE. Prints ADDED/PRESENT.
function winEnsure(binDir) {
  const script = `
$bin = ${JSON.stringify(binDir)}
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
$raw = ''
$kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
if ($key.GetValueNames() -contains 'Path') {
  $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
  $kind = $key.GetValueKind('Path')
}
$parts = @($raw -split ';' | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\\').ToLower() })
$expanded = @($parts | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_) })
$want = $bin.TrimEnd('\\').ToLower()
if (($parts -contains $want) -or ($expanded -contains $want)) {
  Write-Output 'PRESENT'
} else {
  $new = if ($raw) { $raw.TrimEnd(';') + ';' + $bin } else { $bin }
  $key.SetValue('Path', $new, $kind)
  $sig = '[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)] public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);'
  $w32 = Add-Type -MemberDefinition $sig -Name 'PathBroadcast' -Namespace 'Win32' -PassThru
  [UIntPtr]$res = [UIntPtr]::Zero
  $w32::SendMessageTimeout([IntPtr]0xFFFF, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$res) | Out-Null
  Write-Output 'ADDED'
}
$key.Close()
`;
  const out = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
    encoding: 'utf8',
    timeout: 30000,
  }).trim();
  return out.includes('ADDED');
}

function posixEnsure(binDir) {
  if (dirOnPath(binDir, process.env.PATH || '')) return false;
  const block = `\n${MARKER}\nexport PATH="$PATH:${binDir}"\n# <<< agent-takkub PATH <<<\n`;
  const rcs = [path.join(os.homedir(), '.zshrc')];
  const bashrc = path.join(os.homedir(), '.bashrc');
  if (fs.existsSync(bashrc)) rcs.push(bashrc);
  let added = false;
  for (const rc of rcs) {
    const existing = fs.existsSync(rc) ? fs.readFileSync(rc, 'utf8') : '';
    if (existing.includes(MARKER)) continue;
    fs.writeFileSync(rc, existing + block);
    added = true;
  }
  return added;
}

// Returns true when it CHANGED something (caller prints the restart hint).
function ensureGlobalBinOnPath() {
  try {
    const binDir = npmGlobalBinDir();
    if (!binDir) return false;
    if (process.platform === 'win32') return winEnsure(binDir);
    return posixEnsure(binDir);
  } catch (e) {
    console.log(
      `[agent-takkub] PATH check skipped (${e && e.message}) — if 'takkub' is not found later, run: takkub doctor --fix`
    );
    return false;
  }
}

module.exports = { ensureGlobalBinOnPath, npmGlobalBinDir, dirOnPath };
