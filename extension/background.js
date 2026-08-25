// 携程哨兵 · background service worker (MV3 module)
// 职责：
//   1. 监听 m.ctrip.com 上的 soa2 请求（XHR/fetch 劫持不可行，所以用 chrome.webRequest 标记 + content script 兜底）
//   2. 同步 cookies 到 dashboard（每 5 分钟 + onChange）
//   3. 接收 content script 捕获的 round 数据，POST 到 dashboard
//   4. 接收 popup 的命令（"capture_now" → 通知 content script 主动抓一次）

import { CONFIG_DEFAULTS } from "./config.js";

const ALARM_COOKIES        = "ctrip-cookie-sync";
const ALARM_PING           = "ctrip-ping";          // 30 分钟：当前活动 tab 兜底重抓
const ALARM_POLL_COMMANDS  = "ctrip-poll-commands"; // 30 秒：拉服务器命令队列
const ALARM_CAPTURE_NUDGE  = "ctrip-capture-nudge";

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
  chrome.alarms.create(ALARM_COOKIES,        { periodInMinutes: 5 });
  chrome.alarms.create(ALARM_PING,           { periodInMinutes: 30 });
  chrome.alarms.create(ALARM_POLL_COMMANDS,  { periodInMinutes: 0.5 });   // 30s 拉服务器命令
});

chrome.runtime.onStartup.addListener(async () => {
  await loadCfg();
  chrome.alarms.create(ALARM_COOKIES,        { periodInMinutes: 5 });
  chrome.alarms.create(ALARM_PING,           { periodInMinutes: 30 });
  chrome.alarms.create(ALARM_POLL_COMMANDS,  { periodInMinutes: 0.5 });
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
  } else if (alarm.name === ALARM_POLL_COMMANDS) {
    // 服务器"完整 loop"的核心：拉 capture_now 指令并派发到任意 m.ctrip tab
    await pollAndDispatchCommands();
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
 * 服务器"完整 loop"的核心：
 * 1) GET /api/extension/commands  拉未消费的 capture_now 指令
 *    （同时写一行心跳，admin 报头可见"扩展最后活跃"）
 * 2) 找到任意 m.ctrip tab（不限于活动 tab），
 *    若指令指定 viewid 则优先匹配该 viewid 的 tab
 * 3) 派发 capture_now → content.js 主世界抓一轮 → 上传 round
 * 4) POST /api/extension/commands/{id}/ack 回传结果（triggered / no_tab / error）
 *
 * 设计要点：
 *  - 没有可用 tab 时，命令留在 DB 里下次轮询再发——不丢指令
 *  - ack 失败也不重试指令（避免浪费抓取），由 server 端 timeout 视为过期
 *  - content.js 不在线时 sendMessage 抛错，捕获后 ack(noop)
 */
async function pollAndDispatchCommands() {
  const c = await loadCfg();
  if (!c.server || !c.apiSecret) return;

  let j;
  try {
    const r = await fetch(`${c.server}/api/extension/commands`, {
      method: "GET",
      headers: {
        "X-API-Secret": c.apiSecret,
        "X-Extension-Ver": chrome.runtime.getManifest().version,
      },
    });
    if (!r.ok) return;
    j = await r.json();
  } catch (e) {
    console.warn("[ctrip] poll commands failed", e);
    return;
  }

  const cmds = (j && j.commands) || [];
  if (!cmds.length) return;

  // 任何 m.ctrip tab（不只是活动 tab）
  const tabs = await chrome.tabs.query({ url: "*://m.ctrip.com/*" }).catch(() => []);
  if (!tabs.length) {
    // 没 tab 时只把当前这批 ack 成 no_tab，让 admin 可见
    for (const cmd of cmds) {
      fetch(`${c.server}/api/extension/commands/${cmd.id}/ack`, {
        method: "POST",
        headers: { "X-API-Secret": c.apiSecret, "Content-Type": "application/json",
                   "X-Extension-Ver": chrome.runtime.getManifest().version },
        body: JSON.stringify({ result: "no_tab" }),
      }).catch(() => {});
    }
    return;
  }

  function pickTab(viewid) {
    if (!viewid) return tabs[0];
    // 按优先级排序：you/sight/* 优先（数据全），xtnt/ticket-detail 次之
    // （只有门票详情、无货架），最后兜底 tabs[0]。
    const score = (u) => {
      if (/\/you\/sight\//.test(u.pathname)) return 0;
      if (/\/xtnt\//.test(u.pathname)) return 1;
      return 2;
    };
    const matches = tabs.filter((t) => {
      try {
        const u = new URL(t.url);
        const q = u.searchParams.get("viewId");
        if (q && q === String(viewid)) return true;
        const pathMatch = (u.pathname + u.search).match(/\/(\d{2,6})(?:\.html)?(?:[?#]|$)/);
        if (pathMatch && pathMatch[1] === String(viewid)) return true;
        return false;
      } catch (_) { return false; }
    });
    if (matches.length) {
      matches.sort((a, b) => {
        try { return score(new URL(a.url)) - score(new URL(b.url)); } catch (_) { return 0; }
      });
      return matches[0];
    }
    return tabs[0];
  }

  for (const cmd of cmds) {
    const viewid = cmd.args && cmd.args.viewid;
    const tab = pickTab(viewid);
    let result = "triggered";
    let error = "";
    try {
      // 单次 executeScript 同时注入 main + capture_main，然后直接调用 capture_now。
      // 解决 MV3 缓存场景下 content.js（ISOLATED）不自动注入新 tab 的问题。
      try {
        const capRes = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: "MAIN",
          files: ["content_main.js", "capture_main.js"],
        });
        // 立刻调一次 __ctrip_sentry_capture_now。MAIN world 没有 chrome.runtime，
        // 所以走 fetch 直传到 /api/ingest/round —— server + apiSecret + ver 作为
        // args 传过去。22s 内会等 SPA 首屏 fetch。
        const execArgs = [c.server, c.apiSecret, chrome.runtime.getManifest().version];
        const execRes = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: "MAIN",
          func: (server, apiSecret, extVer) => {
            if (typeof window.__ctrip_sentry_capture_now === "function") {
              return window.__ctrip_sentry_capture_now(server, apiSecret, extVer);
            }
            return { ok: false, reason: "capture_main_not_loaded",
                     keys: Object.keys(window).filter(k => k.startsWith("__ctrip")).slice(0, 5) };
          },
          args: execArgs,
        });
        const r = execRes && execRes[0] && execRes[0].result;
        if (r && r.ok) {
          result = "uploaded";
        } else if (r && r.reason) {
          // MAIN 抓取没拿到数据，content.js 兜底再试一次（如果它还活着）
          try {
            await chrome.tabs.sendMessage(tab.id, { cmd: "capture_now" });
            result = "triggered:" + r.reason;
          } catch (e2) {
            result = "no_content";
            error = String(e2 && e2.message || e2) + " | main=" + (r.reason || "?");
          }
        } else {
          result = "no_main_result";
          error = JSON.stringify(r).slice(0, 200);
        }
      } catch (e) {
        // MAIN 注入/调用失败（chrome:// 等不支持的 tab）→ 退到 content.js
        try {
          await chrome.tabs.sendMessage(tab.id, { cmd: "capture_now" });
          result = "triggered:exec_err";
          error = String(e && e.message || e);
        } catch (e2) {
          result = "no_content";
          error = String(e2 && e2.message || e2);
        }
      }
    } catch (e) {
      result = "error";
      error = String(e && e.message || e);
    }
    fetch(`${c.server}/api/extension/commands/${cmd.id}/ack`, {
      method: "POST",
      headers: { "X-API-Secret": c.apiSecret, "Content-Type": "application/json",
                 "X-Extension-Ver": chrome.runtime.getManifest().version },
      body: JSON.stringify({ result, error }),
    }).catch(() => {});
  }
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
    } else if (msg?.cmd === "reinject_main_world") {
      // content.js 触发：用 chrome.scripting.executeScript 把 content_main.js
      // 注入到 MAIN world，那里才能真正拦截 Ctrip 的 fetch 赋值。
      const tabId = sender?.tab?.id;
      if (!tabId) { sendResponse({ ok: false, error: "no_tab_id" }); return; }
      try {
        const res = await chrome.scripting.executeScript({
          target: { tabId },
          world: "MAIN",
          files: ["content_main.js"],
        });
        sendResponse({ ok: true, results: res?.length || 0, reason: msg.reason });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    } else if (msg?.cmd === "main_event") {
      // content.js 转发过来的 main world 事件（request_complete / request_seen / request_error）。
      // 目前不打日志（量大），留口子方便调试。
      // console.debug("[ctrip] main_event", msg.type, msg.payload?.url);
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