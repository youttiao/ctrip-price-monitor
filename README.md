# 携程哨兵 · Ctrip Price Monitor

监控携程 H5 POI 上各 vendor 的 SKU 价格与排名变化。

## 架构

```
┌──────────────────────────────┐    POST /api/ingest/round     ┌──────────────────────┐
│ Chrome 扩展（MV3）            │ ─────────────────────────────►│ FastAPI dashboard    │
│ - 抓 soa2 getProductShelf     │                                │ web/server.py        │
│ - 抓 search + resourceAddInfo │    POST /api/cookies/sync      │                      │
│ - 每 5min 同步 cookies        │ ─────────────────────────────►│                      │
└──────────────────────────────┘                                └──────────────────────┘
                                                                       │
┌──────────────────────────────┐    POST /api/ingest/round     ┌───────┴────────┐
│ server_scraper.py            │ ─────────────────────────────►│ ingest endpoint │
│ - search + addInfo            │                                │                │
│ - 30min 一次（systemd timer）│                                └────────────────┘
└──────────────────────────────┘                                                │
                                                                                ▼
                                                                ┌──────────────────────┐
                                                                │ raw_rounds/*.json    │
                                                                │ + rounds table       │
                                                                └──────────────────────┘
                                                                                │
                                                                                ▼ (1 min timer)
                                                                ┌──────────────────────┐
                                                                │ round_parser.py      │
                                                                │ - parse JSON         │
                                                                │ - sku_snapshot       │
                                                                │ - rank_history       │
                                                                │ - alerts + webhook   │
                                                                └──────────────────────┘
```

## 文件地图

| 路径 | 作用 |
| --- | --- |
| `ctrip_core/selectors.py` | soa2 端点常量 + payload/header 构造器 |
| `ctrip_core/parse.py` | 把 raw JSON round 解析成 sku + rank |
| `ctrip_core/alerts.py` | rank 变化告警 |
| `web/server.py` | FastAPI 入口 |
| `web/ingest.py` | POST round / cookies |
| `web/auth.py` | bcrypt + session |
| `web/routes/pages.py` | dashboard 页面 |
| `web/routes/admin.py` | vendor / watchlist / config 管理 |
| `web/templates/` | Jinja2 HTML |
| `web/static/css/main.css` | 设计令牌 |
| `scripts/init_db.py` | 建库 + 默认 admin/POI |
| `scripts/server_scraper.py` | 服务端 scraper |
| `scripts/round_parser.py` | round → snapshot + alerts |
| `scripts/retention.py` | 30 天清理 + daily_summary |
| `scripts/deploy.sh` | VPS 一键部署 |
| `extension/` | Chrome MV3 采集器 |
| `deploy/Caddyfile` | 反代 |
| `deploy/systemd/*.timer` | 周期任务 |

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 建库
python scripts/init_db.py --admin-password test1234 --ingest-secret test --cookie-secret test2

# 跑 web
INGEST_SECRET=test COOKIE_SYNC_SECRET=test2 CTRIP_DB_PATH=data/monitor.db \
    uvicorn web.server:app --reload --port 8000

# 跑 parser（手动）
python scripts/round_parser.py --db data/monitor.db
```

打开 <http://127.0.0.1:8000>，账号 `admin` / `test1234`。

## 部署

```bash
REMOTE=ctrip@racknerd19 bash scripts/deploy.sh
```

依赖：

- VPS：Ubuntu 22.04+，Python 3.11+，Caddy 2
- DNS：`xiecheng.19880913.xyz` A 记录 → VPS IP
- 用户的 Chrome 安装 `extension/`（加载已解压扩展）

## 业务关键点

1. **vendorId = 999999** 是"上海携程国际旅行社"（自营）。其它自营检测字段不稳定。
2. **我的 vendorId** 不是 hardcode，写进 `my_vendors` 表，dashboard / 告警 / 解析都用它过滤。
3. **货架名黑名单**（`selectors.SHELF_NAME_BLACKLIST`）：周边推荐 / 一日游 / 酒店 / 用车 / 餐饮 / 跟团 / 司导 / 向导 / 演出 / 剧场 / 文创店 — 这些不算直接竞争，不参与排名。
4. **告警范围**：只对 `watchlist` 里勾选过的 (POI × shelfType) 发推送；其他 SKU 仍记录到 sku_snapshot 和 rank_history，不打扰。
5. **告警触发**：
   - `rank_drop`：上一轮第 N，这一轮 > N
   - `still_non_first`：watchlist 里的 SKU 连续不在第 1（每轮都触发）
   - `appeared`：watchlist 里的 SKU 上一轮没有，这一轮出现了
   - `disappeared`：watchlist 里的 SKU 上一轮有，这一轮没了（critical）

## 测试

```bash
python -m pytest tests/ -v
```

19 个测试覆盖 parse（5 个 POI）+ alerts（9 类场景）。