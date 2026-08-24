// popup UI：保存配置 + 触发操作
const $ = (id) => document.getElementById(id);

async function load() {
  const stored = await chrome.storage.local.get(["server", "apiSecret"]);
  $("server").value    = stored.server    || "";
  $("apiSecret").value = stored.apiSecret || "";
}

async function save() {
  const v = {
    server:    $("server").value.trim(),
    apiSecret: $("apiSecret").value.trim(),
  };
  await chrome.storage.local.set(v);
  flash("ok", "已保存");
}

async function syncNow() {
  flash("ok", "同步中…");
  const r = await chrome.runtime.sendMessage({ cmd: "sync_now" });
  flash(r?.ok ? "ok" : "err", r?.ok ? "同步请求已发送" : "失败");
}

async function captureNow() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) { flash("err", "无活动 tab"); return; }
  if (!/ctrip\.com/.test(tab.url || "")) { flash("err", "当前 tab 不是携程页面"); return; }
  try {
    const r = await chrome.tabs.sendMessage(tab.id, { cmd: "capture_now" });
    flash(r?.ok ? "ok" : "err", r?.ok ? `已上传 (${r.status})` : "上传失败");
  } catch (e) {
    flash("err", "未注入 content script，请先打开携程页面");
  }
}

async function syncPoi() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) { flash("err", "无活动 tab"); return; }
  if (!/ctrip\.com/.test(tab.url || "")) { flash("err", "当前 tab 不是携程页面"); return; }
  // 让 content script 抽 POI，然后 background 转发到 dashboard
  try {
    const poi = await chrome.tabs.sendMessage(tab.id, { cmd: "get_poi" });
    if (!poi || !poi.viewid) { flash("err", "当前页面没有可识别的 POI（URL 中没有 viewId）"); return; }
    const r = await chrome.runtime.sendMessage({ cmd: "sync_poi", poi, pageUrl: tab.url });
    flash(r?.ok ? "ok" : "err",
          r?.ok ? `已同步 ${poi.name || "(未命名)"} (viewId=${poi.viewid}) · ${r.action}`
                : `同步失败：${r?.error || "未知"}`);
  } catch (e) {
    flash("err", "未注入 content script，请先打开携程页面");
  }
}

function flash(cls, msg) {
  const el = $("status");
  el.className = "status " + cls;
  el.textContent = msg;
  el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 3000);
}

document.addEventListener("DOMContentLoaded", () => {
  load();
  $("save").addEventListener("click", save);
  $("syncNow").addEventListener("click", syncNow);
  $("captureNow").addEventListener("click", captureNow);
  $("syncPoi").addEventListener("click", syncPoi);
});