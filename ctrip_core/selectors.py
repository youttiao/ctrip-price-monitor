"""携程 soa2 API 集中常量。

改这里 = 改全局行为。
Source: _captures/API-MAP.md + 实测抓包。
"""
from __future__ import annotations

BASE_URL = "https://m.ctrip.com"

# soa2 端点
SEARCH_URL    = f"{BASE_URL}/restapi/h5api/globalsearch/search"
SHELF_URL     = f"{BASE_URL}/restapi/soa2/21052/json/getProductShelf"
ADDINFO_URL   = f"{BASE_URL}/restapi/soa2/12530/json/resourceAddInfo"
PRICE_CAL_URL = f"{BASE_URL}/restapi/soa2/14580/json/getProductPriceCalendar"
OVERVIEW_URL  = f"{BASE_URL}/restapi/soa2/14509/json/GetSightOverview.json"
RESOURCE_DETAIL_URL = f"{BASE_URL}/restapi/soa2/12314/json/resourceDetails.json"

# 自营检测（保留：用于参考信息展示）
SELF_VENDOR_ID = 999999

# 货架名黑名单（"周边推荐"/"组合商品"等不算直接竞争）
SHELF_NAME_BLACKLIST = ("一日游", "酒店", "用车", "餐饮", "跟团", "司导", "向导",
                        "演出", "剧场", "文创店")

# Cookies 必需字段
REQUIRED_COOKIES = ("GUID", "cticket", "bticket", "vbkticket",
                    "login_uid", "Union")

# AllianceID / SID
ALLIANCE_ID = 66672
SID = 1693366

# 服务器后台能用的接口（不需要 w-payload-source）
# 注意：服务器后台拿不到 displayPrice，要拿价格必须靠扩展。
SERVER_FETCHABLE_ENDPOINTS = {ADDINFO_URL, SEARCH_URL}

# 单 POI 单轮最多调多少个 addInfo（避免无谓请求）
MAX_ADDINFO_PER_ROUND = 30


def build_headers(cookies: dict, source: str = "server") -> dict:
    """构造 soa2 POST 请求头。

    cookies 必须含 GUID；source 写进 UA + custom-header 便于服务端区分流量来源。
    """
    head_cid = cookies.get("GUID", "")
    ua = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 server-scraper"
    return {
        "User-Agent": ua,
        "Content-Type": "application/json; charset=utf-8",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "Accept": "*/*",
        "X-Ctrip-Source": source,
        "head_cid": head_cid,
    }


def _common_head(cookies: dict) -> dict:
    """soa2 payload 中 `head` 字段的通用结构。"""
    return {
        "cid": cookies.get("GUID", ""),
        "ctok": "",
        "cver": "1",
        "lang": "zh-CN",
        "sid": SID,
        "syscode": "09",
        "auth": "",
        "ExtensionAttr": [
            {"key": "AllianceID", "value": str(ALLIANCE_ID)},
            {"key": "SID", "value": str(SID)},
            {"key": "OAUID", "value": cookies.get("login_uid", "")},
            {"key": "IsTP", "value": "false"},
            {"key": "pageid", "value": "230105"},
            {"key": "referrer", "value": f"{BASE_URL}/"},
        ],
    }


def search_payload(poi_viewid: int, use_date: str | None = None) -> dict:
    """distributorSearchObj 的 payload。"""
    return {
        "head": _common_head_inner_for_search(),
        "distributorSearchObj": {
            "keyword": "",
            "searchType": 2,
            "extension": [
                {"key": "useDate", "value": use_date or ""},
                {"key": "viewId", "value": str(poi_viewid)},
                {"key": "districtId", "value": ""},
            ],
            "pageIndex": 1,
            "pageSize": 50,
        },
    }


def _common_head_inner_for_search() -> dict:
    """search 接口的 head（serviceId 不同，结构略不同）。"""
    return {
        "cid": "",
        "syscode": "30",
        "lang": "zh-CN",
        "sid": SID,
        "ExtensionAttr": [
            {"key": "AllianceID", "value": str(ALLIANCE_ID)},
            {"key": "SID", "value": str(SID)},
            {"key": "pageid", "value": "230105"},
        ],
    }


def addinfo_payload(resource_id: int, poi_viewid: int) -> dict:
    """resourceAddInfo 的 payload。"""
    return {
        "head": {
            "cid": "",
            "syscode": "09",
            "lang": "zh-CN",
            "sid": SID,
            "ExtensionAttr": [
                {"key": "AllianceID", "value": str(ALLIANCE_ID)},
                {"key": "SID", "value": str(SID)},
                {"key": "pageid", "value": "230105"},
            ],
        },
        "ResourceId": resource_id,
        "PoiViewId": poi_viewid,
    }


def extract_resource_ids(search_resp: dict) -> list[int]:
    """从 search 响应里抽取 resourceId 列表。"""
    out: list[int] = []
    # 实测：data.distributorSearchObj.resourceList[] 或 data.resourceList[]
    candidates = []
    data = search_resp.get("data") or {}
    for k in ("distributorSearchObj", "resourceList", "resources"):
        v = data.get(k)
        if isinstance(v, list):
            candidates = v
            break
        if isinstance(v, dict) and isinstance(v.get("resourceList"), list):
            candidates = v["resourceList"]
            break
    for r in candidates:
        if not isinstance(r, dict):
            continue
        rid = r.get("resourceId") or r.get("id")
        if isinstance(rid, int):
            out.append(rid)
    return out