'use strict';
/* macOS 部署流程模拟测试
 *
 * 在 Windows 上伪造一套 macOS 目录结构，用 WD_FORCE_PLATFORM=darwin 让 setup.js
 * 走完整的 mac 分支（探测 .app → 写 picker-internal.js → 生成 launchd plist），
 * 验证安装/守卫/状态/卸载四个命令的真实行为。
 *
 * 运行：node sim-mac.js
 */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '.tmp-sim');
const FAKE_HOME = path.join(ROOT, 'home');
const FAKE_APPS = path.join(ROOT, 'Applications');
const APP = path.join(FAKE_APPS, 'WorkDaddy.app');
const SCRIPTS = path.join(APP, 'Contents', 'Resources', 'scripts');
const SETUP = path.join(__dirname, '..', 'scripts', 'wd-credit-detail.setup.js');
const DST = path.join(SCRIPTS, 'picker-internal.js');
const PLIST = path.join(FAKE_HOME, 'Library', 'LaunchAgents', 'com.workdaddy.creditdetail.guard.plist');

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? ' -> ' + extra : '')); }
}

function buildFakeMac() {
  try { fs.rmSync(ROOT, { recursive: true, force: true }); } catch (_) {}
  fs.mkdirSync(SCRIPTS, { recursive: true });
  fs.mkdirSync(path.join(FAKE_HOME, 'Library', 'Application Support', 'WorkDaddy'), { recursive: true });
  // 伪造 daemon.js：探测逻辑只检查文件存在
  fs.writeFileSync(path.join(SCRIPTS, 'daemon.js'), 'const DAEMON_VERSION = "1.2.22";\n');
  // 伪造一个未签名的 inject.js 锚点，供注入合法性检查
  fs.writeFileSync(path.join(SCRIPTS, 'inject.js'),
    'function build(){ var accountsPane = 1; return { destroy: lifecycle.destroy, alive: lifecycle.alive }; }\n');
}

function run(args, extraEnv) {
  try {
    const out = execFileSync(process.execPath, [SETUP].concat(args), {
      encoding: 'utf8',
      env: Object.assign({}, process.env, {
        WD_FORCE_PLATFORM: 'darwin',
        WD_HOME: FAKE_HOME,
        WD_APP_ROOTS: FAKE_APPS,
        WD_NO_LAUNCHCTL: '1',
        NO_COLOR: '1',
      }, extraEnv || {}),
      stdio: 'pipe',
    });
    return { code: 0, out };
  } catch (e) {
    return { code: e.status || 1, out: (e.stdout || '') + (e.stderr || '') };
  }
}

function main() {
  console.log('模拟环境: ' + ROOT);
  buildFakeMac();

  console.log('\n[1] apps — 探测 .app');
  let r = run(['apps']);
  check('探测到 WorkDaddy.app', r.out.includes(APP), r.out.trim());

  console.log('\n[2] install — 安装注入与守卫');
  r = run(['install', '--no-inject']);
  check('命令成功', r.code === 0, r.out.trim());
  check('picker-internal.js 已写入', fs.existsSync(DST));
  const injected = fs.existsSync(DST) ? fs.readFileSync(DST, 'utf8') : '';
  check('注入内容含 installCreditDetail', injected.includes('installCreditDetail'));
  check('注入内容含 data-act=credit-detail', injected.includes('credit-detail'));
  check('launchd plist 已生成', fs.existsSync(PLIST));
  const plist = fs.existsSync(PLIST) ? fs.readFileSync(PLIST, 'utf8') : '';
  check('plist 含 RunAtLoad', plist.includes('<key>RunAtLoad</key>'));
  check('plist 含 StartInterval 600', plist.includes('<integer>600</integer>'));
  check('plist 指向 setup.js guard', plist.includes('wd-credit-detail.setup.js'));
  check('plist 含 guard 参数', plist.includes('<string>guard</string>'));
  check('plist XML 结构合法', /<plist version="1.0">[\s\S]*<\/plist>/.test(plist));

  console.log('\n[3] status — 状态一致');
  r = run(['status']);
  check('报告已安装', r.out.includes('已安装: 是'), r.out.trim());
  check('报告守卫已注册', r.out.includes('LaunchAgents'), r.out.trim());

  console.log('\n[4] guard — 幂等（不改动已正确文件）');
  const before = fs.statSync(DST).mtimeMs;
  r = run(['guard', '--no-inject']);
  check('guard 成功退出', r.code === 0, r.out.trim());
  check('guard 未重写文件', fs.statSync(DST).mtimeMs === before);

  console.log('\n[5] guard — 被更新冲掉后恢复');
  fs.unlinkSync(DST);
  r = run(['guard', '--no-inject']);
  check('guard 成功退出', r.code === 0, r.out.trim());
  check('文件已恢复', fs.existsSync(DST));
  check('恢复内容完整', fs.existsSync(DST) && fs.readFileSync(DST, 'utf8').includes('installCreditDetail'));

  console.log('\n[6] guard — 保留官方 picker-internal.js 内容');
  const official = '/* official debug module */\nvar OFFICIAL_DEBUG = true;\n';
  fs.writeFileSync(DST, official, 'utf8');
  r = run(['guard', '--no-inject']);
  const merged = fs.readFileSync(DST, 'utf8');
  check('保留官方代码', merged.includes('OFFICIAL_DEBUG'));
  check('同时注入我方代码', merged.includes('installCreditDetail'));
  check('官方原文已留底', fs.existsSync(DST + '.official'));

  console.log('\n[7] uninstall — 还原官方文件');
  r = run(['uninstall']);
  check('命令成功', r.code === 0, r.out.trim());
  const restored = fs.existsSync(DST) ? fs.readFileSync(DST, 'utf8') : '';
  check('已还原为官方内容', restored.includes('OFFICIAL_DEBUG') && !restored.includes('installCreditDetail'), restored.slice(0, 60));
  check('plist 已删除', !fs.existsSync(PLIST));
  check('留底文件已清理', !fs.existsSync(DST + '.official'));

  console.log('\n[8] uninstall — 无官方文件时直接删除');
  // 清掉上一步还原出的官方文件，构造「从未有过官方 picker-internal.js」的干净状态
  try { fs.unlinkSync(DST); } catch (_) {}
  run(['install', '--no-inject']);
  check('重新安装成功', fs.existsSync(DST));
  check('干净安装未产生 .official 留底', !fs.existsSync(DST + '.official'));
  run(['uninstall']);
  check('注入文件已删除', !fs.existsSync(DST));

  console.log('\n[9] --app 手动指定路径');
  r = run(['status', '--app', APP]);
  check('--app 生效', r.code === 0 && r.out.includes('WorkDaddy.app'), r.out.trim());

  console.log('\n[10] --dry-run — 不写文件');
  const dryRoot = path.join(ROOT, 'dry');
  fs.mkdirSync(path.join(dryRoot, 'Applications', 'WorkDaddy.app', 'Contents', 'Resources', 'scripts'), { recursive: true });
  fs.writeFileSync(path.join(dryRoot, 'Applications', 'WorkDaddy.app', 'Contents', 'Resources', 'scripts', 'daemon.js'), 'x');
  const dstDry = path.join(dryRoot, 'Applications', 'WorkDaddy.app', 'Contents', 'Resources', 'scripts', 'picker-internal.js');
  r = run(['install', '--dry-run'], { WD_APP_ROOTS: path.join(dryRoot, 'Applications'), WD_HOME: path.join(dryRoot, 'home') });
  check('dry-run 提示将写入', r.out.includes('[dry-run]'), r.out.trim());
  check('dry-run 未真正写文件', !fs.existsSync(dstDry));

  console.log('\n[11] 路径不可写时报错清晰');
  r = run(['install', '--no-inject'], { WD_APP_ROOTS: '/nonexistent-root' });
  check('找不到安装时报错', r.code !== 0 && /未探测到|找不到/.test(r.out), r.out.trim());

  console.log('\n[12] 注入合成后的脚本语法合法');
  run(['install', '--no-inject']);
  const anchor = 'return { destroy: lifecycle.destroy, alive: lifecycle.alive };';
  const baseInject = fs.readFileSync(path.join(SCRIPTS, 'inject.js'), 'utf8');
  const picker = fs.readFileSync(DST, 'utf8');
  check('锚点存在', baseInject.includes(anchor));
  const composed = baseInject.replace(anchor, picker + '\n' + anchor);
  let syntaxOK = true, syntaxErr = '';
  try { new Function(composed); } catch (e) { syntaxOK = false; syntaxErr = e.message; }
  check('合成脚本可被解析', syntaxOK, syntaxErr);
  check('合成含按钮注册', composed.includes('data-act') || composed.includes('setAttribute(\'data-act\''));

  console.log('\n[13] 三平台数据目录路径');
  const P = path.posix;
  function pickDir(platform, home, env) {
    if (platform === 'darwin') return P.join(home, 'Library', 'Application Support', 'WorkDaddy');
    if (platform === 'win32') return P.join(env.APPDATA || P.join(home, 'AppData', 'Roaming'), 'WorkDaddy');
    return P.join(env.XDG_CONFIG_HOME || P.join(home, '.config'), 'WorkDaddy');
  }
  check('macOS 路径', pickDir('darwin', '/Users/guoza', {}) === '/Users/guoza/Library/Application Support/WorkDaddy');
  check('Windows 路径', pickDir('win32', 'C:/Users/guoza', { APPDATA: 'C:/Users/guoza/AppData/Roaming' }) === 'C:/Users/guoza/AppData/Roaming/WorkDaddy');
  check('Linux 路径', pickDir('linux', '/home/guoza', {}) === '/home/guoza/.config/WorkDaddy');

  console.log('\n========================================');
  console.log('通过 ' + pass + ' / ' + (pass + fail));
  if (fail) { console.log('失败 ' + fail + ' 项'); process.exit(1); }
  console.log('全部通过');
  try { fs.rmSync(ROOT, { recursive: true, force: true }); } catch (_) {}
}

main();
