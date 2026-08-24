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
  const stored = await chrome.storage.local.get(["server", "ingestSecret", "cookieSecret", "poiList"]);
  cfg = {
    server:       stored.server       || CONFIG_DEFAULTS.server,
    ingestSecret: stored.ingestSecret || CONFIG_DEFAULTS.ingestSecret,
    cookieSecret: stored.cookieSecret || CONFIG_DEFAULTS.cookieSecret,
    poiList:      stored.poiList      || CONFIG_DEFAULTS.poiList,
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
  const cookies = await chrome.cookies.getAll({ domain: ".ctrip.com" });
  if (!cookies.length) return;
  const blob = {};
  for (const ck of cookies) blob[ck.name] = ck.value;
  if (!blob.GUID) return;
  try {
    await fetch(`${c.server}/api/cookies/sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Cookie-Secret": c.cookieSecret },
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
            "X-Ingest-Secret": c.ingestSecret,
            "X-Extension-Ver": chrome.runtime.getManifest().version,
            "X-Source": "extension",
          },
          body: JSON.stringify(msg.payload),
        });
        sendResponse({ ok: r.ok, status: r.status });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    } else if (msg?.cmd === "get_cfg") {
      sendResponse(await loadCfg());
    } else if (msg?.cmd === "sync_now") {
      await syncCookies();
      sendResponse({ ok: true });
    }
  })();
  return true; // async response
});