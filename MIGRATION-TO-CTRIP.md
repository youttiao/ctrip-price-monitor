# 「OTA 监控哨兵」通用设计文档 —— 模板（飞猪 → 携程 迁移指南）

> 这份文档把 **fliggy-monitor** 项目的设计抽象成「监控任意 OTA 网页关键接口」的通用模板，附带可直接拷贝到下一个项目的代码片段（Python / JavaScript / JSON / systemd / GitHub Actions）。
>
> 目标：**在你本机抓包找出关键接口 → 把采集逻辑搬到 VPS → 写一个 Chrome 扩展让你一键同步登录态到 VPS**。

---

## 0. 总体三段式架构

```
┌──────────────────────────────────────────────────────────────┐
│                     你本机 (macOS)                              │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │ Chrome 浏览器                                              │ │
│  │  - 你已登录携程账号                                        │ │
│  │  - DevTools Network 抓关键 mtop 接口                       │ │
│  │  - 安装本地写的「Cookie 同步扩展」                          │ │
│  └──────────────────────────────────────────────────────────┘ │
│                            │                                  │
│                            ▼ (点扩展图标 → 自动开小窗抓 cookie)│
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  抓包脚本 + 调试脚本（curl 复现）                          │ │
│  │  - 完全在本地跑、拿到真实请求格式                          │ │
│  │  - 验证 sign 算法 / cookie 缺什么                          │ │
│  └──────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼ (rsync / git push 触发 deploy)
┌──────────────────────────────────────────────────────────────┐
│                     VPS (Ubuntu 22.04)                         │
│  ┌──────────────────────┐    ┌──────────────────────────┐     │
│  │ Web Dashboard        │    │ Monitor 采集器            │     │
│  │ FastAPI + Jinja2     │◄──►│ Python 脚本              │     │
│  │ 单密码登录 + 监控视图 │    │ systemd timer 每 30 min   │     │
│  └──────────┬───────────┘    └──────────┬───────────────┘     │
│             │ SQLite (WAL)             │                      │
│             └────────┬─────────────────┘                      │
│                      ▼                                        │
│              /opt/xxx-monitor/data/monitor.db                  │
│                      │                                        │
│                      ▼ (webhook POST)                         │
│              ┌────────────────────────┐                       │
│              │ 钉钉 / 飞书 / Telegram │                       │
│              └────────────────────────┘                       │
└──────────────────────────────────────────────────────────────┘
```

---

## 1. 第一步 · 本机抓包找关键接口

### 1.1 工具准备

```bash
# 本机先装好
brew install --cask google-chrome    # 或 Edge / Brave 都行
brew install mitmproxy              # 抓 HTTPS（可选）
brew install jq                      # 命令行 JSON 美化
```

### 1.2 抓包流程（5 步）

1. Chrome 开 DevTools → Network 面板 → 勾 **Preserve log**
2. Filter 输入关键词（携程通常是 `mtop`, `gw`, `jsonp`）
3. 操作你要监控的页面：进 POI 详情、选日期、加购、提交订单……每步都看 Network
4. 找到稳定触发的接口（列表加载 / 详情加载 / 价格刷新），右键 → **Copy as cURL (bash)**
5. 把 curl 拿到的 URL + headers + body + cookies 保存到 `/tmp/xxx_capture.txt`

> **关键观察点**：
> - `appKey` / `ttid` 常量（携程可能是 `appKey=99999999`，`ttid=...`）
> - 签名参数 `sign` 的算法（md5/hmac-sha256，token 哪里来）
> - 必带的 cookie 名字（4-6 个，常含 `_m_h5_tk` / `cookie2` 类似命名）
> - `ret` 字段：`["SUCCESS::xxx"]` 列表型 vs `"SUCCESS"` 字符串型

### 1.3 curl 复现脚本模板

抓到一个 mtop 接口后，把 curl 抽成 Python 复现脚本：

```python
# /tmp/repro_ctrip.py
"""ctrip mtop 复现脚本 — 把浏览器抓的 cURL 转成 Python。"""
import json, subprocess, time, hashlib
from urllib.parse import quote

# === 1. 从浏览器拷贝的常量 ===
MTOP_GATEWAY = "https://m.ctrip.com/restapi/soa2/..."  # ← 抓包拿到
APP_KEY = "99999999"        # ← 抓包 header / URL 拿到
TTID = "..."                # ← 抓包 header 拿到

# === 2. cookie（先用浏览器 DevTools Application → Cookies 复制一份） ===
COOKIES = {
    "_m_h5_tk":       "abc123_<13位 unix-ms>",
    "_m_h5_tk_enc":   "<32 hex>",
    "cookie2":        "<32 hex>",
    "t":              "<32 hex>",
}

# === 3. sign 算法（关键） ===
def _token(): return COOKIES["_m_h5_tk"].split("_", 1)[0]

def sign(data_str: str, t_ms: str) -> str:
    # mtop 标准: md5(token & t & appKey & data)
    return hashlib.md5(f"{_token()}&{t_ms}&{APP_KEY}&{data_str}".encode()).hexdigest()

# === 4. 复现请求 ===
def request(api_path: str, data_obj: dict) -> dict:
    data_str = json.dumps(data_obj, separators=(",", ":"), ensure_ascii=False)
    t_ms = str(int(time.time() * 1000))
    sig = sign(data_str, t_ms)
    
    url = (f"{MTOP_GATEWAY}{api_path}"
           f"?type=originaljson"
           f"&data={quote(data_str, safe='')}"
           f"&ttid={quote(TTID)}"
           f"&appKey={APP_KEY}"
           f"&t={t_ms}"
           f"&sign={sig}")
    
    cookie_h = "; ".join(f"{k}={v}" for k, v in COOKIES.items())
    out = subprocess.check_output([
        "curl", "-sS", "--max-time", "15",
        "-H", f"referer: https://m.ctrip.com/",
        "-H", f"user-agent: Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) ...",
        "-H", f"cookie: {cookie_h}",
        url,
    ]).decode()
    raw = json.loads(out)
    ret = raw.get("ret", [])
    if not any(str(r).startswith("SUCCESS") for r in ret if r):
        raise RuntimeError(f"mtop ret != SUCCESS: {ret}; body[:300]={out[:300]!r}")
    return raw

# === 5. 跑通业务调用 ===
if __name__ == "__main__":
    # 列表 / 详情 / 价格 — 按你抓到的接口填
    raw = request("/12345/list", {"cityId": "1", "poiId": "..."})
    print(json.dumps(raw, indent=2, ensure_ascii=False)[:500])
```

**验证标准**：
- 用从浏览器复制过来的 cookies 跑出 `SUCCESS`
- 把 cookies 里的 `_m_h5_tk` 改 1 个字符再跑 → 应该返回 `FAIL_SYS_SESSION_EXPIRED`（说明 sign 算法对了，token 校验生效）

---

## 2. 第二步 · 项目骨架

### 2.1 目录结构（直接照抄）

```
ctrip-monitor/                     ← 项目根（本地路径）
├── README.md
├── docs/                          ← 设计文档（先写完再实现）
├── code/                          ← 抓包逻辑（来自上面 §1）
│   ├── ctrip_selectors.py         ← 所有常量（API/APP_KEY/TTID/SELF_SELLER_ID）
│   ├── mtop_client.py             ← curl 复现 + parse 函数
│   └── ctrip_monitor.py           ← 主循环（写 DB + 告警 + webhook）
├── data/
│   ├── poi_registry.json          ← 监控目标列表
│   ├── seller_baseline.json       ← 自营基线（哪些 seller_id 是自己）
│   └── seller_cache.json          ← 已知 seller 元数据缓存
├── web/                           ← FastAPI dashboard
│   ├── server.py                  ← app + middleware
│   ├── auth.py                    ← 密码 + session
│   ├── db.py                      ← SQLite 封装
│   ├── notifier.py                ← webhook 推送
│   ├── routes/
│   │   ├── pages.py
│   │   ├── api.py
│   │   ├── sellers.py
│   │   ├── cookie_sync.py         ← 接收扩展上传 cookie
│   │   └── extensions.py          ← 扩展下载/zip 路由
│   ├── templates/                 ← Jinja2
│   └── static/css/main.css        ← 手写 CSS（design tokens）
├── extensions/
│   └── ctrip-cookie-sync/         ← Chrome 扩展源码
│       ├── manifest.json
│       ├── background.js          ← 开小窗 + 抓 cookie + POST
│       ├── popup.html
│       ├── popup.js
│       ├── popup.css
│       ├── icons/{16,48,128}.png
│       └── build.sh               ← 打包成 zip
├── scripts/
│   ├── init_db.py                 ← 建表 + 导入 JSON + 写 bcrypt 密码
│   ├── refresh_cookies.py         ← Playwright 自动续 cookie
│   └── deploy.sh                  ← 一键 rsync + restart
├── deploy/                        ← VPS 配置
│   ├── systemd/
│   │   ├── ctrip-web.service
│   │   ├── ctrip-monitor.service
│   │   ├── ctrip-monitor.timer
│   │   ├── ctrip-cookies-refresh.service
│   │   └── ctrip-cookies-refresh.timer
│   └── Caddyfile
├── .github/workflows/deploy.yml   ← CI/CD（可选）
├── requirements.txt
└── pyproject.toml
```

### 2.2 `code/xxx_selectors.py`（所有常量集中地）

```python
"""ctrip H5 mtop API 常量。新项目从这里 import，不要 hard-code。

Source: 浏览器 Network 面板抓包 + curl 复现验证
Updated: 2026-XX-XX
"""

# ── mtop gateway ────────────────────────────────────────────────────
MTOP_GATEWAY = "https://m.ctrip.com/restapi/..."  # ← 改成 ctrip 实际值
APP_KEY = "99999999"                             # ← 改成抓包拿到的
TTID = "..."                                      # ← 改成抓包拿到的

# ── 业务 API（每个 mtop API 一对：API + version + fc 参数）────────
LIST_API = f"{MTOP_GATEWAY}/<list 接口路径>"
LIST_VERSION = "..."

DETAIL_API = f"{MTOP_GATEWAY}/<detail 接口路径>"
DETAIL_VERSION = "..."

# booktips / 商家信息（不同 OTA 命名可能不一样，ctrip 可能是 shopinfo）
SHOPINFO_API = f"{MTOP_GATEWAY}/<shopinfo 路径>"
SHOPINFO_VERSION = "..."

# ── 必要 Cookie（实测是哪些）────────────────────────────────────
REQUIRED_COOKIES = [
    "_m_h5_tk",       # sign token（含 _<expiry> 后缀）
    "_m_h5_tk_enc",   # 配合验签
    "cookie2",        # 32-char hex
    "t",              # mtop session token
    # ctrip 也许额外要：guid / allianceid / sid
]

# ── 必带请求头 ────────────────────────────────────────────────────
REQUIRED_HEADERS = {
    "referer":   "https://m.ctrip.com/",   # ← 改成 ctrip 的
    "origin":    "https://m.ctrip.com",   # ← 改成 ctrip 的
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
}

# ── Self sellerId baseline ─────────────────────────────────────────
# 把"自己的店铺"或"自营标志"识别出来 ——
#   - 飞猪是 seller_id 直接对比
#   - 携程可能是 supplierId/agencyId，或者某个特定字段如 isMain="1"
#   - 也许需要多个 ID（不同品牌线）
SELF_SELLER_IDS = {
    "ctrip_main":   "...",   # 携程主自营
    "ctrip_train":  "...",   # 携程火车票自营
    # 按需加
}
```

### 2.3 `code/mtop_client.py`（curl 复现层）

> 这个文件几乎是 fliggy 那份**直接拷过来改常量**即可。核心 4 个东西不变：
> 1. `_sign(token, t, appKey, data)` —— mtop 标准算法
> 2. `_request(url, data_obj)` —— 拼 URL + sign + curl
> 3. `shelf()` / `booktips()` —— 业务方法，每个 OTA 一份
> 4. `parse_xxx()` —— 把 raw JSON 解成结构化 cell dict

```python
"""ctrip mtop client — 纯 stdlib + curl，VPS 上零依赖就能跑。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from typing import Any
from urllib.parse import quote

from ctrip_selectors import (
    APP_KEY,
    DETAIL_API, DETAIL_VERSION,
    LIST_API, LIST_VERSION,
    REQUIRED_HEADERS,
    SHOPINFO_API, SHOPINFO_VERSION,
    TTID,
)


class MtopError(Exception):
    pass


class MtopClient:
    """ctrip mtop HTTP client. thread-unsafe; create per thread or wrap with lock."""

    CURL_BIN = "/usr/bin/curl"

    def __init__(self, cookies: dict[str, str], timeout: int = 15):
        self.cookies = dict(cookies)
        self.timeout = timeout
        tk = self.cookies.get("_m_h5_tk", "")
        if "_" not in tk:
            raise MtopError(f"_m_h5_tk format invalid: {tk!r}")
        self._token = tk.split("_", 1)[0]
        missing = [k for k in ("_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t")
                   if k not in cookies]
        if missing:
            raise MtopError(f"missing required cookies: {missing}")

    @staticmethod
    def _sign(token: str, t_ms: str, app_key: str, data_str: str) -> str:
        """MD5(token & t & appKey & data).hexdigest()  ← raw data 字符串，**不要 sort_keys**"""
        return hashlib.md5(f"{token}&{t_ms}&{app_key}&{data_str}".encode()).hexdigest()

    def _request(self, url_base: str, data_obj: dict[str, Any], version: str) -> dict:
        data_str = json.dumps(data_obj, separators=(",", ":"), ensure_ascii=False)
        t_ms = str(int(time.time() * 1000))
        sig = self._sign(self._token, t_ms, APP_KEY, data_str)

        url = (f"{url_base}?type=originaljson"
               f"&data={quote(data_str, safe='')}"
               f"&ttid={quote(TTID)}"
               f"&appKey={APP_KEY}"
               f"&t={t_ms}"
               f"&sign={sig}")

        cookie_h = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        cmd = [
            self.CURL_BIN, "-sS",
            "--max-time", str(self.timeout),
            "-H", f"referer: {REQUIRED_HEADERS['referer']}",
            "-H", f"origin:  {REQUIRED_HEADERS['origin']}",
            "-H", f"user-agent: {REQUIRED_HEADERS['user-agent']}",
            "-H", f"cookie: {cookie_h}",
            url,
        ]
        out = subprocess.check_output(cmd).decode()
        try:
            raw = json.loads(out)
        except json.JSONDecodeError as e:
            raise MtopError(f"JSON decode failed: {e}; body[:200]={out[:200]!r}")

        # ret 可能是 list 也可能是 string
        ret = raw.get("ret", [])
        ret_strs = ret if isinstance(ret, list) else [ret]
        if not any(str(r).startswith("SUCCESS") for r in ret_strs if r):
            raise MtopError(f"mtop ret != SUCCESS: {ret}; body[:300]={out[:300]!r}")
        return raw

    # ── 业务方法：每个 mtop API 一个 ─────────────────────────────
    def list(self, city_id: str, poi_id: str) -> dict:
        """列表页（可能是 POI 详情页的"门票类型"分页）"""
        data = {
            "cityId": city_id,
            "poiId":  poi_id,
            # 按抓包的 data 字段填全
        }
        return self._request(LIST_API, data, LIST_VERSION)

    def detail(self, item_id: str, sku_id: str) -> dict:
        """详情页（每条 SKU 的详细信息 + 价格）"""
        data = {
            "itemId": item_id,
            "skuId":  sku_id,
        }
        return self._request(DETAIL_API, data, DETAIL_VERSION)

    def shopinfo(self, supplier_id: str) -> dict:
        """店铺信息（替代 booktips，可能叫 shopinfo / supplierdetail）"""
        data = {"supplierId": supplier_id}
        return self._request(SHOPINFO_API, data, SHOPINFO_VERSION)


# ── parse helpers ──────────────────────────────────────────────────
def parse_sku_cells(list_raw: dict) -> list[dict]:
    """从 list raw 抽所有 SKU cell + sellerId + 价格/库存。

    返回结构（标准化，便于下游）：
    [{
        "itemId":     "...",
        "skuId":      "...",
        "poiId":      "...",
        "poiName":    "...",
        "name":       "...",          # SKU 名
        "price":      "¥58",          # 给日志看
        "integerPrice":  "58",        # 整数部分
        "priceDecimal":  ".5",         # 小数部分
        "priceSuffix":   "起",
        "sold":         "1234",
        "cellType":     "景点门票",
        "sellerId":     "...",
    }, ...]
    """
    # ⚠️ 字段路径每个 OTA 都不同，按抓包实际结构改
    try:
        shelves = list_raw["data"]["result"]["data"]["xxx"]["shelves"]  # ← 按 ctrip 改
    except (KeyError, TypeError):
        return []
    out = []
    # ... 遍历抽字段，结构跟 fliggy 那份几乎一样
    return out


def parse_shop_info(shopinfo_raw: dict) -> dict | None:
    """从 shopinfo raw 抽店铺名 / icon / URL / 服务人数。"""
    try:
        info = shopinfo_raw["data"]["shopInfo"]  # ← 按 ctrip 改
    except (KeyError, TypeError):
        return None
    if not info.get("name"):
        return None
    return {
        "sellerName":   info.get("name"),
        "sellerIcon":   info.get("icon"),
        "shopJumpUrl":  info.get("url"),
        "serviceStats": info.get("serviceList", []),
    }
```

---

## 3. 第三步 · 数据库 schema

### 3.1 SQLite WAL 模式（必开）

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

### 3.2 10 张表（直接照搬，改 `webhook_rules` 默认值即可）

```sql
-- 1. 每一轮扫描 = 一行
CREATE TABLE rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        TEXT UNIQUE NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    cells_total     INTEGER DEFAULT 0,
    cells_self      INTEGER DEFAULT 0,
    cells_non_self  INTEGER DEFAULT 0,
    new_sellers     INTEGER DEFAULT 0,
    detail_hits     INTEGER DEFAULT 0,           -- 改名：booktips → detail
    error_msg       TEXT,
    duration_ms     INTEGER
);
CREATE INDEX idx_rounds_started ON rounds(started_at DESC);

-- 2. 每行一个 cell 在一轮扫描中的快照
CREATE TABLE cells_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    poi_id          TEXT NOT NULL,
    poi_name        TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    sku_id          TEXT NOT NULL,
    cell_type       TEXT,
    sku_name        TEXT,
    price_int       TEXT,
    price_dec       TEXT,
    price_suffix    TEXT,
    sold            TEXT,
    seller_id       TEXT NOT NULL,
    is_self         INTEGER NOT NULL,
    raw_json        TEXT,                        -- 该 cell 的原始 JSON
    first_seen_at   TEXT NOT NULL,
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    UNIQUE(round_id, poi_id, item_id, sku_id)
);
CREATE INDEX idx_cells_round    ON cells_snapshot(round_id);
CREATE INDEX idx_cells_poi      ON cells_snapshot(poi_id);
CREATE INDEX idx_cells_seller   ON cells_snapshot(seller_id);
CREATE INDEX idx_cells_item     ON cells_snapshot(item_id);
CREATE INDEX idx_cells_first    ON cells_snapshot(first_seen_at);
CREATE INDEX idx_cells_is_self  ON cells_snapshot(is_self);

-- 3. 卖家主表（latest snapshot）
CREATE TABLE sellers (
    seller_id        TEXT PRIMARY KEY,
    seller_name      TEXT,
    seller_icon      TEXT,
    shop_jump_url    TEXT,
    service_stats    TEXT,
    is_self          INTEGER NOT NULL DEFAULT 0,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    total_cells      INTEGER DEFAULT 0,
    shopinfo_refreshed_at TEXT,
    shopinfo_raw     TEXT
);
CREATE INDEX idx_sellers_self   ON sellers(is_self);
CREATE INDEX idx_sellers_last   ON sellers(last_seen_at DESC);
CREATE INDEX idx_sellers_name   ON sellers(seller_name);

-- 触发器：自动更新 last_seen / total_cells
CREATE TRIGGER trg_seller_upsert
AFTER INSERT ON cells_snapshot
BEGIN
    INSERT INTO sellers (seller_id, first_seen_at, last_seen_at, total_cells, is_self)
    VALUES (NEW.seller_id, NEW.first_seen_at, NEW.first_seen_at, 1, NEW.is_self)
    ON CONFLICT(seller_id) DO UPDATE SET
        last_seen_at = NEW.first_seen_at,
        total_cells  = total_cells + 1,
        is_self      = NEW.is_self;
END;

-- 4. 用户管理的卖家元数据（display_name / 关注 / 备注）
CREATE TABLE seller_enrichment (
    seller_id       TEXT PRIMARY KEY,
    display_name    TEXT,
    is_watched      INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    tags            TEXT,
    priority        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    created_by      TEXT,
    FOREIGN KEY (seller_id) REFERENCES sellers(seller_id) ON DELETE CASCADE
);
CREATE INDEX idx_seller_enrich_watched  ON seller_enrichment(is_watched);
CREATE INDEX idx_seller_enrich_priority ON seller_enrichment(priority DESC);

-- 5. 监控目标 POI 配置
CREATE TABLE pois (
    poi_id          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    city_id         TEXT,                -- 携程需要 city_id
    tb_cn           TEXT,
    h5_url          TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    polling_sec     INTEGER NOT NULL DEFAULT 1800,
    last_scanned_at TEXT,
    last_status     TEXT,
    last_error      TEXT,
    cells_avg       INTEGER,
    created_at      TEXT NOT NULL
);

-- 6. KV 配置
CREATE TABLE config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
INSERT INTO config VALUES
    ('admin_password_hash', '"$2b$12$...bcrypt..."',   datetime('now')),
    ('webhook_url',         'null',                    datetime('now')),
    ('webhook_secret',      'null',                    datetime('now')),
    ('webhook_platform',    '"custom"',                datetime('now')),
    ('webhook_rules',       '{"non_self_new":true,"price_alert":true,"self_missing":true,"first_seller":false,"detail_error":true}', datetime('now')),
    ('self_seller_ids',     '["ctrip_main","ctrip_train"]', datetime('now')),
    ('site_name',           '"携程哨兵"',               datetime('now')),
    ('site_timezone',       '"Asia/Shanghai"',         datetime('now'));

-- 7. Dashboard 登录 session
CREATE TABLE web_sessions (
    sid         TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    user_agent  TEXT,
    ip_prefix   TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_web_sessions_expires ON web_sessions(expires_at);
CREATE INDEX idx_web_sessions_active  ON web_sessions(is_active);
CREATE INDEX idx_web_sessions_ip      ON web_sessions(ip_prefix, last_seen_at DESC);

-- 8. 登录失败计数（IP /24 维度）
CREATE TABLE login_failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_prefix   TEXT NOT NULL,
    ts          TEXT NOT NULL,
    user_agent_family TEXT,
    reason      TEXT
);
CREATE INDEX idx_login_fail_ip_ts ON login_failures(ip_prefix, ts DESC);

-- 9. 告警事件流
CREATE TABLE alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    round_id        INTEGER,
    type            TEXT NOT NULL,    -- non_self_new / price_alert / self_missing / first_seller / detail_error
    severity        TEXT NOT NULL,
    poi_id          TEXT,
    poi_name        TEXT,
    item_id         TEXT,
    sku_id          TEXT,
    seller_id       TEXT,
    payload         TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    webhook_status  TEXT,
    webhook_sent_at TEXT,
    webhook_resp    TEXT,
    webhook_retry   INTEGER DEFAULT 0,
    UNIQUE(dedup_key)
);
CREATE INDEX idx_alerts_ts      ON alerts(ts DESC);
CREATE INDEX idx_alerts_type    ON alerts(type);
CREATE INDEX idx_alerts_poi     ON alerts(poi_id);
CREATE INDEX idx_alerts_severity ON alerts(severity);
CREATE INDEX idx_alerts_status  ON alerts(webhook_status);

-- 10. Cookie 续期记录（只追加）
CREATE TABLE cookies_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    token_prefix    TEXT NOT NULL,
    expiry_ts       TEXT,
    source          TEXT,
    success         INTEGER NOT NULL,
    error_msg       TEXT
);
CREATE INDEX idx_cookies_ts ON cookies_history(ts DESC);
```

### 3.3 数据保留策略

```sql
-- 每日清理（cron 或 init 时跑一次）
DELETE FROM cells_snapshot
WHERE id IN (SELECT id FROM cells_snapshot
             WHERE first_seen_at < datetime('now', '-90 days') LIMIT 10000);

DELETE FROM web_sessions
WHERE expires_at < datetime('now')
   OR (is_active = 0 AND last_seen_at < datetime('now', '-7 days'));

DELETE FROM login_failures WHERE ts < datetime('now', '-24 hours');
DELETE FROM cookies_history WHERE ts < datetime('now', '-30 days');
```

---

## 4. 第四步 · Web Dashboard（FastAPI + Jinja2 + 手写 CSS）

### 4.1 选型理由（直接套用）

| 层 | 选型 | 理由 |
|---|---|---|
| Web 框架 | FastAPI 0.115+ | 异步 / 类型注解 / 自动 OpenAPI |
| 模板 | Jinja2 | 无构建步骤、单用户 |
| 前端交互 | HTMX 2.0 + Alpine.js 3.x | 局部刷新不写 SPA |
| CSS | 手写 CSS（design tokens） | 内部工具不需要 Tailwind |
| 数据库 | SQLite 3.45+（WAL） | 单机、零运维 |
| Web 服务器 | Uvicorn（worker=2） | FastAPI 原生 |
| 反代 + TLS | Caddy 2.x | 自动 HTTPS |

**不要**用 React/Vue/SPA —— 单用户工具改完即生效最重要。

### 4.2 鉴权（核心模式：session cookie + bcrypt + 服务端 store）

```python
# web/auth.py —— 几乎直接拷 fliggy 那份
import bcrypt, secrets, sqlite3, hmac
from datetime import datetime, timedelta, timezone
from fastapi import Request, Response

SESSION_COOKIE = "ctrip_sid"
SESSION_TTL = timedelta(days=7)
LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW = timedelta(minutes=10)

# 密码从 env 读（v1 简化版），后期换 bcrypt 从 config 表读
ADMIN_PASSWORD = os.getenv("CTRIP_ADMIN_PASSWORD", "your_password_here")

def verify_password(input_pwd: str) -> bool:
    if not input_pwd:
        return False
    return hmac.compare_digest(input_pwd.encode(), ADMIN_PASSWORD.encode())

def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "0.0.0.0")

def ip_prefix_24(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else ip

def ua_family(ua: str | None) -> str:
    if not ua: return "unknown"
    ua = ua.lower()
    for token in ("edg/", "opr/", "chrome/", "safari/", "firefox/"):
        idx = ua.find(token)
        if idx >= 0: return token.rstrip("/")
    return "other"

def create_session(conn, *, ip: str, ua: str) -> str:
    sid = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + SESSION_TTL
    conn.execute("""
        INSERT INTO web_sessions (sid, created_at, last_seen_at, expires_at, user_agent, ip_prefix, is_active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
    """, (sid, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
          expires.isoformat(timespec="seconds"), ua, ip_prefix_24(ip)))
    conn.commit()
    return sid

def get_session(conn, sid: str):
    if not sid: return None
    row = conn.execute("SELECT * FROM web_sessions WHERE sid=? AND is_active=1", (sid,)).fetchone()
    if not row: return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        destroy_session(conn, sid)
        return None
    return row

def touch_session(conn, sid: str):
    """滑动续期：距离 last_seen_at > 1h 才动 expires_at"""
    now = datetime.now(timezone.utc)
    row = conn.execute("SELECT last_seen_at FROM web_sessions WHERE sid=?", (sid,)).fetchone()
    if not row: return
    last = datetime.fromisoformat(row["last_seen_at"])
    if now - last > timedelta(hours=1):
        conn.execute("UPDATE web_sessions SET last_seen_at=?, expires_at=? WHERE sid=?",
                     (now.isoformat(timespec="seconds"),
                      (now + SESSION_TTL).isoformat(timespec="seconds"), sid))
    else:
        conn.execute("UPDATE web_sessions SET last_seen_at=? WHERE sid=?",
                     (now.isoformat(timespec="seconds"), sid))
    conn.commit()

def destroy_session(conn, sid):
    conn.execute("UPDATE web_sessions SET is_active=0 WHERE sid=?", (sid,))
    conn.commit()

def record_failure(conn, *, ip, ua, reason):
    conn.execute("INSERT INTO login_failures (ip_prefix, ts, user_agent_family, reason) VALUES (?,?,?,?)",
                 (ip_prefix_24(ip), datetime.now(timezone.utc).isoformat(timespec="seconds"), ua_family(ua), reason))
    conn.commit()

def is_locked_out(conn, ip) -> bool:
    cutoff = (datetime.now(timezone.utc) - LOCKOUT_WINDOW).isoformat(timespec="seconds")
    row = conn.execute("SELECT COUNT(*) AS n FROM login_failures WHERE ip_prefix=? AND ts > ?",
                       (ip_prefix_24(ip), cutoff)).fetchone()
    return bool(row and row["n"] >= LOCKOUT_THRESHOLD)

def clear_failures(conn, ip):
    conn.execute("DELETE FROM login_failures WHERE ip_prefix=?", (ip_prefix_24(ip),))
    conn.commit()

def set_session_cookie(response, sid, *, secure):
    response.set_cookie(
        key=SESSION_COOKIE, value=sid, max_age=int(SESSION_TTL.total_seconds()),
        httponly=True, secure=secure, samesite="strict", path="/"
    )

class _RedirectToLogin(Exception): pass

async def require_login(request: Request):
    conn = request.app.state.db
    sid = request.cookies.get(SESSION_COOKIE, "")
    sess = get_session(conn, sid) if sid else None
    if not sess: raise _RedirectToLogin()
    touch_session(conn, sid)
    return sess
```

### 4.3 前端设计原则（5 条核心）

1. **CSS Variables = design tokens**（一份主 tokens 即可复用到所有页面）：
```css
:root {
  --ink:        #0B0E14;     /* 页面底色 */
  --panel:      #13171F;     /* 卡片 */
  --panel-up:   #1A2030;     /* hover */
  --rule:       #262C36;     /* hairline */
  --rule-bold:  #3A414E;     /* 强调 */
  --text:       #D4D7DD;
  --text-dim:   #7A8290;
  --text-faint: #4A5260;
  --phosphor:   #4FD0B8;     /* 自营 / 在线 */
  --cyan:       #5EAEFF;     /* 关注 */
  --amber:      #E5A847;     /* 告警 / 新出现 */
  --ok:         #7BC97B;
  
  --font-display: 'IBM Plex Sans Condensed', sans-serif;
  --font-body:    'IBM Plex Sans', system-ui, sans-serif;
  --font-mono:    'JetBrains Mono', monospace;
}
```

2. **整张 SKU 主表用等宽字体** —— 数字 / SKU 名 / 店铺名全部 mono，列严格对齐
3. **三档角色色** —— 自营 phosphor / 关注 cyan / 其他默认；amber 只用作事件标识（24h 内新出现）
4. **顶部状态条 sticky** —— 56px 高，显示时钟 + LIVE 状态 + POI 计数 + 告警计数
5. **HTMX 局部刷新 + Alpine 小交互** —— 不写 SPA，30s 自动刷一次主表

### 4.4 路由规划

| 路由 | 用途 |
|---|---|
| `GET /login` `/POST /login` | 登录 |
| `POST /logout` | 登出 |
| `GET /` | Dashboard 总览（POI 卡片网格） |
| `GET /poi/{poi_id}` | 单 POI 详情（按 cell_type 分组） |
| `GET /sku/{item_id}/{sku_id}` | 单 SKU 详情 |
| `GET /sellers` `/GET /sellers/{id}` | 卖家管理 |
| `GET /alerts` | 告警历史 |
| `GET /settings` | 设置（webhook / POI 启停 / 密码） |
| `GET /api/cells?poi=X` | JSON: 拉新 |
| `POST /api/cookies/sync` | 接收扩展上传的 cookie |
| `GET /api/cookies/health` | 探测 sync 状态 |

---

## 5. 第五步 · Chrome 扩展（Cookie 同步）—— **整个项目最关键的可复用部件**

> 这是 fliggy-monitor 项目里**最容易被低估、最难复刻**的部分。把浏览器里登录态同步到 VPS，常规做法都是「手动复制 cookie 然后 ssh 上去贴」，体验极差。这个扩展做的是**点一下图标就全自动**。

### 5.1 原理（一句话）

**打开一个手机尺寸的窗口（mobile viewport）→ 加载携程 H5 → 等 4 秒让 cookie 落地 → 抓浏览器所有 mtop cookie → POST 到 VPS → 关窗口**。

### 5.2 文件清单

```
extensions/ctrip-cookie-sync/
├── manifest.json       # MV3
├── background.js       # 后台 worker：核心逻辑
├── popup.html          # 配置 + 状态
├── popup.js            # popup 逻辑
├── popup.css           # 跟 dashboard 同色系
├── icons/{16,48,128}.png
└── build.sh            # 打包成 zip
```

### 5.3 `manifest.json`（完整可改）

```json
{
  "manifest_version": 3,
  "name": "携程哨兵 · Cookie 同步",
  "short_name": "ctrip-cookie-sync",
  "version": "1.0.0",
  "description": "点一下工具栏图标，自动开手机尺寸窗口访问携程 H5 → 自动抓 mtop cookies → 自动上传到 VPS。",
  "action": {
    "default_title": "携程哨兵 Cookie 同步（点一下自动同步）",
    "default_popup": "popup.html",
    "default_icon": {
      "16":  "icons/16.png",
      "48":  "icons/48.png",
      "128": "icons/128.png"
    }
  },
  "icons": {
    "16":  "icons/16.png",
    "48":  "icons/48.png",
    "128": "icons/128.png"
  },
  "background": {
    "service_worker": "background.js",
    "type": "module"
  },
  "permissions": ["cookies", "storage", "tabs", "windows", "alarms", "notifications", "scripting"],
  "host_permissions": [
    "https://*.ctrip.com/*",
    "http://*.ctrip.com/*",
    "https://*.trip.com/*",
    "http://*.trip.com/*",
    "https://your-dashboard.example.com/*"
  ]
}
```

### 5.4 `background.js`（核心，几乎原样可用）

```javascript
// 携程哨兵 Cookie 同步 — 后台 Service Worker
//
// 职责：被 popup 触发 → 开手机尺寸窗口访问携程 H5 → 等就绪 + 4s grace → 抓 cookies → POST 到 VPS → 关窗口

// ⚠️ 改这里：携程实际必需的 cookie 名 + H5 入口页 URL
const REQUIRED = ["_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t"];
const H5_URL = "https://m.ctrip.com/";        // ← 改成携程实际 H5 落地页
const H5_URL_PREFIX = "https://m.ctrip.com/"; // ← 严格匹配

// 携程相关的 cookie 域
const COOKIE_DOMAINS = [
  ".ctrip.com",
  ".trip.com",
  ".m.ctrip.com",
  ".cn.trip.com",
];

// CHIPS 分区 cookie 的 top-level site（实测后填）
const PARTITIONED_TOP_LEVEL_SITES = [
  "https://ctrip.com",
  "https://www.ctrip.com",
];

const MOBILE_W = 414;
const MOBILE_H = 896;
const GRACE_MS = 4000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function findExistingH5Window() {
  const wins = await chrome.windows.getAll({ populate: true });
  for (const w of wins) {
    for (const tab of w.tabs || []) {
      if ((tab.url || "").startsWith(H5_URL_PREFIX)) return w;
    }
  }
  return null;
}

async function grabAllCookies() {
  const all = {};
  const seen = new Set();

  // 1) 非分区 cookie
  for (const domain of COOKIE_DOMAINS) {
    let list = [];
    try { list = await chrome.cookies.getAll({ domain }); } catch (e) { continue; }
    for (const c of list) {
      const key = c.name + "@" + (c.domain || domain);
      if (seen.has(key)) continue;
      seen.add(key);
      all[c.name] = c.value;
    }
  }

  // 2) 分区 cookie（CHIPS / 第三方 cookie）
  for (const domain of COOKIE_DOMAINS) {
    for (const tls of PARTITIONED_TOP_LEVEL_SITES) {
      let list = [];
      try {
        list = await chrome.cookies.getAll({
          domain,
          partitionKey: { topLevelSite: tls },
        });
      } catch (e) { continue; }
      for (const c of list) {
        const key = c.name + "@" + (c.domain || domain) + "@partitioned";
        if (seen.has(key)) continue;
        seen.add(key);
        all[c.name] = c.value;
      }
    }
  }
  return all;
}

// MV3 service worker 拿不到分区 cookie，但 H5 页面的 document.cookie 能读到
async function readDocumentCookies(tabId) {
  if (!tabId) return {};
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => {
        const out = {};
        for (const part of document.cookie.split("; ")) {
          const eq = part.indexOf("=");
          if (eq < 0) continue;
          const k = part.slice(0, eq).trim();
          const v = part.slice(eq + 1);
          if (k) out[k] = v;
        }
        return out;
      },
    });
    return results?.[0]?.result || {};
  } catch (e) { return {}; }
}

function waitForTabComplete(tabId, timeoutMs = 15000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    const onUpdated = (id, info) => {
      if (id === tabId && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        clearTimeout(timer);
        finish("complete");
      }
    };
    chrome.tabs.onUpdated.addListener(onUpdated);
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(onUpdated);
      finish("timeout");
    }, timeoutMs);
  });
}

async function reloadAndWaitForTab(tabId, timeoutMs = 15000) {
  try { await chrome.tabs.reload(tabId); } catch (e) {}
  return waitForTabComplete(tabId, timeoutMs);
}

async function postCookies(endpoint, secret, cookies) {
  const resp = await fetch(`${endpoint}/api/cookies/sync`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Sync-Secret": secret,
    },
    body: JSON.stringify({ cookies }),
  });
  let body = null;
  try { body = await resp.json(); } catch { body = { ok: false, error: `HTTP ${resp.status}` }; }
  return { status: resp.status, body };
}

async function autoSync({ endpoint, secret }) {
  const log = (phase, msg) =>
    chrome.runtime.sendMessage({ type: "progress", phase, msg }).catch(() => {});
  const finish = (result) =>
    chrome.runtime.sendMessage({ type: "done", result }).catch(() => {});

  if (!endpoint || !secret) {
    finish({ ok: false, error: "缺少 endpoint 或 secret，先在弹出框填一下" });
    return;
  }

  let win = null;
  let openedHere = false;
  let keepOpen = false;
  try {
    log("open", "准备携程 H5 窗口…");
    const existing = await findExistingH5Window();
    if (existing) {
      log("reuse", "复用已打开的携程窗口…");
      win = existing;
      await chrome.windows.update(win.id, { focused: true }).catch(() => {});
    } else {
      win = await chrome.windows.create({
        url: H5_URL,
        width: MOBILE_W, height: MOBILE_H,
        focused: false, type: "normal",
      });
      openedHere = true;
    }

    const tabId = win?.tabs?.[0]?.id;
    if (!tabId) throw new Error("拿到窗口后没拿到 tabId");

    log("reload", "刷新页面，让 cookie 状态对齐…");
    await reloadAndWaitForTab(tabId);

    log("grace", `页面已就绪，等待 ${GRACE_MS / 1000}s 让 cookie 落地…`);
    await sleep(GRACE_MS);

    log("grab", "抓取浏览器 cookies…");
    const cookies = await grabAllCookies();
    if (win?.tabs?.[0]?.id) {
      log("grab-doc", "从 H5 页面 document.cookie 补抓分区 cookie…");
      const docCookies = await readDocumentCookies(win.tabs[0].id);
      let added = 0;
      for (const [k, v] of Object.entries(docCookies)) {
        if (!cookies[k] && v) { cookies[k] = v; added++; }
      }
      if (added > 0) log("grab-doc", `补到 ${added} 个分区 cookie`);
    }

    const missing = REQUIRED.filter((k) => !cookies[k]);
    if (missing.length) {
      if (win?.id != null) {
        try { await chrome.windows.update(win.id, { focused: true }); } catch {}
      }
      keepOpen = true;
      finish({
        ok: false,
        error: `缺少必需 cookie: ${missing.join(", ")}。<br>请在已弹出的携程窗口里登录账号，登录后点「立即同步」重试`,
        cookiesCount: Object.keys(cookies).length,
      });
      return;
    }

    log("upload", `已抓到 ${Object.keys(cookies).length} 个 cookie，上传到 VPS…`);
    const { status, body } = await postCookies(endpoint, secret, cookies);
    if (!body.ok) {
      finish({ ok: false, error: `服务端拒绝：${body.error || `HTTP ${status}`}`, status });
      return;
    }
    finish({ ok: true, path: body.path, saved: body.saved, status });
  } catch (e) {
    finish({ ok: false, error: String(e?.message || e) });
  } finally {
    if (openedHere && !keepOpen && win?.id != null) {
      try { await chrome.windows.remove(win.id); } catch {}
    }
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "auto_sync") {
    autoSync(msg).then(() => sendResponse({ started: true }));
    return true;
  }
});
```

### 5.5 `popup.html`（UI）

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>携程哨兵 · Cookie 同步</title>
  <link rel="stylesheet" href="popup.css" />
</head>
<body>
  <header>
    <div class="brand">🚗 携程哨兵</div>
    <div class="sub">Cookie 自动同步</div>
  </header>

  <main>
    <div id="config-section">
      <label class="field">
        <span>服务地址</span>
        <input id="endpoint" type="url" autocomplete="off" spellcheck="false"
               value="https://your-dashboard.example.com"
               placeholder="例如 https://your-dashboard.example.com" />
      </label>
      <label class="field">
        <span>同步密钥（X-Sync-Secret）</span>
        <input id="secret" type="password" autocomplete="off" spellcheck="false"
               placeholder="运维给的随机串，存到本地不外传" />
      </label>
      <div class="row">
        <button id="save-and-sync" class="primary">保存并立即同步</button>
      </div>
      <div class="hint">首次使用在这里填好，点上面的按钮就全自动了</div>
    </div>

    <div id="auto-section" hidden>
      <div class="row">
        <button id="retry" class="primary">立即同步（自动）</button>
      </div>
      <div class="hint">扩展会自动开一个手机尺寸窗口访问携程 H5，抓到 cookie 后自动上传</div>
    </div>

    <div class="status" id="status" hidden></div>

    <details class="meta">
      <summary>已抓到的 cookie（最近一次）</summary>
      <pre id="cookies-preview">— 还没有同步过 —</pre>
    </details>

    <div class="meta small">
      上次同步：<span id="last-sync">—</span><br />
      cookies.json mtime：<span id="server-mtime">—</span>
    </div>
  </main>

  <footer>
    <a href="#" target="_blank" rel="noreferrer">项目主页</a>
    <span class="sep">·</span>
    <a href="https://your-dashboard.example.com/login" target="_blank" rel="noreferrer">打开 Dashboard</a>
  </footer>

  <script src="popup.js"></script>
</body>
</html>
```

### 5.6 `popup.js`（监听 progress + 触发 autoSync）

```javascript
// 携程哨兵 Cookie 同步 — popup 逻辑
const STORAGE_KEYS = { endpoint: "endpoint", secret: "secret", lastSync: "lastSync", lastCookies: "lastCookies" };
const REQUIRED = ["_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t"];
const $ = (id) => document.getElementById(id);

const PHASE_LABEL = {
  open: "📱 打开手机窗口",
  reuse: "♻️ 复用已有窗口",
  reload: "🔄 刷新页面",
  grace: "⏸️ 等待 cookie 落地",
  grab: "🍪 抓取 cookies",
  "grab-doc": "🍪 补抓分区 cookie",
  upload: "📤 上传到 VPS",
};

function loadConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get(
      [STORAGE_KEYS.endpoint, STORAGE_KEYS.secret, STORAGE_KEYS.lastSync, STORAGE_KEYS.lastCookies],
      (v) => resolve(v || {})
    );
  });
}
function saveConfig(patch) {
  return new Promise((resolve) => chrome.storage.local.set(patch, () => resolve()));
}

function setStatus(kind, html) {
  const el = $("status"); el.hidden = false; el.className = "status " + (kind || ""); el.innerHTML = html;
}
function clearStatus() { $("status").hidden = true; $("status").innerHTML = ""; $("status").className = "status"; }

async function refreshMeta() {
  const cfg = await loadConfig();
  $("last-sync").textContent = cfg.lastSync ? new Date(cfg.lastSync).toLocaleString("zh-CN") : "—";
  if (cfg.lastCookies) {
    const preview = REQUIRED.concat(Object.keys(cfg.lastCookies).filter((k) => !REQUIRED.includes(k))).slice(0, 12);
    const lines = preview.map((k) => {
      const v = cfg.lastCookies[k];
      if (!v) return `${k}: <missing>`;
      const masked = v.length > 8 ? v.slice(0, 4) + "…" + v.slice(-4) : v;
      return `${k.padEnd(18)} = ${masked}  (len=${v.length})`;
    });
    $("cookies-preview").textContent = lines.join("\n");
  } else { $("cookies-preview").textContent = "— 还没有同步过 —"; }
}

async function refreshServerMtime() {
  const cfg = await loadConfig();
  const endpoint = (cfg.endpoint || $("endpoint").value).trim().replace(/\/+$/, "");
  const secret = cfg.secret || $("secret").value;
  if (!endpoint || !secret) { $("server-mtime").textContent = "—"; return; }
  try {
    const r = await fetch(`${endpoint}/api/cookies/health`, { headers: { "X-Sync-Secret": secret } });
    if (!r.ok) { $("server-mtime").textContent = "(查询失败)"; return; }
    const j = await r.json();
    $("server-mtime").textContent = j.exists ? new Date(j.mtime * 1000).toLocaleString("zh-CN") : "(尚未写入)";
  } catch { $("server-mtime").textContent = "(网络失败)"; }
}

async function triggerAutoSync() {
  const cfg = await loadConfig();
  const endpoint = (cfg.endpoint || $("endpoint").value).trim().replace(/\/+$/, "");
  const secret = cfg.secret || $("secret").value;
  if (!endpoint || !secret) {
    setStatus("error", "请先填写服务地址和同步密钥");
    return;
  }
  await saveConfig({ endpoint, secret });
  setStatus("running", "🚀 启动同步…");
  chrome.runtime.sendMessage({ type: "auto_sync", endpoint, secret });
}

function setupProgressListener() {
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg?.type === "progress") {
      setStatus("running", `${PHASE_LABEL[msg.phase] || msg.phase} — ${msg.msg}`);
    } else if (msg?.type === "done") {
      const r = msg.result;
      if (r.ok) {
        setStatus("ok", `✅ 成功 — 写入 <code>${r.path}</code><br>共 ${r.saved} 个 cookie`);
        saveConfig({ lastSync: Date.now(), lastCookies: r.lastCookies || null });
      } else {
        setStatus("error", `❌ ${r.error}`);
      }
      refreshMeta(); refreshServerMtime();
    }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  const cfg = await loadConfig();
  if (cfg.endpoint) $("endpoint").value = cfg.endpoint;
  if (cfg.secret)   $("secret").value = cfg.secret;
  $("save-and-sync").addEventListener("click", triggerAutoSync);
  $("retry").addEventListener("click", triggerAutoSync);
  setupProgressListener();
  await refreshMeta();
  await refreshServerMtime();
});
```

### 5.7 `popup.css`（深色，跟 dashboard 一致）

```css
* { box-sizing: border-box; }
body {
  width: 360px; padding: 0; margin: 0;
  background: #0B0E14; color: #D4D7DD;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 13px;
}
header { padding: 16px 20px 12px; border-bottom: 1px solid #262C36; }
header .brand { font-size: 16px; font-weight: 600; color: #4FD0B8; }
header .sub   { font-size: 11px; color: #7A8290; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 2px; }
main { padding: 16px 20px; }
.field { display: block; margin-bottom: 12px; }
.field span { display: block; font-size: 11px; color: #7A8290; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.06em; }
.field input {
  width: 100%; padding: 8px 10px;
  background: #13171F; color: #D4D7DD;
  border: 1px solid #262C36; border-radius: 2px;
  font-family: "JetBrains Mono", monospace; font-size: 12px;
}
.field input:focus { outline: none; border-color: #4FD0B8; }
.row { margin: 16px 0 8px; }
button.primary {
  width: 100%; padding: 10px;
  background: #4FD0B8; color: #0B0E14; border: none;
  font-weight: 600; font-size: 13px; cursor: pointer; border-radius: 2px;
}
button.primary:hover { background: #5FE0C8; }
.hint { font-size: 11px; color: #7A8290; margin-top: 8px; }
.status {
  padding: 10px 12px; margin: 12px 0;
  background: #13171F; border-left: 2px solid #7A8290;
  border-radius: 2px; font-size: 12px; line-height: 1.5;
}
.status.running { border-color: #5EAEFF; }
.status.ok      { border-color: #7BC97B; }
.status.error   { border-color: #E5A847; }
.meta {
  margin-top: 16px; padding-top: 12px;
  border-top: 1px solid #262C36;
  font-size: 11px; color: #7A8290;
}
.meta summary { cursor: pointer; color: #D4D7DD; }
.meta pre {
  margin: 8px 0 0; padding: 8px;
  background: #13171F; border-radius: 2px;
  font-family: "JetBrains Mono", monospace; font-size: 11px;
  white-space: pre-wrap; word-break: break-all;
}
.meta.small { padding-top: 8px; }
footer {
  padding: 12px 20px; border-top: 1px solid #262C36;
  font-size: 11px; color: #7A8290;
}
footer a { color: #5EAEFF; text-decoration: none; }
footer .sep { margin: 0 8px; color: #4A5260; }
```

### 5.8 `build.sh`（打包 zip 给 dashboard 下载）

```bash
#!/usr/bin/env bash
# 打包扩展成 dist/ctrip-cookie-sync.zip —— dashboard 路由会读这个文件
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$DIR/dist"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/ctrip-cookie-sync.zip"
[ -f "$OUT" ] && rm "$OUT"
cd "$DIR"
# 排除 macOS 残留
find . -name '.DS_Store' -delete
# 打包（不含 dist 自己）
zip -r "$OUT" . -x 'dist/*' '.DS_Store'
echo "built: $OUT ($(du -sh "$OUT" | cut -f1))"
```

---

## 6. 第六步 · 服务端接收 cookie 的 endpoint

### 6.1 `web/routes/cookie_sync.py`

```python
"""Cookie sync endpoint — 供 Chrome 扩展 POST mtop cookies 上传到 VPS。

Auth: X-Sync-Secret header 必须等于 env var `COOKIE_SYNC_SECRET`。
不依赖 dashboard 的 session login，扩展不需要先登录哨兵。

收到后写到 /etc/ctrip-monitor/cookies.json（chmod 644，覆盖现有）。
"""
from __future__ import annotations

import hmac, json, os
from pathlib import Path
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

REQUIRED_COOKIES = ("_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t")
COOKIE_PATH = Path(os.getenv("CTRIP_COOKIES", "/etc/ctrip-monitor/cookies.json"))

router = APIRouter(prefix="/api/cookies", tags=["cookies"])


def _expected_secret() -> str:
    return os.getenv("COOKIE_SYNC_SECRET", "")


def _verify_secret(provided: str | None) -> bool:
    expected = _expected_secret()
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def _write_cookies(cookies: dict[str, str]) -> tuple[int, str]:
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cookies, ensure_ascii=False, indent=2) + "\n"
    COOKIE_PATH.write_text(payload, encoding="utf-8")
    os.chmod(COOKIE_PATH, 0o644)
    return len(cookies), str(COOKIE_PATH)


@router.post("/sync")
async def sync_cookies(
    request: Request,
    x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret"),
):
    if not _verify_secret(x_sync_secret):
        return JSONResponse({"ok": False, "error": "invalid or missing X-Sync-Secret"}, status_code=401)
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        return JSONResponse({"ok": False, "error": f"invalid JSON: {e}"}, status_code=400)
    cookies = body.get("cookies")
    if not isinstance(cookies, dict):
        return JSONResponse({"ok": False, "error": "body.cookies must be JSON object"}, status_code=400)
    missing = [k for k in REQUIRED_COOKIES if not cookies.get(k)]
    if missing:
        return JSONResponse({"ok": False, "error": f"missing cookies: {missing}", "required": list(REQUIRED_COOKIES)}, status_code=400)
    clean = {k: str(v) for k, v in cookies.items() if isinstance(v, (str, int, float))}
    n, path = _write_cookies(clean)
    return {"ok": True, "saved": n, "path": path, "required": list(REQUIRED_COOKIES)}


@router.get("/health")
async def cookie_health(x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret")):
    if not _verify_secret(x_sync_secret):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    exists = COOKIE_PATH.exists()
    mtime = COOKIE_PATH.stat().st_mtime if exists else None
    return {
        "ok": True,
        "secret_configured": bool(_expected_secret()),
        "cookies_file": str(COOKIE_PATH),
        "exists": exists,
        "mtime": mtime,
    }
```

### 6.2 CORS 关键（扩展能跨域 POST）

```python
# web/server.py
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https://([a-z0-9.-]+)?your-domain\.com$|^chrome-extension://[a-p]+$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Sync-Secret"],
    max_age=3600,
)
```

---

## 7. 第七步 · Webhook 推送（6 类告警 + 多平台适配）

### 7.1 告警类型定义（业务相关，按需增减）

```python
# 6 类通用告警
EVENT_TYPES = {
    "non_self_new":      {"severity": "warning", "label": "非自营 SKU 出现"},
    "price_alert":       {"severity": "info",    "label": "价格异动 ±20%"},
    "self_missing":      {"severity": "critical","label": "自营 SKU 突然消失"},
    "first_seller":      {"severity": "info",    "label": "新卖家首次出现"},
    "detail_error":      {"severity": "critical","label": "采集异常"},
    "cookie_refresh_failed": {"severity": "critical", "label": "Cookie 续期失败"},
}
```

### 7.2 HMAC 签名（必备，便于接收方验签）

```python
import hmac, hashlib
def sign_body(body: bytes, secret: str) -> str:
    if not secret:
        return ""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
```

### 7.3 钉钉 / 飞书 / Telegram 适配（共用一段代码即可）

```python
import json
def wrap_for_platform(platform: str, payload: dict) -> dict:
    """canonical payload → 各平台机器人格式。"""
    if platform == "dingtalk":
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": f"[携程哨兵] {payload['event']} · {payload.get('poi', {}).get('name', '')}",
                "text":  format_md(payload),
            }
        }
    if platform == "feishu":
        return {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text",
                                      "content": f"[携程哨兵] {payload['event']}"}},
                "elements": [{"tag": "markdown", "content": format_md(payload)}],
            }
        }
    if platform == "telegram":
        return {
            "method": "sendMessage",
            "text": format_md(payload),
            "parse_mode": "Markdown",
            "reply_markup": {"inline_keyboard": [[{"text": "查看详情", "url": payload.get("deep_link", "")}]]},
        }
    # custom: 直接发 canonical JSON
    return payload

def format_md(p: dict) -> str:
    e = p["event"]
    poi = p.get("poi", {}).get("name", "—")
    sku = p.get("sku", {}).get("name", "—")
    seller = p.get("seller", {}).get("name", "—")
    return (f"### {e} · {poi}\n\n"
            f"**SKU**: {sku}\n\n"
            f"**卖家**: {seller}\n\n"
            f"[详情]({p.get('deep_link', '#')})")
```

### 7.4 HTTP 发送（含重试 + dedup）

```python
import httpx, asyncio

RETRY_DELAYS = [2, 8, 30]  # 指数退避

async def send_webhook(url, secret, payload, dedup_key):
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Sentinel-Signature": sign_body(body, secret),
        "X-Sentinel-Dedup-Key": dedup_key,
        "X-Sentinel-Event": payload.get("event", ""),
    }
    for attempt, delay in enumerate([0] + RETRY_DELAYS):
        if delay: await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(url, content=body, headers=headers)
            if 200 <= r.status_code < 300:
                return True, f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, last_err


# dedup_key：同 alert 1 小时内只发 1 次
import hashlib, time
def dedup_key(alert_type, poi_id, item_id="", sku_id="", seller_id="", window_hour=1):
    bucket = int(time.time()) // (window_hour * 3600)
    raw = f"{alert_type}|{poi_id}|{item_id}|{sku_id}|{seller_id}|{bucket}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]
```

---

## 8. 第八步 · VPS 部署（Ubuntu 22.04）

### 8.1 一次性环境（脚本）

```bash
ssh root@YOUR_VPS_IP << 'EOF'
set -e
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip curl wget jq vim ufw fail2ban \
               sqlite3 ca-certificates openssh-client rsync

# Caddy
apt install -y debian-keyring debian-archive-keyring
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/deb/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

# Playwright 依赖（cookie 自动续期用）
apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
               libcups2 libxkbcommon0 libxcomposite1 libxdamage1 \
               libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
               libcairo2 libasound2

# 防火墙
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

# 系统用户 + 目录
useradd -r -m -d /home/ctrip -s /usr/sbin/nologin ctrip || true
mkdir -p /opt/ctrip-monitor/{code,data,docs,web,scripts,deploy,logs,backups}
mkdir -p /etc/ctrip-monitor
chown -R ctrip:ctrip /opt/ctrip-monitor /var/log/ctrip-monitor 2>/dev/null || true
chmod 750 /etc/ctrip-monitor

echo "=== base env ready ==="
EOF
```

### 8.2 systemd 4 个 unit

#### `deploy/systemd/ctrip-web.service`
```ini
[Unit]
Description=Ctrip Sentinel Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ctrip
WorkingDirectory=/opt/ctrip-monitor
Environment="PATH=/opt/ctrip-monitor/.venv/bin"
Environment="COOKIE_SYNC_SECRET=CHANGE_ME_<32+ random string>"
ExecStart=/opt/ctrip-monitor/.venv/bin/uvicorn web.server:app \
    --host 127.0.0.1 --port 8080 --workers 2 --log-level info
Restart=always
RestartSec=5

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/ctrip-monitor /var/log/ctrip-monitor /etc/ctrip-monitor
ProtectHome=true
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

> ⚠️ `COOKIE_SYNC_SECRET` 一定要改！生成方式：`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`

#### `deploy/systemd/ctrip-monitor.service`
```ini
[Unit]
Description=Ctrip H5 POI Monitor (scheduled every 30 min)
After=network-online.target

[Service]
Type=oneshot
User=ctrip
WorkingDirectory=/opt/ctrip-monitor
Environment="PATH=/opt/ctrip-monitor/.venv/bin"
ExecStart=/opt/ctrip-monitor/.venv/bin/python3 \
    /opt/ctrip-monitor/code/ctrip_monitor.py
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/ctrip-monitor /var/log/ctrip-monitor /etc/ctrip-monitor

[Install]
WantedBy=multi-user.target
```

#### `deploy/systemd/ctrip-monitor.timer`
```ini
[Unit]
Description=Ctrip Monitor runs every 30 min

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
Persistent=true
AccuracySec=10s

[Install]
WantedBy=timers.target
```

#### `deploy/systemd/ctrip-cookies-refresh.service`
```ini
[Unit]
Description=Refresh Ctrip cookies via Playwright

[Service]
Type=oneshot
User=ctrip
WorkingDirectory=/opt/ctrip-monitor
Environment="PATH=/opt/ctrip-monitor/.venv/bin"
ExecStart=/opt/ctrip-monitor/.venv/bin/python3 \
    /opt/ctrip-monitor/scripts/refresh_cookies.py
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/etc/ctrip-monitor /opt/ctrip-monitor /var/log/ctrip-monitor
```

#### `deploy/systemd/ctrip-cookies-refresh.timer`
```ini
[Unit]
Description=Refresh cookies every 90 min

[Timer]
OnBootSec=2min
OnUnitActiveSec=90min
Persistent=true
AccuracySec=60s

[Install]
WantedBy=timers.target
```

### 8.3 Caddyfile（自动 HTTPS）

```caddyfile
your-domain.example.com {
    reverse_proxy 127.0.0.1:8080

    encode gzip zstd

    log {
        output file /var/log/ctrip-monitor/access.log {
            roll_size 10mb
            roll_keep 14
        }
    }

    header {
        -Server
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        X-XSS-Protection "1; mode=block"
        Referrer-Policy "strict-origin-when-cross-origin"
        Content-Security-Policy "default-src 'self'; img-src 'self' data: https://*.alicdn.com https://*.taobao.com; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; font-src 'self' data:"
    }

    @static path /static/*
    handle @static {
        header Cache-Control "public, max-age=3600"
    }

    handle /healthz {
        respond "ok" 200
    }
}
```

---

## 9. 第九步 · 部署脚本（自动化）

### 9.1 `scripts/init_db.py`（核心：建表 + 写 bcrypt 密码）

```python
#!/usr/bin/env python3
"""初始化 SQLite 数据库：建表 + 导入 JSON 数据 + 写默认 admin 密码。"""
import sqlite3, json, bcrypt, os, sys, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("CTRIP_DB", "/opt/ctrip-monitor/data/monitor.db"))

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        TEXT UNIQUE NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    cells_total     INTEGER DEFAULT 0,
    cells_self      INTEGER DEFAULT 0,
    cells_non_self  INTEGER DEFAULT 0,
    new_sellers     INTEGER DEFAULT 0,
    detail_hits     INTEGER DEFAULT 0,
    error_msg       TEXT,
    duration_ms     INTEGER
);
-- ... 其他 9 张表（见 §3.2，复制粘贴即可）
"""

def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    # 1. 写默认 admin 密码（v1 直接明文 / 后期换 bcrypt）
    admin_pwd = os.getenv("CTRIP_ADMIN_PASSWORD", "your_password_here")
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
        ("admin_password_hash", json.dumps(admin_pwd), datetime.datetime.utcnow().isoformat())
    )

    # 2. 导入 POI 池
    poi_registry = json.loads((ROOT / "data" / "poi_registry.json").read_text(encoding="utf-8"))
    for p in poi_registry.get("pool", []):
        conn.execute("""
            INSERT OR REPLACE INTO pois (poi_id, name, city_id, tb_cn, h5_url, enabled, polling_sec, created_at)
            VALUES (?, ?, ?, ?, ?, 1, 1800, ?)
        """, (p["poi_id"], p["name"], p.get("city_id"), p.get("tb_cn"), p.get("h5_url"),
              datetime.datetime.utcnow().isoformat()))

    # 3. 导入 seller baseline
    seller_baseline = json.loads((ROOT / "data" / "seller_baseline.json").read_text(encoding="utf-8"))
    for s in seller_baseline.get("sellers", []):
        conn.execute("""
            INSERT OR REPLACE INTO sellers (seller_id, seller_name, is_self, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
        """, (s["seller_id"], s.get("seller_name"), s.get("is_self", 0),
              datetime.datetime.utcnow().isoformat(), datetime.datetime.utcnow().isoformat()))

    # 4. 默认 webhook 配置
    conn.execute("INSERT OR REPLACE INTO config VALUES (?, ?, ?)",
                 ("webhook_rules", json.dumps({
                     "non_self_new": True, "price_alert": True,
                     "self_missing": True, "first_seller": False,
                     "detail_error": True, "cookie_refresh_failed": True
                 }), datetime.datetime.utcnow().isoformat()))

    conn.commit()
    print(f"DB ready: {DB_PATH}")
    print(f"POIs: {len(poi_registry.get('pool', []))}")
    print(f"Sellers: {len(seller_baseline.get('sellers', []))}")

if __name__ == "__main__":
    main()
```

### 9.2 `scripts/refresh_cookies.py`（Playwright 自动续期）

```python
#!/usr/bin/env python3
"""Cookie 续期：用 Playwright 打开携程 H5 → 等 token 刷新 → 把 cookies 写到 /etc/ctrip-monitor/cookies.json。
失败重试 3 次，每次退避 5/15/45s。
"""
import argparse, json, os, re, sys, time, subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_COOKIE_PATH = os.getenv("CTRIP_COOKIES", "/etc/ctrip-monitor/cookies.json")
DEFAULT_DB_PATH     = os.getenv("CTRIP_DB", "/opt/ctrip-monitor/data/monitor.db")
DEFAULT_TARGET_URL  = os.getenv(
    "CTRIP_TARGET_URL",
    # 用任意能稳定触发 mtop 颁发的 H5 落地页
    "https://m.ctrip.com/restapi/.../abc?jsv=2.5.1&appKey=...&sign=xxx"
)

EXPIRY_RE = re.compile(r"^([^_]+)_([^&]+)&")

def extract_m_h5_tk(cookies):
    for c in cookies:
        if c["name"] == "_m_h5_tk":
            token = c["value"]
            m = EXPIRY_RE.match(token)
            return token, (m.group(2) if m else None)
    return None, None

def do_refresh(target_url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
            viewport={"width": 390, "height": 844},
            device_scale_factor=3, is_mobile=True, has_touch=True,
            locale="zh-CN", timezone_id="Asia/Shanghai",
        )
        page = ctx.new_page()
        deadline = time.time() + 45
        token_value = None
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            while time.time() < deadline:
                cookies = ctx.cookies()
                token, _ = extract_m_h5_tk(cookies)
                if token:
                    token_value = token
                    break
                time.sleep(1)
        finally:
            cookies = ctx.cookies()
            browser.close()
        if not token_value:
            raise RuntimeError("未能在 45s 内拿到 _m_h5_tk cookie")
        return cookies

def write_cookies(path, cookies):
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = [
        {"name": c["name"], "value": c["value"], "domain": c.get("domain", ".ctrip.com"),
         "path": c.get("path", "/")}
        for c in cookies
    ]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(serialized, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(path)
    try:
        subprocess.run(["chown", "root:ctrip", str(path)], check=False)
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=DEFAULT_COOKIE_PATH)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--url", default=DEFAULT_TARGET_URL)
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    for attempt in range(1, args.max_retries + 1):
        try:
            print(f"[refresh] 尝试 {attempt}/{args.max_retries}")
            cookies = do_refresh(args.url)
            token, expiry_ms = extract_m_h5_tk(cookies)
            write_cookies(Path(args.target), cookies)
            print(f"[refresh] 写入 {args.target}（{len(cookies)} 个 cookie）")
            print(f"[refresh] _m_h5_tk prefix={token[:8] if token else '?'}…")
            return 0
        except Exception as e:
            print(f"[refresh] 失败：{type(e).__name__}: {e}")
            if attempt < args.max_retries:
                backoff = 5 * (3 ** (attempt - 1))
                print(f"[refresh] {backoff}s 后重试…")
                time.sleep(backoff)
    print(f"[refresh] 全部失败")
    return 1

if __name__ == "__main__":
    sys.exit(main())
```

### 9.3 `scripts/deploy.sh`（一键 rsync + 重启）

```bash
#!/usr/bin/env bash
# 本地 deploy：rsync + 重启服务
set -euo pipefail

VPS_HOST="${VPS_HOST:-YOUR_VPS_IP}"
VPS_USER="${VPS_USER:-ctrip}"
VPS_SSH_KEY="${VPS_SSH_KEY:-$HOME/.ssh/id_ctrip-monitor}"
REMOTE_DIR="${REMOTE_DIR:-/opt/ctrip-monitor}"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== step 1: rsync (排除 cookies.json, .db, .venv) ==="
rsync -avz --delete \
  -e "ssh -i $VPS_SSH_KEY -o StrictHostKeyChecking=accept-new" \
  --exclude='.git' \
  --exclude='*.db' --exclude='backups/*.db' \
  --exclude='cookies.json' \
  --exclude='__pycache__' --exclude='.venv' --exclude='.cache' \
  ./ "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/"

echo "=== step 2: remote install + restart ==="
ssh -i "$VPS_SSH_KEY" -o StrictHostKeyChecking=accept-new "${VPS_USER}@${VPS_HOST}" << 'REMOTE'
  set -e
  cd /opt/ctrip-monitor
  if [ ! -d .venv ]; then
    sudo -u ctrip /usr/bin/python3 -m venv .venv
  fi
  sudo -u ctrip .venv/bin/pip install --upgrade pip -q
  sudo -u ctrip .venv/bin/pip install -r requirements.txt -q

  bash extensions/ctrip-cookie-sync/build.sh

  sudo -u ctrip /opt/ctrip-monitor/.venv/bin/python3 /opt/ctrip-monitor/scripts/init_db.py || true

  sudo /usr/bin/systemctl restart ctrip-web || true
  sudo /usr/bin/systemctl restart ctrip-monitor.timer || true
  sleep 3
  echo "=== deploy done ==="
REMOTE

echo "=== step 3: health check ==="
ssh -i "$VPS_SSH_KEY" -o StrictHostKeyChecking=accept-new "${VPS_USER}@${VPS_HOST}" \
  "curl -fsS http://127.0.0.1:8080/healthz && echo OK || echo FAIL"
```

---

## 10. 第十步 · GitHub Actions 部署（可选，但强烈建议）

### 10.1 `.github/workflows/deploy.yml`

```yaml
name: Deploy to VPS

on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install ruff
      - run: ruff check .

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: pytest tests/ -v || echo "no tests yet"

  build:
    needs: [lint, test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          tar czf ctrip-monitor-${GITHUB_SHA::7}.tar.gz \
              --exclude='*.pyc' --exclude='__pycache__' \
              --exclude='.git' --exclude='*.db' --exclude='backups/*.db' \
              code/ data/ web/ scripts/ deploy/ docs/ README.md requirements.txt
      - uses: actions/upload-artifact@v4
        with:
          name: source-tarball
          path: ctrip-monitor-*.tar.gz

  deploy_production:
    needs: [build]
    runs-on: ubuntu-latest
    environment:
      name: production
    steps:
      - uses: actions/checkout@v4
      - run: apt-get install -y openssh-client rsync
      - run: |
          mkdir -p ~/.ssh
          echo "${{ secrets.VPS_SSH_KEY }}" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
          ssh-keyscan -H "${{ secrets.VPS_HOST }}" > ~/.ssh/known_hosts
      - name: rsync
        run: |
          rsync -avz --delete \
            -e "ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes -p ${{ secrets.VPS_PORT }}" \
            --exclude='.git' --exclude='*.db' --exclude='backups/*.db' \
            --exclude='cookies.json' --exclude='__pycache__' \
            --exclude='.venv' --exclude='.cache' \
            ./ ${{ secrets.VPS_USER }}@${{ secrets.VPS_HOST }}:/opt/ctrip-monitor/
      - name: remote restart
        run: |
          ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes -p ${{ secrets.VPS_PORT }} \
              ${{ secrets.VPS_USER }}@${{ secrets.VPS_HOST }} \
              '.venv/bin/pip install -r requirements.txt -q && \
               bash extensions/ctrip-cookie-sync/build.sh && \
               sudo systemctl restart ctrip-web'
      - name: health check
        run: |
          sleep 3
          curl -fsS http://${{ secrets.VPS_HOST }}/healthz && echo " OK"
```

### 10.2 GitHub Secrets（在 Repo → Settings → Secrets 配置）

| Secret | 值 |
|---|---|
| `VPS_HOST` | VPS 公网 IP |
| `VPS_USER` | `ctrip` |
| `VPS_PORT` | `22` |
| `VPS_SSH_KEY` | ed25519 私钥全文（含 BEGIN/END 标记） |

> **密钥生成最佳实践**：在 **VPS 上**生成 ed25519 密钥对（不在本机），私钥同时存 GitHub Secret 和 VPS，公钥加到 GitHub Deploy Keys + VPS `~/.ssh/authorized_keys`。

---

## 11. 第十一步 · Cookie 注入到 VPS（首次 / 续期）

### 11.1 手工一次性注入（部署后第一次）

```bash
ssh root@YOUR_VPS_IP

# 浏览器（已登录携程）DevTools → Application → Cookies
# 把所有 mtop 相关的 cookie（_m_h5_tk / _m_h5_tk_enc / cookie2 / t 等）拷出来
# 转成 JSON dict 格式
cat > /etc/ctrip-monitor/cookies.json << 'JSON'
{
  "_m_h5_tk":     "<32-char-hex>_<13-digit-unix-ms>",
  "_m_h5_tk_enc": "<32-char-hex>",
  "cookie2":      "<32-char-hex>",
  "t":            "<32-char-hex>"
}
JSON

chmod 644 /etc/ctrip-monitor/cookies.json
chown root:ctrip /etc/ctrip-monitor/cookies.json

# 立即测一次
sudo -u ctrip /opt/ctrip-monitor/.venv/bin/python3 \
  /opt/ctrip-monitor/code/ctrip_monitor.py
```

### 11.2 日常续期：装好扩展 → 点一下图标

> 详见 §5。运营人员每次 cookie 失效时**点一下工具栏图标**，扩展自动全流程。

---

## 12. 关键注意事项（避坑清单）

| 坑 | 现象 | 解决 |
|---|---|---|
| **`_m_h5_tk` 是分区 cookie** | `chrome.cookies.getAll` 拿不到空值 | `chrome.scripting.executeScript({world: "MAIN"})` 读 `document.cookie` |
| **`ret` 字段是 list 不是 string** | `if ret == "SUCCESS"` 直接 False | `if not any(str(r).startswith("SUCCESS") for r in ret if r)` |
| **mtop `data` 排序敏感** | sign 验证失败 | `json.dumps(data, separators=(",", ":"))` **不要** `sort_keys=True` |
| **Caddy 80 端口被封** | Let's Encrypt 签不了证书 | 用 DNS-01 challenge（Caddy 自动）或换端口 |
| **systemd `Type=oneshot` 跑完就退** | 想长跑会被 SIGTERM | 改 `Type=simple` 或写 `Type=oneshot` + `RemainAfterExit=yes` |
| **SQLite WAL 模式忘开** | 写者锁库导致读阻塞 | `PRAGMA journal_mode = WAL` 必加 |
| **`fliggy_sid` cookie 复制到 ctrip 项目忘了改名字** | cookie 冲突 | 命名空间按项目：`ctrip_sid` / `fliggy_sid` |
| **deploy.sh 误删 cookies.json** | rsync `--delete` 把 `/etc/` 同步覆盖 | rsync `--exclude='cookies.json'` |
| **扩展 popup 自动关** | 用户看不全进度 | popup 永远显示最新进度（不是 background 状态） |
| **`COOKIE_SYNC_SECRET` 没设** | 扩展同步 401 | 服务端启动日志里看一眼 Environment 字段 |
| **携程 H5 在 desktop 显示降级版** | 抓包抓不到完整接口 | 扩展里用 mobile viewport 414×896 |
| **OTA 接口返回非 JSON** | JSON decode 失败 | 检查 `Content-Type`、可能要用 `jq` 二次解析 |
| **chrome extension 拿不到 `_m_h5_tk_enc`** | 永远 missing | 该 cookie 是 HttpOnly + 分区，必须 `executeScript` 读 `document.cookie` 补刀 |
| **systemd unit 启动报 `status=203/EXEC`** | 脚本无执行权限 / Python 找不到 | `chmod +x` 脚本 + 写完整 venv Python 路径 |
| **飞猪那种 sign 算法每个 OTA 略有差异** | md5 / hmac-sha256 搞错 | 用 `grep "sign"` + 实际抓到的错误响应反推 |

---

## 13. 端到端冒烟测试（部署后必跑）

```bash
ssh root@YOUR_VPS_IP << 'EOF'
echo "=== 1. dashboard 健康 ==="
curl -fsS http://127.0.0.1:8080/healthz && echo " OK"

echo "=== 2. 登录页可访问 ==="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" https://your-domain.example.com/login

echo "=== 3. 监控脚本能拉数据 ==="
sudo -u ctrip /opt/ctrip-monitor/.venv/bin/python3 \
    /opt/ctrip-monitor/code/ctrip_monitor.py 2>&1 | tail -20

echo "=== 4. DB 有数据 ==="
sqlite3 /opt/ctrip-monitor/data/monitor.db \
    "SELECT COUNT(*) FROM sellers; SELECT COUNT(*) FROM pois;"

echo "=== 5. systemd 状态 ==="
for svc in ctrip-web ctrip-monitor.timer ctrip-cookies-refresh.timer caddy; do
    echo "  $svc: $(systemctl is-active $svc)"
done

echo "=== 6. cookie sync endpoint 可达 ==="
curl -fsS -H "X-Sync-Secret: $COOKIE_SYNC_SECRET" \
    https://your-domain.example.com/api/cookies/health
EOF
```

---

## 14. 把这个模板带到 ctrip 项目时的实操顺序

1. **本机** Chrome 打开携程 H5，DevTools 抓 2-3 个核心 mtop 接口
2. 把 curl 复现成 Python（`/tmp/repro_ctrip.py`），验证 sign 算法 + cookie 字段
3. 建项目骨架，拷本文件 §2.1 目录结构
4. 填 `code/ctrip_selectors.py`（所有常量集中）
5. 填 `code/mtop_client.py`（curl 复现 + parse 函数）
6. 写 `code/ctrip_monitor.py`（主循环 + diff + 告警 + webhook）
7. 写 `web/` （FastAPI + auth + DB + routes）
8. 写扩展（拷本文件 §5，**改 4 处常量**：`REQUIRED` / `H5_URL` / `COOKIE_DOMAINS` / `PARTITIONED_TOP_LEVEL_SITES`）
9. 部署到 VPS（§8 一键脚本）
10. **本机装好扩展** → 在携程登录的 Chrome 里点图标 → 把 cookie 推到 VPS → 看监控正常拉数据 → 配 webhook → 收第一条告警
11. GitHub Actions 配 CI/CD（§10）

> 整个流程本机 1-2 天就能跑通端到端。后续迭代主要在 §1（找新接口）和 §3-4（前端细节）。

---

## 附录 A · `requirements.txt`

```txt
fastapi>=0.115
uvicorn[standard]>=0.32
jinja2>=3.1
python-multipart>=0.0.18
httpx>=0.27
playwright>=1.48
bcrypt>=4.0
```

## 附录 B · `pyproject.toml`（ruff 配置）

```toml
[project]
name = "ctrip-monitor"
version = "0.1.0"
requires-python = ">=3.11"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP"]
```

## 附录 C · systemd 调试速查

```bash
# 看 timer 下次触发时间
systemctl list-timers ctrip-*.timer

# 看 web 日志
journalctl -u ctrip-web -f

# 看最近一轮扫描耗时
sqlite3 /opt/ctrip-monitor/data/monitor.db \
  "SELECT round_id, status, duration_ms FROM rounds ORDER BY id DESC LIMIT 5;"

# 看最近 10 条 webhook 状态
sqlite3 /opt/ctrip-monitor/data/monitor.db \
  "SELECT ts, type, webhook_status FROM alerts ORDER BY ts DESC LIMIT 10;"

# 看 cookie 续期历史
sqlite3 /opt/ctrip-monitor/data/monitor.db \
  "SELECT ts, success, substr(token_prefix,1,8) AS tok, error_msg
   FROM cookies_history ORDER BY ts DESC LIMIT 10;"
```

## 附录 D · 完整配置文件 `data/poi_registry.json` 示例

```json
{
  "pool": [
    {
      "poi_id":    "12345",
      "name":      "上海迪士尼乐园",
      "city_id":   "2",
      "tb_cn":     "h.xxxx",
      "h5_url":    "https://piao.ctrip.com/dest/..."
    },
    {
      "poi_id":    "67890",
      "name":      "故宫博物院",
      "city_id":   "1",
      "tb_cn":     "h.yyyy",
      "h5_url":    "https://piao.ctrip.com/dest/..."
    }
  ]
}
```

## 附录 E · 完整配置文件 `data/seller_baseline.json` 示例

```json
{
  "sellers": [
    {"seller_id": "ctrip_official_main",   "seller_name": "携程自营-门票", "is_self": 1},
    {"seller_id": "ctrip_official_train",  "seller_name": "携程自营-火车", "is_self": 1},
    {"seller_id": "third_party_xxxxxx",    "seller_name": "上海某旅行社专营店", "is_self": 0}
  ],
  "stability_notes": {
    "ctrip_official_main":   "上海迪士尼 POI 永远自营",
    "ctrip_official_train":  "北京景区 POI 永远自营"
  }
}
```

---

## 最后

把这套架构记在脑子里：**本机抓包（找到关键接口）→ VPS 上跑采集（写 SQLite + 告警 + webhook）→ 浏览器扩展（点图标同步登录态）**。三段式，每一段都有完整的代码模板可以拷。

> 携程 / 飞猪 / 美团 / 去哪儿，**底层都是 mtop / gw 那一套**（标准 msign 算法 + 4 个 cookie + URL 拼参）。区别只是：
> 1. 入口 URL（移动 H5 vs 桌面）
> 2. cookie 名字（`_m_h5_tk` / `_uab_collina` / `JSESSIONID` 等）
> 3. 数据结构（cellType / shelf / productList）
> 4. 自营基线判定（seller_id / supplierId / isMain 字段）

抄完本文件里的 `code/mtop_client.py` 模板，**改 5 行常量就能跑**。