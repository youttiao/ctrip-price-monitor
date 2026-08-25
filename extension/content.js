// content.js — 注入到 ISOLATED world（manifest 注册）
// 职责：
//   1. 触发 background 用 chrome.scripting.executeScript({world:"MAIN"}) 把
//      content_main.js 注入到 main world，那里才会真正拦截 Ctrip 的 fetch。
//   2. 监听 main world 的 postMessage，转发给 background。
//   3. 监听 background 的消息（capture_now / get_poi / diagnose），
//      通过 window.__ctrip_sentry_get_inflight 等从 main world 拿数据。
//   4. SPA URL change 时清 inflight。

(function () {
  const SENTINEL = "[ctrip-sentry:isolated:v0.2.5]";
  console.log(SENTINEL, "loading on", location.href);
  try { document.documentElement.setAttribute("data-ctrip-sentry", "v0.2.3-3"); } catch (_) {}

  function detectPOIFromURL() {
    try {
      const u = new URL(location.href);
      const q = u.searchParams.get("viewId");
      if (q && /^\d+$/.test(q)) return { viewid: +q, name: document.title || "", source: "query" };
      const m = (u.pathname + u.search).match(/\/(\d{2,6})/);
      if (m) return { viewid: +m[1], name: document.title || "", source: "path" };
    } catch (_) {}
    return null;
  }
  let currentPOI = detectPOIFromURL();

  // ---- 接 main world postMessage ----
  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || d.src !== "ctrip-sentry-main") return;
    try {
      chrome.runtime.sendMessage({ cmd: "main_event", type: d.type, payload: d.payload }).catch(() => {});
    } catch (_) {}
  });

  // ---- 触发 main world 注入（self-ping：background 收到后用 scripting.executeScript）----
  function requestMainWorldInject(reason) {
    try {
      chrome.runtime.sendMessage({ cmd: "reinject_main_world", reason: reason || "init" }).catch(() => {});
    } catch (_) {}
  }
  requestMainWorldInject("content_script_loaded");

  // ---- capture + upload ----
  // 实际抓一轮 inflight 并上传；无请求则返回 ok=false reason=no_requests
  // 等待策略：Ctrip SPA 启动后大约 15–25s 才发完首屏 fetch，所以这里给到 22s。
  // 用"最近 1.5s 累计数没涨"作为"已发完"信号。
  async function waitForRequestsStable(maxMs = 22000) {
    let lastCount = -1;
    let stableTicks = 0;
    const deadline = Date.now() + maxMs;
    while (Date.now() < deadline) {
      const st = (window.__ctrip_sentry_status && window.__ctrip_sentry_status()) || null;
      const have = (window.__ctrip_sentry_get_inflight && (window.__ctrip_sentry_get_inflight() || []).length) || 0;
      if (st && have > 0) {
        if (have === lastCount) stableTicks++; else { stableTicks = 0; lastCount = have; }
        if (stableTicks >= 6) return { waitedMs: Date.now() - (deadline - maxMs), lastCount };
      }
      await new Promise((r) => setTimeout(r, 250));
    }
    const have = (window.__ctrip_sentry_get_inflight && (window.__ctrip_sentry_get_inflight() || []).length) || 0;
    return { waitedMs: maxMs, lastCount: have };
  }

  async function doCapture() {
    if (!location.host.includes("ctrip.com")) return { ok: false, reason: "not_ctrip" };

    // 给 main world 时间收完正在飞的请求
    await waitForRequestsStable(22000);

    currentPOI = detectPOIFromURL();
    if (!currentPOI) return { ok: false, reason: "no_poi_in_url" };

    const reqs = (window.__ctrip_sentry_get_inflight && window.__ctrip_sentry_get_inflight()) || [];
    if (!reqs.length) return { ok: false, reason: "no_requests" };

    const payload = {
      capturedAt: new Date().toISOString(),
      poi: { viewid: currentPOI.viewid, name: currentPOI.name },
      pageUrl: location.href,
      requests: reqs,
    };

    try {
      const r = await chrome.runtime.sendMessage({ cmd: "upload_round", payload });
      return { ok: r?.ok, status: r?.status, requests: r?.requests, error: r?.error };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  // 从 background 收到 capture_now 时的入口
  // 历史:之前这里会在 doCapture 返回 no_requests 时 location.reload(),想等 SPA 重发
  //   请求再抓一次。但 reload 会销毁 isolated listener,popup 那条 await sendMessage 的
  //   Promise 还没拿到 sendResponse 就被 close 掉,reject 进 catch 后 popup 显示
  //   "未注入 content script" — 误导(实际注入成功了,只是 channel 被 reload 关闭了)。
  // 现在:no_requests 直接返回,popup 那边显示"再抓一轮"提示;reload-then-recapture
  //   这条机制本来也只有 popup 路径触发,后台 pollAndDispatchCommands 走的是
  //   capture_main.js,不经过这里。
  async function captureAndUpload() {
    const r = await doCapture();
    return r;
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg?.cmd === "capture_now") {
      console.log("[ctrip-sentry:isolated] capture_now received");
      // 触发 main world 兜底重注入，避免用户中途刷新失败
      requestMainWorldInject("capture_now");
      captureAndUpload().then((r) => {
        console.log("[ctrip-sentry:isolated] capture_now result", r);
        sendResponse(r);
      });
      return true;
    }
    if (msg?.cmd === "get_poi") {
      const poi = currentPOI || detectPOIFromURL();
      sendResponse(poi || null);
      return false;
    }
    if (msg?.cmd === "diagnose") {
      let mainStatus = null;
      try {
        if (window.__ctrip_sentry_get_inflight && window.__ctrip_sentry_status) {
          const s = window.__ctrip_sentry_status();
          const inflightCount = s.inflight;
          mainStatus = { ...s, requests: (window.__ctrip_sentry_get_inflight() || []).length, inflightCount };
        }
      } catch (_) {}
      sendResponse({
        url: location.href,
        mainInstalled: !!window.__ctrip_sentry_main_installed,
        mainStatus,
      });
      return false;
    }
    if (msg?.cmd === "clear_inflight") {
      try { window.__ctrip_sentry_clear_inflight && window.__ctrip_sentry_clear_inflight(); } catch (_) {}
      sendResponse({ ok: true });
      return false;
    }
  });

  // SPA URL 切换清空缓存
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      try { window.__ctrip_sentry_clear_inflight && window.__ctrip_sentry_clear_inflight(); } catch (_) {}
      currentPOI = detectPOIFromURL();
    }
  }, 1500);

  // ---- 旧:auto-reload 后再 capture 的机制 (captureAndUpload 不再 reload,这里成死代码)
  //      保留 tryAutoCaptureAfterReload 的目的是:万一将来 background 派发的命令里
  //      重新启用 reload-then-recapture 流程,可以在这里接住。日常 popup 路径不再触发它。
  //      调用约定:谁要触发 reload 后自动 capture,就在 reload 前
  //        sessionStorage.setItem("__ctrip_sentry_capture_pending", "1")。
  async function tryAutoCaptureAfterReload() {
    let pending = false;
    try {
      pending = sessionStorage.getItem("__ctrip_sentry_capture_pending") === "1";
    } catch (_) {}
    if (!pending) return;
    try { sessionStorage.removeItem("__ctrip_sentry_capture_pending"); } catch (_) {}
    console.log("[ctrip-sentry:isolated] auto-capture-after-reload: flag found, waiting for SPA fetches");

    const w = await waitForRequestsStable(22000);
    console.log("[ctrip-sentry:isolated] auto-capture-after-reload: stable-wait done", w);
    const r = await doCapture();
    console.log("[ctrip-sentry:isolated] auto-capture-after-reload result", r);
  }
  // 给 SPA 一拍启动时间再开始等
  setTimeout(tryAutoCaptureAfterReload, 800);
})();
