// content_main.js — 注入到 MAIN world
// 策略：用 accessor descriptor 把 window.fetch / XMLHttpRequest.prototype.{open,send}
//        升级为带 setter 的 descriptor。Ctrip SDK 后续每次赋值都会被 setter 拦到，
//        自动包成 wrapped 版本。
// 因为跑在 MAIN world，能真正影响 Ctrip H5 SDK 的 fetch 调用。
//
// 与 isolated world 的 content.js 通信：window.postMessage，
//   src === 'ctrip-sentry-main' 时由 content.js 监听并转发到 background。

(function () {
  if (window.__ctrip_sentry_main_installed) return;
  window.__ctrip_sentry_main_installed = true;

  const SENTINEL = "[ctrip-sentry:main:v0.2.5]";
  console.log(SENTINEL, "loading on", location.href);

  const TARGET_PATHS = [
    "/restapi/soa2/21052/json/getProductShelf",
    "/restapi/soa2/12530/json/resourceAddInfo",
    "/restapi/h5api/globalsearch/search",
    "/restapi/soa2/14580/json/getProductPriceCalendar",
  ];
  function isCtripTarget(url) {
    if (!url) return false;
    for (const p of TARGET_PATHS) if (String(url).indexOf(p) !== -1) return true;
    return false;
  }

  // 共享 inflight 表（main world 里维护，postMessage 出去由 isolated world 上报）
  const inflight = new Map();
  let counter = 0;

  // document_start 锁定最原生 fetch / XHR
  const REAL_FETCH = window.fetch && window.fetch.bind(window);
  const REAL_XHR_OPEN = XMLHttpRequest.prototype.open;
  const REAL_XHR_SEND = XMLHttpRequest.prototype.send;

  function markWrapped(fn) {
    try {
      Object.defineProperty(fn, "__ctrip_sentry_wrapped", { value: true, configurable: false });
    } catch (_) {
      try { fn.__ctrip_sentry_wrapped = true; } catch (__) {}
    }
    return fn;
  }

  function emit(type, payload) {
    try {
      window.postMessage({ src: "ctrip-sentry-main", type, payload }, "*");
    } catch (_) {}
  }

  // ---- fetch accessor ----
  function installFetchAccessor() {
    if (window.__ctrip_sentry_fetch_installed) return true;
    try {
      const currentRaw = window.fetch;
      window.__ctrip_sentry_fetch_raw__ = currentRaw;
      window.__ctrip_sentry_fetch_installed = true;
      Object.defineProperty(window, "fetch", {
        configurable: true,
        enumerable: true,
        get() {
          const raw = window.__ctrip_sentry_fetch_raw__ || REAL_FETCH;
          const patched = function (input, init) {
            const url = (typeof input === "string" ? input : (input && input.url)) || "";
            const method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
            const startedAt = Date.now();
            const id = ++counter;
            const postData = init && init.body ? String(init.body) : undefined;
            const meta = { id, url, method, postData, startedAt };
            inflight.set(id, meta);
            let resp;
            try {
              resp = raw(input, init);
            } catch (e) {
              meta.error = String(e);
              emit("request_error", meta);
              throw e;
            }
            if (isCtripTarget(url) && resp && typeof resp.then === "function") {
              Promise.resolve(resp).then(async (r) => {
                try {
                  const text = await r.clone().text();
                  meta.responseBody = text;
                  meta.responseStatus = r.status;
                  meta.completedAt = Date.now();
                  emit("request_complete", meta);
                } catch (e) {
                  meta.responseError = String(e);
                  emit("request_complete", meta);
                }
              });
            } else {
              meta.skipped = "not_target";
              emit("request_seen", meta);
            }
            return resp;
          };
          return markWrapped(patched);
        },
        set(v) {
          // Ctrip 后续每次赋值都会被这里拦到，自动更新 raw ref，
          // 下次 getter 拿到的就是这个 raw + 我们的 wrapper。
          window.__ctrip_sentry_fetch_raw__ = v;
        },
      });
      return true;
    } catch (e) {
      console.warn(SENTINEL, "installFetchAccessor failed", e);
      return false;
    }
  }

  // ---- XHR accessor ----
  function installXHRAccessors() {
    if (window.__ctrip_sentry_xhr_installed) return true;
    try {
      Object.defineProperty(XMLHttpRequest.prototype, "open", {
        configurable: true,
        writable: true,
        value: markWrapped(function (method, url) {
          this.__ctrip = {
            id: ++counter,
            method: String(method),
            url: String(url),
            startedAt: Date.now(),
            postData: undefined,
          };
          inflight.set(this.__ctrip.id, this.__ctrip);
          return REAL_XHR_OPEN.apply(this, arguments);
        }),
      });
      Object.defineProperty(XMLHttpRequest.prototype, "send", {
        configurable: true,
        writable: true,
        value: markWrapped(function (body) {
          const meta = this.__ctrip || (this.__ctrip = {});
          if (body) meta.postData = String(body);
          const self = this;
          this.addEventListener("loadend", function () {
            if (!meta.url) return;
            if (!isCtripTarget(meta.url)) return;
            meta.responseBody = self.responseText;
            meta.responseStatus = self.status;
            meta.completedAt = Date.now();
            emit("request_complete", meta);
          });
          return REAL_XHR_SEND.apply(this, arguments);
        }),
      });
      window.__ctrip_sentry_xhr_installed = true;
      return true;
    } catch (e) {
      console.warn(SENTINEL, "installXHRAccessors failed", e);
      return false;
    }
  }

  installFetchAccessor();
  installXHRAccessors();

  // 兜底：万一 descriptor 不可配置（极端情况），反复重试 3 秒
  let n = 0;
  const earlyId = setInterval(() => {
    if (!window.__ctrip_sentry_fetch_installed) installFetchAccessor();
    if (!window.__ctrip_sentry_xhr_installed) installXHRAccessors();
    if (window.__ctrip_sentry_fetch_installed && window.__ctrip_sentry_xhr_installed) {
      clearInterval(earlyId);
      return;
    }
    if (++n > 30) clearInterval(earlyId);
  }, 100);

  document.addEventListener("DOMContentLoaded", () => { installFetchAccessor(); installXHRAccessors(); });
  window.addEventListener("load", () => { installFetchAccessor(); installXHRAccessors(); });

  // 暴露给 isolated world 调用
  window.__ctrip_sentry_get_inflight = () => {
    const out = [];
    for (const [, m] of inflight) {
      if (m.responseBody) {
        out.push({
          url: m.url,
          method: m.method,
          postData: m.postData ? { text: m.postData } : undefined,
          response: { status: m.responseStatus, bodyText: m.responseBody },
        });
      }
    }
    const dedup = new Map();
    for (const r of out) dedup.set(r.url, r);
    return Array.from(dedup.values());
  };
  window.__ctrip_sentry_clear_inflight = () => inflight.clear();
  window.__ctrip_sentry_status = () => {
    const f = window.fetch;
    const o = XMLHttpRequest.prototype.open;
    return {
      fetchWrapped: !!(f && f.__ctrip_sentry_wrapped),
      xhrOpenWrapped: !!(o && o.__ctrip_sentry_wrapped),
      fetchRawIsCtrip: (() => {
        try {
          const r = window.__ctrip_sentry_fetch_raw__;
          return r && /getConfig/.test(String(r));
        } catch (_) { return false; }
      })(),
      inflight: inflight.size,
    };
  };
})();
