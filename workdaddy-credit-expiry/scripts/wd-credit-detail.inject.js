/* WorkDaddy 账号面板：积分到期明细按钮（按到期日合并 · 数字+堆叠条形图）
 * 由 daemon 的 buildInjectScript() 通过 picker-internal.js 官方锚点注入。
 * 本段代码被插入到 build() 函数体末尾，共享闭包：accountsPane / state / el /
 * listen / mountPersistentOverlay / esc / fmtCredits / applyI18n / registerDisposer。
 *
 * 口径：
 *   1. 跨账号按「到期日」合并，忽略时分秒；
 *   2. 每行条形固定占满 100%，段宽 = 该账号在当天的积分占比（当天只有一家则整条满）；
 *   3. 剩余天数按自然日相减（目标日 00:00 − 今天 00:00），今天到期显示「今天」。
 */
(function installCreditDetail() {
  if (typeof accountsPane === 'undefined' || !accountsPane) return;
  var BTN_ACT = 'credit-detail';
  var POP_ID = 'wbs-credit-detail-popover';
  if (accountsPane.querySelector('[data-act="' + BTN_ACT + '"]')) return; // 幂等

  var ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>';
  var COLORS = ['#22c55e', '#3b82f6', '#f59e0b', '#a855f7', '#ec4899', '#06b6d4'];
  var HOT_THRESHOLD = 500;

  // 按钮：插在「汇总」星星之后
  var summaryBtn = accountsPane.querySelector('[data-act="credit-summary"]');
  var button = document.createElement('button');
  button.className = 'wbs-acct-eye';
  button.type = 'button';
  button.setAttribute('data-act', BTN_ACT);
  button.setAttribute('title', '积分到期明细');
  button.setAttribute('aria-label', '积分到期明细');
  button.setAttribute('aria-expanded', 'false');
  button.innerHTML = ICON;
  if (summaryBtn && summaryBtn.nextSibling) {
    summaryBtn.parentNode.insertBefore(button, summaryBtn.nextSibling);
  } else if (summaryBtn) {
    summaryBtn.parentNode.appendChild(button);
  } else {
    return; // 找不到锚点按钮，静默失败，不破坏面板
  }

  var popup = document.createElement('div');
  popup.className = 'wbs-credit-summary-popover';
  popup.id = POP_ID;
  popup.setAttribute('role', 'region');
  popup.setAttribute('aria-label', '积分到期明细');
  popup.hidden = true;
  button.setAttribute('aria-controls', POP_ID);
  mountPersistentOverlay(popup);

  var css = document.createElement('style');
  css.setAttribute('data-wbs-credit-detail-css', '');
  css.textContent = [
    '#' + POP_ID + '{width:480px;max-width:calc(100vw - 16px)}',
    '#' + POP_ID + ' .cd-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:12px}',
    '#' + POP_ID + ' .cd-tbl th{text-align:left;font-size:10px;font-weight:500;color:var(--wb-color-text-tertiary,#999);padding:0 6px 6px 0;white-space:nowrap}',
    '#' + POP_ID + ' .cd-tbl th.num,#' + POP_ID + ' .cd-tbl td.num{text-align:right;padding-right:18px}',
    '#' + POP_ID + ' .cd-tbl td{padding:7px 6px 7px 0;border-top:1px solid var(--wb-border-subtle,#f0f0f0);vertical-align:middle}',
    '#' + POP_ID + ' .cd-tbl th.chart,#' + POP_ID + ' .cd-tbl td.chart{width:100%;padding-right:0}',
    '#' + POP_ID + ' .cd-date{font-weight:600;white-space:nowrap;color:var(--wb-color-text-primary,#1f1f1f)}',
    '#' + POP_ID + ' .cd-days{display:inline-block;font-size:10px;font-weight:400;color:var(--wb-color-text-secondary,#666);background:var(--wb-bg-tertiary,#f2f2f2);padding:0 5px;border-radius:8px;margin-left:4px;vertical-align:1px}',
    '#' + POP_ID + ' .cd-cred{font-weight:700;color:#159447;white-space:nowrap}',
    '#' + POP_ID + ' .cd-cred.hot{color:#c2410c}',
    '#' + POP_ID + ' .cd-bar{display:flex;height:14px;border-radius:4px;overflow:hidden;background:var(--wb-bg-tertiary,#f2f2f2);min-width:2px}',
    '#' + POP_ID + ' .cd-bar span{display:block;height:100%}',
    '#' + POP_ID + ' .cd-dist{font-size:10px;color:var(--wb-color-text-secondary,#666);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '#' + POP_ID + ' .cd-dist b{color:var(--wb-color-text-primary,#1f1f1f);font-weight:500}',
    '#' + POP_ID + ' .cd-foot{border-top:1px solid var(--wb-border-subtle,#f0f0f0);padding-top:8px;margin-top:8px;font-size:10px;color:var(--wb-color-text-secondary,#666);display:flex;justify-content:space-between;gap:10px}',
    '#' + POP_ID + ' .cd-scroll{max-height:320px;overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:var(--wb-icon-secondary,#999) transparent}',
    '#' + POP_ID + ' .cd-empty{padding:12px 0;color:var(--wb-color-text-secondary,#666)}'
  ].join('\n');
  document.head.appendChild(css);

  function pad(n) { return String(n).padStart(2, '0'); }
  function cell(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = String(text);
    return n;
  }
  function startOfDay(ts) {
    var d = new Date(ts);
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  }

  function collect(now) {
    var accounts = (typeof state !== 'undefined' && Array.isArray(state.accounts)) ? state.accounts : [];
    var usable = accounts.filter(function (a) { return a && !a.creditUnlimited && !a.creditExpired; });
    var colorOf = {};
    usable.forEach(function (a, i) {
      colorOf[String(a.uid || a.nickname || i)] = COLORS[i % COLORS.length];
    });

    var buckets = {};
    var rawCount = 0;
    usable.forEach(function (account) {
      var uid = String(account.uid || account.nickname || '');
      var name = String(account.nickname || account.uid || '未命名账号');
      var color = colorOf[uid] || COLORS[0];
      var segs = Array.isArray(account.creditSegments) ? account.creditSegments : [];
      segs.forEach(function (s) {
        var remain = Number(s && s.remaining);
        if (!isFinite(remain) || remain <= 0) return;
        var ts = (s.expiresAt === null || s.expiresAt === undefined || !isFinite(Number(s.expiresAt))) ? null : Number(s.expiresAt);
        var key, sortKey;
        if (ts === null) {
          key = '__unknown__'; sortKey = Infinity;
        } else {
          var d = new Date(ts);
          key = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
          sortKey = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
        }
        var b = buckets[key];
        if (!b) { b = buckets[key] = { key: key, sortKey: sortKey, total: 0, parts: {}, order: [] }; }
        b.total += remain;
        if (!b.parts[uid]) { b.parts[uid] = { name: name, amount: 0, color: color }; b.order.push(uid); }
        b.parts[uid].amount += remain;
        rawCount++;
      });
    });

    var rows = Object.keys(buckets).map(function (k) { return buckets[k]; });
    rows.sort(function (a, b) {
      if (a.sortKey === Infinity) return 1;
      if (b.sortKey === Infinity) return -1;
      return a.sortKey - b.sortKey;
    });
    var todayStart = startOfDay(now);
    rows.forEach(function (r) {
      r.days = r.sortKey === Infinity ? null : Math.round((r.sortKey - todayStart) / 86400000);
      r.partsList = r.order.map(function (uid) { return r.parts[uid]; });
      r.partsList.sort(function (a, b) { return b.amount - a.amount; });
      r.top = r.partsList[0] || null;
    });
    return { rows: rows, rawCount: rawCount };
  }

  function render() {
    var now = Date.now();
    var data = collect(now);
    var rows = data.rows;

    popup.innerHTML = '';
    var head = document.createElement('div');
    head.className = 'wbs-credit-summary-head';
    head.appendChild(cell('strong', '', '积分到期明细'));
    head.appendChild(cell('span', '', '按到期日合并 · 共 ' + rows.length + ' 天'));
    popup.appendChild(head);

    if (!rows.length) {
      popup.appendChild(cell('div', 'cd-empty', '暂无可展示的到期积分'));
    } else {
      var scroll = document.createElement('div');
      scroll.className = 'cd-scroll';
      var table = document.createElement('table');
      table.className = 'cd-tbl';

      var thead = document.createElement('thead');
      var htr = document.createElement('tr');
      htr.appendChild(cell('th', '', '到期日'));
      htr.appendChild(cell('th', 'num', '到期积分'));
      htr.appendChild(cell('th', 'chart', '账号占比'));
      thead.appendChild(htr);
      table.appendChild(thead);

      var tbody = document.createElement('tbody');
      var thisYear = String(new Date().getFullYear());
      rows.forEach(function (r) {
        var tr = document.createElement('tr');

        var dateLabel;
        if (r.key === '__unknown__') dateLabel = '未知';
        else if (r.key.slice(0, 4) === thisYear) dateLabel = r.key.slice(5);          // 今年：MM-DD
        else dateLabel = r.key.slice(2, 4) + '-' + r.key.slice(5);                     // 跨年：YY-MM-DD
        var dateTd = cell('td', 'cd-date', dateLabel);
        if (r.days !== null) {
          var dayTxt = r.days < 0 ? '已到期' : (r.days === 0 ? '今天' : r.days + '天');
          dateTd.appendChild(cell('span', 'cd-days', dayTxt));
        }
        tr.appendChild(dateTd);

        var amtTd = cell('td', 'num cd-cred' + (r.total >= HOT_THRESHOLD ? ' hot' : ''), r.total.toFixed(2));
        tr.appendChild(amtTd);

        var chartTd = cell('td', 'chart');
        var bar = cell('div', 'cd-bar');
        if (r.key === '__unknown__' || !(r.total > 0)) {
          var unknownSeg = cell('span', '');
          unknownSeg.style.width = '100%';
          unknownSeg.style.background = 'var(--wb-border-strong,#ddd)';
          bar.appendChild(unknownSeg);
        } else {
          r.partsList.forEach(function (p) {
            var seg = cell('span', '');
            var pct = p.amount / r.total * 100;
            seg.style.width = pct.toFixed(2) + '%';
            seg.style.background = p.color;
            seg.title = p.name + ' ' + p.amount.toFixed(2);
            bar.appendChild(seg);
          });
        }
        chartTd.appendChild(bar);

        var dist = cell('div', 'cd-dist');
        r.partsList.forEach(function (p, i) {
          if (i) dist.appendChild(document.createTextNode(' · '));
          var label = p.name + ' ' + p.amount.toFixed(2);
          if (p === r.top) dist.appendChild(cell('b', '', label));
          else dist.appendChild(document.createTextNode(label));
        });
        chartTd.appendChild(dist);
        tr.appendChild(chartTd);

        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      scroll.appendChild(table);
      popup.appendChild(scroll);
    }

    var foot = document.createElement('div');
    foot.className = 'cd-foot';
    foot.appendChild(cell('span', '', '共 ' + rows.length + ' 个到期日（原 ' + data.rawCount + ' 笔）'));
    foot.appendChild(cell('span', '', '点击按钮关闭'));
    popup.appendChild(foot);

    applyI18n(popup);
    popup.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    var rect = button.getBoundingClientRect();
    popup.style.left = Math.max(8, Math.min(rect.left - 60, window.innerWidth - popup.offsetWidth - 8)) + 'px';
    popup.style.top = (rect.bottom + 6) + 'px';
    popup.style.maxHeight = Math.max(80, window.innerHeight - rect.bottom - 14) + 'px';
  }

  function hide() {
    popup.hidden = true;
    button.setAttribute('aria-expanded', 'false');
  }
  function toggle() { if (popup.hidden) render(); else hide(); }

  listen(button, 'click', function (event) { event.stopPropagation(); toggle(); });
  listen(button, 'keydown', function (event) {
    if (event.key === 'Escape') { event.stopPropagation(); hide(); }
  });
  listen(popup, 'click', function (event) { event.stopPropagation(); });
  listen(document, 'click', function () { if (!popup.hidden) hide(); });
  listen(popup, 'wheel', function (event) { event.stopPropagation(); });

  registerDisposer(function () {
    if (button && button.parentNode) button.parentNode.removeChild(button);
    if (popup && popup.parentNode) popup.parentNode.removeChild(popup);
    if (css && css.parentNode) css.parentNode.removeChild(css);
  });
})();
