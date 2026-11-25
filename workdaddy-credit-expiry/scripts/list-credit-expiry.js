#!/usr/bin/env node
'use strict';

// WorkDaddy 积分到期明细查看器
// 默认读 WorkDaddy 本地积分缓存（不发网络请求、不碰登录 token）
// 用法：
//   node list-credit-expiry.js                 列出每个账号每笔积分的到期时间
//   node list-credit-expiry.js --days 7        只看 7 天内到期的
//   node list-credit-expiry.js --refresh       先让 WorkDaddy 守护进程拉实时数据再列出
//   node list-credit-expiry.js --json          输出 JSON（便于二次处理）

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

// 数据目录：Windows %APPDATA%\WorkDaddy / macOS ~/Library/Application Support/WorkDaddy
function defaultDataDir() {
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'WorkDaddy');
  }
  if (process.platform === 'win32') {
    return path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'WorkDaddy');
  }
  return path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config'), 'WorkDaddy');
}
const DATA_DIR = process.env.WORKDADDY_DATA_DIR || defaultDataDir();
const CACHE_FILE = path.join(DATA_DIR, 'account-credit-cache.json');

const args = process.argv.slice(2);
const wantJson = args.includes('--json');
const wantRefresh = args.includes('--refresh');
const daysIdx = args.indexOf('--days');
const daysLimit = daysIdx >= 0 ? Number(args[daysIdx + 1]) : null;

function pad(n, width = 2) { return String(n).padStart(width, '0'); }
function fmtTime(ms) {
  if (!Number.isFinite(ms)) return '未知';
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
// 剩余天数 = 自然日相减（目标日 00:00 − 今天 00:00）。今天到期为 0。
function daysLeft(expiresAt, now) {
  if (!Number.isFinite(expiresAt)) return null;
  const d = new Date(expiresAt);
  const n = new Date(now);
  const a = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const b = new Date(n.getFullYear(), n.getMonth(), n.getDate()).getTime();
  return Math.round((a - b) / 86400000);
}
function num(v) { return Number.isFinite(Number(v)) ? Number(v) : 0; }

// 读本地缓存（WorkDaddy daemon 每次查询积分后自动写入）
function readCache() {
  try {
    const raw = JSON.parse(fs.readFileSync(CACHE_FILE, 'utf8'));
    const out = new Map();
    for (const [uid, value] of Object.entries(raw)) {
      if (!value || typeof value !== 'object') continue;
      out.set(uid, {
        credits: num(value.credits),
        unlimited: value.creditUnlimited === true,
        segments: (Array.isArray(value.creditSegments) ? value.creditSegments : [])
          .map(s => ({
            remaining: num(s.remaining),
            total: num(s.total),
            expiresAt: Number.isFinite(Number(s.expiresAt)) ? Number(s.expiresAt) : null,
            source: String(s.source || '积分'),
            packageCode: String(s.packageCode || ''),
          }))
          .filter(s => s.remaining > 0),
      });
    }
    return out;
  } catch (e) {
    throw new Error(`读取积分缓存失败：${e.message}\n路径：${CACHE_FILE}`);
  }
}

// 从 daemon 拿账号昵称映射（只读本地数据，不发官方网络请求）
async function readNicknames() {
  try {
    const portRaw = fs.readFileSync(path.join(DATA_DIR, 'ui-port.json'), 'utf8');
    const port = JSON.parse(portRaw).port;
    const token = fs.readFileSync(path.join(DATA_DIR, '.api-token'), 'utf8').trim();
    const res = await fetch(`http://127.0.0.1:${port}/api/accounts`, {
      headers: { 'x-workdaddy-token': token },
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return new Map();
    const data = await res.json();
    const map = new Map();
    for (const a of (data.accounts || [])) {
      map.set(String(a.uid), {
        nickname: String(a.nickname || ''),
        phone: String(a.phone || ''),
        isCurrent: String(data.current && data.current.uid) === String(a.uid),
      });
    }
    return map;
  } catch (_) {
    return new Map();
  }
}

// --refresh：调 daemon /api/credits 让 WorkDaddy 真实拉取官方接口并回写缓存
async function refreshAll(cache) {
  try {
    const portRaw = fs.readFileSync(path.join(DATA_DIR, 'ui-port.json'), 'utf8');
    const port = JSON.parse(portRaw).port;
    const token = fs.readFileSync(path.join(DATA_DIR, '.api-token'), 'utf8').trim();
    const results = [];
    for (const uid of cache.keys()) {
      try {
        const res = await fetch(`http://127.0.0.1:${port}/api/credits`, {
          method: 'POST',
          headers: { 'content-type': 'application/json', 'x-workdaddy-token': token },
          body: JSON.stringify({ uid }),
          signal: AbortSignal.timeout(15000),
        });
        const data = await res.json();
        results.push({ uid, ok: res.ok && data.ok !== false, credits: data.credits, error: data.error });
      } catch (e) {
        results.push({ uid, ok: false, error: e.message });
      }
    }
    return results;
  } catch (e) {
    return [{ ok: false, error: `无法连接 WorkDaddy 守护进程：${e.message}` }];
  }
}

function buildReport(cache, nicknames, now) {
  const accounts = [];
  for (const [uid, info] of cache.entries()) {
    const meta = nicknames.get(uid) || {};
    const label = meta.nickname || meta.phone || `${uid.slice(0, 8)}…`;
    const segments = info.segments
      .map(s => Object.assign({}, s, { days: daysLeft(s.expiresAt, now) }))
      .filter(s => daysLimit === null || (s.days !== null && s.days <= daysLimit))
      .sort((a, b) => {
        if (a.expiresAt === null) return 1;
        if (b.expiresAt === null) return -1;
        return a.expiresAt - b.expiresAt;
      });
    accounts.push({
      uid, label, isCurrent: !!meta.isCurrent, credits: info.credits,
      unlimited: info.unlimited, segments,
      shown: segments.reduce((sum, s) => sum + s.remaining, 0),
    });
  }
  accounts.sort((a, b) => {
    const ae = a.segments[0] && a.segments[0].expiresAt;
    const be = b.segments[0] && b.segments[0].expiresAt;
    if (ae == null && be == null) return 0;
    if (ae == null) return 1;
    if (be == null) return -1;
    return ae - be;
  });
  return { generatedAt: fmtTime(now), accounts };
}

function renderText(report, cacheMtime) {
  const lines = [];
  lines.push('WorkDaddy 积分到期明细（按账号拆分）');
  lines.push(`生成时间：${report.generatedAt}`);
  lines.push(`数据来源：${fs.existsSync(CACHE_FILE) ? `本地缓存 ${fmtTime(cacheMtime)}` : '无缓存'}`);
  if (daysLimit !== null) lines.push(`筛选条件：仅显示 ${daysLimit} 天内到期`);
  lines.push('');

  let totalAll = 0;
  for (const acc of report.accounts) {
    lines.push('═'.repeat(64));
    lines.push(`账号：${acc.label}${acc.isCurrent ? '  [当前使用]' : ''}`);
    lines.push(`UID：${acc.uid}`);
    lines.push(`总余额：${acc.credits.toFixed(2)}${acc.unlimited ? '（不限量）' : ''}`);
    if (!acc.segments.length) {
      lines.push('  （无符合条件的积分段）');
      lines.push('');
      continue;
    }
    lines.push('');
    lines.push('  ' + ['剩余', '积分', '到期时间', '来源'].map((h, i) => {
      const widths = [9, 12, 22, 40];
      return h.padEnd(widths[i] - (h.length - h.replace(/[^\x00-\xff]/g, 'xx').length));
    }).join(''));
    for (const s of acc.segments) {
      const days = s.days === null ? '未知' : s.days < 0 ? '已到期' : s.days === 0 ? '今天' : `${s.days} 天`;
      const source = s.source.length > 38 ? s.source.slice(0, 37) + '…' : s.source;
      const cells = [days, s.remaining.toFixed(2), fmtTime(s.expiresAt), source];
      const widths = [9, 12, 22, 40];
      lines.push('  ' + cells.map((c, i) => {
        const visualLen = c.length + (c.replace(/[^\x00-\xff]/g, 'xx').length - c.length);
        return c + ' '.repeat(Math.max(1, widths[i] - visualLen));
      }).join(''));
      totalAll += s.remaining;
    }
    lines.push(`  小计：${acc.shown.toFixed(2)}（${acc.segments.length} 笔）`);
    lines.push('');
  }
  lines.push('═'.repeat(64));
  lines.push(`全部账号合计（当前筛选）：${totalAll.toFixed(2)}`);
  return lines.join('\n');
}

(async () => {
  let cache = readCache();
  if (!cache.size) {
    console.error('缓存里没有账号数据。请先在 WorkDaddy 账号页刷新一次积分。');
    process.exit(1);
  }
  if (wantRefresh) {
    const results = await refreshAll(cache);
    cache = readCache(); // daemon 已回写缓存
    if (!wantJson) {
      for (const r of results) {
        console.log(`[刷新] ${r.uid.slice(0, 8)}… ${r.ok ? `余额 ${Number(r.credits).toFixed(2)}` : `失败：${r.error || '未知错误'}`}`);
      }
      console.log('');
    }
  }
  const cacheMtime = fs.existsSync(CACHE_FILE) ? fs.statSync(CACHE_FILE).mtimeMs : 0;
  const report = buildReport(cache, await readNicknames(), Date.now());
  if (wantJson) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(renderText(report, cacheMtime));
  }
})().catch(e => { console.error(e.message); process.exit(1); });
