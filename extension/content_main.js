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

  const SENTINEL = "[ctrip-sentry:main:v0.2.26]";
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
    // 单 chip 详情：官方页点 (可选人群) 下的儿童/老人/学生 chip 时触发，
    // 返回 sibling crowd child rid + 自带 crowd 后缀的 name。
    "/restapi/soa2/21052/getShelfResourceList",
  ];

  // 主动 fire 用到的端点（浏览器 fetch 自动带 cookie + fingerprint，无需 w-payload-source）
  // 注意：旧 14509/GetSightOverview 在 2026-08 已返 404 整个死掉了；20036/getSightExtendInfo
  //       接替了但只返 393 字节短摘要，parser 不用，proactive 不调它。
  const PROACTIVE_ENDPOINTS = {
    ADDINFO_URL:             "https://m.ctrip.com/restapi/soa2/12530/json/resourceAddInfo",
    PRICE_CAL_URL:           "https://m.ctrip.com/restapi/soa2/14580/json/getProductPriceCalendar",
    RESOURCE_DETAIL_URL:     "https://m.ctrip.com/restapi/soa2/12314/json/resourceDetails.json",
    SHELF_RESOURCE_LIST_URL: "https://m.ctrip.com/restapi/soa2/21052/getShelfResourceList",
  };
  // 单轮主动 fire 的 resourceAddInfo 上限。whaleguard 2026-08-25 实测：20 个接连 (3s 间隔)
  // 仍有 19/20 被 430 ban。降为 12 + jitter + retry-after-429 队列。
  const PROACTIVE_ADDINFO_CAP = 12;
  // getShelfResourceList 单轮上限：(可选人群) SKU 通常 2-3 个 property，每个查 1 chip，
  // 12 个 SKU 最多 ~36 次查询；30 够用，留冗余 — 但同样降为 15 防风暴。
  const PROACTIVE_CROWD_QUERY_CAP = 36;
  // 两次 addInfo 之间的基线间隔（ms），与 jitter 一起用于 sleepWithJitter。
  // 2026-08-25 测试: viewid=233/5153/5170 在 stagger=80ms 时 100% 走 WafAntibotCheckFailed
  // (whaleguard 430), stagger=3000ms 后部分放行; 但仍 19/20 ban 圆明园 — 加上 jitter +
  // 完整浏览器头后实测再调。
  // 基线 3500ms + ±1750ms jitter → 实际范围 1750-5250ms, floor 钳到 2000ms
  // (用户硬性约束:同一 API 间隔不低于 2 秒)
  const PROACTIVE_STAGGER_MS = 3500;
  const PROACTIVE_STAGGER_JITTER_MS = 1750;
  // 主动 fire 完后等待响应的最大时长（addInfo + priceCal + resourceDetails 三轮）。
  // 12 SKU × 3 calls × 4s ≈ 144s, 提至 25s 等响应窗口, 总 ~170s。
  const PROACTIVE_WAIT_MS = 25000;
  // 单 viewid 的总预算（ms）：3 分钟。超时强制 break 防 WAF 单 viewid 风暴。
  const PROACTIVE_TOTAL_BUDGET_MS = 180000;
  // 单个请求的超时（ms）：10s。配合 AbortController。
  const PROACTIVE_REQUEST_TIMEOUT_MS = 10000;
  // 命中 429/430 后，把该 rid 推到 sessionStorage defer 队列，5 分钟内不重试。
  const PROACTIVE_RETRY_AFTER_429_MS = 5 * 60 * 1000;
  // sessionStorage 锁 key 前缀（每个 viewid 每天一个标志）。
  const FIRE_LOCK_PREFIX = "__ctrip_sentry_fired";
  const DEFER_QUEUE_PREFIX = "__ctrip_sentry_defer";
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

  // resourceDetails (soa2/12314) payload — 单 rid 接口，响应 data.peopleProperty
  // 给出该 rid 的人群标签（"成人票"/"儿童票"/"老人票"/"不限人群"）。
  // 参考 ctrip_core/selectors.py:resource_detail_payload。
  function resourceDetailPayload(rid, poiIdStr) {
    return {
      resourceId: Number(rid),
      filters: [{ type: "DateFilter", filterItems: [{ key: "Date", value: "" }] }],
      tags: [
        { key: "needRateLimit", value: "T" },
        { key: "needPackingVersion3", value: "true" },
        { key: "needForcedLogin", value: "T" },
      ],
      clientInfo: {
        currency: "CNY",
        locale: "zh-CN",
        pageId: 10650097502,
        channelId: 116,
        extension: [
          { name: "poiId", value: String(poiIdStr) },
          { name: "needPackagingVersion3", value: "true" },
        ],
        oriSyscode: "09",
        syscode: "09",
        cid: "",
        appPlatform: "",
        ic_traceid: (crypto && crypto.randomUUID ? crypto.randomUUID() : ""),
      },
      enviroment: "PROD",
    };
  }

  // getShelfResourceList (soa2/21052) payload — 单 (rid, peoplePropertyId) 查询 sibling crowd
  // child rid。官方页点 chip 时调用；扩展 proactive fire 同粒度 fan-out (每个 rid 的
  // propertyIdList) 把所有 sibling crowd 一次拿齐。响应 resources[].name 自带 crowd
  // 后缀 ("圆明园通票+智旅手册儿童票")，resources[].minPriceRelationInfo.peoplePropertyName
  // 给出人群标签。
  // 参考 ctrip_core/selectors.py:shelf_resource_list_payload。
  function shelfResourceListPayload(spotid, idList, dateStr, peoplePropertyId) {
    return {
      clientInfo: {
        currency: "CNY",
        locale: "zh-CN",
        pageId: "10650104114",
        channelId: 116,
        extension: [],
        oriSyscode: "09",
        syscode: "09",
        cid: "",
        appPlatform: "",
        ic_traceid: (crypto && crypto.randomUUID ? crypto.randomUUID() : ""),
      },
      enviroment: "PROD",
      spotid: Number(spotid),
      tags: [{ key: "callRecallPK", value: "pkOneOrMore" }],
      needResourceDetails: true,
      idList: (idList || []).map(Number),
      date: String(dateStr || ""),
      token: "",
      peoplePropertyId: Number(peoplePropertyId) || 0,
      needResourceFilter: true,
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
    };
  }

  function findShelfBody() {
    const captured = (window.__ctrip_sentry_get_inflight && window.__ctrip_sentry_get_inflight()) || [];
    const shelf = captured.find((r) => String(r.url).includes("getProductShelf"));
    if (!shelf || !shelf.response || !shelf.response.bodyText) return null;
    try { return JSON.parse(shelf.response.bodyText); } catch (_) { return null; }
  }

  // 抖动 sleep：base + uniform(-jitter, +jitter)，最小 500ms。
  // 破除"脚本等距"模式特征 — 携程 whaleguard 2026-08-25 实测固定 3000ms stagger
  // 仍 19/20 ban，加 jitter 后行为更接近真人浏览节奏。
  function sleepWithJitter(base, jitter) {
    const delta = Math.round((Math.random() - 0.5) * 2 * (jitter || 0));
    // 用户硬性约束:同一 API 间隔不低于 2 秒 — 这里 clamp 到 2000ms 作 hard floor
    const ms = Math.max(2000, (base || 1000) + delta);
    return new Promise((r) => setTimeout(r, ms));
  }

  // 构造"看起来像正常浏览器发出"的 ctrip soa2 fetch headers。
  // 浏览器 fetch 自动加 cookie + User-Agent + sec-ch-ua 等等, 但 Referer/Origin/
  // Accept-Language/sec-fetch-* 必须在 fetch header 里给出, 否则 whaleguard 直接拦。
  // Referer 取 window.location.href (若含 ctrip.com)，否则回退到 sight URL。
  function buildCtripHeaders(viewid) {
    let referer = "";
    try {
      if (location && /ctrip\.com/.test(location.href)) {
        referer = location.href;
      }
    } catch (_) {}
    if (!referer && viewid) {
      referer = "https://m.ctrip.com/webapp/you/sight/1/" + viewid + ".html";
    }
    return {
      "content-type": "application/json",
      "accept": "application/json, text/plain, */*",
      "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
      "origin": "https://m.ctrip.com",
      "referer": referer,
      "sec-fetch-mode": "cors",
      "sec-fetch-site": "same-origin",
      "sec-fetch-dest": "empty",
    };
  }

  // sessionStorage 锁：每天每 viewid 只 fire 一次，防止 SPA 内路由切换 / 用户点 chip 后
  // 反复触发 fan-out 把 cookie 烧穿。返回 {alreadyFired, deferredSet}
  function readFireLock(viewid) {
    const day = new Date().toISOString().slice(0, 10);
    let alreadyFired = false;
    try {
      alreadyFired = sessionStorage.getItem(FIRE_LOCK_PREFIX + ":" + viewid + ":" + day) === "1";
    } catch (_) {}
    const defer = [];
    try {
      const raw = sessionStorage.getItem(DEFER_QUEUE_PREFIX + ":" + viewid);
      if (raw) {
        const obj = JSON.parse(raw);
        const now = Date.now();
        for (const [rid, ts] of Object.entries(obj || {})) {
          if (now - ts < PROACTIVE_RETRY_AFTER_429_MS) defer.push(Number(rid));
        }
      }
    } catch (_) {}
    return { alreadyFired, deferredSet: new Set(defer) };
  }

  function writeFireLock(viewid) {
    const day = new Date().toISOString().slice(0, 10);
    try {
      sessionStorage.setItem(FIRE_LOCK_PREFIX + ":" + viewid + ":" + day, "1");
    } catch (_) {}
  }

  function pushDefer(viewid, rids) {
    if (!viewid || !rids || !rids.length) return;
    try {
      const key = DEFER_QUEUE_PREFIX + ":" + viewid;
      const raw = sessionStorage.getItem(key);
      const obj = raw ? JSON.parse(raw) : {};
      const now = Date.now();
      for (const rid of rids) obj[String(rid)] = now;
      sessionStorage.setItem(key, JSON.stringify(obj));
    } catch (_) {}
  }

  // Fisher-Yates 洗牌：破除"按 rid 顺序发"特征。
  function shuffleInPlace(arr) {
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      const t = arr[i]; arr[i] = arr[j]; arr[j] = t;
    }
    return arr;
  }

  async function fireOne(url, payload, label, ident) {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), PROACTIVE_REQUEST_TIMEOUT_MS);
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: buildCtripHeaders(payload && payload.__viewid),
        body: JSON.stringify(payload),
        credentials: "include",
        signal: ac.signal,
      });
      clearTimeout(timer);
      console.log(SENTINEL, "✓ proactive " + label, ident, "HTTP", r.status);
      PROACTIVE_STATE.fired++;
      return { ok: r.ok, status: r.status };
    } catch (e) {
      clearTimeout(timer);
      console.warn(SENTINEL, "✗ proactive " + label, ident, e);
      PROACTIVE_STATE.errors++;
      return { ok: false, status: 0 };
    }
  }

  // signature: __ctrip_sentry_proactive_fire(viewid, opts?: { force?: boolean })
  // opts.force=true 时绕过 sessionStorage 每日锁（popup "立即采集" 按钮场景）。
  window.__ctrip_sentry_proactive_fire = async function (viewid, opts) {
    PROACTIVE_STATE.phase = "running";
    PROACTIVE_STATE.fired = 0;
    PROACTIVE_STATE.errors = 0;
    PROACTIVE_STATE.total = 0;

    if (!viewid) {
      PROACTIVE_STATE.phase = "skipped";
      return { fired: 0, errors: 0, total: 0, reason: "no_viewid" };
    }

    // 锁检查：当天是否已经 fire 过本 viewid？force=true 跳过锁。
    const lock = readFireLock(viewid);
    const deferredSet = lock.deferredSet;
    if (lock.alreadyFired && !(opts && opts.force)) {
      PROACTIVE_STATE.phase = "skipped";
      console.log(SENTINEL, "proactive fire: already fired today, skip",
        "(force=true to bypass)");
      return { fired: 0, errors: 0, total: 0, reason: "already_fired_today" };
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

    // 过滤掉已经在 defer 队列里的 rid（5 分钟内被 ban 过）— force 模式跳过过滤。
    const candidates = (opts && opts.force)
      ? mineRids.slice(0, PROACTIVE_ADDINFO_CAP)
      : mineRids.filter((m) => !deferredSet.has(m.rid)).slice(0, PROACTIVE_ADDINFO_CAP);

    // Fisher-Yates 洗牌破除顺序特征，并标记 fire 队列（同时落锁）。
    const capped = shuffleInPlace(candidates.slice());
    PROACTIVE_STATE.total = capped.length * 3; // addInfo + priceCal + resourceDetails
    writeFireLock(viewid);

    console.log(SENTINEL,
      "proactive fire start: viewid=" + viewid + " poiId=" + poiId +
      " resources=" + capped.length + "/" + mineRids.length +
      " deferred=" + Array.from(deferredSet).length);

    const startedAt = Date.now();
    function budgetExceeded() {
      return Date.now() - startedAt > PROACTIVE_TOTAL_BUDGET_MS;
    }
    // 收集被 whaleguard 拦截的 SKU，下轮重试。
    const rateLimitedRids = [];

    // 把 __viewid 注入 payload，让 fireOne 的 buildCtripHeaders 拿到正确的 referer/origin。
    function withViewid(p) { p.__viewid = viewid; return p; }

    // 第 1 轮：resourceAddInfo（最关键 — 拿 vendorInfo）
    for (let i = 0; i < capped.length; i++) {
      if (budgetExceeded()) {
        console.warn(SENTINEL, "addInfo: budget exceeded, break at", i);
        PROACTIVE_STATE.total -= (capped.length - i) * 3;
        break;
      }
      const r = await fireOne(PROACTIVE_ENDPOINTS.ADDINFO_URL,
        withViewid(addinfoPayload(capped[i].rid, viewid, capped[i].productId)),
        "addInfo",
        "rid=" + capped[i].rid + " pid=" + capped[i].productId);
      if (r && (r.status === 429 || r.status === 430)) {
        rateLimitedRids.push(capped[i].rid);
      }
      if (i < capped.length - 1) {
        await sleepWithJitter(PROACTIVE_STAGGER_MS, PROACTIVE_STAGGER_JITTER_MS);
      }
    }

    // 第 2 轮：getProductPriceCalendar（拿每日价）
    for (let i = 0; i < capped.length; i++) {
      if (budgetExceeded()) {
        console.warn(SENTINEL, "priceCal: budget exceeded, break at", i);
        break;
      }
      await fireOne(PROACTIVE_ENDPOINTS.PRICE_CAL_URL,
        withViewid(priceCalendarPayload(capped[i].rid, poiId)),
        "priceCal",
        "rid=" + capped[i].rid);
      if (i < capped.length - 1) {
        await sleepWithJitter(PROACTIVE_STAGGER_MS, PROACTIVE_STAGGER_JITTER_MS);
      }
    }

    // 第 3 轮：resourceDetails（人群标签）— 已能从 addInfo 拿到，但保留以兼容老 SKU。
    for (let i = 0; i < capped.length; i++) {
      if (budgetExceeded()) {
        console.warn(SENTINEL, "resourceDetail: budget exceeded, break at", i);
        break;
      }
      await fireOne(PROACTIVE_ENDPOINTS.RESOURCE_DETAIL_URL,
        withViewid(resourceDetailPayload(capped[i].rid, poiId)),
        "resourceDetail",
        "rid=" + capped[i].rid);
      if (i < capped.length - 1) {
        await sleepWithJitter(PROACTIVE_STAGGER_MS, PROACTIVE_STAGGER_JITTER_MS);
      }
    }

    // 第 4 轮：getShelfResourceList（可选人群 sibling crowd）
    const crowdQueries = [];
    for (const r of allResources) {
      if (Number(r.spotid) !== Number(viewid)) continue;
      const props = Array.isArray(r.propertyIdList) ? r.propertyIdList : [];
      if (!props.length) continue;
      const today = new Date().toISOString().slice(0, 10);
      for (const pid of props) {
        crowdQueries.push({ rid: r.resourceId, peoplePropertyId: pid, date: today });
        if (crowdQueries.length >= PROACTIVE_CROWD_QUERY_CAP) break;
      }
      if (crowdQueries.length >= PROACTIVE_CROWD_QUERY_CAP) break;
    }
    // 洗牌 crowdQueries
    shuffleInPlace(crowdQueries);
    PROACTIVE_STATE.total += crowdQueries.length;
    if (crowdQueries.length) {
      console.log(SENTINEL,
        "proactive fire crowd discovery: " + crowdQueries.length + " (rid, propertyId) pairs");
    }
    for (let i = 0; i < crowdQueries.length; i++) {
      if (budgetExceeded()) {
        console.warn(SENTINEL, "shelfResList: budget exceeded, break at", i);
        break;
      }
      const q = crowdQueries[i];
      await fireOne(PROACTIVE_ENDPOINTS.SHELF_RESOURCE_LIST_URL,
        withViewid(shelfResourceListPayload(viewid, [q.rid], q.date, q.peoplePropertyId)),
        "shelfResList",
        "rid=" + q.rid + " pid=" + q.peoplePropertyId);
      if (i < crowdQueries.length - 1) {
        await sleepWithJitter(PROACTIVE_STAGGER_MS, PROACTIVE_STAGGER_JITTER_MS);
      }
    }

    // 第 4.5 轮：扫描 inflight 里 crowd 端点响应，抽出真正的 sibling child rid，给它们
    // 补 priceCalendar + resourceDetails — 不然 chip 拆出来的 学生/儿童 rid 进了 crowd_map
    // 但 price_day 表空，dashboard join 时这些行直接不显示。
    // capped 是 addInfo cap 范围内的 rid（含父 rid）；新 sibling 不在 capped 里，需要单独打。
    await sleepWithJitter(PROACTIVE_WAIT_MS, 4000);  // 等 crowd 响应落 inflight
    const siblingRids = new Set();
    const cappedRids = new Set(capped.map((c) => c.rid));
    try {
      const inflight = (window.__ctrip_sentry_get_inflight && window.__ctrip_sentry_get_inflight()) || [];
      for (const r of inflight) {
        if (!r || !r.url || !String(r.url).includes("/getShelfResourceList")) continue;
        const bodyText = r.response && r.response.bodyText;
        if (!bodyText) continue;
        let body;
        try { body = JSON.parse(bodyText); } catch (_) { continue; }
        const resources = (body && body.resources) || [];
        for (const child of resources) {
          const childRid = child && (child.resourceId || child.id);
          if (childRid && !cappedRids.has(Number(childRid))) {
            siblingRids.add(Number(childRid));
          }
        }
      }
    } catch (e) {
      console.warn(SENTINEL, "sibling rid scan failed", e);
    }
    const siblingList = Array.from(siblingRids);
    PROACTIVE_STATE.total += siblingList.length * 2;
    if (siblingList.length) {
      console.log(SENTINEL,
        "proactive fire sibling discovery: " + siblingList.length + " new rids from crowd fan-out");
    }
    for (let i = 0; i < siblingList.length; i++) {
      if (budgetExceeded()) {
        console.warn(SENTINEL, "sibling priceCal: budget exceeded, break at", i);
        break;
      }
      const sibRid = siblingList[i];
      // priceCalendar for sibling
      await fireOne(PROACTIVE_ENDPOINTS.PRICE_CAL_URL,
        withViewid(priceCalendarPayload(sibRid, poiId)),
        "priceCal-sibling",
        "rid=" + sibRid);
      // resourceDetails for sibling（补 people_property，理论上 crowd_map 已有，但兜底）
      await fireOne(PROACTIVE_ENDPOINTS.RESOURCE_DETAIL_URL,
        withViewid(resourceDetailPayload(sibRid, poiId)),
        "resourceDetail-sibling",
        "rid=" + sibRid);
      if (i < siblingList.length - 1) {
        await sleepWithJitter(PROACTIVE_STAGGER_MS, PROACTIVE_STAGGER_JITTER_MS);
      }
    }

    // 把被 WAF 拦的 rid 推到 defer 队列，5 分钟内不重试。
    if (rateLimitedRids.length) {
      pushDefer(viewid, rateLimitedRids);
      console.warn(SENTINEL,
        "WAF 限速命中 " + rateLimitedRids.length + " 个 rid, 5 分钟内不重试:",
        rateLimitedRids.join(","));
    }

    // 等响应回灌进 inflight map（wrapper 是异步 .then 读 body）。
    await sleepWithJitter(PROACTIVE_WAIT_MS, 4000);

    PROACTIVE_STATE.phase = "done";
    console.log(SENTINEL,
      "proactive fire done: fired=" + PROACTIVE_STATE.fired +
      " errors=" + PROACTIVE_STATE.errors +
      " total=" + PROACTIVE_STATE.total +
      " elapsed=" + Math.round((Date.now() - startedAt) / 1000) + "s");
    return {
      fired: PROACTIVE_STATE.fired,
      errors: PROACTIVE_STATE.errors,
      total: PROACTIVE_STATE.total,
      poiId,
      rids: capped.map((c) => c.rid),
      rateLimitedRids,
    };
  };

  window.__ctrip_sentry_proactive_state = () => ({ ...PROACTIVE_STATE });
})();
