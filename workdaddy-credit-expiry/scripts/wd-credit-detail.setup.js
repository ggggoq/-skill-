'use strict';
/* WorkDaddy 积分到期明细按钮 — 跨平台部署 / 守卫 / 卸载（Windows + macOS）
 *
 * 用法：
 *   node wd-credit-detail.setup.js install      安装（写注入文件 + 注册登录守卫 + 触发注入）
 *   node wd-credit-detail.setup.js guard        守卫（被更新冲掉则恢复，并触发一次重新注入）
 *   node wd-credit-detail.setup.js status       查看状态
 *   node wd-credit-detail.setup.js uninstall    卸载
 *   node wd-credit-detail.setup.js apps         列出探测到的 WorkDaddy 安装
 *
 * 可选参数：
 *   --app <path>      指定 .app（macOS）或 WorkDaddy 安装目录（Windows），跳过自动探测
 *   --dry-run         只打印将执行的操作，不写任何文件
 *   --no-inject       安装/守卫后不触发 /api/inject
 *
 * 环境变量（用于测试/模拟，正常使用无需设置）：
 *   WD_FORCE_PLATFORM  强制平台：darwin / win32
 *   WD_HOME            覆盖用户主目录
 *   WD_APP_ROOTS       覆盖应用根目录（macOS，多个用 path.delimiter 分隔）
 *   WD_NO_LAUNCHCTL    设为 1 时不调用 launchctl（模拟用）
 */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

// ---------- 平台与路径 ----------
const PLATFORM = process.env.WD_FORCE_PLATFORM || process.platform;
const IS_MAC = PLATFORM === 'darwin';
const IS_WIN = PLATFORM === 'win32';
// sudo 运行时 HOME 会变成 root 家目录，会把 LaunchAgent / 守卫写到错误位置。
// 有 SUDO_USER 时纠正回真实用户家目录。
function resolveHome() {
  if (process.env.WD_HOME) return process.env.WD_HOME;
  const sudoUser = process.env.SUDO_USER;
  if (sudoUser && sudoUser !== 'root') {
    const guess = IS_MAC ? '/Users/' + sudoUser : path.join('/home', sudoUser);
    if (fs.existsSync(guess)) return guess;
  }
  return os.homedir();
}
const HOME = resolveHome();
const HERE = __dirname;
const SRC = path.join(HERE, 'wd-credit-detail.inject.js');
const SETUP = path.join(HERE, 'wd-credit-detail.setup.js');
const MARKER = 'installCreditDetail';       // 我方代码特征串
const AGENT_LABEL = 'com.workdaddy.creditdetail.guard';

const ARGV = process.argv.slice(3);
const DRY = ARGV.includes('--dry-run');
const NO_INJECT = ARGV.includes('--no-inject');
function argValue(flag) {
  const i = ARGV.indexOf(flag);
  return i >= 0 && ARGV[i + 1] ? ARGV[i + 1] : null;
}
const APP_OVERRIDE = argValue('--app');

// 候选 .app 名（WorkDaddy 四个 profile 的产物名）
const MAC_APP_NAMES = ['WorkDaddy.app', 'WorkDaddy AI.app', 'CodeDaddy.app', 'CodeDaddy CN.app'];

function log(msg) { console.log(msg); }
function fail(msg, code) { console.error('错误: ' + msg); process.exit(code || 1); }

function macAppRoots() {
  if (process.env.WD_APP_ROOTS) return process.env.WD_APP_ROOTS.split(path.delimiter).filter(Boolean);
  return ['/Applications', path.join(HOME, 'Applications')];
}

/** macOS：返回 [{app, scripts}]，只认 Content/Resources/scripts/daemon.js 存在的 */
function discoverMacApps() {
  const found = [];
  const seen = new Set();
  macAppRoots().forEach((root) => {
    if (!fs.existsSync(root)) return;
    let entries = [];
    try { entries = fs.readdirSync(root); } catch (_) { return; }
    const names = MAC_APP_NAMES.filter((n) => entries.includes(n))
      .concat(entries.filter((n) => n.endsWith('.app') && !MAC_APP_NAMES.includes(n))); // 兜底：其它命名的 .app
    names.forEach((name) => {
      const app = path.join(root, name);
      const scripts = path.join(app, 'Contents', 'Resources', 'scripts');
      if (seen.has(scripts)) return;
      if (fs.existsSync(path.join(scripts, 'daemon.js'))) { seen.add(scripts); found.push({ app, scripts }); }
    });
  });
  return found;
}

function discoverWinApps() {
  const local = process.env.LOCALAPPDATA || path.join(HOME, 'AppData', 'Local');
  const candidates = [
    path.join(local, 'Programs', 'WorkDaddy'),
    path.join(local, 'Programs', 'WorkDaddy AI'),
    path.join(local, 'Programs', 'CodeDaddy'),
  ];
  return candidates
    .map((app) => ({ app, scripts: path.join(app, 'scripts') }))
    .filter((x) => fs.existsSync(path.join(x.scripts, 'daemon.js')));
}

function resolveTarget() {
  if (APP_OVERRIDE) {
    const app = path.resolve(APP_OVERRIDE);
    const scripts = IS_MAC ? path.join(app, 'Contents', 'Resources', 'scripts') : path.join(app, 'scripts');
    if (!fs.existsSync(path.join(scripts, 'daemon.js'))) fail('指定目录下找不到 scripts/daemon.js：' + scripts);
    return { app, scripts };
  }
  const found = IS_MAC ? discoverMacApps() : discoverWinApps();
  if (!found.length) fail('未探测到 WorkDaddy 安装；可用 --app <路径> 手动指定');
  return found[0];
}

function dataDir() {
  if (IS_MAC) return path.join(HOME, 'Library', 'Application Support', 'WorkDaddy');
  const appData = process.env.APPDATA || path.join(HOME, 'AppData', 'Roaming');
  return path.join(appData, 'WorkDaddy');
}

function guardPaths() {
  if (IS_MAC) {
    const dir = path.join(HOME, 'Library', 'LaunchAgents');
    return { kind: 'launchd', dir, file: path.join(dir, AGENT_LABEL + '.plist') };
  }
  const dir = path.join(HOME, 'AppData', 'Roaming', 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup');
  return { kind: 'vbs', dir, file: path.join(dir, 'WD-CreditDetail-Guard.vbs') };
}

// ---------- 工具 ----------
function sha(p) {
  try { return crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex'); }
  catch (_) { return null; }
}
function readIfExists(p) { try { return fs.readFileSync(p, 'utf8'); } catch (_) { return null; } }
function xmlEscape(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function writable(dir) {
  try { fs.accessSync(dir, fs.constants.W_OK); return true; } catch (_) { return false; }
}

// ---------- 注入文件读写 ----------
/**
 * 目标内容 = 官方原有内容（若有）+ 我方代码。
 * 若官方将来自带 picker-internal.js，两份都注入，不覆盖对方。
 */
function composeTarget() {
  const src = readIfExists(SRC);
  if (!src) return null;
  const cur = readIfExists(DST_PATH);
  if (cur && !cur.includes(MARKER) && cur.trim()) {
    return { content: cur.replace(/\s*$/, '') + '\n\n' + src, official: cur };
  }
  return { content: src, official: null };
}

let DST_PATH = '';

function guard(verbose) {
  const src = readIfExists(SRC);
  if (!src) return { ok: false, reason: '源文件缺失: ' + SRC };

  const cur = readIfExists(DST_PATH);
  if (cur && cur.includes(src.trim().slice(0, 200)) && cur.includes(MARKER)) {
    if (verbose) log('[guard] 注入文件正常，无需操作');
    return { ok: true, changed: false };
  }

  const target = composeTarget();
  if (!target) return { ok: false, reason: '源文件缺失' };

  if (!writable(path.dirname(DST_PATH))) {
    return {
      ok: false,
      reason: '目录不可写: ' + path.dirname(DST_PATH) + '\n' +
        '  不要用 sudo 运行本脚本（否则 LaunchAgent 会装到 root 家目录，守卫失效）。\n' +
        '  正确做法：先执行 sudo chmod u+w "' + path.dirname(DST_PATH) + '"，再以普通用户重跑 install。'
    };
  }
  if (DRY) { log('[dry-run] 将写入 ' + DST_PATH); return { ok: true, changed: true, dry: true }; }

  try {
    fs.mkdirSync(path.dirname(DST_PATH), { recursive: true });
    if (cur && !cur.includes(MARKER)) {
      // 官方文件：先留底，卸载时还原
      fs.writeFileSync(DST_PATH + '.official', cur, 'utf8');
      if (verbose) log('[guard] 已备份官方 picker-internal.js -> ' + DST_PATH + '.official');
    }
    fs.writeFileSync(DST_PATH, target.content, 'utf8');
    if (verbose) log('[guard] 已写入注入文件 -> ' + DST_PATH);
    return { ok: true, changed: true };
  } catch (e) {
    return { ok: false, reason: '写入失败: ' + e.message + '（若为权限问题，请用 sudo 运行）' };
  }
}

// ---------- 守卫注册 ----------
function macPlistBody() {
  const node = process.execPath;
  const out = path.join(HOME, 'Library', 'Logs', 'workdaddy-creditdetail-guard.log');
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
    '<plist version="1.0">',
    '<dict>',
    '  <key>Label</key>',
    '  <string>' + AGENT_LABEL + '</string>',
    '  <key>ProgramArguments</key>',
    '  <array>',
    '    <string>' + xmlEscape(node) + '</string>',
    '    <string>' + xmlEscape(SETUP) + '</string>',
    '    <string>guard</string>',
    '  </array>',
    '  <key>RunAtLoad</key>',
    '  <true/>',
    '  <key>StartInterval</key>',
    '  <integer>600</integer>',
    '  <key>StandardOutPath</key>',
    '  <string>' + xmlEscape(out) + '</string>',
    '  <key>StandardErrorPath</key>',
    '  <string>' + xmlEscape(out) + '</string>',
    '</dict>',
    '</plist>',
    ''
  ].join('\n');
}

const VBS_BODY = [
  "' WorkDaddy credit-detail guard - restore inject file on logon",
  'Dim sh, cmd',
  'Set sh = CreateObject("WScript.Shell")',
  'cmd = """' + process.execPath + '"" ""' + SETUP + '"" guard"',
  'sh.Run cmd, 0, False',
].join('\r\n');

function installGuard() {
  const g = guardPaths();
  if (DRY) { log('[dry-run] 将写入守卫 ' + g.file); return { ok: true, dry: true }; }
  try {
    fs.mkdirSync(g.dir, { recursive: true });
    fs.writeFileSync(g.file, g.kind === 'launchd' ? macPlistBody() : VBS_BODY,
      { encoding: g.kind === 'launchd' ? 'utf8' : 'ascii' });
    if (g.kind === 'launchd' && !process.env.WD_NO_LAUNCHCTL) {
      const uid = typeof process.getuid === 'function' ? process.getuid() : null;
      const domain = 'gui/' + uid;
      try { execFileSync('launchctl', ['bootout', domain + '/' + AGENT_LABEL], { stdio: 'pipe' }); } catch (_) {}
      execFileSync('launchctl', ['bootstrap', domain, g.file], { stdio: 'pipe' });
      execFileSync('launchctl', ['enable', domain + '/' + AGENT_LABEL], { stdio: 'pipe' });
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, reason: '守卫注册失败: ' + (e.stderr || e.message).toString().trim() };
  }
}

function removeGuard() {
  const g = guardPaths();
  if (g.kind === 'launchd' && !process.env.WD_NO_LAUNCHCTL) {
    const uid = typeof process.getuid === 'function' ? process.getuid() : null;
    try { execFileSync('launchctl', ['bootout', 'gui/' + uid + '/' + AGENT_LABEL], { stdio: 'pipe' }); } catch (_) {}
  }
  try {
    if (fs.existsSync(g.file)) { fs.unlinkSync(g.file); return true; }
  } catch (_) {}
  return false;
}

// ---------- 触发注入 ----------
function triggerInject(timeoutMs) {
  const dir = dataDir();
  let port, token;
  try {
    port = JSON.parse(readIfExists(path.join(dir, 'ui-port.json')) || '{}').port;
    token = (readIfExists(path.join(dir, '.api-token')) || '').trim();
  } catch (_) {}
  if (!port) return { ok: false, reason: '读不到 ui-port.json（WorkDaddy 未运行？）' };
  return new Promise((resolve) => {
    const body = '{}';
    const req = require('node:http').request({
      host: '127.0.0.1', port, path: '/api/inject', method: 'POST',
      headers: {
        'content-type': 'application/json',
        'content-length': Buffer.byteLength(body),
        'x-workdaddy-token': token,
      },
      timeout: timeoutMs || 8000,
    }, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => resolve({ ok: res.statusCode === 200, status: res.statusCode, body: data.slice(0, 120) }));
    });
    req.on('error', (e) => resolve({ ok: false, reason: e.message }));
    req.on('timeout', () => { req.destroy(); resolve({ ok: false, reason: '请求超时' }); });
    req.write(body); req.end();
  });
}

// ---------- 命令 ----------
async function install() {
  const t = resolveTarget();
  DST_PATH = path.join(t.scripts, 'picker-internal.js');
  log('平台: ' + PLATFORM);
  log('应用: ' + t.app);
  log('注入: ' + DST_PATH);

  const r = guard(true);
  if (!r.ok) fail(r.reason);
  const g = installGuard();
  if (!g.ok) fail(g.reason);

  let injectResult = null;
  if (!NO_INJECT && !DRY) injectResult = await triggerInject();

  log('');
  log('安装完成:');
  log('  注入文件: ' + DST_PATH);
  log('  守卫: ' + guardPaths().file + (guardPaths().kind === 'launchd' ? '（launchd，登录后每 10 分钟自检）' : '（登录时自检）'));
  if (injectResult) log('  重新注入: ' + (injectResult.ok ? '已触发（按钮已生效）' : '未触发 — ' + (injectResult.reason || ('HTTP ' + injectResult.status))));
  if (!injectResult && NO_INJECT) log('  重新注入: 已跳过（--no-inject）');
  log('  下一步: 打开 WorkDaddy 账号面板，点「总积分」右侧的列表按钮');
}

function status() {
  const t = resolveTarget();
  DST_PATH = path.join(t.scripts, 'picker-internal.js');
  const s1 = sha(SRC), s2 = sha(DST_PATH);
  const cur = readIfExists(DST_PATH);
  log('平台: ' + PLATFORM + (process.env.WD_FORCE_PLATFORM ? '（强制）' : ''));
  log('应用: ' + t.app);
  log('源文件: ' + SRC + '  ' + (s1 || '缺失'));
  log('目标文件: ' + DST_PATH + '  ' + (s2 || '缺失'));
  log('已安装: ' + (cur && cur.includes(MARKER) ? '是' : '否'));
  const g = guardPaths();
  log('守卫: ' + (fs.existsSync(g.file) ? g.file : '未注册'));
  log('数据目录: ' + dataDir());
}

function listApps() {
  const found = IS_MAC ? discoverMacApps() : discoverWinApps();
  if (!found.length) { log('未探测到 WorkDaddy 安装'); return; }
  found.forEach((f, i) => log((i + 1) + '. ' + f.app));
}

function uninstall() {
  const t = resolveTarget();
  DST_PATH = path.join(t.scripts, 'picker-internal.js');
  const removedGuard = removeGuard();
  log(removedGuard ? '已删除守卫: ' + guardPaths().file : '守卫不存在');

  const cur = readIfExists(DST_PATH);
  if (!cur) { log('注入文件不存在: ' + DST_PATH); return; }
  if (!cur.includes(MARKER)) { log('目标文件非本补丁内容，未删除: ' + DST_PATH); return; }

  const officialPath = DST_PATH + '.official';
  const official = readIfExists(officialPath);
  if (official !== null) {
    fs.writeFileSync(DST_PATH, official, 'utf8');
    try { fs.unlinkSync(officialPath); } catch (_) {}
    log('已还原官方 picker-internal.js: ' + DST_PATH);
  } else {
    fs.unlinkSync(DST_PATH);
    log('已删除注入文件: ' + DST_PATH);
  }
}

(async () => {
  const cmd = (process.argv[2] || '').toLowerCase();
  if (cmd === 'install') await install();
  else if (cmd === 'guard') {
    const t = resolveTarget();
    DST_PATH = path.join(t.scripts, 'picker-internal.js');
    const r = guard(true);
    if (!r.ok) { console.error('[guard] ' + r.reason); process.exit(1); }
    if (r.changed && !NO_INJECT && !DRY) {
      const ir = await triggerInject(5000);
      if (ir.ok) console.log('[guard] 已重新注入');
    }
  }
  else if (cmd === 'status') status();
  else if (cmd === 'apps') listApps();
  else if (cmd === 'uninstall') uninstall();
  else log('用法: node wd-credit-detail.setup.js install|guard|status|apps|uninstall [--app <path>] [--dry-run] [--no-inject]');
})();
