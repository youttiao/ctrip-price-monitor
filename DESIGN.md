# 携程门票价格监控 · 系统设计（v2）

> 起草日：2026-08-24 · 定稿日：2026-08-24
> 适配项目：`/Users/argo/666-XCJ/ctrip-price-monitor/`
> 部署目标：RackNerd VPS `racknerd19` · 域名 `xiecheng.19880913.xyz`
> 数据基础：`_captures/` 5 个北京 POI × 58 SKU × 19 vendor 实测

---

## 0. 一句话总结

> **混合抓取**：服务器后台 30 分钟跑一次搜索 + addInfo（用 cookies 即可），Chrome 扩展补全 `getProductShelf`（需浏览器 `w-payload-source` 签名）→ 两路都 POST 到 VPS 的 FastAPI → SQLite 入库 → rank 对比告警 → 看板。

身份不再是"自营方"，而是**持多个 vendorId 的代理商**。核心单位从 `(resourceId, is_self)` 改为 `(poi_viewid, shelfType, vendorId)`。告警从"自营 vs 非自营变化"改为 **rank 变化 + 我消失 critical**。

---

## 1. 设计决策清单（grill-me 12 + 4 决策）

| # | 决策 | 对设计的影响 |
|---|---|---|
| Q1 | webhook 实时 + 长期监控 | 通用 webhook URL + alerts 表 |
| Q2 | 我是代理商，不是自营方 | 主体从 `vendorId==999999` 改为 `vendorId IN my_vendor_ids` |
| Q3 | 30 分钟采集 | systemd timer 30 min + chrome.alarms 30 min |
| Q4 | rid 与 vendorId 1:1 | sku_snapshot 用 `(resource_id, primary_vendor_id)`，无 `is_self` 字段 |
| Q5 | hero=高级专业+日历+趋势（我设计）；watchlist=手动选 shelfType | dashboard 默认布局由我设计 |
| Q6 | rank 变化才报 + 我消失 critical | 告警引擎重写为 rank-centric |
| Q7 | 多个 vendorId + 后台可编辑 | `vendors` 表 + admin UI |
| Q8 | '我的足迹' + 逐行 toggle 进 watchlist | `watchlist` 表 + dashboard 交互 |
| Q9 | 完整登录页 + session（多用户预留） | `users` 表 + bcrypt + session |
| Q10 | 通用 webhook URL | 结构化 JSON payload |
| Q11 | 混合架构（服务器+扩展）；cookies 是 C 端账号，无敏感 | 服务器后台抓 + 扩展补全；cookies 存 VPS |
| Q12 | 30 天原始 + 之后 `daily_summary` 聚合 | 保留策略 + 聚合表 |
| Q+A1 | 持续非 #1 = 持续告警（每次 round 都评估） | rank 评估每轮都跑 |
| Q+A2 | cookie 同步尽可能频繁 | 扩展每抓一次 push + 5 分钟 alarms 推送 |
| Q+A3 | 新 shelfType 自动进 dashboard，watchlist 手动 toggle | "我的足迹" 实时更新，不入 watchlist |
| Q+A4 | 多 POI = 顶部 tab 切换（单 POI 视图） | dashboard 默认布局 |

---

## 2. 总体架构

```
┌──────────────────────────────┐       ┌──────────────────────────┐
│ VPS · RackNerd               │       │ 你的电脑 (常开 Chrome)    │
│ xiecheng.19880913.xyz         │       │                          │
│ ┌──────────────────────────┐ │       │ ┌──────────────────────┐ │
│ │ Caddy (TLS, gzip)        │ │       │ │ Chrome MV3 扩展     │ │
│ └──────────────────────────┘ │       │ │  • 拦 soa2 fetch   │ │
│         │                    │       │ │  • POST round       │ │
│ ┌──────────────────────────┐ │◀──────│ │  • 5min alarms 上传 │ │
│ │ FastAPI + uvicorn        │ │       │ │    cookie           │ │
│ │  • /admin/login,vendors  │ │       │ └──────────────────────┘ │
│ │  • /api/ingest/round     │ │       │                          │
│ │  • /api/cookies/sync     │ │       └──────────────────────────┘
│ │  • /api/alerts/config    │ │                 ▲
│ └──────────────────────────┘ │                 │ cookies
│         │                    │─────────────────┘
│ ┌──────┴───────┐             │
│ │ SQLite WAL   │             │
│ │  rounds      │             │
│ │  sku_snapshot│             │
│ │  price_day   │             │
│ │  rank_history│             │
│ │  daily_summary│            │
│ │  vendors     │             │
│ │  watchlist   │             │
│ │  users/sessions│           │
│ │  cookies     │             │
│ │  alerts      │             │
│ └──────────────┘             │
│         ▲                    │
│ ┌──────┴───────────────┐     │
│ │ systemd timer 30 min │     │
│ │ server_scraper.py    │     │
│ │  • httpx + cookies    │     │
│ │  • search + addInfo   │     │
│ │  • 写 sku_snapshot    │     │
│ └──────────────────────┘     │
│         ▲                    │
│ ┌──────┴───────────────┐     │
│ │ systemd timer 1 min  │     │
│ │ round_parser.py      │     │
│ │  • 解析 raw_rounds    │     │
│ │  • 算 rank + 告警     │     │
│ │  • 发 webhook         │     │
│ └──────────────────────┘     │
│         ▲                    │
│ ┌──────┴───────────────┐     │
│ │ systemd timer daily  │     │
│ │ retention.py         │     │
│ │  • 30 天后聚合        │     │
│ └──────────────────────┘     │
└──────────────────────────────┘
                  │
                  ▼
          ┌──────────────┐
          │ Webhook URL  │
          │ (你配置的)    │
          └──────────────┘
```

---

## 3. 数据模型

### 3.1 实体关系

```
POI (pois)
 └─ shelf_type (shelf_types: poi_viewid + shelfTypeId + name)
     └─ sku_snapshot (round_id, resource_id, primary_vendor_id, displayPrice)
         └─ vendor (vendors: vendor_id + brand + licence)
         └─ price_day (round_id, resource_id, date, salePrice)

rank_history (round_id, shelfTypeId, vendorId, rank, lowest_rid, lowest_price, gap)
  → 由每轮解析后计算：SELECT vendorId, RANK() OVER (PARTITION BY shelfTypeId ORDER BY displayPrice)

watchlist (user_id, shelfTypeId, active)  ← 手动 toggle
my_vendors (user_id, vendor_id, is_active)  ← 多 vendorId 列表
alerts (ts, type, severity, payload, dedup_key, ...)
daily_summary (date, shelfTypeId, vendorId, avg_rank, min_price, max_price, sku_count)
cookies (id, blob_json, uploaded_at, source)  ← VPS 上最新一份 cookies
users (id, username, pw_hash, role, created_at)
sessions (sid, user_id, expires_at, ip_prefix)
```

### 3.2 关键判断规则

```python
# code/selectors.py

# 你的 vendorId 列表（从 my_vendors 表读，不是写死）
# MY_VENDOR_IDS = SELECT vendor_id FROM my_vendors WHERE is_active=1

# 自营检测（仍然有用：判断 shelf 里有没有 999999 货架）
SELF_VENDOR_ID = 999999

# "我" = vendorId IN my_vendors
# 一个 shelfType 里：我有几个 SKU（每个 vendorId 一个）
# 我排第几 = 我的 SKU 按 displayPrice 升序的位置

# 货架过滤
SHELF_NAME_BLACKLIST = ("一日游", "酒店", "用车", "餐饮", "跟团", "司导", "向导")

# 价格日历窗口
PRICE_CALENDAR_WINDOW_DAYS = 37
```

### 3.3 关键不变式

1. **(resource_id, primary_vendor_id) 唯一**：抓包样本里 rid ↔ vendorId 是 1:1（`vendorInfos[]` 只用于组合产品，常见 SKU 都是单 vendor）
2. **shelfTypeId 决定"竞争场"**：同 shelfType 下所有 vendorId 的 SKU 按 displayPrice 升序排，第一位就是 "排第一"
3. **rank_history 是派生表**：每次解析完一个 round，对每个 `(poi_viewid, shelfType)` 算一次 RANK，写入

---

## 4. SQLite Schema（13 张表）

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ① 一轮扫描
CREATE TABLE rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        TEXT UNIQUE NOT NULL,
    captured_at     TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    poi_name        TEXT,
    source          TEXT NOT NULL,          -- 'extension' / 'server'
    requests_count        INTEGER DEFAULT 0,
    skus_total      INTEGER DEFAULT 0,
    skus_mine       INTEGER DEFAULT 0,      -- 实际属于我的
    status          TEXT NOT NULL DEFAULT 'pending',
    error_msg       TEXT,
    duration_ms     INTEGER,
    raw_path        TEXT
);
CREATE INDEX idx_rounds_viewid ON rounds(poi_viewid);
CREATE INDEX idx_rounds_received ON rounds(received_at DESC);

-- ② SKU 快照（每条 rid 一行）
CREATE TABLE sku_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    resource_id     INTEGER NOT NULL,
    primary_vendor_id INTEGER NOT NULL,     -- 替代 is_self
    full_name       TEXT NOT NULL,
    shelf_type_id   INTEGER,
    shelf_type_name TEXT,
    spotid          INTEGER,
    primary_vendor_name  TEXT,
    primary_vendor_brand TEXT,
    primary_vendor_licence TEXT,
    primary_vendor_licence_pic TEXT,
    display_price   REAL,
    market_price    REAL,
    first_booking_date TEXT,
    sale_count      INTEGER,
    raw_resource    TEXT,
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    UNIQUE(round_id, resource_id)
);
CREATE INDEX idx_sku_round ON sku_snapshot(round_id);
CREATE INDEX idx_sku_viewid ON sku_snapshot(poi_viewid);
CREATE INDEX idx_sku_vendor ON sku_snapshot(primary_vendor_id);
CREATE INDEX idx_sku_resid ON sku_snapshot(resource_id);
CREATE INDEX idx_sku_shelf ON sku_snapshot(shelf_type_id);

-- ③ 每日价格
CREATE TABLE price_day (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    resource_id     INTEGER NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    sale_date       TEXT NOT NULL,
    min_price       REAL,
    sale_price      REAL,
    inventory       INTEGER,
    available       INTEGER,
    package_id      INTEGER,
    raw             TEXT,
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    UNIQUE(round_id, resource_id, sale_date)
);
CREATE INDEX idx_pd_round ON price_day(round_id);
CREATE INDEX idx_pd_viewid ON price_day(poi_viewid);
CREATE INDEX idx_pd_date ON price_day(sale_date);
CREATE INDEX idx_pd_resid ON price_day(resource_id);

-- ④ Vendor 主表
CREATE TABLE vendors (
    vendor_id       INTEGER PRIMARY KEY,
    name            TEXT,
    brand_company_name TEXT,
    licence_no      TEXT,
    licence_pic_url TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    sku_count       INTEGER DEFAULT 0
);
CREATE INDEX idx_vendors_brand ON vendors(brand_company_name);

-- ⑤ 我的 vendorId 列表（来自我的配置，可编辑）
CREATE TABLE my_vendors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id       INTEGER NOT NULL UNIQUE,
    label           TEXT,                  -- 显示名（可改名）
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_my_vendors_active ON my_vendors(is_active);

-- ⑥ POI 配置
CREATE TABLE pois (
    viewid          INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    district        TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_round_id   INTEGER,
    last_status     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- ⑦ Watchlist（手动 toggle）
CREATE TABLE watchlist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    shelf_type_id   INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(user_id, poi_viewid, shelf_type_id)
);
CREATE INDEX idx_watchlist_user ON watchlist(user_id);

-- ⑧ Rank 历史（每轮每个 shelfType 里每个 vendor 的位置）
CREATE TABLE rank_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    shelf_type_id   INTEGER NOT NULL,
    vendor_id       INTEGER NOT NULL,
    resource_id     INTEGER NOT NULL,
    rank            INTEGER NOT NULL,           -- 1, 2, 3, ...
    display_price   REAL NOT NULL,
    lowest_resource_id INTEGER,                 -- 该 shelfType 里第 1 名的 rid
    lowest_price    REAL,                       -- 该 shelfType 里最低 displayPrice
    gap             REAL,                       -- displayPrice - lowest_price
    is_mine         INTEGER NOT NULL DEFAULT 0, -- 冗余方便查询
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    UNIQUE(round_id, shelf_type_id, vendor_id)
);
CREATE INDEX idx_rh_round ON rank_history(round_id);
CREATE INDEX idx_rh_shelf ON rank_history(poi_viewid, shelf_type_id);
CREATE INDEX idx_rh_vendor ON rank_history(vendor_id);
CREATE INDEX idx_rh_mine ON rank_history(is_mine);

-- ⑨ Daily summary（30 天后聚合）
CREATE TABLE daily_summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_date    TEXT NOT NULL,           -- "2026-08-24"
    poi_viewid      INTEGER NOT NULL,
    shelf_type_id   INTEGER NOT NULL,
    vendor_id       INTEGER NOT NULL,
    rank_min        INTEGER,                 -- 当日最佳 rank
    rank_max        INTEGER,                 -- 当日最差 rank
    rank_avg        REAL,                    -- 当日平均 rank
    price_min       REAL,
    price_max       REAL,
    price_avg       REAL,
    rounds_count    INTEGER NOT NULL,
    UNIQUE(summary_date, poi_viewid, shelf_type_id, vendor_id)
);
CREATE INDEX idx_ds_date ON daily_summary(summary_date);

-- ⑩ Cookies（最新一份）
CREATE TABLE cookies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    blob_json       TEXT NOT NULL,           -- {"GUID": "...", ...}
    uploaded_at     TEXT NOT NULL,
    source          TEXT NOT NULL,           -- 'extension' / 'manual'
    uploaded_by     TEXT                     -- 'extension@<hostname>' / 'user:1'
);

-- ⑪ 用户表
CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    pw_hash         TEXT NOT NULL,           -- bcrypt
    role            TEXT NOT NULL DEFAULT 'admin',
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

-- ⑫ Session
CREATE TABLE sessions (
    sid             TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    ip_prefix       TEXT NOT NULL,
    user_agent      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

-- ⑬ 告警
CREATE TABLE alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    round_id        INTEGER,
    type            TEXT NOT NULL,           -- rank_drop / rank_up / disappeared / appeared / extension_offline / round_failed
    severity        TEXT NOT NULL,
    poi_viewid      INTEGER,
    poi_name        TEXT,
    shelf_type_id   INTEGER,
    shelf_type_name TEXT,
    resource_id     INTEGER,
    sku_name        TEXT,
    vendor_id       INTEGER,
    payload         TEXT NOT NULL,           -- JSON: 完整上下文
    dedup_key       TEXT NOT NULL UNIQUE,
    webhook_status  TEXT,
    webhook_sent_at TEXT,
    webhook_resp    TEXT,
    webhook_retry   INTEGER DEFAULT 0
);
CREATE INDEX idx_alerts_ts ON alerts(ts DESC);
CREATE INDEX idx_alerts_type ON alerts(type);
CREATE INDEX idx_alerts_severity ON alerts(severity);

-- KV 配置
CREATE TABLE config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
INSERT INTO config (key, value, updated_at) VALUES
    ('ingest_secret',         '"<random>"', datetime('now')),
    ('cookie_sync_secret',    '"<random>"', datetime('now')),
    ('webhook_url',           'null',        datetime('now')),
    ('webhook_secret',        'null',        datetime('now')),
    ('alert_threshold_rank_drop', '1',        datetime('now')),
    ('alert_threshold_ext_offline_min', '120', datetime('now')),
    ('site_name',             '"携程哨兵"',  datetime('now'));
```

### 4.1 数据保留

```sql
-- 每晚执行
DELETE FROM rounds       WHERE received_at < datetime('now','-30 days');
DELETE FROM sku_snapshot WHERE round_id NOT IN (SELECT id FROM rounds);
DELETE FROM price_day    WHERE round_id NOT IN (SELECT id FROM rounds);
DELETE FROM rank_history WHERE round_id NOT IN (SELECT id FROM rounds);
DELETE FROM sessions     WHERE expires_at < datetime('now');
DELETE FROM cookies      WHERE id < (SELECT MAX(id) - 5 FROM cookies);  -- 仅保留最近 5 份
DELETE FROM alerts       WHERE ts < datetime('now','-180 days');

-- 聚合：每日 02:00 把 30 天前的 rounds 聚到 daily_summary
INSERT INTO daily_summary
SELECT date(r.captured_at) AS d,
       s.poi_viewid, s.shelf_type_id, s.primary_vendor_id,
       MIN(rh.rank), MAX(rh.rank), AVG(rh.rank),
       MIN(s.display_price), MAX(s.display_price), AVG(s.display_price),
       COUNT(DISTINCT rh.round_id)
FROM rounds r
JOIN sku_snapshot s ON s.round_id = r.id
JOIN rank_history rh ON rh.round_id = r.id AND rh.resource_id = s.resource_id
WHERE r.captured_at < datetime('now','-30 days')
GROUP BY d, s.poi_viewid, s.shelf_type_id, s.primary_vendor_id
ON CONFLICT DO UPDATE SET
    rank_min = excluded.rank_min,
    rank_max = excluded.rank_max,
    rank_avg = excluded.rank_avg,
    price_min = excluded.price_min,
    price_max = excluded.price_max,
    price_avg = excluded.price_avg,
    rounds_count = excluded.rounds_count;
```

---

## 5. 后端模块（VPS）

### 5.1 目录

```
ctrip-monitor/
├── code/
│   ├── selectors.py          # 集中常量（vendor 判定、shelf 黑名单等）
│   ├── soa2_client.py        # httpx 直接抓（服务器后台用）
│   ├── parse.py              # 解析 shelf/addInfo/priceCalendar
│   ├── ingest.py             # /api/ingest/round, /api/cookies/sync
│   └── alerts.py             # rank 对比 + 告警生成
├── web/
│   ├── server.py             # FastAPI app
│   ├── auth.py               # session + bcrypt
│   ├── db.py                 # sqlite3 连接池
│   ├── notifier.py           # webhook 推送
│   ├── routes/
│   │   ├── pages.py          # /login / / / /poi/<v> /admin/vendors /admin/watchlist
│   │   ├── ingest.py         # /api/ingest/*
│   │   ├── cookies.py        # /api/cookies/*
│   │   ├── api.py            # /api/rank, /api/calendar
│   │   └── admin.py          # /admin/vendors, /admin/watchlist, /admin/users
│   ├── templates/            # Jinja2（HTMX + Alpine + 自写 CSS）
│   └── static/
│       ├── css/main.css
│       ├── css/calendar.css
│       └── js/sparkline.js
├── extension/
│   ├── manifest.json
│   ├── background.js
│   ├── popup.html / popup.js
│   ├── content.js
│   └── icons/
├── scripts/
│   ├── init_db.py
│   ├── server_scraper.py     # systemd timer 入口
│   ├── round_parser.py       # 解析 raw + 算 rank + 告警
│   ├── retention.py          # 聚合 + 清理
│   └── deploy.sh
├── deploy/
│   ├── systemd/
│   │   ├── ctrip-web.service
│   │   ├── ctrip-server-scraper.service / .timer (30min)
│   │   ├── ctrip-round-parser.service / .timer (1min)
│   │   └── ctrip-retention.service / .timer (daily)
│   └── Caddyfile
├── data/
│   ├── monitor.db
│   ├── raw_rounds/
│   └── cookies/
└── secrets.env
```

### 5.2 `code/selectors.py`（更新版）

```python
"""携程 soa2 API 集中常量。"""
from __future__ import annotations

BASE_URL = "https://m.ctrip.com"

# soa2 端点
SEARCH_URL   = f"{BASE_URL}/restapi/h5api/globalsearch/search"
SHELF_URL    = f"{BASE_URL}/restapi/soa2/21052/json/getProductShelf"        # 需 w-payload-source
ADDINFO_URL  = f"{BASE_URL}/restapi/soa2/12530/json/resourceAddInfo"
PRICE_CAL_URL = f"{BASE_URL}/restapi/soa2/14580/json/getProductPriceCalendar"
OVERVIEW_URL = f"{BASE_URL}/restapi/soa2/14509/json/GetSightOverview.json"

# 自营判定（保留：用于"自营方也出现"这种参考信息）
SELF_VENDOR_ID = 999999

# 货架名黑名单
SHELF_NAME_BLACKLIST = ("一日游", "酒店", "用车", "餐饮", "跟团", "司导", "向导")

# Cookies 必需字段
REQUIRED_COOKIES = ("GUID", "cticket", "bticket", "vbkticket",
                    "login_uid", "Union")

# AllianceID / SID
ALLIANCE_ID = 66672
SID = 1693366

# 服务器后台能用的接口（不需要 w-payload-source）
SERVER_FETCHABLE = {"search", "addInfo"}  # shelf 需要浏览器栈
```

### 5.3 `code/soa2_client.py`（服务器后台抓取）

```python
"""httpx 直接调 soa2。用 VPS 上的最新 cookies。"""
from __future__ import annotations
import httpx, json
from typing import Optional

from . import selectors as S


class Soa2Client:
    def __init__(self, cookies: dict[str, str]):
        self.cookies = cookies
        self.client = httpx.Client(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={
                "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                              "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
                "accept": "application/json",
                "origin": "https://m.ctrip.com",
                "referer": "https://m.ctrip.com/",
            },
        )

    def _headers(self) -> dict:
        return {
            "content-type": "application/json",
            "cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
        }

    def _post(self, url: str, body: dict) -> Optional[dict]:
        try:
            r = self.client.post(url, json=body, headers=self._headers())
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def search_by_district(self, district_id: int, keyword: str = "") -> list[dict]:
        """返回 POI 候选 (viewid + name)。"""
        body = {
            "head": {"cid": self.cookies.get("GUID", ""),
                     "ctok": "", "cver": "1.0",
                     "extension": [
                         {"name": "aid", "value": str(S.ALLIANCE_ID)},
                         {"name": "sid", "value": str(S.SID)},
                         {"name": "H5",  "value": "H5"},
                     ]},
            "districtId": district_id,
            "keyword": keyword,
            "pageIndex": 1,
        }
        resp = self._post(S.SEARCH_URL, body)
        return resp.get("data", {}).get("sightList", []) if resp else []

    def get_add_info(self, poi_viewid: int, resource_ids: list[int]) -> dict[int, dict]:
        """rid → vendorInfo。"""
        body = {
            "head": {"cid": self.cookies.get("GUID", ""), "ctok": "", "cver": "1.0",
                     "extension": [{"name": "aid", "value": str(S.ALLIANCE_ID)},
                                   {"name": "sid", "value": str(S.SID)},
                                   {"name": "H5",  "value": "H5"}]},
            "districtId": 0,
            "resourceIds": resource_ids,
            "platformId": 1,
        }
        resp = self._post(S.ADDINFO_URL, body)
        if not resp: return {}
        return {r["id"]: r.get("vendorInfo", {}) for r in
                resp.get("data", {}).get("resources", []) if r.get("id")}
```

### 5.4 `code/parse.py`（更新版：rank_history 是核心）

```python
"""解析 raw round JSON → 写 sku_snapshot + price_day + 计算 rank_history。"""
from __future__ import annotations
import json
from datetime import datetime
from . import selectors as S


def parse_round(raw_round: dict) -> dict:
    captured_at = raw_round["capturedAt"]
    poi = raw_round["poi"]
    viewid = int(poi["viewid"])

    shelf_body = _find_body(raw_round, S.SHELF_URL)
    addinfo_bodies = _find_bodies(raw_round, S.ADDINFO_URL)
    price_cal_bodies = _find_bodies(raw_round, S.PRICE_CAL_URL)

    rids_in_scope = _parse_shelf_rids(shelf_body, viewid)
    vendor_map = _parse_addinfos(addinfo_bodies, rids_in_scope)
    price_days = _parse_price_calendars(price_cal_bodies)

    skus = []
    for rid, info in vendor_map.items():
        skus.append({
            "resource_id": rid,
            "primary_vendor_id": info["primary"]["vendorId"],
            "primary_vendor_name": info["primary"]["name"],
            "primary_vendor_brand": info["primary"]["brandCompanyName"],
            "primary_vendor_licence": info["primary"]["licenceNo"],
            "primary_vendor_licence_pic": info["primary"].get("licencePicUrl"),
            "display_price": next((r["display_price"] for r in rids_in_scope
                                   if r["resource_id"] == rid), None),
            "full_name": next((r["full_name"] for r in rids_in_scope
                               if r["resource_id"] == rid), None),
            "shelf_type_id": next((r["shelf_type_id"] for r in rids_in_scope
                                   if r["resource_id"] == rid), None),
            "shelf_type_name": next((r["shelf_type_name"] for r in rids_in_scope
                                     if r["resource_id"] == rid), None),
            "spotid": next((r["spotid"] for r in rids_in_scope
                            if r["resource_id"] == rid), None),
            "market_price": next((r["market_price"] for r in rids_in_scope
                                  if r["resource_id"] == rid), None),
            "first_booking_date": next((r["first_booking_date"] for r in rids_in_scope
                                         if r["resource_id"] == rid), None),
            "sale_count": next((r["sale_count"] for r in rids_in_scope
                                if r["resource_id"] == rid), None),
            "raw_resource": next((r["raw"] for r in rids_in_scope
                                  if r["resource_id"] == rid), None),
        })

    return {
        "captured_at": captured_at,
        "viewid": viewid,
        "poi_name": poi.get("name"),
        "skus": skus,
        "price_days": price_days,
    }


def compute_rank_history(conn, round_pk: int, viewid: int) -> list[dict]:
    """对当前 round 的每个 (shelfType) 计算 RANK → 写 rank_history。"""
    # 拉我的 vendorIds（动态）
    my_vids = {r["vendor_id"] for r in conn.execute(
        "SELECT vendor_id FROM my_vendors WHERE is_active=1").fetchall()}

    # 该 round 的所有 SKU 按 shelfType 分组
    rows = conn.execute("""
        SELECT shelf_type_id, shelf_type_name, resource_id, primary_vendor_id,
               display_price, full_name
        FROM sku_snapshot
        WHERE round_id=? AND display_price IS NOT NULL AND shelf_type_id IS NOT NULL
    """, (round_pk,)).fetchall()

    by_shelf = {}
    for r in rows:
        by_shelf.setdefault(r["shelf_type_id"], []).append(r)

    out = []
    for shelf_id, items in by_shelf.items():
        items_sorted = sorted(items, key=lambda x: x["display_price"])
        lowest = items_sorted[0]
        for rank, r in enumerate(items_sorted, start=1):
            out.append({
                "round_id": round_pk,
                "poi_viewid": viewid,
                "shelf_type_id": shelf_id,
                "vendor_id": r["primary_vendor_id"],
                "resource_id": r["resource_id"],
                "rank": rank,
                "display_price": r["display_price"],
                "lowest_resource_id": lowest["resource_id"],
                "lowest_price": lowest["display_price"],
                "gap": round(r["display_price"] - lowest["display_price"], 2),
                "is_mine": int(r["primary_vendor_id"] in my_vids),
                "shelf_type_name": next((x["shelf_type_name"] for x in items
                                         if x["resource_id"] == r["resource_id"]), None),
                "full_name": r["full_name"],
            })
    return out


def _find_body(rr, url_substr):
    for r in rr.get("requests", []):
        if url_substr in r.get("url", ""):
            return r.get("body")
    return None


def _find_bodies(rr, url_substr):
    return [r["body"] for r in rr.get("requests", [])
            if url_substr in r.get("url", "")]


def _parse_shelf_rids(shelf_body, viewid):
    if not shelf_body:
        return []
    out = []
    for r in shelf_body.get("resources", []):
        if r.get("spotid") != viewid:
            continue
        name = r.get("shelfTypeName") or ""
        if any(b in name for b in S.SHELF_NAME_BLACKLIST):
            continue
        out.append({
            "resource_id": r["resourceId"],
            "product_id": (r.get("productIds") or [None])[0],
            "full_name": r.get("fullName"),
            "spotid": r.get("spotid"),
            "shelf_type_id": r.get("shelfTypeId"),
            "shelf_type_name": r.get("shelfTypeName"),
            "display_price": r.get("displayPrice"),
            "market_price": (r.get("marketPriceInfo") or {}).get("price"),
            "first_booking_date": r.get("firstBookingDate"),
            "sale_count": (r.get("statisticInfo") or {}).get("saleCount"),
            "raw": r,
        })
    return out


def _parse_addinfos(bodies, rids):
    out = {}
    rid_set = {r["resource_id"] for r in rids}
    for b in bodies:
        if not b: continue
        for r in (b.get("data") or {}).get("resources", []) or []:
            rid = r.get("id")
            if rid not in rid_set: continue
            vi = r.get("vendorInfo") or {}
            if not vi: continue
            out[rid] = {
                "primary": {
                    "vendorId": vi.get("vendorId"),
                    "name": vi.get("name"),
                    "brandCompanyName": vi.get("brandCompanyName"),
                    "licenceNo": vi.get("licenceNo"),
                    "licencePicUrl": vi.get("rawLicencePicUrl") or vi.get("licencePicUrl"),
                },
            }
    return out


def _parse_price_calendars(bodies):
    out = []
    for b in bodies:
        if not b: continue
        for day in (b.get("data") or {}).get("priceAndStockInfos", []):
            sale_date = day.get("date")
            for pkg in day.get("packagePriceAndStockInfos", []):
                package_id = pkg.get("packageId")
                for r in pkg.get("resourcePriceAndStockInfos", []):
                    out.append({
                        "resource_id": r.get("resourceId"),
                        "sale_date": sale_date,
                        "min_price": r.get("salePrice"),
                        "sale_price": r.get("price"),
                        "inventory": r.get("inventoryNum"),
                        "available": bool(r.get("available")),
                        "package_id": package_id,
                        "raw": r,
                    })
    return out
```

### 5.5 `code/alerts.py`（rank-centric 告警）

```python
"""vendor-centric 告警引擎：rank 变化 + 我消失 + 我出现 + 扩展离线。"""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone


def detect_rank_alerts(conn, round_pk: int, parsed: dict) -> list[dict]:
    """
    对每个 watchlist 的 (poi, shelfType) 比较本轮 rank vs 上一轮。
    返回需要写入 alerts 表的条目。
    """
    viewid = parsed["viewid"]

    # 1. 我的 vendorIds（动态从 my_vendors 表读）
    my_vids = {r["vendor_id"] for r in conn.execute(
        "SELECT vendor_id FROM my_vendors WHERE is_active=1").fetchall()}
    if not my_vids:
        return []

    # 2. 我关注的所有 shelfType
    watched_shelves = [r["shelf_type_id"] for r in conn.execute("""
        SELECT DISTINCT shelf_type_id FROM watchlist
        WHERE user_id=(SELECT id FROM users WHERE is_active=1 LIMIT 1)
    """).fetchall()]

    # 3. 本轮我的 rank（来自刚写入的 rank_history）
    my_now = {r["shelf_type_id"]: r for r in conn.execute("""
        SELECT shelf_type_id, rank, display_price, lowest_price, gap,
               resource_id, vendor_id, shelf_type_name, full_name
        FROM rank_history
        WHERE round_id=? AND is_mine=1
    """, (round_pk,)).fetchall()}

    # 4. 我上一轮的 rank（最近一个已解析 round）
    prev_round = conn.execute("""
        SELECT MAX(id) FROM rounds
        WHERE poi_viewid=? AND status='parsed' AND id<?
    """, (viewid, round_pk)).fetchone()[0]

    my_prev = {}
    if prev_round:
        my_prev = {r["shelf_type_id"]: r for r in conn.execute("""
            SELECT shelf_type_id, rank, display_price, lowest_price, gap
            FROM rank_history
            WHERE round_id=? AND is_mine=1
        """, (prev_round,)).fetchall()}

    # 5. 上一轮我在场、本轮不在场 → disappeared
    #    本轮我在场、上一轮不在场 → appeared
    alerts = []
    for shelf_id in set(my_now) | set(my_prev):
        now = my_now.get(shelf_id)
        prev = my_prev.get(shelf_id)
        sku_name = (now or prev).get("full_name") if (now or prev) else None
        shelf_name = (now or prev).get("shelf_type_name") if (now or prev) else None
        vendor_id = (now or prev)["vendor_id"] if (now or prev) else None

        if now and not prev:
            alerts.append(_mk_alert(
                conn, round_pk, "appeared", "info",
                viewid, parsed, shelf_id, shelf_name, sku_name, vendor_id,
                {"new_rank": now["rank"], "display_price": now["display_price"]},
            ))
        elif prev and not now:
            alerts.append(_mk_alert(
                conn, round_pk, "disappeared", "critical",
                viewid, parsed, shelf_id, shelf_name, sku_name, vendor_id,
                {"was_rank": prev["rank"]},
            ))
        elif now and prev and now["rank"] != prev["rank"]:
            kind = "rank_drop" if now["rank"] > prev["rank"] else "rank_up"
            sev = "warning" if kind == "rank_drop" else "info"
            alerts.append(_mk_alert(
                conn, round_pk, kind, sev,
                viewid, parsed, shelf_id, shelf_name, sku_name, vendor_id,
                {
                    "old_rank": prev["rank"], "new_rank": now["rank"],
                    "my_price": now["display_price"],
                    "lowest_price": now["lowest_price"],
                    "gap": now["gap"],
                },
            ))
        elif now and prev and now["rank"] == prev["rank"] and now["rank"] != 1:
            # 持续非 #1 状态：每 N 轮评估一次（这里每轮都评，dedup_key 保证不重复）
            alerts.append(_mk_alert(
                conn, round_pk, "still_non_first", "warning",
                viewid, parsed, shelf_id, shelf_name, sku_name, vendor_id,
                {"rank": now["rank"], "my_price": now["display_price"],
                 "lowest_price": now["lowest_price"], "gap": now["gap"]},
            ))

    return alerts


def _mk_alert(conn, round_pk, kind, sev, viewid, parsed,
              shelf_id, shelf_name, sku_name, vendor_id, payload_dict):
    ts = datetime.now(timezone.utc).isoformat()
    dedup = hashlib.sha1(
        f"{kind}|{viewid}|{shelf_id}|{vendor_id}|{payload_dict.get('new_rank', payload_dict.get('rank', ''))}".encode()
    ).hexdigest()[:24]
    return {
        "ts": ts, "round_id": round_pk, "type": kind, "severity": sev,
        "poi_viewid": viewid, "poi_name": parsed.get("poi_name"),
        "shelf_type_id": shelf_id, "shelf_type_name": shelf_name,
        "resource_id": None, "sku_name": sku_name,
        "vendor_id": vendor_id, "payload": json.dumps(payload_dict, ensure_ascii=False),
        "dedup_key": dedup,
    }


def insert_alerts(conn, alerts: list[dict]):
    for a in alerts:
        conn.execute("""
            INSERT OR IGNORE INTO alerts (ts, round_id, type, severity, poi_viewid, poi_name,
                shelf_type_id, shelf_type_name, resource_id, sku_name, vendor_id,
                payload, dedup_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (a["ts"], a["round_id"], a["type"], a["severity"],
              a["poi_viewid"], a["poi_name"], a["shelf_type_id"],
              a["shelf_type_name"], a["resource_id"], a["sku_name"],
              a["vendor_id"], a["payload"], a["dedup_key"]))
    conn.commit()
```

### 5.6 `code/ingest.py`（POST 端点）

```python
"""POST /api/ingest/round 和 /api/cookies/sync。"""
from __future__ import annotations
import hmac, json, os, secrets, uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["ingest"])

RAW_DIR = Path(os.getenv("CTRIP_RAW_DIR", "/opt/ctrip-monitor/data/raw_rounds"))
COOKIE_DIR = Path(os.getenv("CTRIP_COOKIE_DIR", "/opt/ctrip-monitor/data/cookies"))
RAW_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_DIR.mkdir(parents=True, exist_ok=True)


def _expected_secret(env_key: str) -> str:
    return os.getenv(env_key, "")


def _verify(provided: str | None, env_key: str) -> bool:
    exp = _expected_secret(env_key)
    return bool(exp) and bool(provided) and hmac.compare_digest(
        provided.encode(), exp.encode())


@router.post("/ingest/round")
async def ingest_round(
    request: Request,
    x_ingest_secret: str | None = Header(default=None, alias="X-Ingest-Secret"),
    x_extension_ver: str | None = Header(default=None, alias="X-Extension-Ver"),
    x_source: str = Header(default="extension", alias="X-Source"),
):
    if not _verify(x_ingest_secret, "INGEST_SECRET"):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)

    body = await request.json()
    poi = body.get("poi") or {}
    viewid = poi.get("viewid")
    if not viewid:
        return JSONResponse({"ok": False, "error": "missing poi.viewid"}, status_code=400)

    captured_at = body.get("capturedAt") or datetime.now(timezone.utc).isoformat()
    round_id = str(uuid.uuid4())
    fname = f"{captured_at.replace(':','-')}_{viewid}_{round_id[:8]}.json"
    path = RAW_DIR / fname
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    conn = request.app.state.db
    cur = conn.execute("""
        INSERT INTO rounds (round_id, captured_at, received_at, poi_viewid, poi_name,
                            source, requests_count, status, raw_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (round_id, captured_at, datetime.now(timezone.utc).isoformat(),
          int(viewid), poi.get("name"), x_source,
          len(body.get("requests") or []), str(path)))
    conn.commit()
    return {"ok": True, "round_id": round_id, "round_pk": cur.lastrowid}


@router.post("/cookies/sync")
async def sync_cookies(
    request: Request,
    x_cookie_secret: str | None = Header(default=None, alias="X-Cookie-Secret"),
):
    if not _verify(x_cookie_secret, "COOKIE_SYNC_SECRET"):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)

    body = await request.json()
    cookies = body.get("cookies") or {}
    if not cookies.get("GUID"):
        return JSONResponse({"ok": False, "error": "missing GUID"}, status_code=400)

    conn = request.app.state.db
    blob = json.dumps(cookies, ensure_ascii=False)
    conn.execute("""
        INSERT INTO cookies (blob_json, uploaded_at, source)
        VALUES (?, ?, 'extension')
    """, (blob, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return {"ok": True}
```

### 5.7 `scripts/server_scraper.py`（30 分钟一次）

```python
#!/usr/bin/env python3
"""服务器后台抓取：用 VPS 上最新 cookies 跑 search + addInfo。
不能调 getProductShelf（要 w-payload-source，扩展补）。
"""
from __future__ import annotations
import json, sys, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from code.soa2_client import Soa2Client
from code.parse import parse_round, compute_rank_history

DB = Path("/opt/ctrip-monitor/data/monitor.db")


def latest_cookies(conn):
    row = conn.execute(
        "SELECT blob_json FROM cookies ORDER BY id DESC LIMIT 1").fetchone()
    if not row: return {}
    return json.loads(row["blob_json"])


def active_pois(conn):
    return [dict(r) for r in conn.execute(
        "SELECT viewid, name FROM pois WHERE enabled=1").fetchall()]


def fetch_poi(client, poi):
    """对一个 POI：搜索 → 拿 rid 列表 → addInfo 拿 vendor。
    拼装成 round JSON 写到 raw_rounds/。
    """
    # 北京 district_id (硬编码 + 可配置)
    district_id = 36
    sights = client.search_by_district(district_id, keyword=poi["name"])
    target = next((s for s in sights
                   if str(s.get("id")) == str(poi["viewid"])
                   or s.get("sightId") == poi["viewid"]), None)
    if not target:
        return None

    rids = [target["resourceId"]] if "resourceId" in target else target.get("resourceIds", [])
    if not rids:
        return None

    add_info = client.get_add_info(poi["viewid"], rids)
    # 拼成与扩展 POST 的 round 兼容结构
    return {
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "extensionVersion": "server-1.0",
        "poi": {"viewid": poi["viewid"], "name": poi["name"]},
        "requests": [
            {
                "url": "/restapi/soa2/12530/json/resourceAddInfo",
                "ok": True,
                "body": {"data": {"resources": [
                    {"id": rid, "vendorInfo": info} for rid, info in add_info.items()
                ]}},
            },
        ],
        "cookies": {},
    }


def main():
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    cookies = latest_cookies(conn)
    if not cookies.get("GUID"):
        print("[server_scraper] no cookies, skip"); return

    client = Soa2Client(cookies)
    pois = active_pois(conn)

    raw_dir = Path("/opt/ctrip-monitor/data/raw_rounds")
    for p in pois:
        try:
            round_data = fetch_poi(client, p)
            if not round_data: continue
            captured = round_data["capturedAt"]
            rid = str(uuid.uuid4())
            fname = f"{captured.replace(':','-')}_{p['viewid']}_{rid[:8]}_server.json"
            (raw_dir / fname).write_text(json.dumps(round_data, ensure_ascii=False))
            cur = conn.execute("""
                INSERT INTO rounds (round_id, captured_at, received_at, poi_viewid, poi_name,
                                    source, requests_count, status, raw_path)
                VALUES (?, ?, ?, ?, ?, 'server', ?, 'pending', ?)
            """, (rid, captured, datetime.now(timezone.utc).isoformat(),
                  p["viewid"], p["name"], len(round_data["requests"]), str(raw_dir / fname)))
            conn.commit()
            print(f"[server_scraper] {p['name']}: stored as round {cur.lastrowid}")
        except Exception as e:
            print(f"[server_scraper] {p['name']}: {e}")

    conn.close()


if __name__ == "__main__":
    main()
```

### 5.8 `scripts/round_parser.py`（每分钟跑一次）

```python
#!/usr/bin/env python3
"""解析 raw_rounds/ → 写 sku_snapshot + price_day + rank_history → 告警 → webhook。"""
from __future__ import annotations
import json, sys, sqlite3, traceback, os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from code.parse import parse_round, compute_rank_history
from code.alerts import detect_rank_alerts, insert_alerts
from web.notifier import send_webhook, format_payload

DB = Path("/opt/ctrip-monitor/data/monitor.db")


def process_round(conn, row):
    raw = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8"))
    parsed = parse_round(raw)

    # 写 sku_snapshot
    for s in parsed["skus"]:
        conn.execute("""
            INSERT OR REPLACE INTO sku_snapshot
              (round_id, poi_viewid, resource_id, primary_vendor_id, full_name,
               shelf_type_id, shelf_type_name, spotid,
               primary_vendor_name, primary_vendor_brand,
               primary_vendor_licence, primary_vendor_licence_pic,
               display_price, market_price, first_booking_date, sale_count, raw_resource)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (row["id"], parsed["viewid"], s["resource_id"],
              s["primary_vendor_id"], s["full_name"],
              s["shelf_type_id"], s["shelf_type_name"], s["spotid"],
              s["primary_vendor_name"], s["primary_vendor_brand"],
              s["primary_vendor_licence"], s["primary_vendor_licence_pic"],
              s["display_price"], s["market_price"],
              s["first_booking_date"], s["sale_count"],
              json.dumps(s["raw_resource"], ensure_ascii=False)))

        # 更新 vendor 主表
        conn.execute("""
            INSERT INTO vendors (vendor_id, name, brand_company_name, licence_no,
                                 licence_pic_url, first_seen_at, last_seen_at, sku_count)
            VALUES (?,?,?,?,?,?,?,1)
            ON CONFLICT(vendor_id) DO UPDATE SET
                name=excluded.name,
                brand_company_name=excluded.brand_company_name,
                licence_no=excluded.licence_no,
                last_seen_at=excluded.last_seen_at,
                sku_count=vendors.sku_count+1
        """, (s["primary_vendor_id"], s["primary_vendor_name"],
              s["primary_vendor_brand"], s["primary_vendor_licence"],
              s["primary_vendor_licence_pic"],
              parsed["captured_at"], parsed["captured_at"]))

    # 写 price_day
    for p in parsed["price_days"]:
        conn.execute("""
            INSERT OR REPLACE INTO price_day
              (round_id, resource_id, poi_viewid, sale_date, min_price, sale_price,
               inventory, available, package_id, raw)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (row["id"], p["resource_id"], parsed["viewid"], p["sale_date"],
              p["min_price"], p["sale_price"], p["inventory"],
              int(p["available"]), p["package_id"],
              json.dumps(p["raw"], ensure_ascii=False)))

    # 算 rank_history
    rank_rows = compute_rank_history(conn, row["id"], parsed["viewid"])
    for r in rank_rows:
        conn.execute("""
            INSERT OR REPLACE INTO rank_history
              (round_id, poi_viewid, shelf_type_id, vendor_id, resource_id, rank,
               display_price, lowest_resource_id, lowest_price, gap, is_mine)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (r["round_id"], r["poi_viewid"], r["shelf_type_id"], r["vendor_id"],
              r["resource_id"], r["rank"], r["display_price"],
              r["lowest_resource_id"], r["lowest_price"], r["gap"], r["is_mine"]))

    # 告警（仅 source=extension 的轮次，因为只有它有 displayPrice）
    if row["source"] == "extension":
        alerts = detect_rank_alerts(conn, row["id"], parsed)
        insert_alerts(conn, alerts)
        # 推 webhook
        cfg = _load_webhook_cfg(conn)
        if cfg["url"]:
            for a in alerts:
                _send(conn, a, cfg)

    # 更新 round 状态
    conn.execute("""
        UPDATE rounds SET status='parsed', skus_mine=(
            SELECT COUNT(*) FROM sku_snapshot s
            JOIN my_vendors m ON m.vendor_id = s.primary_vendor_id AND m.is_active=1
            WHERE s.round_id=?
        ), skus_total=?, duration_ms=0 WHERE id=?
    """, (row["id"], len(parsed["skus"]), row["id"]))
    conn.commit()


def _load_webhook_cfg(conn):
    url = conn.execute("SELECT value FROM config WHERE key='webhook_url'").fetchone()
    secret = conn.execute("SELECT value FROM config WHERE key='webhook_secret'").fetchone()
    return {"url": json.loads(url["value"]) if url else None,
            "secret": json.loads(secret["value"]) if secret else None}


def _send(conn, alert, cfg):
    payload = format_payload(alert)
    ok, resp = send_webhook(cfg["url"], cfg["secret"], payload, alert["dedup_key"])
    conn.execute("""
        UPDATE alerts SET webhook_status=?, webhook_sent_at=?, webhook_resp=?
        WHERE dedup_key=?
    """, ("ok" if ok else "fail",
          datetime.now(timezone.utc).isoformat(), resp[:500],
          alert["dedup_key"]))
    conn.commit()


def main():
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    pending = conn.execute("""
        SELECT id, round_id, poi_viewid, poi_name, source, raw_path
        FROM rounds WHERE status='pending' ORDER BY id LIMIT 30
    """).fetchall()

    for row in pending:
        try:
            process_round(conn, row)
            print(f"[parser] round {row['id']} parsed")
        except Exception as e:
            conn.execute("UPDATE rounds SET status='failed', error_msg=? WHERE id=?",
                         (traceback.format_exc()[:1000], row["id"]))
            conn.commit()
            print(f"[parser] round {row['id']} failed: {e}")
    conn.close()


if __name__ == "__main__":
    main()
```

### 5.9 Web dashboard 路由表

| 路由 | 内容 |
|---|---|
| `GET /login` | 登录页（用户名/密码） |
| `POST /login` | 校验 → session |
| `POST /logout` | 清 session |
| `GET /` | dashboard 入口（hero 视图，单 POI） |
| `GET /poi/<viewid>` | 切换 POI（tab 切换时用） |
| `GET /myfootprint` | '我的足迹'页：所有我出现的 shelfType + toggle 进 watchlist |
| `GET /api/poi/<viewid>/hero` | HTMX 拉 POI 的状态卡 + sparkline + 日历（hero 三件套） |
| `GET /api/shelf/<id>/rank_history` | JSON：rank 7 天趋势 |
| `GET /api/shelf/<id>/calendar` | JSON：30 天价格日历 |
| `GET /admin/vendors` | 我的 vendorId 列表（增删改） |
| `GET /admin/watchlist` | 我关注的 shelfType 列表 |
| `POST /admin/watchlist/toggle` | toggle 一个 shelfType |
| `GET /admin/users` | 用户管理（仅 admin role） |
| `GET /admin/config` | webhook URL / threshold 配置 |
| `GET /extension` | 扩展 zip 下载 + 安装步骤 |

### 5.10 dashboard hero 设计

单 POI 视图，三个块（按用户决策：professional + calendar + trend）：

```
┌────────────────────────────────────────────────────────────────┐
│ [天坛] [景山] [雍和宫] [颐和园] [圆明园]   👤 admin · 登出     │
├────────────────────────────────────────────────────────────────┤
│  A · 状态卡                                                  │
│  ─────────────                                                │
│   今日 watchlist 货架: 12                                     │
│     我排第一: 5                                               │
│     我排第二: 3   ← 这些是问题                                 │
│     我排第三+: 4   ← 我已无竞争力                              │
│   最近 30 分钟 rank 变化: 2 次                                │
│   活跃 alerts: 7                                              │
├────────────────────────────────────────────────────────────────┤
│  B · rank 趋势列表（每个 watchlist shelfType 一行）           │
│  ───────────────────────────                                  │
│   讲解服务-故宫全景                                            │
│     7 天 sparkline (rank 线 + 最低价线):                       │
│     rank 1 ●━━━━━━━━━━━━━●━━━━━━━●━━━━━━━━●                   │
│     ¥45  ●━━━━━●━━━●━━━━━━━━━━●━━━●                           │
│     [details →]                                                │
│                                                                │
│   讲解服务-中轴线                                              │
│     7 天 sparkline: ...                                        │
│                                                                │
│   ... (所有 watchlist shelfType)                               │
├────────────────────────────────────────────────────────────────┤
│  C · 价格日历（点击 B 中某 shelfType 切换）                   │
│  ─────────────────────────                                    │
│   今日: 讲解服务-故宫全景                                       │
│   ┌───┬───┬───┬───┬───┬───┬───┐                              │
│   │25 │26 │27 │28 │29 │30 │31 │  (本周)                      │
│   │ 1 │ 2 │ 3 │ 1 │ 1 │ 4 │ 1 │  颜色=我当日 rank            │
│   ├───┼───┼───┼───┼───┼───┼───┤                              │
│   │ 1 │ 1 │ 1 │ 1 │ 1 │ 1 │ 1 │                             │
│   │9/1│9/2│9/3│9/4│9/5│9/6│9/7│                             │
│   │ ¥45 ¥45 ¥45 ¥45 ¥45 ¥45 ¥45 │ 当日我的价格              │
│   │ #1 #1 #1 #1 #1 #1 #1 │                              │
│   └───┴───┴───┴───┴───┴───┴───┘                              │
└────────────────────────────────────────────────────────────────┘
```

样式设计原则（用户要求"专业高级"）：
- 主色调：深色背景 #0B0E14 + 高对比文字 #E8ECF1
- 数据色阶：rank 1 绿 → rank 2 黄 → rank 3+ 红
- 数字字体：tabular-nums，等宽对齐
- 卡片圆角 6px，细边框 1px，无大阴影
- 强调色：告警 critical #FF5252、warning #FFA726、info #4FC3F7
- 设计参考：Bloomberg Terminal / Linear App / Vercad Dashboard

### 5.11 '我的足迹' 页面设计

```
┌────────────────────────────────────────────────────────────────┐
│ 我的足迹                                                       │
├────────────────────────────────────────────────────────────────────────┤
│ 天坛公园 (viewid=233)                                          │
│   ☑ 故宫全景讲解-人工          #1   ¥45       [取消关注]    │
│   ☐ 一日游-市区              #1   ¥299      [+ 加关注]      │
│   ☑ 联票-天坛+雍和宫           #3   ¥80       [取消关注]    │
│   ☐ 研学-中学生              #2   ¥120      [+ 加关注]      │
├────────────────────────────────────────────────────────────────────────┤
│ 景山公园 (viewid=5170)                                          │
│   ☑ 故宫全景讲解              #1   ¥45       [取消关注]    │
│   ...                                                            │
├────────────────────────────────────────────────────────────────────────┤
│ [保存所有 toggle]                                               │
└────────────────────────────────────────────────────────────────────────┘
```

### 5.12 Webhook payload 格式（通用 JSON）

```json
{
  "title": "天坛 - 讲解服务-故宫全景：你从 #1 掉到 #2",
  "severity": "warning",
  "ts": "2026-08-24T13:35:00Z",
  "dedup_key": "rank_drop|233|791885|1184705",
  "poi": {"viewid": 233, "name": "天坛公园"},
  "shelf_type": {"id": 791885, "name": "讲解服务-故宫全景"},
  "sku": {"name": "天坛-人工讲解2小时", "vendor_id": 1184705, "vendor_brand": "万程"},
  "rank": {"old": 1, "new": 2, "my_price": 45, "lowest_price": 42, "gap": 3},
  "links": {
    "dashboard": "https://xiecheng.19880913.xyz/poi/233",
    "vendor_pricing": "https://merchant.ctrip.com/..."
  }
}
```

签名：`HMAC-SHA256(secret, JSON.stringify(payload))` → header `X-Signature`。

---

## 6. Chrome 扩展（核心）

### 6.1 职责清单

1. **每 30 分钟**：用 chrome.alarms 触发 → 打开 hidden tab → 拦截 fetch → POST round 到 `/api/ingest/round`
2. **每 5 分钟**：用 chrome.alarms 触发 → POST 当前 cookies 到 `/api/cookies/sync`（心跳保活）
3. **每次抓取后**：立即 POST cookies（保证服务器后台 scraper 总是用最新 cookies）
4. **popup 手动**：立即采集 / 立即同步 cookie / 配置 endpoint + secret + POI 列表

### 6.2 `manifest.json`

```json
{
  "manifest_version": 3,
  "name": "携程哨兵 · 采集 + Cookie 同步",
  "version": "1.0.0",
  "description": "周期性抓携程 soa2 接口响应，频繁同步 cookie 到 VPS。",
  "action": {
    "default_title": "携程哨兵",
    "default_popup": "popup.html",
    "default_icon": {"16": "icons/16.png", "48": "icons/48.png", "128": "icons/128.png"}
  },
  "icons": {"16": "icons/16.png", "48": "icons/48.png", "128": "icons/128.png"},
  "background": {"service_worker": "background.js", "type": "module"},
  "permissions": [
    "cookies", "storage", "tabs", "alarms",
    "notifications", "scripting", "webNavigation", "idle"
  ],
  "host_permissions": [
    "https://*.ctrip.com/*",
    "http://*.ctrip.com/*",
    "https://xiecheng.19880913.xyz/*"
  ]
}
```

### 6.3 `background.js`

```javascript
// 携程哨兵 — 后台 Service Worker
// 1) 30 min 采集（拦 fetch 抓 round）
// 2) 5 min 同步 cookies 到 VPS
// 3) 每次采集后立刻同步 cookies

const ALARM_SCAN  = "ctrip-sentinel-scan";       // 30 min
const ALARM_BEAT  = "ctrip-sentinel-heartbeat";  // 5 min

const POI_URL_TPL = (viewid) => `https://m.ctrip.com/webapp/you/sight/1/${viewid}.html`;
const REQUIRED_COOKIES = ["GUID", "cticket", "bticket", "vbkticket", "login_uid", "Union"];

const SOA2_HOST = "m.ctrip.com";
const CAPTURE_PATHS = [
  "/restapi/soa2/21052/json/getProductShelf",
  "/restapi/soa2/12530/json/resourceAddInfo",
  "/restapi/soa2/14580/json/getProductPriceCalendar",
];

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function loadConfig() {
  return new Promise(res =>
    chrome.storage.local.get(["endpoint", "ingestSecret", "cookieSecret", "pois"],
      v => res(v || {})));
}
async function saveConfig(patch) {
  return new Promise(res => chrome.storage.local.set(patch, () => res()));
}

// ── fetch 注入器 ──
const INJECT_FN = () => {
  if (window.__ctripSentinel) return;
  window.__ctripSentinel = { shelf: null, addInfos: [], priceCals: [], seen: new Set() };

  const realFetch = window.fetch;
  window.fetch = async function(input, init) {
    const url = typeof input === "string" ? input : input.url;
    const res = await realFetch(input, init);
    try {
      if (url.includes("/restapi/soa2/") && !window.__ctripSentinel.seen.has(url)) {
        const cloned = res.clone();
        const body = await cloned.json().catch(() => null);
        if (body) {
          window.__ctripSentinel.seen.add(url);
          if (url.includes("/getProductShelf")) window.__ctripSentinel.shelf = body;
          else if (url.includes("/resourceAddInfo")) window.__ctripSentinel.addInfos.push(body);
          else if (url.includes("/getProductPriceCalendar")) window.__ctripSentinel.priceCals.push(body);
        }
      }
    } catch {}
    return res;
  };
};

// ── 拉所有 cookie ──
async function grabAllCookies() {
  const all = await chrome.cookies.getAll({ domain: ".ctrip.com" });
  const out = {};
  for (const c of all) out[c.name] = c.value;
  return out;
}

function cookieHeader(cookies) {
  return Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join("; ");
}

// ── 单 POI 采集（用浏览器真实栈）──
async function capturePoi(viewid, poiName) {
  const url = POI_URL_TPL(viewid);
  const tabs = await chrome.tabs.query({ url: "https://m.ctrip.com/webapp/you/sight/*" });
  let tab = tabs.find(t => t.url.includes(`/${viewid}.html`));
  if (!tab) tab = await chrome.tabs.create({ url, active: false });

  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: INJECT_FN,
    world: "MAIN",
    injectImmediately: true,
  });

  await chrome.tabs.reload(tab.id);
  await new Promise(r => {
    const l = (id, info) => {
      if (id === tab.id && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(l); r();
      }
    };
    chrome.tabs.onUpdated.addListener(l);
  });
  await sleep(6000);  // 等所有 soa2 落地

  const captured = await chrome.scripting.executeScript({
    target: { tabId: tab.id }, world: "MAIN",
    func: () => window.__ctripSentinel,
  });
  const c = captured?.[0]?.result || {};
  const cookies = await grabAllCookies();

  if (!c.shelf) return null;

  return {
    capturedAt: new Date().toISOString(),
    extensionVersion: chrome.runtime.getManifest().version,
    poi: { viewid, name: poiName },
    requests: [
      { url: "/restapi/soa2/21052/json/getProductShelf", ok: !!c.shelf, body: c.shelf },
      ...(c.addInfos || []).map(b => ({
        url: "/restapi/soa2/12530/json/resourceAddInfo", ok: true, body: b,
      })),
      ...(c.priceCals || []).map(b => ({
        url: "/restapi/soa2/14580/json/getProductPriceCalendar", ok: true, body: b,
      })),
    ],
    cookies,
  };
}

// ── POST round ──
async function postRound(endpoint, secret, round) {
  const r = await fetch(`${endpoint}/api/ingest/round`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Ingest-Secret": secret,
      "X-Extension-Ver": chrome.runtime.getManifest().version,
      "X-Source": "extension",
    },
    body: JSON.stringify(round),
  });
  return { ok: r.ok, status: r.status };
}

// ── POST cookies ──
async function postCookies(endpoint, secret, cookies) {
  const r = await fetch(`${endpoint}/api/cookies/sync`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Cookie-Secret": secret,
    },
    body: JSON.stringify({ cookies }),
  });
  return { ok: r.ok, status: r.status };
}

// ── 30 min 采集 ──
chrome.alarms.create(ALARM_SCAN, { delayInMinutes: 1, periodInMinutes: 30 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_SCAN) {
    const cfg = await loadConfig();
    if (!cfg.endpoint || !cfg.ingestSecret || !cfg.pois?.length) return;
    for (const p of cfg.pois) {
      try {
        const round = await capturePoi(p.viewid, p.name);
        if (round) {
          await postRound(cfg.endpoint, cfg.ingestSecret, round);
          // 采集完立刻同步 cookies（"尽可能频繁"）
          if (round.cookies && Object.keys(round.cookies).length) {
            await postCookies(cfg.endpoint, cfg.cookieSecret, round.cookies);
          }
        }
      } catch (e) {
        console.error("[sentinel] scan failed", p.viewid, e);
      }
      await sleep(5000);
    }
  } else if (alarm.name === ALARM_BEAT) {
    // 5 min 心跳：仅同步 cookies
    const cfg = await loadConfig();
    if (!cfg.endpoint || !cfg.cookieSecret) return;
    try {
      const cookies = await grabAllCookies();
      if (cookies.GUID) await postCookies(cfg.endpoint, cfg.cookieSecret, cookies);
    } catch (e) {
      console.error("[sentinel] heartbeat failed", e);
    }
  }
});

chrome.alarms.create(ALARM_BEAT, { delayInMinutes: 1, periodInMinutes: 5 });

// ── popup 消息 ──
chrome.runtime.onMessage.addListener((msg, _s, send) => {
  (async () => {
    if (msg?.type === "manual_scan") {
      try {
        const round = await capturePoi(msg.viewid, msg.name);
        if (!round) { send({ ok: false, error: "no shelf captured" }); return; }
        const r = await postRound(msg.endpoint, msg.ingestSecret, round);
        if (round.cookies?.GUID && msg.cookieSecret) {
          await postCookies(msg.endpoint, msg.cookieSecret, round.cookies);
        }
        send({ ok: r.ok, status: r.status });
      } catch (e) { send({ ok: false, error: String(e) }); }
    } else if (msg?.type === "manual_cookie_sync") {
      try {
        const cookies = await grabAllCookies();
        if (!cookies.GUID) { send({ ok: false, error: "no cookies" }); return; }
        const r = await postCookies(msg.endpoint, msg.cookieSecret, cookies);
        send({ ok: r.ok, count: Object.keys(cookies).length });
      } catch (e) { send({ ok: false, error: String(e) }); }
    } else if (msg?.type === "save_config") {
      await saveConfig(msg.config);
      send({ ok: true });
    }
  })();
  return true;
});
```

---

## 7. 部署到 racknerd19

### 7.1 VPS 一次性初始化

```bash
ssh root@racknerd19

apt update && apt -y upgrade
apt install -y python3 python3-venv python3-pip curl jq ufw sqlite3 ca-certificates

# Caddy
apt install -y debian-keyring debian-archive-keyring
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/deb/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt -y install caddy

# 系统用户
useradd -r -m -d /home/ctrip -s /usr/sbin/nologin ctrip
mkdir -p /opt/ctrip-monitor/{code,data,data/raw_rounds,data/cookies,extension,scripts,deploy/systemd,logs}
mkdir -p /etc/ctrip-monitor
chown -R ctrip:ctrip /opt/ctrip-monitor
chmod 750 /etc/ctrip-monitor

ufw default deny incoming
ufw default allow outgoing
ufw allow 22,80,443/tcp
ufw enable
```

### 7.2 部署命令

```bash
cd /Users/argo/666-XCJ/ctrip-price-monitor

# 1. 生成密钥
INGEST_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
COOKIE_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
ADMIN_PWD=$(python3 -c "import secrets;print(secrets.token_urlsafe(12))")
echo "记下: INGEST=$INGEST_SECRET COOKIE=$COOKIE_SECRET ADMIN=$ADMIN_PWD"

# 2. rsync
rsync -avz --delete -e "ssh -i ~/.ssh/id_ed25519" \
  --exclude='.git' --exclude='*.db' --exclude='__pycache__' --exclude='.venv' \
  ./ ctrip@racknerd19:/opt/ctrip-monitor/

# 3. VPS 端初始化
ssh ctrip@racknerd19 << EOF
cd /opt/ctrip-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export INGEST_SECRET='$INGEST_SECRET'
export COOKIE_SYNC_SECRET='$COOKIE_SECRET'
export CTRIP_ADMIN_PASSWORD='$ADMIN_PWD'

# 写 secrets.env
cat > /etc/ctrip-monitor/secrets.env << EOV
INGEST_SECRET=$INGEST_SECRET
COOKIE_SYNC_SECRET=$COOKIE_SECRET
CTRIP_ADMIN_PASSWORD=$ADMIN_PWD
EOV
chmod 640 /etc/ctrip-monitor/secrets.env
chown root:ctrip /etc/ctrip-monitor/secrets.env

# 初始化 DB
.venv/bin/python scripts/init_db.py
EOF

# 4. systemd + caddy
ssh root@racknerd19 << EOF
cp /opt/ctrip-monitor/deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ctrip-web ctrip-server-scraper.timer ctrip-round-parser.timer ctrip-retention.timer caddy
EOF

# 5. Caddyfile
ssh root@racknerd19 bash -c "cat > /etc/caddy/Caddyfile << 'CADDY'
xiecheng.19880913.xyz {
    reverse_proxy 127.0.0.1:8080
    encode zstd gzip
    header {
        -Server
        Strict-Transport-Security \"max-age=31536000; includeSubDomains; preload\"
        X-Content-Type-Options \"nosniff\"
        X-Frame-Options \"DENY\"
        Referrer-Policy \"strict-origin-when-cross-origin\"
    }
    @static path /static/*
    handle @static { header Cache-Control \"public, max-age=3600\" }
    handle /healthz { respond \"ok\" 200 }
}
CADDY
systemctl restart caddy"

curl -fsS https://xiecheng.19880913.xyz/healthz
```

### 7.3 systemd 单元

**ctrip-web.service**（FastAPI）：
```ini
[Unit]
Description=Ctrip Sentinel Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ctrip
WorkingDirectory=/opt/ctrip-monitor
EnvironmentFile=/etc/ctrip-monitor/secrets.env
Environment="PATH=/opt/ctrip-monitor/.venv/bin"
ExecStart=/opt/ctrip-monitor/.venv/bin/uvicorn web.server:app \
    --host 127.0.0.1 --port 8080 --workers 2 --log-level info
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/ctrip-monitor /var/log/ctrip-monitor /etc/ctrip-monitor

[Install]
WantedBy=multi-user.target
```

**ctrip-server-scraper.service / .timer**（30 min）：
```ini
[Unit]
Description=Ctrip Server Background Scraper

[Service]
Type=oneshot
User=ctrip
WorkingDirectory=/opt/ctrip-monitor
EnvironmentFile=/etc/ctrip-monitor/secrets.env
Environment="PATH=/opt/ctrip-monitor/.venv/bin"
ExecStart=/opt/ctrip-monitor/.venv/bin/python scripts/server_scraper.py
NoNewPrivileges=true
PrivateTmp=true
```

```ini
[Unit]
Description=Run Ctrip Server Scraper every 30 min

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

**ctrip-round-parser.service / .timer**（每分钟）：
```ini
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
Persistent=true
```

**ctrip-retention.service / .timer**（每日 02:00）：
```ini
[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true
```

---

## 8. 操作步骤

### 8.1 首次部署

1. VPS 部署完成（§7）
2. 本机 Chrome 登录携程 H5
3. 访问 `https://xiecheng.19880913.xyz/login`，用 `ADMIN_PWD` 登录
4. 进 `/admin/vendors` 录入你的 vendorId（可多个）
5. 进 `/extension` 下载扩展 zip
6. Chrome 加载解压的扩展
7. popup 配置：endpoint + ingestSecret + cookieSecret + POI 列表
8. 点 "立即同步 cookie" → 服务器后台 scraper 下次跑能用
9. 点 "立即采集" 测试 1 POI

### 8.2 日常

- 浏览器常开 + 登录着 → 30 min 自动采集 + 5 min 自动 cookie 心跳
- 服务器 30 min 自动后台抓结构数据
- 服务器 1 min 解析 raw → 算 rank → 告警
- webhook 实时推
- 每天 02:00 聚合 30 天前的数据

### 8.3 故障排查

```bash
# VPS
journalctl -u ctrip-web -f
journalctl -u ctrip-server-scraper -f
journalctl -u ctrip-round-parser -f

sqlite3 /opt/ctrip-monitor/data/monitor.db \
  "SELECT id, status, error_msg, received_at FROM rounds ORDER BY id DESC LIMIT 10;"

# 扩展离线告警阈值
sqlite3 /opt/ctrip-monitor/data/monitor.db \
  "SELECT MAX(received_at) FROM rounds WHERE source='extension';"
```

---

## 9. 风险与兜底

| 风险 | 兜底 |
|---|---|
| 浏览器关闭 → 扩展静默 | 服务器后台 scraper 继续跑（仅 search/addInfo），结构性数据不缺；价格数据 stale，dashboard 顶部告警条 |
| cookies 过期 | 服务器后台 scraper 自动跳过（`if not cookies.GUID: return`）；告警 `cookie_expired` |
| 同一 POI 多个 tab 同时打开 | background.js 用 `tabLock[viewid]` 串行 |
| soa2 字段名变化 | 解析失败 → 写 `parse_failures/` + 推 webhook `round_failed` |
| RackNerd IP 被 Ctrip 风控 | 服务器走 cookies（同一 IP 模式）；扩展走浏览器原生栈（同样）；如发生再考虑加代理 |
| 多用户密码泄露 | bcrypt + session 7 天过期；登录失败计数 24h 清理 |
| VPS 被 root | cookies 是 C 端账号，攻击者拿到的权限 = 普通用户浏览价；生产环境加 SSH key + ufw + fail2ban |

---

## 10. 实施路线图（4 天）

| 阶段 | 任务 | 验证 |
|---|---|---|
| **D1 上午** | 重写 `code/selectors.py` + `code/parse.py`（含 rank_history）+ 单测（拿 `_captures/` 已有 JSON 跑通） | pytest |
| **D1 下午** | `code/alerts.py`（rank 检测）+ `web/notifier.py`（webhook）+ 单测 | pytest |
| **D2 上午** | FastAPI 骨架（auth + login + dashboard 路由 + 5 模板）+ design tokens | 本地 uvicorn 起 |
| **D2 下午** | `scripts/init_db.py` + `code/ingest.py`（/api/ingest/round + /api/cookies/sync）+ `scripts/server_scraper.py` + `scripts/round_parser.py` | 本地手动 POST round → 解析 → 看 rank_history |
| **D3 上午** | 扩展 manifest + background.js（含 cookie 心跳）+ popup + content script | 本地加载扩展 → 手动采集 1 POI |
| **D3 下午** | 部署到 racknerd19（Caddy + systemd 4 个 + .env） | `curl https://xiecheng.19880913.xyz/healthz` |
| **D4** | 5 POI 全部接入 + 装扩展 + 跑 24h | dashboard 显示 24h 数据 |
| **D5** | 多 POI tab + 我的足迹 toggle + 日历视图 + sparkline | 自我验收 |

---

## 11. 关键不变式（写进代码注释）

1. **`vendorId ∈ my_vendors` ≡ "我"**——任何 "is_self" 判断走这个集合，不要硬编码 999999
2. **rank_history 在解析时算，不存"今日快照"**——rounds 删 30 天后 rank_history 自然级联删，daily_summary 保留日粒度
3. **webhook payload 通用 JSON，不绑平台**——用户随便改 URL
4. **cookie sync 永远 latest-wins**——只留最近 5 份（防 disk 膨胀）
5. **dashboard hero 三件套**：状态卡（A）+ sparkline 列表（B）+ 日历热图（C）
6. **告警仅在 extension source 的 round 上触发**——服务器后台 scraper 没有 displayPrice（没 getProductShelf），算不出 rank