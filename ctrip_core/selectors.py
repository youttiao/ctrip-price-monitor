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
# 单 POI 单 ticket group 下的 sibling crowd 资源查询：官方页点 chip 时主动 fire。
# 返回的 resources[].name 自带 crowd 后缀（"圆明园通票+智旅手册儿童票"）。
# Source: 实测 2026-08-25 m.ctrip.com/restapi/soa2/21052/getShelfResourceList 抓包。
SHELF_RESOURCE_LIST_URL = f"{BASE_URL}/restapi/soa2/21052/getShelfResourceList"

# 同源 substring 匹配：parser / extension 都用 URL 中去掉 BASE_URL 的尾段做 substring。
# 注意：extension TARGET_PATHS 故意写的是 `/restapi/soa2/12314/json/resourceDetails`
# （无 `.json`），对 `.../resourceDetails.json` 同样命中，所以两端无需对齐。
RESOURCE_DETAIL_PATH = "/restapi/soa2/12314/json/resourceDetails"
SHELF_RESOURCE_LIST_PATH = "/restapi/soa2/21052/getShelfResourceList"

# 自营检测（保留：用于参考信息展示）
SELF_VENDOR_ID = 999999

# 货架名黑名单（"周边推荐"/"组合商品"等不算直接竞争）
SHELF_NAME_BLACKLIST = ("一日游", "酒店", "用车", "餐饮", "跟团", "司导", "向导",
                        "演出", "剧场", "文创店")

# viewid → poiId 兜底映射（日历接口需要 URL 路径里的 poiId，不是 viewid）。
# 优先从 search 响应抽 poiId，抽不到用这里。新 POI 上线时需手动补。
POI_VIEWID_TO_POI_ID: dict[int, str] = {
    233: "75599",    # 天坛公园
    5153: "76599",   # 雍和宫
    231: "75597",    # 颐和园
    5208: "76625",   # 圆明园
    5170: "76610",   # 景山公园
}

# Cookies 必需字段
REQUIRED_COOKIES = ("GUID", "cticket", "bticket", "vbkticket",
                    "login_uid", "Union")

# AllianceID / SID
ALLIANCE_ID = 66672
SID = 1693366

# 服务器后台能用的接口（不需要 w-payload-source）
# 注意：服务器后台拿不到 displayPrice，要拿价格必须靠扩展。
SERVER_FETCHABLE_ENDPOINTS = {ADDINFO_URL, SEARCH_URL, PRICE_CAL_URL}

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


# ── resourceDetails (soa2/12314) ──
#
# 每个 rid 单独请求。响应 data.peopleProperty 是该 resourceId 的人群标签
# (e.g. "成人票"/"儿童票"/"老人票"/"不限人群")；扩展与 server 都可调。
# Source: _captures/resourceDetails_req.network-request 实测 + API-MAP.md:103-115。

def resource_detail_payload(rid: int, poi_id_str: str) -> dict:
    """resourceDetails 的 payload。

    poi_id_str 必须是 URL 路径里的 poiId（字符串如 "75599"），不是 viewid。
    """
    return {
        "resourceId": int(rid),
        "filters": [{"type": "DateFilter",
                     "filterItems": [{"key": "Date", "value": ""}]}],
        "tags": [
            {"key": "needRateLimit", "value": "T"},
            {"key": "needPackingVersion3", "value": "true"},
            {"key": "needForcedLogin", "value": "T"},
        ],
        "clientInfo": {
            "currency": "CNY",
            "locale": "zh-CN",
            "pageId": 10650097502,
            "channelId": 116,
            "extension": [
                {"name": "poiId", "value": poi_id_str},
                {"name": "needPackagingVersion3", "value": "true"},
            ],
            "oriSyscode": "09",
            "syscode": "09",
            "cid": "",
            "appPlatform": "",
            "ic_traceid": "",
        },
        "enviroment": "PROD",
    }


# ── getProductPriceCalendar (priceCalendar) ──

_CALENDAR_TAGS: list[dict] = [
    {"key": "relatedResource", "value": "newLogic"},
    {"key": "needReturnUnavailableDate", "value": "true"},
    {"key": "noNeedTicketRelationResources", "value": "true"},
    {"key": "needSelectDateFirst", "value": "true"},
    {"key": "needSelectDateFirstV2", "value": "true"},
    {"key": "needSelectDateSort", "value": "true"},
    {"key": "supportAlternateTkt", "value": "true"},
    {"key": "needForcedLogin", "value": "T"},
    {"key": "needCardTagInfo", "value": "true"},
    {"key": "needPackingVersion3", "value": "true"},
    {"key": "needReservationMark", "value": "true"},
    {"key": "needRateLimit", "value": "true"},
    {"key": "needUnSaleAloneRes", "value": "true"},
    {"key": "needAggregationInfo", "value": "true"},
    {"key": "callRecallPK", "value": "pkOneOrMore"},
    {"key": "seckill", "value": "newSeckill"},
    {"key": "needResourceMinPriceInfo", "value": "true"},
    {"key": "needCalcTicketPriceCalendar", "value": "true"},
]


def price_calendar_payload(rid: int, poi_id_str: str, cookies: dict) -> dict:
    """getProductPriceCalendar 的 payload。

    poi_id_str 必须是 URL 路径里的 poiId（字符串，如 "75599"），不是 viewid。
    cookies 必须含 GUID，会被写入 head.cid（携程用这个鉴权）。
    """
    return {
        "bizLineType": 4,
        "id": "",
        "token": "",
        "needAggregations": True,
        "needBasicInfo": True,
        "needSaleProperties": True,
        "needUnavailableSaleDates": True,
        "needSaleStatistics": True,
        "needTags": True,
        "tags": _CALENDAR_TAGS,
        "filter": {"recommendScan": False, "beginDate": "", "endDate": ""},
        "poiId": poi_id_str,
        "mainResourceIds": [rid],
        "head": _common_head(cookies),
        "clientInfo": {
            "currency": "CNY",
            "locale": "zh-CN",
            "pageId": 10650097502,
            "channelId": 116,
            "extension": [],
            "oriSyscode": "09",
            "syscode": "09",
            "cid": "",
            "appPlatform": "",
            "ic_traceid": "",
        },
        "enviroment": "PROD",
    }


def _extract_resource_candidates(search_resp: dict) -> list[dict]:
    """从 search 响应里挑出所有 dict 形式的候选元素（不假设 shape）。

    历史兼容：2025-08 前 `data` 是 dict（`data.distributorSearchObj.resourceList[]`）；
    携程 8 月改版后 `data` 是 list（mixed-category 搜索建议，含 district/author/
    sight/... 等）。这条路径上不再有"本 POI 的资源列表"，所以 server-side
    找不到 product rid → 必须靠扩展抓 shelf。

    返回所有 dict 候选，调用方按需过滤（type=sight 也不一定是本 POI 的产品）。
    """
    out: list[dict] = []
    if not isinstance(search_resp, dict):
        return out
    data = search_resp.get("data")
    if isinstance(data, list):
        out.extend(e for e in data if isinstance(e, dict))
        return out
    if not isinstance(data, dict):
        return out
    for k in ("distributorSearchObj", "resourceList", "resources"):
        v = data.get(k)
        if isinstance(v, list):
            out.extend(e for e in v if isinstance(e, dict))
            return out
        if isinstance(v, dict) and isinstance(v.get("resourceList"), list):
            out.extend(e for e in v["resourceList"] if isinstance(e, dict))
            return out
    return out


# ── getShelfResourceList (soa2/21052) ──
#
# 单 POI 单 ticket group 下的 sibling crowd 资源查询：官方页点 chip 时主动 fire。
# body 形如 {spotid, idList: [<rid>], peoplePropertyId: <pid>, date}，
# 响应 resources[].name 自带 crowd 后缀 ("圆明园通票+智旅手册儿童票")，
# resources[].minPriceRelationInfo.peoplePropertyName 给出人群标签。
# 仅扩展调用，server scraper 走不通（需 w-payload-source）。
# Source: 实测抓包 2026-08-25 POI 5208「圆明园通票+智旅手册(可选人群)」点儿童 chip。

def shelf_resource_list_payload(spotid: int, id_list: list[int],
                                 date: str, people_property_id: int | None) -> dict:
    """getShelfResourceList payload。

    spotid: POI 的 spotid (= viewid 实测一致，可直接传 viewid)。
    id_list: 本次 query 的 rid 列表（单 chip = 1 个 rid）。
    people_property_id: chip 的 property id (从 shelf.resources[i].propertyIdList 拿)。
    date: 选日期字符串 YYYY-MM-DD。
    """
    return {
        "clientInfo": {
            "currency": "CNY",
            "locale": "zh-CN",
            "pageId": "10650104114",
            "channelId": 116,
            "extension": [],
            "oriSyscode": "09",
            "syscode": "09",
            "cid": "",
            "appPlatform": "",
            "ic_traceid": "",
        },
        "enviroment": "PROD",
        "spotid": int(spotid),
        "tags": [{"key": "callRecallPK", "value": "pkOneOrMore"}],
        "needResourceDetails": True,
        "idList": [int(r) for r in id_list],
        "date": date,
        "token": "",
        "peoplePropertyId": int(people_property_id) if people_property_id else 0,
        "needResourceFilter": True,
        "head": {
            "cid": "",
            "ctok": "",
            "cver": "1.0",
            "lang": "01",
            "sid": str(SID),
            "syscode": "09",
            "auth": "",
            "xsid": "",
            "extension": [
                {"name": "aid", "value": str(ALLIANCE_ID)},
                {"name": "sid", "value": str(SID)},
                {"name": "H5", "value": "H5"},
            ],
        },
    }


def extract_resource_ids(search_resp: dict) -> list[int]:
    """从 search 响应里抽取 resourceId 列表（兼容 list & dict 两种 `data` shape）。"""
    out: list[int] = []
    for r in _extract_resource_candidates(search_resp):
        rid = r.get("resourceId") or r.get("id")
        if isinstance(rid, int):
            out.append(rid)
    return out


def extract_search_resources(search_resp: dict) -> list[tuple[int, str | None]]:
    """返回 [(rid, poiId_str_or_None), ...]。兼容 list & dict 两种 `data` shape。

    poiId 抽取路径（按优先级）：
      1) data.poiId
      2) data.distributorSearchObj.poiId / spotid
      3) resources[].poiId

    都找不到时返回 None，调用方应查 POI_VIEWID_TO_POI_ID 兜底。

    注：2025-08 后 search 返回 mixed-category 列表，没有本 POI 产品，调用方应
    期待空列表（→ server-scraper 这轮不会有 addInfo/priceCal 调用）。
    """
    if not isinstance(search_resp, dict):
        return []
    data = search_resp.get("data")
    top_poi_str: str | None = None
    candidates: list[dict] = []

    if isinstance(data, list):
        candidates = [e for e in data if isinstance(e, dict)]
    elif isinstance(data, dict):
        top_poi_id = data.get("poiId")
        top_poi_str = str(top_poi_id) if top_poi_id is not None else None
        for k in ("distributorSearchObj", "resourceList", "resources"):
            v = data.get(k)
            if isinstance(v, list):
                candidates = [e for e in v if isinstance(e, dict)]
                break
            if isinstance(v, dict) and isinstance(v.get("resourceList"), list):
                candidates = [e for e in v["resourceList"] if isinstance(e, dict)]
                nested_poi = v.get("poiId") or v.get("spotid")
                if top_poi_str is None and nested_poi is not None:
                    top_poi_str = str(nested_poi)
                break

    out: list[tuple[int, str | None]] = []
    for r in candidates:
        rid = r.get("resourceId") or r.get("id")
        if not isinstance(rid, int):
            continue
        rid_poi = r.get("poiId")
        rid_poi_str = str(rid_poi) if rid_poi is not None else None
        out.append((rid, rid_poi_str or top_poi_str))
    return out