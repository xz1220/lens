/* lens 看板前端 — 墨色冷调 v3.1
   三栏看板：左筛选 | 中信息流 | 右详情（评判 → 讨论 → 灵感 闭环）。
   全中文界面，发给 server.py 的值用 DB 英文枚举。零运行时依赖。 */

'use strict';

// ---------------- 小工具 ----------------
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const kid of kids) {
    if (kid == null || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return n;
};
const numfmt = (n) => String(n ?? 0).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
const dDate = (iso) => (iso || '').slice(0, 10);                       // 2026-06-08
const dStamp = (iso) => (iso ? iso.slice(5, 16).replace('T', ' ') : '—'); // 06-08 08:13

async function api(url, opts) {
  const r = await fetch(url, opts);
  let j = {};
  try { j = await r.json(); } catch { /* non-json */ }
  if (!r.ok) throw new Error(j.error || `${r.status} ${r.statusText}`);
  return j;
}
const POST = (body) => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

let _toastTimer;
function toast(msg, isErr = false) {
  let t = $('.toast');
  if (!t) { t = el('div', { class: 'toast' }); document.body.append(t); }
  t.textContent = msg;
  t.classList.toggle('err', isErr);
  // reflow so re-show animates
  void t.offsetWidth;
  t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), 1900);
}

// ---------------- 中文 ↔ DB 枚举映射 ----------------
const TIER = {
  P0:   { label: '官方', color: 'var(--tier-official)' },
  P1:   { label: '研究', color: 'var(--tier-research)' },
  P2:   { label: '社区', color: 'var(--tier-community)' },
  heat: { label: '热门', color: 'var(--tier-hot)' },
};
const STATUS = [
  { v: 'captured', label: '新进' },
  { v: 'reviewed', label: '已读' },
  { v: 'promoted', label: '收藏' },
  { v: 'ignored',  label: '忽略' },
];
const STATUS_LABEL = Object.fromEntries(STATUS.map((s) => [s.v, s.label]));
const SORT_LABEL = { date: '最新在上', score: '高分在上', fetched: '新抓在上' };

const ICON = {
  search: '<svg class="ic" width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.4"/><line x1="10.8" y1="10.8" x2="15" y2="15" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
};

// ---------------- 状态 ----------------
const filters = { status: '', tier: '', source: '', min_score: '', q: '', sort: 'date' };
let statsCache = { total: 0, by_status: {}, by_tier: {}, last_collect: null };
let sourcesMap = {};      // source_key -> {name, desc, tier, ...}，用于来源说明
let itemsCache = [];
let current = null;       // 当前选中的（已保存的）item 行
let selectedIndex = -1;
let draft = null;         // { score, tags:[], status, comment } 暂存的评判改动
let discussion = null;    // { path, content } 本次生成的讨论稿
const recentIdeas = [];   // 本会话记下的灵感（接口无列表，故本地维护）
const backfills = {};     // path -> 本会话已回填次数（接口不返回，故本地累计）

const railEl = $('#rail');
const listEl = $('#list');
const detailEl = $('#detail');

// ================= 顶栏统计 =================
function renderStats() {
  const s = statsCache;
  const bs = s.by_status || {};
  const seg = (label, val) =>
    `${label ? label + ' ' : ''}<b class="num">${val}</b>${label ? '' : ' 条'}`;
  const parts = [
    seg('', numfmt(s.total)),
    seg('新进', bs.captured || 0),
    seg('已读', bs.reviewed || 0),
    seg('收藏', bs.promoted || 0),
    seg('忽略', bs.ignored || 0),
    `抓取 <b class="num">${dStamp(s.last_collect)}</b>`,
  ];
  $('#stats').innerHTML = parts.join('<span class="sep">·</span>');
}

async function loadStats() {
  statsCache = await api('/api/stats');
  renderStats();
  updateRailCounts();
}

// 写操作成功后刷新看板（统计 + 列表）。这是次级刷新：即便失败也只提示、
// 绝不抛到 console，主写操作已经落库。
async function refreshBoard() {
  try { await loadStats(); } catch (e) { toast('统计刷新失败：' + e.message, true); }
  await loadItems(); // 自带 try/catch：失败会提示并保留旧列表
}

// ================= 左栏 rail =================
function buildRail() {
  railEl.innerHTML = '';

  // 搜索
  const search = el('div', { class: 'search' });
  search.innerHTML = ICON.search;
  const q = el('input', { type: 'search', id: 'q', placeholder: '搜索标题 / 摘要', autocomplete: 'off' });
  let qTimer;
  q.addEventListener('input', () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { filters.q = q.value.trim(); loadItems(); }, 240);
  });
  search.append(q);
  railEl.append(search);

  // 状态导航
  railEl.append(fgroup('状态', (() => {
    const nav = el('nav', { class: 'statusnav', id: 'statusnav' });
    const rows = [{ v: '', label: '全部' }, ...STATUS];
    for (const r of rows) {
      const row = el('button', {
        class: 'navrow' + (filters.status === r.v ? ' on' : ''),
        'data-status': r.v, type: 'button',
        onclick: () => { filters.status = r.v; syncRail(); loadItems(); },
      },
        el('span', { text: r.label }),
        el('span', { class: 'num', 'data-count': r.v || '__total' }, '0'),
      );
      nav.append(row);
    }
    return nav;
  })()));

  // 分层
  railEl.append(fgroup('分层', chipRow(
    [{ v: '', label: '全部' }, ...Object.entries(TIER).map(([v, t]) => ({ v, label: t.label, color: t.color }))],
    () => filters.tier,
    (v) => { filters.tier = v; syncRail(); loadItems(); },
    'tier',
  )));

  // 来源（选中某个源时，下方小字显示该源的说明）
  const sel = el('select', { class: 'select', id: 'sourcesel' },
    el('option', { value: '' }, '全部来源'));
  const srcDesc = el('div', { class: 'source-desc', id: 'source-desc', hidden: true });
  sel.addEventListener('change', () => { filters.source = sel.value; updateSourceDesc(); loadItems(); });
  railEl.append(fgroup('来源', el('div', { class: 'source-wrap' }, sel, srcDesc)));

  // 最低分
  railEl.append(fgroup('最低分', chipRow(
    [{ v: '', label: '不限' }, ...[1, 2, 3, 4, 5].map((n) => ({ v: String(n), label: '★' + n, mono: true }))],
    () => filters.min_score,
    (v) => { filters.min_score = v; syncRail(); loadItems(); },
  )));

  // 排序
  railEl.append(fgroup('排序', chipRow(
    [{ v: 'date', label: '最新' }, { v: 'score', label: '评分' }, { v: 'fetched', label: '抓取' }],
    () => filters.sort,
    (v) => { filters.sort = v; syncRail(); loadItems(); },
  )));
}

function fgroup(label, body) {
  return el('div', { class: 'fgroup' }, el('div', { class: 'flabel', text: label }), body);
}

function chipRow(opts, getActive, onPick, kind) {
  const wrap = el('div', { class: 'chips', 'data-chiprow': kind || 'plain' });
  for (const o of opts) {
    const chip = el('button', {
      class: 'chip' + (o.mono ? ' num-chip' : '') + (getActive() === o.v ? ' on' : ''),
      'data-v': o.v, type: 'button',
      onclick: () => onPick(o.v),
    });
    if (o.color) chip.append(el('span', { class: 'tdot', style: `background:${o.color}` }));
    chip.append(document.createTextNode(o.label));
    wrap.append(chip);
  }
  return wrap;
}

// 重新同步 rail 上所有 chip / nav 的选中态（不重建 DOM）
function syncRail() {
  railEl.querySelectorAll('.navrow').forEach((n) =>
    n.classList.toggle('on', n.dataset.status === filters.status));
  railEl.querySelectorAll('[data-chiprow="tier"] .chip').forEach((c) =>
    c.classList.toggle('on', c.dataset.v === filters.tier));
  // min_score + sort 这两组 chip 用值匹配（sort 值是 date/score/fetched）
  railEl.querySelectorAll('.chips').forEach((row) => {
    if (row.dataset.chiprow === 'tier') return;
    row.querySelectorAll('.chip').forEach((c) => {
      const v = c.dataset.v;
      if (['date', 'score', 'fetched'].includes(v)) c.classList.toggle('on', v === filters.sort);
      else c.classList.toggle('on', v === filters.min_score);
    });
  });
}

function updateRailCounts() {
  const bs = statsCache.by_status || {};
  railEl.querySelectorAll('.navrow .num').forEach((n) => {
    const key = n.dataset.count;
    n.textContent = numfmt(key === '__total' ? statsCache.total : (bs[key] || 0));
  });
}

async function loadSources() {
  const srcs = await api('/api/sources');
  sourcesMap = {};
  for (const s of srcs) sourcesMap[s.key] = s;
  const sel = $('#sourcesel');
  if (!sel) return;
  for (const s of srcs) {
    const t = TIER[s.tier];
    const tag = t ? t.label : (s.tier || '');
    // option 自带 title 悬浮，鼠标停在下拉项上也能看说明
    sel.append(el('option', { value: s.key, title: s.desc || '' }, `${tag ? tag + ' · ' : ''}${s.name || s.key}`));
  }
  updateSourceDesc();
}

// 来源筛选选中某个源时，在下拉框下方显示该源的说明（小字、muted）
function updateSourceDesc() {
  const box = $('#source-desc');
  if (!box) return;
  const s = sourcesMap[filters.source];
  if (filters.source && s && s.desc) {
    box.textContent = s.desc;
    box.hidden = false;
  } else {
    box.textContent = '';
    box.hidden = true;
  }
}

// ================= 中栏 列表 =================
function buildListShell() {
  listEl.innerHTML = '';
  const head = el('div', { class: 'list-head' },
    el('span', { class: 'list-title', text: '信息流' }),
    el('span', { class: 'list-count', id: 'list-count' }),
    el('span', { class: 'list-sort', id: 'list-sort' }),
  );
  listEl.append(head, el('div', { class: 'list-scroll', id: 'list-scroll' }));
}

function queryString() {
  const p = new URLSearchParams();
  for (const k of ['status', 'tier', 'source', 'min_score', 'q', 'sort']) {
    if (filters[k]) p.set(k, filters[k]);
  }
  return p.toString();
}

async function loadItems() {
  listEl.classList.add('loading');
  const scroll0 = $('#list-scroll');
  if (scroll0 && !itemsCache.length) scroll0.innerHTML = '<div class="list-loading">载入中…</div>';
  let data;
  try {
    data = await api('/api/items?' + queryString());
  } catch (e) {
    listEl.classList.remove('loading');
    const sc = $('#list-scroll');
    if (sc && !itemsCache.length) { sc.innerHTML = ''; sc.append(el('div', { class: 'list-empty' }, '列表加载失败：' + e.message)); }
    toast('列表加载失败：' + e.message, true);
    return;
  }
  listEl.classList.remove('loading');
  itemsCache = data.items || [];
  renderListRows();

  // 保持选中（按 id）。若当前选中项被筛选/搜索/排序后排除出列表，必须 deselect 回空态：
  // 否则详情区会继续显示这条已不在列表里的 item，且 保存/讨论/灵感 等写操作仍会打到这个隐藏 id。
  if (current) {
    const idx = itemsCache.findIndex((x) => x.id === current.id);
    if (idx >= 0) { selectedIndex = idx; highlightRow(); }
    else { deselect(); }
  }
}

function renderListRows() {
  const scroll = $('#list-scroll');
  $('#list-count').textContent = `${numfmt(itemsCache.length)} 条`;
  $('#list-sort').textContent = SORT_LABEL[filters.sort] || '';
  scroll.innerHTML = '';

  if (!itemsCache.length) {
    scroll.append(el('div', { class: 'list-empty' }, '没有符合条件的信息。', el('br'), '调整筛选或清空搜索试试。'));
    return;
  }
  itemsCache.forEach((it, i) => scroll.append(rowEl(it, i)));
  highlightRow();
}

function tierBadge(tier) {
  const t = TIER[tier];
  if (!t) return el('span');
  return el('span', { class: 'tier', style: `color:${t.color}` },
    el('span', { class: 'dot', style: `background:${t.color}` }),
    t.label,
  );
}

function rowEl(it, i) {
  const ignored = it.status === 'ignored';
  const row = el('div', {
    class: 'row' + (ignored ? ' ig' : ''),
    'data-id': it.id,
    onclick: () => selectItem(it, i),
  });

  const top = el('div', { class: 'row-top' },
    tierBadge(it.tier),
    el('span', { class: 'src', text: it.source_name || it.source_key || '' }),
    el('span', { class: 'row-date num', text: dDate(it.published_at) || '—' }),
  );

  const title = el('div', { class: 'row-title', text: it.title || '(无标题)' });

  const bot = el('div', { class: 'row-bot' });
  if (it.score != null) {
    bot.append(el('span', { class: 'score-tag' },
      el('span', { class: 'star', text: '★' }),
      el('span', { class: 'num', text: String(it.score) })));
  }
  if (it.thread_count) {
    bot.append(el('span', { class: 'pill thread', text: `${it.thread_count} 讨论` }));
  }
  if (it.status === 'promoted') {
    bot.append(el('span', { class: 'pill saved', text: '收藏' }));
  }

  row.append(top, title);
  if (bot.children.length) row.append(bot);
  return row;
}

function highlightRow() {
  const scroll = $('#list-scroll');
  if (!scroll) return;
  scroll.querySelectorAll('.row').forEach((r) =>
    r.classList.toggle('sel', current != null && r.dataset.id === current.id));
}

// ================= 右栏 详情 =================
function selectItem(it, idx) {
  current = it;
  selectedIndex = idx;
  draft = {
    score: it.score == null ? null : Number(it.score),
    tags: (it.tags || '').split(',').map((t) => t.trim()).filter(Boolean),
    status: it.status,
    comment: it.comment || '',
  };
  discussion = null;
  renderDetail();
  highlightRow();
  const rowNode = $(`#list-scroll .row[data-id="${it.id}"]`);
  if (rowNode) rowNode.scrollIntoView({ block: 'nearest' });
}

function deselect() {
  current = null; selectedIndex = -1; draft = null; discussion = null;
  renderDetail();
  highlightRow();
}

function renderDetail() {
  detailEl.innerHTML = '';
  if (!current) { detailEl.append(emptyState()); return; }
  const it = current;

  // —— 详情头 ——
  const srcDesc = (sourcesMap[it.source_key] || {}).desc || '';
  const head = el('div', { class: 'detail-head' },
    el('div', { class: 'dh-meta' },
      tierBadge(it.tier),
      // 来源名旁的说明：有则虚线下划线 + hover 悬浮（title）
      el('span', { class: 'src' + (srcDesc ? ' has-desc' : ''), title: srcDesc || null, text: it.source_name || it.source_key || '' }),
      el('span', { class: 'dh-date num', text: dDate(it.published_at) || '—' }),
    ),
    el('a', { class: 'open-link', href: it.url || '#', target: '_blank', rel: 'noopener' }, '打开原文 ↗'),
  );

  const title = el('h1', { class: 'detail-title', text: it.title || '(无标题)' });

  const summary = it.summary
    ? el('div', { class: 'summary', text: it.summary })
    : el('div', { class: 'summary faint', text: '（这条没有摘要，打开原文看全文。）' });

  detailEl.append(head, title, summary, triageBlock(), el('hr', { class: 'rule' }),
    discussionBlock(), el('hr', { class: 'rule' }), ideaBlock());

  // 节点挂载到 detailEl 后再填充动态子视图：标签 chip / 讨论文件 chip /
  // 讨论稿框 / 最近灵感。否则这些 render 在挂载前跑，#tagchips / #thread-chips /
  // #recent-ideas 的全局查找会拿到 null，已有标签和讨论文件就会在初次选中时丢失。
  renderTagChips();
  renderThreadChips();
  if (discussion) renderDiscussBox();
  renderRecentIdeas();
}

function emptyState() {
  const wrap = el('div', { class: 'empty' });
  wrap.innerHTML = `
    <svg class="aperture" width="60" height="60" viewBox="0 0 60 60" fill="none" aria-hidden="true">
      <circle cx="30" cy="30" r="21" stroke="var(--strong)" stroke-width="1.5"/>
      <circle cx="30" cy="30" r="13" stroke="var(--hair)" stroke-width="1.2"/>
      <circle cx="30" cy="30" r="4.5" fill="var(--accent)"/>
    </svg>`;
  wrap.append(
    el('div', { class: 'empty-title', text: '选一条信息开始' }),
    el('div', { class: 'empty-hint', text: '左侧筛选，中间挑一条，在这里评判、和 AI 发散讨论、沉淀灵感。' }),
    el('div', { class: 'empty-latin', text: 'a reading & review desk' }),
  );
  return wrap;
}

// —— 评判区 ——
function triageBlock() {
  const block = el('section', { class: 'block' });

  // 评分 0–5 分段控件
  const seg = el('div', { class: 'seg', id: 'seg' });
  for (let n = 0; n <= 5; n++) {
    seg.append(el('button', {
      class: 'seg-box' + ((draft.score ?? 0) === n ? ' on' : ''),
      'data-n': n, type: 'button',
      onclick: () => setScore(n === 0 ? null : n),
    }, String(n)));
  }
  seg.append(el('span', { class: 'seg-val num', id: 'seg-val', text: `${draft.score ?? '—'} / 5` }));
  block.append(field('评分', seg));

  // 标签
  const tagbox = el('div', { class: 'tagbox', id: 'tagbox' });
  const chips = el('span', { class: 'tagchips', id: 'tagchips' });
  const tagInput = el('input', { class: 'taginput', id: 'taginput', placeholder: '添加标签…', autocomplete: 'off' });
  tagInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      addTag(tagInput.value);
      tagInput.value = '';
    } else if (e.key === 'Backspace' && !tagInput.value && draft.tags.length) {
      draft.tags.pop(); renderTagChips();
    }
  });
  tagbox.append(chips, tagInput);
  block.append(field('标签', tagbox));
  // chip 由 renderDetail 在挂载后填充

  // 状态 pill
  const pills = el('div', { class: 'pills', id: 'status-pills' });
  for (const s of STATUS) {
    pills.append(el('button', {
      class: 'spill' + (draft.status === s.v ? ' on' : ''),
      'data-v': s.v, type: 'button',
      onclick: () => { draft.status = s.v; updateStatusPills(); },
    }, s.label));
  }
  block.append(field('状态', pills));

  // 我的评注
  const comment = el('textarea', { class: 'input', id: 'comment', placeholder: '写下你对这条的判断 / 想问 AI 的角度…' });
  comment.value = draft.comment;
  comment.addEventListener('input', () => { draft.comment = comment.value; });
  block.append(field('我的评注', comment));

  // 保存
  const save = el('button', { class: 'btn btn-primary', id: 'save', type: 'button', onclick: saveAndNext }, '保存评判');
  block.append(el('div', { class: 'save-row' }, save, el('span', { class: 'hint mono', text: '⌘S 保存并看下一条' })));

  return block;
}

function field(label, body) {
  return el('div', { class: 'field' }, el('div', { class: 'flabel', text: label }), body);
}

function setScore(n) {
  draft.score = n;
  const seg = $('#seg');
  if (!seg) return;
  // null（清空）映射到 0 号方块，使「未评」也有明确的选中态
  seg.querySelectorAll('.seg-box').forEach((b) =>
    b.classList.toggle('on', Number(b.dataset.n) === (n ?? 0)));
  $('#seg-val').textContent = `${n ?? '—'} / 5`;
}

function addTag(raw) {
  for (let t of (raw || '').split(',')) {
    t = t.trim().toLowerCase();
    if (t && !draft.tags.includes(t)) draft.tags.push(t);
  }
  renderTagChips();
}

function renderTagChips() {
  const box = $('#tagchips');
  if (!box) return;
  box.innerHTML = '';
  draft.tags.forEach((t, i) => {
    box.append(el('span', { class: 'tagchip' }, '#' + t,
      el('button', { type: 'button', title: '删除', onclick: () => { draft.tags.splice(i, 1); renderTagChips(); } }, '×')));
  });
}

function updateStatusPills() {
  const pills = $('#status-pills');
  if (!pills) return;
  pills.querySelectorAll('.spill').forEach((p) =>
    p.classList.toggle('on', p.dataset.v === draft.status));
}

function collectChanges() {
  const c = {};
  const saved = current;
  if (draft.score !== (saved.score == null ? null : Number(saved.score))) c.score = draft.score;
  const t = draft.tags.join(',');
  if (t !== (saved.tags || '')) c.tags = t;
  if (draft.status !== saved.status) c.status = draft.status;
  if ((draft.comment || '') !== (saved.comment || '')) c.comment = draft.comment;
  return c;
}

async function saveAndNext() {
  if (!current) return;
  const changes = collectChanges();
  const curId = current.id;      // refreshBoard 可能因筛选把 current 清空，先存下 id
  const nextId = itemsCache[selectedIndex + 1]?.id || null;

  if (Object.keys(changes).length) {
    try {
      await api(`/api/items/${curId}`, POST(changes));
    } catch (e) { toast('保存失败：' + e.message, true); return; }
    toast('已保存');
    await refreshBoard();         // 刷新列表行 + 计数（loadItems 可能已把被筛掉的 current 清空）
    if (nextId && itemsCache.some((x) => x.id === nextId)) selectById(nextId);
    else if (itemsCache.some((x) => x.id === curId)) selectById(curId);
    else deselect();
  } else {
    // 无改动：直接看下一条
    if (nextId) { selectById(nextId); }
    else toast('已是最后一条');
  }
}

function selectById(id) {
  const idx = itemsCache.findIndex((x) => x.id === id);
  if (idx >= 0) selectItem(itemsCache[idx], idx);
}

// —— 讨论区 ——
function discussionBlock() {
  const block = el('section', { class: 'block' });
  block.append(el('h3', { class: 'block-title serif', text: '讨论' }));

  const genBtn = el('button', { class: 'btn btn-outline', type: 'button', onclick: genDiscussion }, '生成讨论稿');
  block.append(el('div', { class: 'row-actions' }, genBtn));

  // 已有讨论文件 chip（由 renderDetail 在挂载后填充）
  const chips = el('div', { class: 'thread-chips', id: 'thread-chips' });
  block.append(chips);

  // 生成后的讨论稿框（动态，挂载后由 renderDetail 按需填充）
  block.append(el('div', { id: 'discuss-box' }));

  return block;
}

function threadBasename(p) { return (p || '').split('/').pop() || p; }

function renderThreadChips() {
  const box = $('#thread-chips');
  if (!box) return;
  box.innerHTML = '';
  const path = (discussion && discussion.path) || current.last_thread;
  if (!path) return;
  const n = backfills[path] || 0;
  box.append(el('span', { class: 'file-chip' },
    el('span', { class: 'fname', text: threadBasename(path) }),
    el('span', { class: 'ftimes', text: `· 已回填 ${n} 次` }),
  ));
}

async function genDiscussion() {
  if (!current) return;
  const curId = current.id;      // refreshBoard 后 captured→reviewed 可能把 current 筛掉
  let res;
  try {
    res = await api(`/api/items/${curId}/discussion`, POST({ comment: draft.comment }));
  } catch (e) { toast('生成失败：' + e.message, true); return; }
  discussion = { path: res.path, content: res.content };
  toast('已生成');

  // 后端会把 comment 落库、captured→reviewed —— 本地同步以免重复保存误判
  current.comment = draft.comment || current.comment;
  if (current.status === 'captured') { current.status = 'reviewed'; draft.status = 'reviewed'; updateStatusPills(); }

  renderDiscussBox();
  await refreshBoard();   // 刷新列表（讨论数 / 状态）；若状态筛选把这条筛掉，loadItems 已 deselect 回空态
  // current 仍在（未被筛掉）才同步整行并刷新 chip；否则它已是 null，不能再访问
  if (current) {
    const fresh = itemsCache.find((x) => x.id === curId);
    if (fresh) current = fresh;
    renderThreadChips();
  }
}

function renderDiscussBox() {
  const box = $('#discuss-box');
  if (!box) return;
  box.innerHTML = '';

  const paper = el('div', { class: 'paper' });
  const copyBtn = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, '复制');
  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(discussion.content);
      toast('已复制');
    } catch { toast('复制失败，可手动选择文本', true); }
  });
  paper.append(
    el('div', { class: 'paper-head' }, el('span', { class: 'fname', text: discussion.path }), copyBtn),
    el('pre', { class: 'paper-body', text: discussion.content }),
  );

  const backfill = el('textarea', { class: 'input', id: 'backfill', placeholder: '把和 AI 讨论后的结论 / 过程思考贴回这里，追加进讨论文件…' });
  const appendBtn = el('button', { class: 'btn btn-primary', type: 'button' }, '回填到讨论文件');
  appendBtn.addEventListener('click', async () => {
    const content = backfill.value.trim();
    if (!content) { toast('先写点回填内容', true); return; }
    try {
      await api('/api/discussion/append', POST({ path: discussion.path, content }));
    } catch (e) { toast('回填失败：' + e.message, true); return; }
    backfill.value = '';
    backfills[discussion.path] = (backfills[discussion.path] || 0) + 1;
    renderThreadChips();
    toast('已回填');
  });

  box.append(
    paper,
    el('div', { class: 'field', style: 'margin-top:14px' },
      el('div', { class: 'flabel', text: '回填讨论结论' }), backfill),
    el('div', { class: 'row-actions', style: 'margin-top:12px' }, appendBtn),
  );
}

// —— 灵感区 ——
function ideaBlock() {
  const block = el('section', { class: 'block' });
  block.append(el('h3', { class: 'block-title serif', text: '灵感' }));

  const titleI = el('input', { class: 'input', id: 'idea-title', placeholder: '灵感标题（可空）', autocomplete: 'off' });
  const bodyI = el('textarea', { class: 'input', id: 'idea-body', placeholder: '念头一闪，立刻记下…' });

  const saveBtn = el('button', { class: 'btn btn-primary', type: 'button', onclick: () => saveIdea(false) }, '记为灵感');
  const parkBtn = el('button', { class: 'btn btn-ghost', type: 'button', onclick: () => saveIdea(true) }, '先放着');

  block.append(titleI, bodyI, el('div', { class: 'row-actions' }, saveBtn, parkBtn));

  block.append(el('span', { class: 'link-chip' }, '触发自 ',
    el('span', { class: 'lc-title', text: current.title || '(无标题)' })));

  // 最近灵感（本会话）
  const recent = el('div', { class: 'recent' }, el('div', { class: 'flabel', text: '最近灵感' }));
  const ul = el('ul', { id: 'recent-ideas' });
  recent.append(ul);
  block.append(recent);
  // 列表由 renderDetail 在挂载后填充（含空态）

  return block;
}

function renderRecentIdeas() {
  const ul = $('#recent-ideas');
  if (!ul) return;
  ul.innerHTML = '';
  if (!recentIdeas.length) {
    ul.append(el('li', { class: 'recent-empty', text: '本次还没记下灵感。' }));
    return;
  }
  for (const r of recentIdeas.slice(0, 6)) {
    ul.append(el('li', {},
      el('span', { text: r.title || '(未命名)' }),
      r.parked ? el('span', { class: 'ri-park', text: '· 暂存' }) : null,
      el('span', { class: 'ri-path', text: threadBasename(r.path) }),
    ));
  }
}

async function saveIdea(parked) {
  const body = $('#idea-body').value.trim();
  if (!body) { toast('写点灵感再记', true); return; }
  const title = $('#idea-title').value.trim();
  let res;
  try {
    res = await api('/api/ideas', POST({ title, body, parked, item_id: current ? current.id : null }));
  } catch (e) { toast('记录失败：' + e.message, true); return; }
  $('#idea-title').value = ''; $('#idea-body').value = '';
  recentIdeas.unshift({ title, parked, path: res.path });
  renderRecentIdeas();
  toast(parked ? '丢进暂存' : '已记');
}

// ================= 键盘流 =================
function move(delta) {
  if (!itemsCache.length) return;
  let idx = selectedIndex < 0 ? (delta > 0 ? 0 : itemsCache.length - 1) : selectedIndex + delta;
  idx = Math.max(0, Math.min(itemsCache.length - 1, idx));
  selectItem(itemsCache[idx], idx);
}

document.addEventListener('keydown', (e) => {
  const ae = document.activeElement;
  const editing = ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT');

  // ⌘S / Ctrl+S 始终保存（即便在输入框里）
  if ((e.metaKey || e.ctrlKey) && (e.key === 's' || e.key === 'S')) {
    e.preventDefault(); saveAndNext(); return;
  }
  if (editing) {
    if (e.key === 'Escape') ae.blur();
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  switch (e.key) {
    case 'j': case 'ArrowDown': e.preventDefault(); move(1); break;
    case 'k': case 'ArrowUp': e.preventDefault(); move(-1); break;
    case 'Escape': deselect(); break;
    case 's': case 'S': e.preventDefault(); saveAndNext(); break;
    case 'g': case 'G': if (current) genDiscussion(); break;
    case 'o': case 'O': if (current && current.url) window.open(current.url, '_blank', 'noopener'); break;
    case 'i': case 'I': if (current) { e.preventDefault(); $('#idea-body')?.focus(); } break;
    case '0': if (current) setScore(null); break;
    case '1': case '2': case '3': case '4': case '5':
      if (current) setScore(Number(e.key)); break;
    default: break;
  }
});

// ================= 启动 =================
(async function init() {
  buildRail();
  buildListShell();
  renderDetail(); // 空态
  try {
    await loadSources();
    await loadStats();
    await loadItems();
  } catch (e) {
    $('#list-scroll') && ($('#list-scroll').innerHTML = '');
    toast('启动失败：' + e.message, true);
    const scroll = $('#list-scroll');
    if (scroll) scroll.append(el('div', { class: 'list-empty' },
      '启动失败：' + e.message, el('br'), '先跑 python3 collect.py 填充看板。'));
  }
})();
