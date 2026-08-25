// 携程哨兵 · background service worker (MV3 module)
// 职责：
//   1. 监听 m.ctrip.com 上的 soa2 请求（XHR/fetch 劫持不可行，所以用 chrome.webRequest 标记 + content script 兜底）
//   2. 同步 cookies 到 dashboard（每 5 分钟 + onChange）
//   3. 接收 content script 捕获的 round 数据，POST 到 dashboard
//   4. 接收 popup 的命令（"capture_now" → 通知 content script 主动抓一次）

import { CONFIG_DEFAULTS } from "./config.js";

const ALARM_COOKIES = "ctrip-cookie-sync";
const ALARM_PING    = "ctrip-ping";
const ALARM_CAPTURE = "ctrip-capture-nudge";

let cfg = null;

async function loadCfg() {
  const stored = await chrome.storage.local.get(["server", "apiSecret", "poiList"]);
  cfg = {
    server:    stored.server    || CONFIG_DEFAULTS.server,
    apiSecret: stored.apiSecret || CONFIG_DEFAULTS.apiSecret,
    poiList:   stored.poiList   || CONFIG_DEFAULTS.poiList,
  };
  return cfg;
}

chrome.runtime.onInstalled.addListener(async () => {
  await loadCfg();
  chrome.alarms.create(ALARM_COOKIES, { periodInMinutes: 5 });
  chrome.alarms.create(ALARM_PING,    { periodInMinutes: 30 });
});

chrome.runtime.onStartup.addListener(async () => {
  await loadCfg();
  chrome.alarms.create(ALARM_COOKIES, { periodInMinutes: 5 });
  chrome.alarms.create(ALARM_PING,    { periodInMinutes: 30 });
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local") loadCfg();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_COOKIES) {
    await syncCookies();
  } else if (alarm.name === ALARM_PING) {
    // 触发 content script 重抓（如果有 tab 在 m.ctrip.com）
    await nudgeActiveTab();
  }
});

async function syncCookies() {
  const c = await loadCfg();
  if (!c.apiSecret) { console.warn("[ctrip] apiSecret not set"); return; }
  const cookies = await chrome.cookies.getAll({ domain: ".ctrip.com" });
  if (!cookies.length) return;
  const blob = {};
  for (const ck of cookies) blob[ck.name] = ck.value;
  if (!blob.GUID) return;
  try {
    await fetch(`${c.server}/api/cookies/sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Secret": c.apiSecret },
      body: JSON.stringify({ cookies: blob }),
    });
  } catch (e) {
    console.error("[ctrip] syncCookies failed", e);
  }
}

async function nudgeActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) return;
  if (!/ctrip\.com/.test(tab.url)) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { cmd: "capture_now" });
  } catch (_) { /* tab 可能没注入 content script */ }
}

/**
 * 打开 m.ctrip.com 登录页 → 轮询 .ctrip.com cookie 直到拿到 GUID（最多 90s）
 * → POST /api/cookies/sync → 广播结果到 popup + 落 storage。
 * 整个流程在 background 跑，即使 popup 关闭也能完成。
 */
async function startCookieSyncFlow() {
  let loginTab = null;
  try {
    loginTab = await chrome.tabs.create({
      url: "https://m.ctrip.com/webapp/myctrip/",
      active: true,
    });
    await chrome.storage.local.set({
      lastCookieSync: { ok: false, phase: "waiting_login", count: 0, at: Date.now() },
    });
    broadcast({ cmd: "cookie_sync_result", ok: false, phase: "waiting_login", count: 0 });
  } catch (e) {
    const result = { ok: false, error: "open_failed:" + String(e), count: 0, at: Date.now() };
    await chrome.storage.local.set({ lastCookieSync: result });
    broadcast({ cmd: "cookie_sync_result", ...result });
    return;
  }

  const deadline = Date.now() + 90_000;
  let snapshot = [];
  let lastBroadcast = 0;
  while (Date.now() < deadline) {
    try {
      const cks = await chrome.cookies.getAll({ domain: ".ctrip.com" });
      snapshot = cks;
      if (cks.find((x) => x.name === "GUID")) break;
    } catch (_) {}
    // 每 10s 推一次「还在等」给 popup（防止 popup 没收到任何东西以为失败了）
    if (Date.now() - lastBroadcast > 10_000) {
      lastBroadcast = Date.now();
      const phase = { ok: false, phase: "waiting_login", count: snapshot.length, at: Date.now() };
      await chrome.storage.local.set({ lastCookieSync: phase });
      broadcast({ cmd: "cookie_sync_result", ...phase });
    }
    await new Promise((r) => setTimeout(r, 2000));
  }

  const gotGuid = snapshot.some((x) => x.name === "GUID");
  if (!gotGuid) {
    const result = { ok: false, error: "timeout: 未在 90s 内拿到 GUID", count: snapshot.length, at: Date.now() };
    await chrome.storage.local.set({ lastCookieSync: result });
    broadcast({ cmd: "cookie_sync_result", ...result });
    if (loginTab?.id) {
      try { await chrome.tabs.remove(loginTab.id); } catch (_) {}
    }
    return;
  }

  // 上传
  try {
    const c = await loadCfg();
    if (!c.apiSecret) {
      const result = { ok: false, error: "未配置 API Secret", count: snapshot.length, at: Date.now() };
      await chrome.storage.local.set({ lastCookieSync: result });
      broadcast({ cmd: "cookie_sync_result", ...result });
      return;
    }
    await syncCookies();
    const result = { ok: true, count: snapshot.length, at: Date.now() };
    await chrome.storage.local.set({ lastCookieSync: result });
    broadcast({ cmd: "cookie_sync_result", ...result });
  } catch (e) {
    const result = { ok: false, error: String(e), count: snapshot.length, at: Date.now() };
    await chrome.storage.local.set({ lastCookieSync: result });
    broadcast({ cmd: "cookie_sync_result", ...result });
  } finally {
    // 登录页可由用户留着，关闭容易误伤；留 3s 再试着关，方便用户继续看登录态
    if (loginTab?.id) {
      setTimeout(() => {
        chrome.tabs.remove(loginTab.id).catch(() => {});
      }, 3000);
    }
  }
}

function broadcast(msg) {
  try { chrome.runtime.sendMessage(msg).catch(() => {}); } catch (_) {}
}

// content script 上报 round
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg?.cmd === "upload_round") {
      const c = await loadCfg();
      try {
        const r = await fetch(`${c.server}/api/ingest/round`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-API-Secret": c.apiSecret,
            "X-Extension-Ver": chrome.runtime.getManifest().version,
            "X-Source": "extension",
          },
          body: JSON.stringify(msg.payload),
        });
        sendResponse({ ok: r.ok, status: r.status, requests: msg.payload?.requests?.length });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    } else if (msg?.cmd === "get_cfg") {
      sendResponse(await loadCfg());
    } else if (msg?.cmd === "sync_now") {
      await syncCookies();
      sendResponse({ ok: true });
    } else if (msg?.cmd === "start_cookie_sync") {
      // popup 请求启动一次「打开登录 + 轮询 GUID + 上传」流程。
      // 这里 fire-and-forget；结果通过 broadcast 发回 popup，并落 storage 让
      // popup 重开后仍能看到上次结果。
      startCookieSyncFlow().catch((e) => console.error("[ctrip] startCookieSyncFlow", e));
      sendResponse({ ok: true, started: true });
    } else if (msg?.cmd === "cookie_sync_result_ack") {
      // popup 收到结果后清掉持久化记录
      await chrome.storage.local.remove("lastCookieSync");
    } else if (msg?.cmd === "sync_poi") {
      // popup → 这里 → POST /api/admin/pois/add-via-extension
      const c = await loadCfg();
      if (!c.apiSecret) {
        sendResponse({ ok: false, error: "未配置 apiSecret" });
        return;
      }
      const body = {
        viewid: msg.poi?.viewid,
        name: msg.poi?.name || "",
        pageUrl: msg.pageUrl || "",
      };
      try {
        const r = await fetch(`${c.server}/api/admin/pois/add-via-extension`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-API-Secret": c.apiSecret,
            "X-Extension-Ver": chrome.runtime.getManifest().version,
            "X-Source": "extension",
          },
          body: JSON.stringify(body),
        });
        const data = await r.json().catch(() => ({}));
        sendResponse({ ok: r.ok && data.ok, status: r.status,
                       action: data.action, error: data.error });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    }
  })();
  return true; // async response
});