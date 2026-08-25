// popup — 携程哨兵 · 采集器 (version lives in manifest.json; right-top "verText" displays it at runtime)
// 行为：
//   - 加载/保存 server + apiSecret
//   - 嗅探当前 tab：
//       (a) 非携程 → 提示打开示例 POI 链接
//       (b) 是携程但没有 viewId → 同样提示
//       (c) 是 POI → 渲染 POI 面板 + 自动跑一次 capture_now
//   - "打开携程登录页并同步"：交给 background 启动登录页 + 轮询 + 上传
//   - 后台把 cookie_sync_result 推回 popup，并落 storage：刷新/重开 popup 仍可见

const $ = (id) => document.getElementById(id);
const POI_EXAMPLE = "https://m.ctrip.com/webapp/you/sight/1/5208.html"; // 圆明园
const EXAMPLE_NAME = "圆明园";
const EXAMPLE_VIEWID = 5208;

function setStatus(kind, msg) {
  const el = $("status");
  if (!msg) { el.hidden = true; return; }
  el.className = "status " + (kind || "");
  el.textContent = msg;
  el.hidden = false;
}

function tickClock() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  $("clockText").textContent =
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
setInterval(tickClock, 1000);
tickClock();

async function loadStored() {
  const s = await chrome.storage.local.get(["server", "apiSecret"]);
  $("server").value    = s.server    || "https://xiecheng.19880913.xyz";
  $("apiSecret").value = s.apiSecret || "";
}

// 版本号从 manifest 拿，避免 HTML 写死永远落后
try {
  const v = chrome.runtime.getManifest().version;
  const el = document.getElementById("verText");
  if (el) el.textContent = "v" + v;
} catch (_) {}

async function save() {
  const v = {
    server:    $("server").value.trim(),
    apiSecret: $("apiSecret").value.trim(),
  };
  await chrome.storage.local.set(v);
  setStatus("ok", `已保存 · ${v.server}`);
}

async function startCookieSyncFlow() {
  setStatus("run", "正在打开携程登录页…");
  try {
    await chrome.runtime.sendMessage({ cmd: "start_cookie_sync" });
  } catch (e) {
    setStatus("err", "无法启动同步：" + e.message);
  }
}

// ---- POI panel renderers ----

function renderNotCtrip() {
  const panel = $("poiPanel");
  panel.className = "poi-panel empty";
  panel.innerHTML =
    `<div class="guide">当前 tab 不是携程页面。<br>请先打开任意 POI，例如 <a href="${POI_EXAMPLE}" target="_blank" rel="noopener">${EXAMPLE_NAME}</a>（viewId #${EXAMPLE_VIEWID}），回来这里就会自动嗅探并初次抓取。</div>`;
}

function renderCtripNoPoi(tabUrl) {
  const panel = $("poiPanel");
  panel.className = "poi-panel empty";
  panel.innerHTML =
    `<div class="guide">当前是携程页面（<span style="font-family:var(--mono);font-size:10px">${escapeHtml(shortHost(tabUrl))}</span>），但没识别到 POI 的 viewId。<br>请访问 <a href="${POI_EXAMPLE}" target="_blank" rel="noopener">这个示例链接</a> 试试。</div>`;
}

function renderPoi(tab, poi) {
  const host = shortHost(tab.url);
  const panel = $("poiPanel");
  panel.className = "poi-panel";
  panel.innerHTML = `
    <h3 class="poi-name">${escapeHtml(poi.name || `viewId #${poi.viewid}（页面还在加载名字…）`)}</h3>
    <div class="poi-meta">
      <span class="id">viewId #${poi.viewid}</span>
      <span class="dot">·</span>
      <span>${escapeHtml(host)}</span>
    </div>
    <div class="poi-status run" id="poiStatus">初次嗅探抓取中…</div>
    <div class="stream" id="streamLog" hidden>
      <div class="stream-ticker">
        <span class="pulse idle" id="streamPulse"></span>
        <span class="label" id="streamLabel">TAPE</span>
        <span class="sep">·</span>
        <span><span class="cnt" id="streamFired">0</span>/<span class="cnt" id="streamTotal">0</span></span>
        <span class="sep">·</span>
        <span class="elap" id="streamElap">0s</span>
      </div>
      <div class="stream-body" id="streamBody">
        <div class="stream-empty">等待事件…</div>
      </div>
    </div>
    <div class="btn-row">
      <button id="captureBtn">再抓一轮</button>
      <button id="syncPoiBtn">写入 dashboard</button>
    </div>`;

  // chrome.tabs.sendMessage 的失败模式细分:
  //   1) chrome.runtime.lastError  "Could not establish connection" → 真的没注入
  //   2) reject "The message port closed before a response was received." →
  //      reload race(我们已不再 reload,所以这种只剩"用户刚 reload 页面 / 切到别的 tab")
  //   3) reject 其他 → 网络/异常,显示 message
  function describeSendError(e) {
    const msg = (e && e.message) || (chrome.runtime.lastError && chrome.runtime.lastError.message) || "未知错误";
    if (/Could not establish connection/i.test(msg)) {
      return "未注入 content script（请刷新一次页面）";
    }
    if (/message port closed/i.test(msg)) {
      return "页面正在刷新或刚切走 — 等加载完再点「再抓一轮」";
    }
    return `页面没响应：${msg}`;
  }
  function describeCaptureResult(r) {
    if (r && r.ok) {
      return { kind: "ok", html: `已上传 · HTTP ${r.status} · <span class="count">${r.requests ?? 0}</span> 条请求` };
    }
    if (r && r.reason === "no_poi_in_url") {
      return { kind: "err", html: "URL 里没有 viewId" };
    }
    if (r && r.reason === "no_requests") {
      return { kind: "err", html: "没拦截到 soa2 请求（页面还在加载 / fetch 已被覆盖？）— 刷新页面或点「再抓一轮」" };
    }
    if (r && r.status === 401) {
      return { kind: "err", html: "上传 401 auth 失败 — 检查 popup 里 API Secret 是否与 /admin/api-secret 一致" };
    }
    if (r && (r.status >= 500 || r.status === 0)) {
      return { kind: "err", html: `上传失败 HTTP ${r.status} — 服务端异常,稍后重试` };
    }
    const detail = r && (r.error || r.reason);
    return { kind: "err", html: `失败：${detail || "未知"}` };
  }

  $("captureBtn").addEventListener("click", async () => {
    $("captureBtn").disabled = true;
    runWithProgress(tab, "再抓一轮中…");
  });

  $("syncPoiBtn").addEventListener("click", async () => {
    $("syncPoiBtn").disabled = true;
    setPoiStatus("run", "写入 dashboard…");
    try {
      const cur = await chrome.tabs.sendMessage(tab.id, { cmd: "get_poi" });
      if (chrome.runtime.lastError) throw new Error(chrome.runtime.lastError.message);
      if (!cur || !cur.viewid) { setPoiStatus("err", "页面里没有可识别的 POI"); return; }
      const r = await chrome.runtime.sendMessage({ cmd: "sync_poi", poi: cur, pageUrl: tab.url });
      if (r?.ok) {
        setPoiStatus("ok", `已同步 ${cur.name || "(未命名)"} · ${r.action || "added"}`);
      } else {
        setPoiStatus("err", `失败：${r?.error || "未知"}`);
      }
    } catch (e) {
      setPoiStatus("err", describeSendError(e));
    } finally {
      $("syncPoiBtn").disabled = false;
    }
  });
}

function setPoiStatus(kind, html) {
  const el = $("poiStatus");
  if (!el) return;
  el.className = "poi-status " + kind;
  el.innerHTML = html;
}

async function autoCapture(tab) {
  await runWithProgress(tab, "初次嗅探抓取中…");
}

// 把 sendMessage 包成"会报进度的版本" — 后端最长要等 22s，老版本 popup 卡在
// "抓取中…" 看不到任何动静，用户以为死了。这里每 1s 更新一次状态显示已等多久，
// 每 500ms 拉一次 get_progress 展示每个 expected endpoint 的状态；
// 同时拉 get_events 增量补 stream log（tape reader）。
//
// 模块级共享状态：stream ticker (▸ 抓取中 / ■ 完成) + elapsed 计时跨 renderProgress
// 和 updateStreamTicker，需要同一份起点。runStartedAt / lastProactive 在
// runWithProgress 入口赋值，stream helper 读它。
let runStartedAt = Date.now();
let lastProactive = null;

async function runWithProgress(tab, label) {
  runStartedAt = Date.now();
  lastProactive = null;
  const startedAt = runStartedAt;
  let timer = null;
  let lastRenderedHtml = "";
  let lastEventIndex = 0;          // 给 get_events 增量读用

  const renderProgress = async () => {
    const sec = Math.floor((Date.now() - startedAt) / 1000);
    let html = `${label}（已等 ${sec}s）`;
    try {
      const r = await chrome.tabs.sendMessage(tab.id, { cmd: "get_progress" });
      if (r?.ok && r.progress && Array.isArray(r.progress.targets)) {
        const { targets, completed, pending, error, missing } = r.progress;
        html += ` · <span class="count">${completed}</span>/${targets.length} 已完成`;
        if (pending) html += ` · ${pending} 在-flight`;
        if (error)    html += ` · <span class="err">${error} 失败</span>`;
        // 主动 fire 进度：phase=running/done/idle/skipped，fired/total 让用户看到「我在自己抓」不是干等
        if (r.proactive && (r.proactive.phase === "running" || r.proactive.phase === "done")) {
          const p = r.proactive;
          const pct = p.total ? ` ${p.fired}/${p.total}` : "";
          const errs = p.errors ? ` · <span class="err">${p.errors} 失败</span>` : "";
          html += ` · <span style="color:var(--amber)">主动 fire ${p.phase === "done" ? "✓" : "…"}${pct}${errs}</span>`;
        }
        html += "<br>";
        // 列表里：✓绿色 / ◌黄色 pending / ✗红色 error / ·灰色 missing
        for (const t of targets) {
          const name = t.path.split("/").pop();
          if (t.state === "completed") {
            html += `<div class="t-done">✓ ${escapeHtml(name)} <span class="mute">HTTP ${t.status || 200}</span></div>`;
          } else if (t.state === "pending") {
            html += `<div class="t-pend">◌ ${escapeHtml(name)} <span class="mute">在-flight</span></div>`;
          } else if (t.state === "error") {
            html += `<div class="t-err">✗ ${escapeHtml(name)} <span class="mute">HTTP ${t.status || "?"}</span></div>`;
          } else {
            html += `<div class="t-miss">· ${escapeHtml(name)} <span class="mute">等发起</span></div>`;
          }
        }
      }
      if (r?.proactive) lastProactive = r.proactive;
    } catch (_) {
      // popup 的 tabs.sendMessage 在 content script 重新注入时会 reject；忽略
    }
    if (html !== lastRenderedHtml) {
      lastRenderedHtml = html;
      setPoiStatus("run", html);
    }
  };

  const renderStream = async () => {
    try {
      const r = await chrome.tabs.sendMessage(tab.id, {
        cmd: "get_events", sinceIndex: lastEventIndex
      });
      if (!r?.ok) return;
      if (typeof r.total === "number") lastEventIndex = r.total;
      const evs = r.events || [];
      if (!evs.length && !($("streamLog") && !$("streamLog").hidden)) return;
      appendStreamEvents(evs);
      updateStreamTicker();
    } catch (_) {
      // content script 重新注入时 reject；忽略
    }
  };

  renderProgress();
  renderStream();
  timer = setInterval(() => { renderProgress(); renderStream(); }, 500);
  console.log("[ctrip-sentry:popup] capture_now → content.js");
  try {
    const r = await chrome.tabs.sendMessage(tab.id, { cmd: "capture_now" });
    console.log("[ctrip-sentry:popup] capture_now ← result", r);
    if (chrome.runtime.lastError) throw new Error(chrome.runtime.lastError.message);
    const d = describeCaptureResult(r);
    setPoiStatus(d.kind, d.html);
  } catch (e) {
    console.warn("[ctrip-sentry:popup] capture_now error", e);
    setPoiStatus("err", describeSendError(e));
  } finally {
    clearInterval(timer);
    // 抓完后还多渲染几秒 stream，让最后几条事件落定
    setTimeout(() => { renderStream(); updateStreamTicker(); }, 800);
    const btn = document.getElementById("captureBtn");
    if (btn) btn.disabled = false;
  }
}

// ---- stream log helpers ----

const TAG_LABELS = {
  addInfo: "addInfo", priceCal: "priceCal", resourceDetail: "resDet",
  shelfResList: "shelfResLst",
  "priceCal-sibling": "priceCal-sib", "resourceDetail-sibling": "resDet-sib",
  start: "▸ start", done: "■ done",
};
const GLYPH_FOR = {
  ok: "✓", err: "✗", warn: "!", rate_limited: "⚠", started: "·",
};

function eventRowHtml(ev) {
  const ts = fmtClock(ev.at);
  const tag = TAG_LABELS[ev.label] || ev.label || "?";
  const tagClass = ev.kind === "info" ? "info" :
                   ev.kind === "skip" ? "skip" :
                   ev.kind === "phase" ? "phase" :
                   (ev.label || "fire");
  const ident = formatIdent(ev.ident);
  const glyph = GLYPH_FOR[ev.status] || "?";
  const muted = (ev.kind === "info" || ev.kind === "phase") ? "muted" : "";
  return `<div class="ev ${muted}">
    <span class="ts">${ts}</span>
    <span class="tag ${escapeHtml(tagClass)}">${escapeHtml(tag)}</span>
    <span class="ident" title="${escapeHtml(ev.ident || "")}">${ident}</span>
    <span class="glyph ${escapeHtml(ev.status || "")}">${glyph}</span>
  </div>`;
}

function formatIdent(s) {
  if (!s) return "";
  // 把 "rid=110368162 pid=110384413" 高亮 rid/pid
  return escapeHtml(s)
    .replace(/(rid=)(\d+)/g, '$1<span class="rid">$2</span>')
    .replace(/(pid=)(\d+)/g, '$1<span class="pid">$2</span>');
}

function fmtClock(ms) {
  if (!ms) return "--:--:--";
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function appendStreamEvents(evs) {
  const wrap = $("streamLog");
  const body = $("streamBody");
  if (!wrap || !body) return;
  wrap.hidden = false;
  // 第一次有事件时去掉 empty placeholder
  const empty = body.querySelector(".stream-empty");
  if (empty) empty.remove();
  const frag = document.createDocumentFragment();
  const tmp = document.createElement("div");
  // CSS column-reverse 让最新在顶；这里把多事件按 push 顺序插入，CSS 自动倒序显示。
  // 但单元素 append 时 column-reverse 会让新元素跳到底部 — 所以用 prepend。
  tmp.innerHTML = evs.map(eventRowHtml).join("");
  while (tmp.firstChild) {
    tmp.firstChild.classList && tmp.firstChild.classList.add("ev-new");
    frag.appendChild(tmp.firstChild);
  }
  body.prepend(frag);
  // 限速：UI 上保留最近 80 条
  const rows = body.querySelectorAll(".ev");
  if (rows.length > 80) {
    for (let i = 80; i < rows.length; i++) rows[i].remove();
  }
}

function updateStreamTicker() {
  const wrap = $("streamLog");
  if (!wrap) return;
  const p = lastProactive || {};
  const $cnt = $("streamFired");
  const $tot = $("streamTotal");
  const $elap = $("streamElap");
  const $pulse = $("streamPulse");
  const $label = $("streamLabel");
  if ($cnt) $cnt.textContent = p.fired ?? 0;
  if ($tot) $tot.textContent = p.total ?? 0;
  if ($elap) {
    const sec = Math.floor((Date.now() - runStartedAt) / 1000);
    $elap.textContent = sec + "s";
  }
  if ($pulse && $label) {
    if (p.phase === "running") {
      $pulse.className = "pulse";
      $label.textContent = "抓取中";
    } else if (p.phase === "done") {
      $pulse.className = "pulse done";
      $label.textContent = (p.errors ? "完成 · 有错" : "完成");
    } else if (p.phase === "skipped") {
      $pulse.className = "pulse err";
      $label.textContent = "已跳过";
    } else {
      $pulse.className = "pulse idle";
      $label.textContent = "TAPE";
    }
  }
}

async function sniffCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) return { kind: "no_tab" };
  if (!/ctrip\.com/.test(tab.url)) return { kind: "not_ctrip", tab };
  try {
    const poi = await chrome.tabs.sendMessage(tab.id, { cmd: "get_poi" });
    if (poi && poi.viewid) return { kind: "poi", tab, poi };
    return { kind: "ctrip_no_poi", tab, poi };
  } catch (e) {
    // content script 没注入：可能 tab 还没加载完，或不是匹配页
    return { kind: "ctrip_no_inject", tab };
  }
}

async function restoreLastCookieSync() {
  const { lastCookieSync } = await chrome.storage.local.get("lastCookieSync");
  if (!lastCookieSync) return;
  const ageMs = Date.now() - (lastCookieSync.at || 0);
  if (ageMs > 5 * 60 * 1000) return; // 5 分钟前的旧结果忽略
  const when = new Date(lastCookieSync.at).toLocaleTimeString();
  if (lastCookieSync.phase === "waiting_login") {
    setStatus("run", `${when} · 仍在等待携程登录…`);
  } else if (lastCookieSync.ok) {
    setStatus("ok", `${when} · 已同步 ${lastCookieSync.count} 条 cookie（含 GUID）`);
  } else {
    setStatus("err", `${when} · ${lastCookieSync.error || "失败"}（检测到 ${lastCookieSync.count} 条 cookie）`);
  }
}

function shortHost(u) {
  try { return new URL(u).host.replace(/^www\./, ""); } catch (_) { return ""; }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;" }[c]));
}

// ---- init ----

async function init() {
  await loadStored();
  $("save").addEventListener("click", save);
  $("syncNow").addEventListener("click", startCookieSyncFlow);

  // 后台推回的 cookie 同步结果
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg?.cmd !== "cookie_sync_result") return;
    const when = new Date(msg.at || Date.now()).toLocaleTimeString();
    if (msg.phase === "waiting_login") {
      setStatus("run", `${when} · 等待携程登录（已检测到 ${msg.count} 条 cookie）…`);
    } else if (msg.ok) {
      setStatus("ok", `${when} · 已同步 ${msg.count} 条 cookie（含 GUID）`);
    } else {
      setStatus("err", `${when} · ${msg.error || "失败"}（检测到 ${msg.count} 条 cookie）`);
    }
  });

  await restoreLastCookieSync();

  const ctx = await sniffCurrentTab();
  if (ctx.kind === "no_tab" || ctx.kind === "not_ctrip") {
    renderNotCtrip();
  } else if (ctx.kind === "ctrip_no_inject" || ctx.kind === "ctrip_no_poi") {
    renderCtripNoPoi(ctx.tab.url);
  } else if (ctx.kind === "poi") {
    renderPoi(ctx.tab, ctx.poi);
    await autoCapture(ctx.tab);
  }
}

document.addEventListener("DOMContentLoaded", init);
