// content script — 注入到 m.ctrip.com/*
// 策略：用 PerformanceObserver 捕获 soa2 请求/响应。
// 因为 soa2 调用有 w-payload-source + 必须从浏览器发出，我们只能在浏览器侧抓。

(function () {
  const TARGET_HOST = "m.ctrip.com";
  const SHELF_PATH = "/restapi/soa2/21052/json/getProductShelf";
  const ADDINFO_PATH = "/restapi/soa2/12530/json/resourceAddInfo";
  const SEARCH_PATH = "/restapi/h5api/globalsearch/search";

  /** @type { Map<string, { url: string, method: string, postData?: string, startedAt: number }> } */
  const inflight = new Map();
  /** 当前 POI context（从 URL 推断） */
  let currentPOI = null;

  function detectPOIFromURL() {
    const u = new URL(location.href);
    const path = u.pathname + u.search;
    // 1) query ?viewId=
    const q = u.searchParams.get("viewId");
    if (q && /^\d+$/.test(q)) return { viewid: +q, name: "", source: "query" };
    // 2) path: /sight/233.html 之类的
    const m = path.match(/\/(\d{2,6})/);
    if (m) return { viewid: +m[1], name: document.title || "", source: "path" };
    return null;
  }

  function shouldCapture() {
    return location.host.includes(TARGET_HOST);
  }

  async function readResponseBody(response) {
    try {
      // response.clone() 防止 stream 被消费
      return await response.clone().text();
    } catch (_) {
      return "";
    }
  }

  function installPerfObserver() {
    if (!window.PerformanceObserver) return;
    try {
      const obs = new PerformanceObserver(async (list) => {
        for (const entry of list.getEntries()) {
          // PerformanceResourceEntry 没有 body，但有 timing + name
          // 我们仍记录请求；响应体在 fetch hook 里取
        }
      });
      obs.observe({ type: "resource", buffered: true });
    } catch (_) {}
  }

  // 劫持 fetch + XHR（这是拿 body 的唯一办法）
  function hookFetch() {
    const origFetch = window.fetch;
    window.fetch = async function patchedFetch(input, init) {
      const url = (typeof input === "string" ? input : input?.url) || "";
      const method = (init?.method || "GET").toUpperCase();
      const startedAt = Date.now();
      const postData = init?.body ? String(init.body) : undefined;
      const key = method + " " + url + " " + startedAt;
      inflight.set(key, { url, method, postData, startedAt });

      try {
        const resp = await origFetch.apply(this, arguments);
        // 仅关心 soa2 路径
        if (url.includes("/restapi/soa2/21052/json/getProductShelf") ||
            url.includes("/restapi/soa2/12530/json/resourceAddInfo") ||
            url.includes("/restapi/h5api/globalsearch/search")) {
          const text = await readResponseBody(resp);
          inflight.get(key).responseBody = text;
          inflight.get(key).responseStatus = resp.status;
        }
        return resp;
      } catch (e) {
        inflight.get(key).error = String(e);
        throw e;
      }
    };
  }

  function hookXHR() {
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__ctrip = { method, url, startedAt: Date.now(), postData: undefined };
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
      const meta = this.__ctrip || {};
      meta.postData = body ? String(body) : undefined;
      const self = this;
      this.addEventListener("loadend", function () {
        if (!meta.url) return;
        if (meta.url.includes("/restapi/soa2/21052/json/getProductShelf") ||
            meta.url.includes("/restapi/soa2/12530/json/resourceAddInfo") ||
            meta.url.includes("/restapi/h5api/globalsearch/search")) {
          meta.responseBody = self.responseText;
          meta.responseStatus = self.status;
        }
      });
      return origSend.apply(this, arguments);
    };
  }

  /** 收集一个 round 并上传。 */
  async function captureAndUpload() {
    if (!shouldCapture()) return { ok: false, reason: "not_ctrip" };

    // 等待最近请求完成（最多 2s）
    await new Promise((r) => setTimeout(r, 1500));

    currentPOI = detectPOIFromURL();
    if (!currentPOI) return { ok: false, reason: "no_poi_in_url" };

    const reqs = [];
    for (const [, meta] of inflight) {
      if (!meta.responseBody) continue;
      try {
        reqs.push({
          url: meta.url,
          method: meta.method,
          postData: meta.postData ? { text: meta.postData } : undefined,
          response: { status: meta.responseStatus, bodyText: meta.responseBody },
        });
      } catch (_) {}
    }

    if (!reqs.length) return { ok: false, reason: "no_requests" };

    // 去重（同一 URL 同一分钟内只保留最新）
    const dedup = new Map();
    for (const r of reqs) {
      const k = r.url;
      const cur = dedup.get(k);
      if (!cur) dedup.set(k, r);
    }

    const payload = {
      capturedAt: new Date().toISOString(),
      poi: { viewid: currentPOI.viewid, name: currentPOI.name },
      pageUrl: location.href,
      requests: Array.from(dedup.values()),
    };

    try {
      const r = await chrome.runtime.sendMessage({ cmd: "upload_round", payload });
      return r;
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  // 监听来自 background 的命令
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg?.cmd === "capture_now") {
      captureAndUpload().then(sendResponse);
      return true;
    }
  });

  // 初始化
  hookFetch();
  hookXHR();
  installPerfObserver();

  // 页面变化时清空 in-flight
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      inflight.clear();
      currentPOI = detectPOIFromURL();
    }
  }, 1500);
})();