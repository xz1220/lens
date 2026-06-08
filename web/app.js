// PLACEHOLDER frontend logic. Talks to every server.py endpoint so the whole
// loop (collect → triage → discuss → store → idea) is usable end-to-end.
// The look & interaction model is the next design pass — see CLAUDE.md.

const $ = (s) => document.querySelector(s);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};
const toast = (msg) => {
  let t = $(".toast") || Object.assign(document.body.appendChild(document.createElement("div")), { className: "toast" });
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 1800);
};
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

let current = null;

async function loadStats() {
  const s = await api("/api/stats");
  $("#stats").textContent =
    `${s.total} items · ` +
    Object.entries(s.by_status || {}).map(([k, v]) => `${k} ${v}`).join(" / ") +
    (s.last_collect ? ` · 抓取 ${s.last_collect.slice(0, 16).replace("T", " ")}` : "");
}

async function loadSources() {
  const srcs = await api("/api/sources");
  const sel = $("#f-source");
  for (const s of srcs) {
    const o = document.createElement("option");
    o.value = s.key; o.textContent = `${s.tier || "--"} ${s.name}`;
    sel.appendChild(o);
  }
}

async function loadItems() {
  const p = new URLSearchParams();
  const map = { status: "#f-status", tier: "#f-tier", source: "#f-source", min_score: "#f-minscore", q: "#f-q", sort: "#f-sort" };
  for (const [k, sel] of Object.entries(map)) { const v = $(sel).value; if (v) p.set(k, v); }
  const { items } = await api("/api/items?" + p.toString());
  const list = $("#list");
  list.innerHTML = items.length ? "" : '<div class="card muted">没有 item。先跑 <code>python3 collect.py</code></div>';
  for (const it of items) {
    const c = document.createElement("div");
    c.className = "card"; c.dataset.id = it.id;
    c.innerHTML =
      `<div class="t">${esc(it.title) || "(无标题)"}</div>
       <div class="meta">
         <span class="badge ${it.tier}">${it.tier || ""}</span>
         <span>${esc(it.source_name || it.source_key)}</span>
         ${it.score != null ? `<span class="pill">★${it.score}</span>` : ""}
         ${it.thread_count ? `<span class="badge has-thread">💬${it.thread_count}</span>` : ""}
         <span>${(it.published_at || "").slice(0, 10)}</span>
       </div>`;
    c.onclick = () => selectItem(it);
    list.appendChild(c);
  }
}

function selectItem(it) {
  current = it;
  document.querySelectorAll(".card").forEach((c) => c.classList.toggle("active", c.dataset.id === it.id));
  const d = $("#detail"); d.className = "";
  d.innerHTML = `
    <h2>${esc(it.title) || "(无标题)"}</h2>
    <div class="meta muted">${esc(it.source_name || "")} · ${it.tier || ""} · ${it.category || ""} · ${(it.published_at || "").slice(0,10)}</div>
    <p><a href="${esc(it.url)}" target="_blank" rel="noopener">打开原文 ↗</a></p>
    <div class="summary">${esc(it.summary) || "(无摘要)"}</div>

    <div class="row score">
      <label>评分</label>
      ${[0,1,2,3,4,5].map((n) => `<button data-score="${n}" class="${it.score===n?"on":""}">${n}</button>`).join("")}
    </div>
    <div class="row"><label>标签（逗号分隔）</label><input type="text" id="d-tags" value="${esc(it.tags || "")}" /></div>
    <div class="row"><label>状态</label>
      <select id="d-status">${["captured","reviewed","promoted","ignored"].map((s)=>`<option ${it.status===s?"selected":""}>${s}</option>`).join("")}</select>
    </div>
    <div class="row"><label>我的 comment</label><textarea id="d-comment">${esc(it.comment || "")}</textarea>
      <div class="actions"><button class="primary" id="d-save">保存 triage</button></div>
    </div>

    <hr/>
    <div class="row">
      <label>讨论（手动接 Codex/Claude）</label>
      <div class="actions">
        <button id="d-discuss">生成讨论 prompt</button>
        ${it.last_thread ? `<span class="pill">${esc(it.last_thread)}</span>` : ""}
      </div>
      <div id="d-discuss-out"></div>
    </div>

    <hr/>
    <div class="row">
      <label>灵感</label>
      <input type="text" id="d-idea-title" placeholder="标题（可空）" />
      <textarea id="d-idea-body" placeholder="冒出来的想法…"></textarea>
      <div class="actions">
        <button id="d-idea-now" class="primary">记为灵感</button>
        <button id="d-idea-parked">丢进 inbox</button>
      </div>
    </div>`;

  d.querySelectorAll(".score button").forEach((b) =>
    (b.onclick = () => patch({ score: Number(b.dataset.score) })));
  $("#d-save").onclick = () => patch({
    tags: $("#d-tags").value, status: $("#d-status").value, comment: $("#d-comment").value,
  });
  $("#d-discuss").onclick = genDiscussion;
  $("#d-idea-now").onclick = () => saveIdea(false);
  $("#d-idea-parked").onclick = () => saveIdea(true);
}

async function patch(body) {
  const it = await api(`/api/items/${current.id}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  current = { ...current, ...it };
  selectItem(current); loadItems(); loadStats();
  toast("已保存");
}

async function genDiscussion() {
  const comment = $("#d-comment").value;
  const res = await api(`/api/items/${current.id}/discussion`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ comment }),
  });
  const out = $("#d-discuss-out");
  out.innerHTML = `
    <p class="muted">已写入 <code>${esc(res.path)}</code> — 拖进 Codex/Claude 聊。聊完把结论贴回下面回填。</p>
    <textarea id="d-prompt" style="min-height:120px">${esc(res.content)}</textarea>
    <div class="actions">
      <button id="d-copy">复制 prompt</button>
    </div>
    <label style="margin-top:10px">回填讨论结论</label>
    <textarea id="d-backfill" placeholder="把 AI 给的结论 / 你的思考贴这里…"></textarea>
    <div class="actions"><button id="d-append" class="primary">回填到讨论文件</button></div>`;
  $("#d-copy").onclick = () => { navigator.clipboard.writeText(res.content); toast("已复制"); };
  $("#d-append").onclick = async () => {
    const content = $("#d-backfill").value.trim();
    if (!content) return toast("先写点东西");
    await api("/api/discussion/append", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: res.path, content }),
    });
    $("#d-backfill").value = ""; toast("已回填");
  };
  loadItems(); loadStats();
}

async function saveIdea(parked) {
  const body = $("#d-idea-body").value.trim();
  if (!body) return toast("写点灵感再记");
  const res = await api("/api/ideas", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: $("#d-idea-title").value, body, parked, item_id: current.id }),
  });
  $("#d-idea-title").value = ""; $("#d-idea-body").value = "";
  toast(parked ? "丢进 inbox" : "已记 → " + res.path);
}

$("#f-apply").onclick = loadItems;
["#f-status", "#f-tier", "#f-source", "#f-sort"].forEach((s) => ($(s).onchange = loadItems));
$("#f-q").addEventListener("keydown", (e) => e.key === "Enter" && loadItems());

(async () => {
  try { await loadSources(); await loadStats(); await loadItems(); }
  catch (e) { $("#list").innerHTML = `<div class="card">启动失败：${esc(e.message)}<br/>先跑 <code>python3 collect.py</code></div>`; }
})();
