// capture_main.js — 注入到 MAIN world，从这里抓 inflight 并通过浏览器
// fetch 直接 POST 到 /api/ingest/round（绕开 chrome.runtime.sendMessage，
// 因为 chrome.runtime 在 MAIN world 中是 undefined）。
//
// 入口：
//   window.__ctrip_sentry_capture_now(server, apiSecret, extVer, options?)
//     → SW 通过 executeScript({world: "MAIN", func, args}) 调用
//
// 设计：与 background.js 的 pollAndDispatchCommands 对齐，SW 把 server 和
// apiSecret 作为参数传进来，capture_main 直接 fetch 上传，避免对
// chrome.runtime 的依赖。

(function () {
  const SENTINEL = "[ctrip-sentry:capture-main:v0.2.19]";
  if (window.__ctrip_sentry_capture_main_installed) return;
  window.__ctrip_sentry_capture_main_installed = true;

  console.log(SENTINEL, "loading on", location.href);

  function urlViewid() {
    try {
      const u = new URL(location.href);
      const q = u.searchParams.get("viewId");
      if (q && /^\d+$/.test(q)) return +q;
      const m = (u.pathname + u.search).match(/\/(\d{2,6})/);
      if (m) return +m[1];
    } catch (_) {}
    return null;
  }
  // 同 content.js：多源兜底取 POI 名。SPA 详情页 document.title 经常是模板名或空。
  function extractPOIName() {
    const candidates = [];
    try {
      const t = document.title && document.title.trim();
      if (t) candidates.push(t);
    } catch (_) {}
    try {
      const og = document.querySelector('meta[property="og:title"]');
      if (og && og.content) candidates.push(og.content.trim());
    } catch (_) {}
    try {
      const h1 = document.querySelector("h1");
      if (h1 && h1.textContent) candidates.push(h1.textContent.trim());
    } catch (_) {}
    const blacklist = /^(携程旅行|ctrip|c trip|trip\.com)/i;
    for (const raw of candidates) {
      const s = String(raw)
        .replace(/\s*[|_｜-]\s*(携程|c trip|trip\.com).*$/i, "")
        .replace(/^【.*?】/, "")
        .trim();
      if (!s) continue;
      if (blacklist.test(s)) continue;
      if (s.length > 40) continue;
      return s;
    }
    return "";
  }
  function detectPOIFromURL() {
    const viewid = urlViewid();
    if (!viewid) return null;
    return { viewid, name: extractPOIName(), source: "url" };
  }

  // 等 inflight 累计数稳定 1.5s（最多 22s）—— 与 isolated 同步策略一致
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

  async function uploadDirect(payload, server, apiSecret, extVer) {
    try {
      const r = await fetch(`${server}/api/ingest/round`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Secret": apiSecret,
          "X-Extension-Ver": extVer || "capture_main",
          "X-Source": "extension",
        },
        body: JSON.stringify(payload),
      });
      let body = null;
      try { body = await r.json(); } catch (_) {}
      return { ok: r.ok, status: r.status, body };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  // SW 调用的入口。参数：(server, apiSecret, extVer, options?)
  // options.testUrl: 测试 URL（仅诊断）
  window.__ctrip_sentry_capture_now = async function (server, apiSecret, extVer, options) {
    try {
      if (!location.host.includes("ctrip.com")) return { ok: false, reason: "not_ctrip" };
      if (!server || !apiSecret) return { ok: false, reason: "missing_args" };

      const poi = detectPOIFromURL();
      if (!poi) return { ok: false, reason: "no_poi_in_url" };

      // 1) 等 SPA 首屏 fetch 落到 buffer（最多 22s）
      const w = await waitForRequestsStable(22000);
      console.log(SENTINEL, "waitForRequestsStable result", w);
      let reqs = (window.__ctrip_sentry_get_inflight && window.__ctrip_sentry_get_inflight()) || [];

      // 2) 主动 fire overview + resourceAddInfo × N（与 popup 路径完全一致）
      //    这样 server 推任务过来时也是完整数据，不需要用户手动点 SKU
      let proactive = null;
      if (window.__ctrip_sentry_proactive_fire) {
        try {
          proactive = await window.__ctrip_sentry_proactive_fire(poi.viewid);
          console.log(SENTINEL, "proactive fire result", proactive);
        } catch (e) {
          console.warn(SENTINEL, "proactive fire threw", e);
        }
      }
      reqs = (window.__ctrip_sentry_get_inflight && window.__ctrip_sentry_get_inflight()) || [];

      if (!reqs.length) return { ok: false, reason: "no_requests" };

      const payload = {
        capturedAt: new Date().toISOString(),
        poi: { viewid: poi.viewid, name: poi.name },
        pageUrl: location.href,
        requests: reqs,
        proactive: proactive || undefined,
      };

      const uploadResult = await uploadDirect(payload, server, apiSecret, extVer);
      console.log(SENTINEL, "upload result", uploadResult);
      return {
        ok: uploadResult.ok,
        status: uploadResult.status,
        requests: reqs.length,
        body: uploadResult.body,
        error: uploadResult.error,
        proactive,
      };
    } catch (e) {
      console.error(SENTINEL, "capture_now threw", e);
      return { ok: false, error: String(e && e.message || e) };
    }
  };
})();
