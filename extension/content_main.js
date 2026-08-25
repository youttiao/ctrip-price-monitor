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

  const SENTINEL = "[ctrip-sentry:main:v0.2.22]";
  console.log(SENTINEL, "loading on", location.href);

  const TARGET_PATHS = [
    "/restapi/soa2/21052/json/getProductShelf",
    "/restapi/soa2/12530/json/resourceAddInfo",
    "/restapi/h5api/globalsearch/search",
    "/restapi/soa2/14580/json/getProductPriceCalendar",
    // POI 详情页主调：拿 POI 元信息（名字、地址、图片）。detail 页会先发这个再发 shelf。
    "/restapi/soa2/14509/json/GetSightOverview",
    // 单 SKU 详情：圆明园/雍和宫详情页上点「查看详情」会触发；server parser 用不到但完整
    // 抓包对调试有用。
    "/restapi/soa2/12314/json/resourceDetails",
  ];

  // 主动 fire 用到的端点（浏览器 fetch 自动带 cookie + fingerprint，无需 w-payload-source）
  // 注意：旧 14509/GetSightOverview 在 2026-08 已返 404 整个死掉了；20036/getSightExtendInfo
  //       接替了但只返 393 字节短摘要，parser 不用，proactive 不调它。
  const PROACTIVE_ENDPOINTS = {
    ADDINFO_URL:   "https://m.ctrip.com/restapi/soa2/12530/json/resourceAddInfo",
    PRICE_CAL_URL: "https://m.ctrip.com/restapi/soa2/14580/json/getProductPriceCalendar",
  };
  // 单轮主动 fire 的 resourceAddInfo 上限。详情页 shelf 通常 5-15 个 SKU，30 足够；
  // 同时也是防 server 429 / browser socket 耗尽。
  const PROACTIVE_ADDINFO_CAP = 20;
  // 两次 addInfo 之间的最小间隔（ms），避免瞬时风暴触发风控
  const PROACTIVE_STAGGER_MS = 80;
  // 主动 fire 完后等待响应的最大时长
  const PROACTIVE_WAIT_MS = 12000;
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
                  console.log(SENTINEL, "✓ captured", url.replace(/^https?:\/\/[^/]+/, "").substring(0, 80), "HTTP", r.status, `(${meta.completedAt - meta.startedAt}ms)`);
                  emit("request_complete", meta);
                } catch (e) {
                  meta.responseError = String(e);
                  console.warn(SENTINEL, "✗ read body failed", url.replace(/^https?:\/\/[^/]+/, ""), e);
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
            console.log(SENTINEL, "✓ XHR captured", String(meta.url).substring(0, 80), "HTTP", self.status);
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
  //   dedup key = url + canonical(postData) 哈希。
  //
  // 历史 bug (2026-08-25): ctrip SPA 会在用户操作时反复重发同一 (URL, rid)
  // 的 priceCalendar/addInfo 调用；每次重发都换 ic_traceid（UUID），导致
  // URL+body-hash dedup 失效，单 round 累积 315 个 priceCalendar + 315 个
  // addInfo（~16MB），upload 耗时 10+ 分钟，502 偶发 → CORS 阻塞。
  //
  // 修法：dedup 前 canonicalize 掉所有 volatile 字段（UUID / 时间戳 /
  // randomUUID）。同 (URL, rid) 的多次 fire 折叠为一条记录，payload 从
  // ~16MB 降至 ~1MB，upload < 1s。
  const VOLATILE_KEYS = new Set([
    "ic_traceid", "traceid", "traceId", "_t", "ts", "timestamp",
    "nonce", "requestId", "rid_token", "randomId",
  ]);
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  function canonicalizeBody(parsed) {
    if (Array.isArray(parsed)) {
      return parsed.map(canonicalizeBody);
    }
    if (parsed && typeof parsed === "object") {
      const out = {};
      for (const k of Object.keys(parsed)) {
        if (VOLATILE_KEYS.has(k)) continue;
        const v = parsed[k];
        if (typeof v === "string" && UUID_RE.test(v)) continue;
        out[k] = canonicalizeBody(v);
      }
      return out;
    }
    return parsed;
  }
  function stableStringify(v) {
    if (v === null || typeof v !== "object") return JSON.stringify(v);
    if (Array.isArray(v)) {
      return "[" + v.map(stableStringify).join(",") + "]";
    }
    const keys = Object.keys(v).sort();
    return "{" + keys.map(k => JSON.stringify(k) + ":" + stableStringify(v[k])).join(",") + "}";
  }
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
    for (const r of out) {
      const raw = r.postData && r.postData.text ? r.postData.text : "";
      let canon = raw;
      if (raw) {
        try {
          const parsed = JSON.parse(raw);
          canon = stableStringify(canonicalizeBody(parsed));
        } catch (_) { /* 解析失败则用原 body */ }
      }
      // 32-bit djb2 hash on canonical body
      let h = 5381;
      for (let i = 0; i < canon.length; i++) {
        h = ((h << 5) + h) + canon.charCodeAt(i);
        h |= 0;
      }
      const k = r.url + "::" + canon.length + ":" + h.toString(36);
      dedup.set(k, r);
    }
    return Array.from(dedup.values());
  };
  // 给 popup 进度用：返回每个 TARGET_PATH 端点的实时状态
  //   state: 'pending' (in flight) / 'completed' (有响应) / 'error' (有 error) / 'missing' (还没 start)
  //   含 completed 数 + pending 数 + per-target 列表
  // 用 path 段（如 "/json/getProductShelf"）做 key 去重。
  window.__ctrip_sentry_get_progress = () => {
    const targets = TARGET_PATHS.map((p) => {
      const seg = p.replace(/^.*?\/json\//, "/json/").replace(/^\/restapi\/h5api\//, "/h5api/");
      return { path: p, key: seg, state: "missing", status: null };
    });
    const byKey = new Map(targets.map((t) => [t.key, t]));
    for (const [, m] of inflight) {
      if (!isCtripTarget(m.url)) continue;
      let key = null;
      for (const [k, t] of byKey) if (m.url.indexOf(t.path) !== -1) { key = k; break; }
      if (!key) continue;
      const t = byKey.get(key);
      if (m.responseBody) { t.state = "completed"; t.status = m.responseStatus; }
      else if (m.responseError) { t.state = "error"; t.status = m.responseStatus; }
      else if (t.state !== "completed") { t.state = "pending"; }
    }
    const completed = targets.filter((t) => t.state === "completed").length;
    const pending = targets.filter((t) => t.state === "pending").length;
    const error = targets.filter((t) => t.state === "error").length;
    return { targets, completed, pending, error, missing: targets.length - completed - pending - error };
  };
  window.__ctrip_sentry_clear_inflight = () => inflight.clear();
  window.__ctrip_sentry_status = () => {
    const f = window.fetch;
    const o = XMLHttpRequest.prototype.open;
    let pendingCount = 0, completedCount = 0;
    for (const [, m] of inflight) {
      if (m.responseBody || m.responseError) completedCount++;
      else pendingCount++;
    }
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
      pending: pendingCount,
      completed: completedCount,
    };
  };

  // ────────────────────────────────────────────────────────────────────
  // 主动 fire：在页面自然首屏 fetch 完成后，自动补齐 详情页 SPA 不会主动发的
  // endpoint，让一次 capture 拿到完整的 (shelf + addInfo × N + overview)。
  // 否则用户得手动点每个 SKU「查看详情」才能拿到 vendor，价格日历也是同样的
  // 触发条件。这是「自动化抓取」的关键。
  //
  // 浏览器内 fetch 自动带 cookie + fingerprint + w-payload-source(自动生成)，
  // 不需要 wsm 反爬绕过。服务器端裸 httpx 会被反爬挡死，扩展走这条路完全合法。
  //
  // 暴露两个全局：
  //   window.__ctrip_sentry_proactive_fire(viewid) → {fired, errors, total, reason?}
  //   window.__ctrip_sentry_proactive_state()      → {phase, fired, errors, total}
  // ────────────────────────────────────────────────────────────────────

  const PROACTIVE_STATE = { phase: "idle", fired: 0, errors: 0, total: 0 };

  function getGuidFromCookie() {
    try {
      const m = document.cookie.match(/(?:^|;\s*)GUID=([^;]+)/);
      return m ? m[1] : "";
    } catch (_) { return ""; }
  }

  function addinfoPayload(rid, viewid, productId) {
    const today = new Date().toISOString().slice(0, 10);
    return {
      resids: [Number(rid)],
      viewid: Number(viewid),
      productId: Number(productId) || 0,
      filters: [{ type: "DateFilter", filterItems: [{ key: "Date", value: today }] }],
      tags: [
        { key: "addinfo", value: "structureTicketexchanges" },
        { key: "refundRules", value: "newLogic" },
        { key: "needStructuralReserveDetail", value: "T" },
        { key: "needForcedLogin", value: "T" },
        { key: "needRateLimit", value: "T" },
        { key: "needScenicUseInfo", value: "T" },
      ],
      head: {
        cid: getGuidFromCookie(), ctok: "", cver: "1.0", lang: "01",
        sid: "8888", syscode: "09", auth: "", xsid: "",
        extension: [
          { name: "aid", value: "66672" },
          { name: "sid", value: "1693366" },
          { name: "H5", value: "H5" },
        ],
      },
      clientInfo: {
        currency: "CNY", locale: "zh-CN", pageId: 10650097502,
        channelId: 116, extension: [], oriSyscode: "09", syscode: "09",
        cid: "", appPlatform: "",
        ic_traceid: (crypto && crypto.randomUUID ? crypto.randomUUID() : ""),
      },
      enviroment: "PROD",
      extInfo: [],
      subResourceList: [],
    };
  }

  // _CALENDAR_TAGS — 与 ctrip_core/selectors.py:144-163 同源
  const CALENDAR_TAGS = [
    { key: "relatedResource", value: "newLogic" },
    { key: "needReturnUnavailableDate", value: "true" },
    { key: "noNeedTicketRelationResources", value: "true" },
    { key: "needSelectDateFirst", value: "true" },
    { key: "needSelectDateFirstV2", value: "true" },
    { key: "needSelectDateSort", value: "true" },
    { key: "supportAlternateTkt", value: "true" },
    { key: "needForcedLogin", value: "T" },
    { key: "needCardTagInfo", value: "true" },
    { key: "needPackingVersion3", value: "true" },
    { key: "needReservationMark", value: "true" },
    { key: "needRateLimit", value: "true" },
    { key: "needUnSaleAloneRes", value: "true" },
    { key: "needAggregationInfo", value: "true" },
    { key: "callRecallPK", value: "pkOneOrMore" },
    { key: "seckill", value: "newSeckill" },
    { key: "needResourceMinPriceInfo", value: "true" },
    { key: "needCalcTicketPriceCalendar", value: "true" },
  ];

  // 参考 ctrip_core/selectors.py:166-200 price_calendar_payload。
  // poiIdStr 必须是 URL 路径里的 poiId（字符串，如 "75599"），不是 viewid。
  function priceCalendarPayload(rid, poiIdStr) {
    return {
      bizLineType: 4,
      id: "",
      token: "",
      needAggregations: true,
      needBasicInfo: true,
      needSaleProperties: true,
      needUnavailableSaleDates: true,
      needSaleStatistics: true,
      needTags: true,
      tags: CALENDAR_TAGS,
      filter: { recommendScan: false, beginDate: "", endDate: "" },
      poiId: String(poiIdStr),
      mainResourceIds: [Number(rid)],
      head: {
        cid: getGuidFromCookie(),
        ctok: "",
        cver: "1.0",
        lang: "01",
        sid: "8888",
        syscode: "09",
        auth: "",
        xsid: "",
        extension: [
          { name: "aid", value: "66672" },
          { name: "sid", value: "1693366" },
          { name: "H5", value: "H5" },
        ],
      },
      clientInfo: {
        currency: "CNY",
        locale: "zh-CN",
        pageId: 10650097502,
        channelId: 116,
        extension: [],
        oriSyscode: "09",
        syscode: "09",
        cid: "",
        appPlatform: "",
        ic_traceid: (crypto && crypto.randomUUID ? crypto.randomUUID() : ""),
      },
      enviroment: "PROD",
    };
  }

  function findShelfBody() {
    const captured = (window.__ctrip_sentry_get_inflight && window.__ctrip_sentry_get_inflight()) || [];
    const shelf = captured.find((r) => String(r.url).includes("getProductShelf"));
    if (!shelf || !shelf.response || !shelf.response.bodyText) return null;
    try { return JSON.parse(shelf.response.bodyText); } catch (_) { return null; }
  }

  async function fireOne(url, payload, label, ident) {
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "include",
      });
      console.log(SENTINEL, `✓ proactive ${label}`, ident, "HTTP", r.status);
      PROACTIVE_STATE.fired++;
      return r.ok;
    } catch (e) {
      console.warn(SENTINEL, `✗ proactive ${label}`, ident, e);
      PROACTIVE_STATE.errors++;
      return false;
    }
  }

  window.__ctrip_sentry_proactive_fire = async function (viewid) {
    PROACTIVE_STATE.phase = "running";
    PROACTIVE_STATE.fired = 0;
    PROACTIVE_STATE.errors = 0;
    PROACTIVE_STATE.total = 0;

    if (!viewid) {
      PROACTIVE_STATE.phase = "skipped";
      return { fired: 0, errors: 0, total: 0, reason: "no_viewid" };
    }

    const shelf = findShelfBody();
    if (!shelf) {
      PROACTIVE_STATE.phase = "skipped";
      console.log(SENTINEL, "proactive fire: no shelf captured, skip");
      return { fired: 0, errors: 0, total: 0, reason: "no_shelf_captured" };
    }

    const allResources = Array.isArray(shelf.resources) ? shelf.resources : [];
    const mineRids = [];
    for (const r of allResources) {
      if (Number(r.spotid) === Number(viewid)) {
        const rid = r.resourceId;
        const productId = (r.productIds && r.productIds[0]) || 0;
        if (rid) mineRids.push({ rid, productId });
      }
    }
    const poiId = shelf.poiId;

    if (!mineRids.length) {
      PROACTIVE_STATE.phase = "skipped";
      console.log(SENTINEL, "proactive fire: no resources for viewid", viewid, "poiId=", poiId);
      return { fired: 0, errors: 0, total: 0, reason: "no_resources", poiId };
    }

    const capped = mineRids.slice(0, PROACTIVE_ADDINFO_CAP);
    PROACTIVE_STATE.total = capped.length * 2; // addInfo + priceCalendar (每 rid × 2 calls)

    console.log(SENTINEL,
      `proactive fire start: viewid=${viewid} poiId=${poiId} resources=${capped.length}/${mineRids.length}`);

    // resourceAddInfo × N，每个带 stagger 避免瞬时风暴触发风控
    for (let i = 0; i < capped.length; i++) {
      fireOne(PROACTIVE_ENDPOINTS.ADDINFO_URL,
        addinfoPayload(capped[i].rid, viewid, capped[i].productId),
        "addInfo",
        `rid=${capped[i].rid} pid=${capped[i].productId}`);
      if (i < capped.length - 1) {
        await new Promise((r) => setTimeout(r, PROACTIVE_STAGGER_MS));
      }
    }

    // getProductPriceCalendar × N：拿每日价（calendar responses are big — 4-8KB each）
    for (let i = 0; i < capped.length; i++) {
      fireOne(PROACTIVE_ENDPOINTS.PRICE_CAL_URL,
        priceCalendarPayload(capped[i].rid, poiId),
        "priceCal",
        `rid=${capped[i].rid}`);
      if (i < capped.length - 1) {
        await new Promise((r) => setTimeout(r, PROACTIVE_STAGGER_MS));
      }
    }

    // 3) 等响应进 inflight map（wrapper 是异步 .then 读 body）
    await new Promise((r) => setTimeout(r, PROACTIVE_WAIT_MS));

    PROACTIVE_STATE.phase = "done";
    console.log(SENTINEL,
      `proactive fire done: fired=${PROACTIVE_STATE.fired} errors=${PROACTIVE_STATE.errors} total=${PROACTIVE_STATE.total}`);
    return {
      fired: PROACTIVE_STATE.fired,
      errors: PROACTIVE_STATE.errors,
      total: PROACTIVE_STATE.total,
      poiId,
      rids: capped.map((c) => c.rid),
    };
  };

  window.__ctrip_sentry_proactive_state = () => ({ ...PROACTIVE_STATE });
})();
