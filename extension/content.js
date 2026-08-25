// content script — 注入到 https://*.ctrip.com/*
// 策略：劫持 fetch + XHR 拿 soa2 响应体。
//
// 关键难点：Ctrip H5 SDK 在 document_start 之后异步模块里
//   `window.fetch = theirown` （用 data descriptor，不是 accessor）。
//   所以 defineProperty 拦截完全失效，只能靠轮询重新覆盖。
//   为了不让 wrapper 自己递归调用，我们必须在脚本最一开始就锁住
//   「最原子的 fetch」引用（此时它就是浏览器原生 fetch），之后永远用它。
//
// 时机：manifest 已声明 run_at=document_start。

(function () {
  const SENTINEL = "[ctrip-sentry:v0.2.3-2]";
  console.log(SENTINEL, "loading on", location.href);
  const TARGET_PATHS = [
    "/restapi/soa2/21052/json/getProductShelf",
    "/restapi/soa2/12530/json/resourceAddInfo",
    "/restapi/h5api/globalsearch/search",
  ];

  function isCtripTarget(url) {
    if (!url) return false;
    for (const p of TARGET_PATHS) if (url.includes(p)) return true;
    return false;
  }

  /** @type { Map<string, any> } */
  const inflight = new Map();
  let currentPOI = null;

  function detectPOIFromURL() {
    const u = new URL(location.href);
    const q = u.searchParams.get("viewId");
    if (q && /^\d+$/.test(q)) return { viewid: +q, name: document.title || "", source: "query" };
    const m = (u.pathname + u.search).match(/\/(\d{2,6})/);
    if (m) return { viewid: +m[1], name: document.title || "", source: "path" };
    return null;
  }

  // ---- 在 document_start 锁定「最原子的 fetch / XHR.open / XHR.send」 ----
  // 此时还没有任何页面脚本（document_start 是最早注入点）。
  const realFetch = window.fetch.bind(window);
  const realXHROpen = XMLHttpRequest.prototype.open;
  const realXHRSend = XMLHttpRequest.prototype.send;

  // 给 wrapped 函数挂 marker
  function markWrapped(fn) {
    try {
      Object.defineProperty(fn, "__ctrip_sentry_wrapped", { value: true, configurable: false });
    } catch (_) {
      fn.__ctrip_sentry_wrapped = true;
    }
    return fn;
  }

  // ---- fetch wrapper ----
  function makePatchedFetch() {
    const patched = function (input, init) {
      const url = (typeof input === "string" ? input : input?.url) || "";
      const method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
      const startedAt = Date.now();
      const postData = init && init.body ? String(init.body) : undefined;
      const key = method + " " + url + " " + startedAt;
      inflight.set(key, { url, method, postData, startedAt });
      let resp;
      try {
        resp = realFetch(input, init);
      } catch (e) {
        const m = inflight.get(key);
        if (m) m.error = String(e);
        throw e;
      }
      if (isCtripTarget(url) && resp && typeof resp.then === "function") {
        Promise.resolve(resp).then(async (r) => {
          try {
            const text = await r.clone().text();
            const m = inflight.get(key);
            if (m) { m.responseBody = text; m.responseStatus = r.status; }
          } catch (_) {}
        });
      }
      return resp;
    };
    return markWrapped(patched);
  }

  function installFetchHook() {
    if (window.fetch && window.fetch.__ctrip_sentry_wrapped) return;
    try {
      window.fetch = makePatchedFetch();
    } catch (_) {}
  }

  // ---- XHR wrapper ----
  function makePatchedXHROpen() {
    const patched = function (method, url) {
      this.__ctrip = { method, url: String(url), startedAt: Date.now(), postData: undefined };
      return realXHROpen.apply(this, arguments);
    };
    return markWrapped(patched);
  }
  function makePatchedXHRSend() {
    const patched = function (body) {
      const meta = this.__ctrip || (this.__ctrip = {});
      if (body) meta.postData = String(body);
      const self = this;
      this.addEventListener("loadend", function () {
        if (!meta.url) return;
        if (!isCtripTarget(meta.url)) return;
        meta.responseBody = self.responseText;
        meta.responseStatus = self.status;
      });
      return realXHRSend.apply(this, arguments);
    };
    return markWrapped(patched);
  }
  function installXHRHooks() {
    if (XMLHttpRequest.prototype.open.__ctrip_sentry_wrapped) return;
    try {
      XMLHttpRequest.prototype.open = makePatchedXHROpen();
    } catch (_) {}
    try {
      XMLHttpRequest.prototype.send = makePatchedXHRSend();
    } catch (_) {}
  }

  // 立刻装
  installFetchHook();
  installXHRHooks();

  // 早期密集轮询（前 15s，每 100ms 检查一次）
  let n = 0;
  const earlyId = setInterval(() => {
    installFetchHook();
    installXHRHooks();
    if (++n >= 150) clearInterval(earlyId);
  }, 100);

  // 之后每 2s 检查一次（Ctrip 偶尔会再覆盖）
  setInterval(() => {
    installFetchHook();
    installXHRHooks();
  }, 2000);

  // SDK 加载完可能会再触发一轮
  document.addEventListener("DOMContentLoaded", () => { installFetchHook(); installXHRHooks(); });
  window.addEventListener("load", () => { installFetchHook(); installXHRHooks(); });

  // ---- capture + upload ----

  async function captureAndUpload() {
    if (!location.host.includes("ctrip.com")) return { ok: false, reason: "not_ctrip" };

    // 等最近一波请求完成（最多 3s）
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      let anyInflight = false;
      for (const [, m] of inflight) {
        if (m.startedAt + 2500 > Date.now() && !m.responseBody && !m.error) { anyInflight = true; break; }
      }
      if (!anyInflight) break;
      await new Promise((r) => setTimeout(r, 200));
    }

    currentPOI = detectPOIFromURL();
    if (!currentPOI) return { ok: false, reason: "no_poi_in_url" };

    const reqs = [];
    for (const [, meta] of inflight) {
      if (!meta.responseBody) continue;
      reqs.push({
        url: meta.url,
        method: meta.method,
        postData: meta.postData ? { text: meta.postData } : undefined,
        response: { status: meta.responseStatus, bodyText: meta.responseBody },
      });
    }
    if (!reqs.length) return { ok: false, reason: "no_requests" };

    const dedup = new Map();
    for (const r of reqs) dedup.set(r.url, r);

    const payload = {
      capturedAt: new Date().toISOString(),
      poi: { viewid: currentPOI.viewid, name: currentPOI.name },
      pageUrl: location.href,
      requests: Array.from(dedup.values()),
    };

    try {
      const r = await chrome.runtime.sendMessage({ cmd: "upload_round", payload });
      return { ok: r?.ok, status: r?.status, requests: r?.requests, error: r?.error };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg?.cmd === "capture_now") {
      captureAndUpload().then(sendResponse);
      return true;
    }
    if (msg?.cmd === "get_poi") {
      const poi = currentPOI || detectPOIFromURL();
      sendResponse(poi || null);
      return false;
    }
    if (msg?.cmd === "diagnose") {
      const f = window.fetch;
      const o = XMLHttpRequest.prototype.open;
      sendResponse({
        url: location.href,
        fetchWrapped: !!(f && f.__ctrip_sentry_wrapped),
        xhrOpenWrapped: !!(o && o.__ctrip_sentry_wrapped),
        fetchToString: f ? String(f).slice(0, 120) : null,
        inflight: inflight.size,
      });
      return false;
    }
  });

  // SPA URL 切换清空缓存
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      inflight.clear();
      currentPOI = detectPOIFromURL();
    }
  }, 1500);
})();
