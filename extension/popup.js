// popup — 携程哨兵 · 采集器 (v0.2.3)
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
    <h3 class="poi-name">${escapeHtml(poi.name || "(未命名 POI)")}</h3>
    <div class="poi-meta">
      <span class="id">viewId #${poi.viewid}</span>
      <span class="dot">·</span>
      <span>${escapeHtml(host)}</span>
    </div>
    <div class="poi-status run" id="poiStatus">初次嗅探抓取中…</div>
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
      return { kind: "err", html: "这次没拦截到 soa2 请求（页面刚打开？）— 点「再抓一轮」" };
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
    setPoiStatus("run", "抓取中…（等待 1.5s）");
    try {
      const r = await chrome.tabs.sendMessage(tab.id, { cmd: "capture_now" });
      if (chrome.runtime.lastError) throw new Error(chrome.runtime.lastError.message);
      const d = describeCaptureResult(r);
      setPoiStatus(d.kind, d.html);
    } catch (e) {
      setPoiStatus("err", describeSendError(e));
    } finally {
      $("captureBtn").disabled = false;
    }
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
  setPoiStatus("run", "初次嗅探抓取中…（等待 1.5s）");
  try {
    const r = await chrome.tabs.sendMessage(tab.id, { cmd: "capture_now" });
    if (r?.ok) {
      setPoiStatus("ok", `已上传 · HTTP ${r.status} · <span class="count">${r.requests ?? 0}</span> 条请求`);
    } else if (r?.reason === "no_poi_in_url") {
      setPoiStatus("err", "URL 里没有 viewId");
    } else if (r?.reason === "no_requests") {
      setPoiStatus("err", "这次没拦截到 soa2 请求（页面刚打开？）— 点「再抓一轮」");
    } else {
      setPoiStatus("err", `失败：${r?.error || "未注入"}`);
    }
  } catch (e) {
    setPoiStatus("err", "未注入 content script（请刷新一次页面）");
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
