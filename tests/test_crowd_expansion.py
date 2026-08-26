"""子人群票种展开 — 合成数据单测（不依赖 _captures/，CI 永远跑）。

验证：
1. _parse_shelf 抽出 people_property fallback（resourceDetails 是权威源覆盖）
2. _parse_resource_details 抽出 peopleProperty
3. _parse_shelf_resource_list 抽出 sibling crowd child rid（chip 端点）
4. parse_round 输出每行 sku 带 people_property（含 chip 端点补全的 sibling crowd）
5. _build_sku_name 直接用 shelf.fullName / vendor 段名字（chip name 优先）
6. price_day 行带 people_property（来自 resourceDetails > shelf > crowd fallback）

注意：父 SKU 与子人群之间没有可靠的 API 链接（children 的 mpri_rid 指向自己，
与 parent 不共享 l1/productIds/fullName 前缀），所以 parent_resource_id 暂不填。
父子在 dashboard 通过 shelfType 自然聚合。

getShelfResourceList 是补全非默认 crowd 的唯一权威源：shelf 只返默认 crowd rid，
扩展 proactive fire 把它 fan-out 到 propertyIdList 上抓所有 sibling。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctrip_core.parse import (
    parse_round,
    _parse_shelf,
    _parse_resource_details,
    _parse_shelf_resource_list,
    _parse_price_calendars,
    _build_sku_name,
)


# ── 合成数据 fixtures ──

VIEWID = 5208  # 圆明园
POI_ID = "76625"


def synthetic_shelf_with_parent_sku() -> dict:
    """构造一个含「可选人群」父 SKU 的最小 shelf 响应。

    设计：
    - 父 rid=100 fullName='圆明园通票+智旅手册(可选人群)'，productIds=[501, 502]
      minPriceRelationInfo.resourceId=101（指向最便宜的成人子票 — 但这只是
      推荐 cheapest 提示，不是父子关联。children 的 mpri_rid 指向 self）。
    - 成人子 rid=101，fullName='成人票'，productIds=[501]，mpri_rid=101（self）
    - 儿童子 rid=102，fullName='儿童票'，productIds=[502]，mpri_rid=102（self）
    - 单人群 rid=200，fullName='成人票'，productIds=[601]，mpri_rid=200（self）
    """
    return {
        "poiId": POI_ID,
        "spotid": VIEWID,
        "resources": [
            {
                "id": 100, "resourceId": 100,
                "fullName": "圆明园通票+智旅手册(可选人群)",
                "productIds": [501, 502],
                "level1SaleUnitId": 9001,
                "spotid": VIEWID, "displayPrice": 34,
                "minPriceRelationInfo": {
                    "resourceId": 101, "productId": 501,
                    "peoplePropertyCode": "chengrp", "peoplePropertyName": "成人票",
                },
            },
            {
                "id": 101, "resourceId": 101,
                "fullName": "成人票",
                "productIds": [501],
                "level1SaleUnitId": 9002,
                "spotid": VIEWID, "displayPrice": 34,
                "minPriceRelationInfo": {
                    "resourceId": 101, "productId": 501,
                    "peoplePropertyCode": "chengrp", "peoplePropertyName": "成人票",
                },
            },
            {
                "id": 102, "resourceId": 102,
                "fullName": "儿童票",
                "productIds": [502],
                "level1SaleUnitId": 9002,
                "spotid": VIEWID, "displayPrice": 9,
                "minPriceRelationInfo": {
                    "resourceId": 102, "productId": 502,
                    "peoplePropertyCode": "ertgrp", "peoplePropertyName": "儿童票",
                },
            },
            {
                "id": 200, "resourceId": 200,
                "fullName": "成人票",
                "productIds": [601],
                "level1SaleUnitId": 9003,
                "spotid": VIEWID, "displayPrice": 30,
                "minPriceRelationInfo": {
                    "resourceId": 200, "productId": 601,
                    "peoplePropertyCode": "chengrp", "peoplePropertyName": "成人票",
                },
            },
        ],
        "shelfGroups": [
            {"id": "sg1", "ticketGroups": [{"id": 9002}, {"id": 9003}]},
        ],
        "shelfTypes": [
            {"id": 1, "name": "门票", "shelfItems": [{"shelfGroupId": "sg1"}]},
        ],
    }


def synthetic_addinfo(vendor_id: int = 1429575) -> dict:
    """为每个 rid 返回 vendorId=1429575（合成 vendor）。"""
    return {
        "data": {"resources": [
            {"id": rid,
             "vendorInfo": {"vendorId": vendor_id, "name": "江西千寻旅游有限公司",
                            "brandCompanyName": "江西千寻旅游有限公司",
                            "licenceNo": "91360103MACW1REM6T",
                            "rawLicencePicUrl": None}}
            for rid in [100, 101, 102, 200]
        ]}
    }


def synthetic_pricecal(rid: int, days: list[str] = ("2026-08-25", "2026-08-26")) -> dict:
    return {
        "data": {
            "priceAndStockInfos": [
                {
                    "date": d,
                    "minPrice": 34 if rid == 101 else 9 if rid == 102 else 30,
                    "packagePriceAndStockInfos": [{
                        "packageId": 7000 + rid,
                        "minPrice": 34 if rid == 101 else 9 if rid == 102 else 30,
                        "resourcePriceAndStockInfos": [{
                            "resourceId": rid,
                            "salePrice": 34 if rid == 101 else 9 if rid == 102 else 30,
                            "price": 34 if rid == 101 else 9 if rid == 102 else 30,
                            "marketPrice": 60 if rid == 101 else 18 if rid == 102 else 50,
                            "inventoryNum": 100,
                            "available": True,
                        }],
                    }],
                } for d in days
            ]
        }
    }


def synthetic_rdetail(rid: int, people: str) -> dict:
    return {"data": {"resourceId": rid, "peopleProperty": people, "name": f"rid {rid}"}}


def make_round(include_rdetails: list[tuple[int, str]] | None = None,
               include_pricecal: list[int] | None = None) -> dict:
    """合成一个完整 round（含 shelf + addInfo + 可选 resourceDetails × N + 可选 priceCal × N）。"""
    requests = [
        {"url": "/restapi/soa2/21052/json/getProductShelf",
         "ok": True, "body": synthetic_shelf_with_parent_sku()},
        {"url": "/restapi/soa2/12530/json/resourceAddInfo",
         "ok": True, "body": synthetic_addinfo()},
    ]
    for rid in (include_pricecal or [101, 102, 200]):
        requests.append({
            "url": "/restapi/soa2/14580/json/getProductPriceCalendar",
            "ok": True, "body": synthetic_pricecal(rid),
        })
    for rid, people in (include_rdetails or []):
        requests.append({
            "url": "/restapi/soa2/12314/json/resourceDetails",
            "ok": True, "body": synthetic_rdetail(rid, people),
        })
    return {
        "capturedAt": "2026-08-25T13:35:00Z",
        "extensionVersion": "test-synth",
        "poi": {"viewid": VIEWID, "name": "圆明园"},
        "requests": requests,
        "cookies": {},
    }


# ── 单测 ──

def test_parse_shelf_parent_sku_inherits_mpri_people():
    """父 SKU（productIds > 1）→ people_property 从 mpri.peoplePropertyName 抽（默认最便宜 chip）。

    父 SKU 自身就是 (可选人群) 容器，但 addInfo 响应里它的 name 已经带 chip 后缀
    （"…成人票"），不挂 chip tag 会让 dashboard 出现「name 带 chip 但无 chip tag」。
    让 mpri.peoplePropertyName 流过，crowd fan-out 命中后再由 crowd_map 覆盖。
    """
    shelf = synthetic_shelf_with_parent_sku()
    lookup = _parse_shelf(shelf, VIEWID)
    parent = lookup[100]
    assert parent["parent_resource_id"] is None
    assert parent["people_property"] == "成人票"
    assert len(parent["product_ids"]) == 2


def test_parse_shelf_child_extracts_people_from_mpri():
    """单人群子 SKU → people_property 从 mpri.peoplePropertyName 抽。"""
    shelf = synthetic_shelf_with_parent_sku()
    lookup = _parse_shelf(shelf, VIEWID)
    child_adult = lookup[101]
    assert child_adult["people_property"] == "成人票"
    assert child_adult["parent_resource_id"] is None
    child_kid = lookup[102]
    assert child_kid["people_property"] == "儿童票"


def test_parse_shelf_single_crowd_no_parent():
    """单人群 rid=200（不属任何父 SKU）→ people_property 正常。"""
    shelf = synthetic_shelf_with_parent_sku()
    lookup = _parse_shelf(shelf, VIEWID)
    solo = lookup[200]
    assert solo["parent_resource_id"] is None
    assert solo["people_property"] == "成人票"


def test_parse_resource_details_extracts_people_property():
    """多 rid 的 resourceDetails 响应 → dict[rid] = peopleProperty。"""
    bodies = [
        synthetic_rdetail(101, "成人票"),
        synthetic_rdetail(102, "儿童票"),
        synthetic_rdetail(200, "成人票"),
    ]
    out = _parse_resource_details(bodies)
    assert out == {101: "成人票", 102: "儿童票", 200: "成人票"}


def test_parse_resource_details_handles_missing_field():
    """缺 peopleProperty → 不入 dict。"""
    bodies = [
        synthetic_rdetail(101, "成人票"),
        {"data": {"resourceId": 999}},  # 没 peopleProperty
        {"data": {"resourceId": 102, "peopleProperty": "  "}},  # 空字符串
    ]
    out = _parse_resource_details(bodies)
    assert 101 in out
    assert 999 not in out
    assert 102 not in out


def test_build_sku_name_uses_fallback_fullname():
    """build_sku_name 直接用 shelf.fullName（不拼父 · 人群）。"""
    shelf = synthetic_shelf_with_parent_sku()
    lookup = _parse_shelf(shelf, VIEWID)
    # 有 shelf.fullName 时直接用
    name = _build_sku_name(lookup, 101, "成人票", None)
    assert name == "成人票"
    # 父 SKU 用自己的 fullName
    name_parent = _build_sku_name(lookup, 100, None, None)
    assert name_parent == "圆明园通票+智旅手册(可选人群)"
    # fallback 到 people_property
    name_only_people = _build_sku_name({}, 101, "成人票", None)
    assert name_only_people == "成人票"
    # 什么都没有 → "rid <id>"
    name_rid = _build_sku_name({}, 999, None, None)
    assert name_rid == "rid 999"


def test_parse_round_emits_people_property():
    """端到端：parse_round 输出每行 sku 带 people_property。"""
    raw = make_round(
        include_rdetails=[(101, "成人票"), (102, "儿童票"), (200, "成人票")],
        include_pricecal=[101, 102, 200],
    )
    parsed = parse_round(raw)
    by_rid = {s["resource_id"]: s for s in parsed["skus"]}
    # 父 SKU：mpri 兜底（resourceDetails 没打它，但 shelf.mpri.peoplePropertyName="成人票"）
    assert 100 in by_rid
    assert by_rid[100]["parent_resource_id"] is None
    assert by_rid[100]["people_property"] == "成人票"
    # 子人群：people 来自 resourceDetails
    assert by_rid[101]["people_property"] == "成人票"
    assert by_rid[102]["people_property"] == "儿童票"
    # 单人群：people 来自 resourceDetails
    assert by_rid[200]["people_property"] == "成人票"


def test_parse_round_sku_name_format():
    """端到端：sku_name = shelf.fullName（不拼前缀）。"""
    raw = make_round(
        include_rdetails=[(101, "成人票"), (102, "儿童票"), (200, "成人票")],
        include_pricecal=[101, 102, 200],
    )
    parsed = parse_round(raw)
    by_rid = {s["resource_id"]: s for s in parsed["skus"]}
    # 子人群：用 shelf.fullName（"成人票"/"儿童票"）
    assert by_rid[101]["full_name"] == "成人票"
    assert by_rid[102]["full_name"] == "儿童票"
    # 父 SKU：自己的 fullName（含"(可选人群)"）
    assert by_rid[100]["full_name"] == "圆明园通票+智旅手册(可选人群)"
    # 单人群：保留 fullName
    assert by_rid[200]["full_name"] == "成人票"


def test_parse_round_falls_back_to_shelf_min_price_relation():
    """无 resourceDetails → people_property 走 shelf.minPriceRelationInfo fallback。"""
    raw = make_round(include_rdetails=[], include_pricecal=[101, 102, 200])
    parsed = parse_round(raw)
    by_rid = {s["resource_id"]: s for s in parsed["skus"]}
    assert by_rid[101]["people_property"] == "成人票"
    assert by_rid[102]["people_property"] == "儿童票"


def test_parse_round_price_day_has_people_property():
    """price_days[] 每行带 people_property（来自 resourceDetails）。"""
    raw = make_round(
        include_rdetails=[(101, "成人票"), (102, "儿童票"), (200, "成人票")],
        include_pricecal=[101, 102, 200],
    )
    parsed = parse_round(raw)
    days_by_rid = {d["resource_id"]: d for d in parsed["price_days"]}
    assert days_by_rid[101]["people_property"] == "成人票"
    assert days_by_rid[102]["people_property"] == "儿童票"
    assert days_by_rid[200]["people_property"] == "成人票"


def test_parse_price_calendars_with_people_map_and_shelf():
    """_parse_price_calendars 接受 people_map + shelf_lookup 参数，注入每行。"""
    shelf = synthetic_shelf_with_parent_sku()
    lookup = _parse_shelf(shelf, VIEWID)
    people_map = {101: "成人票", 102: "儿童票"}  # resourceDetails
    days = _parse_price_calendars(
        [synthetic_pricecal(101), synthetic_pricecal(102)],
        vendor_map=None,
        people_map=people_map,
        shelf_lookup=lookup,
    )
    # 至少 2 天 × 2 rids = 4 行
    assert len(days) == 4
    for d in days:
        assert d["people_property"] in ("成人票", "儿童票")
        assert d["sale_price"] is not None


# ── chip 端点 (getShelfResourceList) sibling crowd 补全 ──


def synthetic_shelf_only_adult() -> dict:
    """真实场景：shelf 只返默认 crowd (成人)，儿童/老人/学生 rid 不在 shelf.resources[]。

    - rid=101 (成人子) 在 shelf，有 propertyIdList=[1642411, 1642413, 1586433]
      表示 3 个可选人群 (成人/儿童/老人)。adult 是默认，被 shelf 直接收。
    - rid=102 (儿童子) / rid=103 (老人子) **不在 shelf**，要靠 getShelfResourceList chip 端点。
    """
    return {
        "poiId": POI_ID,
        "spotid": VIEWID,
        "resources": [
            {
                "id": 101, "resourceId": 101,
                "fullName": "圆明园通票+智旅手册",
                "productIds": [501],
                "level1SaleUnitId": 9002,
                "spotid": VIEWID, "displayPrice": 34,
                "propertyIdList": [1642411, 1642413, 1586433],
                "minPriceRelationInfo": {
                    "resourceId": 101, "productId": 501,
                    "peoplePropertyCode": "chengrp", "peoplePropertyName": "成人票",
                },
            },
            {
                "id": 200, "resourceId": 200,
                "fullName": "成人票",
                "productIds": [601],
                "level1SaleUnitId": 9003,
                "spotid": VIEWID, "displayPrice": 30,
                "minPriceRelationInfo": {
                    "resourceId": 200, "productId": 601,
                    "peoplePropertyCode": "chengrp", "peoplePropertyName": "成人票",
                },
            },
        ],
        "shelfGroups": [
            {"id": "sg1", "ticketGroups": [{"id": 9002}, {"id": 9003}]},
        ],
        "shelfTypes": [
            {"id": 1, "name": "门票", "shelfItems": [{"shelfGroupId": "sg1"}]},
        ],
    }


def synthetic_shelf_resource_list_body(rid: int, people_property_id: int,
                                       child_rid: int, child_name: str,
                                       people_name: str, display_price: float) -> dict:
    """合成一个 chip 端点响应（模拟点 chip 后的返回）。"""
    return {
        "resources": [{
            "id": child_rid, "resourceId": child_rid,
            "name": child_name,
            "spotid": VIEWID, "displayPrice": display_price,
            "level1SaleUnitId": 9002,
            "minPriceRelationInfo": {
                "resourceId": child_rid,
                "productId": 502 if people_name == "儿童票" else 503,
                "peoplePropertyCode": f"{people_name}grp",
                "peoplePropertyName": people_name,
                "peoplePropertyId": people_property_id,
            },
        }],
        "idList": [rid],
        "spotid": VIEWID,
        "peoplePropertyId": people_property_id,
    }


def test_parse_shelf_resource_list_extracts_children():
    """_parse_shelf_resource_list 单测：响应 resources[] → dict[rid] = {name, people_property}。"""
    bodies = [
        synthetic_shelf_resource_list_body(
            rid=101, people_property_id=1642413,
            child_rid=102, child_name="圆明园通票+智旅手册儿童票",
            people_name="儿童票", display_price=9),
        synthetic_shelf_resource_list_body(
            rid=101, people_property_id=1586433,
            child_rid=103, child_name="圆明园通票+智旅手册老人票",
            people_name="老人票", display_price=20),
    ]
    out = _parse_shelf_resource_list(bodies)
    assert 102 in out and 103 in out
    assert out[102]["name"] == "圆明园通票+智旅手册儿童票"
    assert out[102]["people_property"] == "儿童票"
    assert out[103]["name"] == "圆明园通票+智旅手册老人票"
    assert out[103]["people_property"] == "老人票"


def test_parse_shelf_resource_list_skips_empty_body():
    """空 body / 无 resources[] → 不入 dict。"""
    bodies = [{}, None, {"resources": []}, {"resources": [{}]}]
    out = _parse_shelf_resource_list(bodies)
    assert out == {}


def test_parse_round_picks_up_children_from_shelf_resource_list():
    """端到端：shelf 只有 rid=101，getShelfResourceList 给出 rid=102 (儿童)。

    parse_round 输出 sku 应含 102，people_property=儿童票，full_name 含 chip 后缀。
    """
    raw = {
        "capturedAt": "2026-08-25T13:35:00Z",
        "extensionVersion": "test-synth",
        "poi": {"viewid": VIEWID, "name": "圆明园"},
        "requests": [
            {"url": "/restapi/soa2/21052/json/getProductShelf",
             "ok": True, "body": synthetic_shelf_only_adult()},
            {"url": "/restapi/soa2/12530/json/resourceAddInfo",
             "ok": True, "body": {
                 "data": {"resources": [
                     {"id": 101,
                      "vendorInfo": {"vendorId": 1429575, "name": "v", "brandCompanyName": "b",
                                     "licenceNo": "L"}},
                     {"id": 200,
                      "vendorInfo": {"vendorId": 1429576, "name": "v2", "brandCompanyName": "b2",
                                     "licenceNo": "L2"}},
                 ]}
             }},
            # chip 端点：rid=101 + propertyId=儿童 → 给出 child rid=102
            {"url": "/restapi/soa2/21052/getShelfResourceList",
             "ok": True, "body": synthetic_shelf_resource_list_body(
                 rid=101, people_property_id=1642413,
                 child_rid=102, child_name="圆明园通票+智旅手册儿童票",
                 people_name="儿童票", display_price=9)},
        ],
        "cookies": {},
    }
    parsed = parse_round(raw)
    by_rid = {s["resource_id"]: s for s in parsed["skus"]}
    # 关键：102 必须出现在 sku 里（child crowd 不是 shelf 显式资源）
    assert 102 in by_rid, f"sibling crowd child rid=102 missing; have rids={list(by_rid)}"
    assert by_rid[102]["people_property"] == "儿童票"
    # full_name 应该来自 chip 端点的 name（含 crowd 后缀）
    assert by_rid[102]["full_name"] == "圆明园通票+智旅手册儿童票"
    # parent (101) 还在
    assert by_rid[101]["people_property"] == "成人票"
    assert 200 in by_rid


def test_parse_round_prefers_shelf_resource_list_over_default_name():
    """resourceDetails + chip name 同在时，chip name 优先 (chip 后缀更具体)。"""
    raw = {
        "capturedAt": "2026-08-25T13:35:00Z",
        "extensionVersion": "test-synth",
        "poi": {"viewid": VIEWID, "name": "圆明园"},
        "requests": [
            {"url": "/restapi/soa2/21052/json/getProductShelf",
             "ok": True, "body": synthetic_shelf_only_adult()},
            {"url": "/restapi/soa2/12530/json/resourceAddInfo",
             "ok": True, "body": {
                 "data": {"resources": [
                     {"id": 102,
                      "vendorInfo": {"vendorId": 1429575, "name": "v", "brandCompanyName": "b",
                                     "licenceNo": "L"}},
                 ]}
             }},
            # resourceDetails 给 "儿童票"（无 chip 后缀），chip 端点给 "圆明园通票+智旅手册儿童票"
            {"url": "/restapi/soa2/12314/json/resourceDetails",
             "ok": True, "body": {"data": {"resourceId": 102, "peopleProperty": "儿童票"}}},
            {"url": "/restapi/soa2/21052/getShelfResourceList",
             "ok": True, "body": synthetic_shelf_resource_list_body(
                 rid=101, people_property_id=1642413,
                 child_rid=102, child_name="圆明园通票+智旅手册儿童票",
                 people_name="儿童票", display_price=9)},
        ],
        "cookies": {},
    }
    parsed = parse_round(raw)
    by_rid = {s["resource_id"]: s for s in parsed["skus"]}
    assert by_rid[102]["people_property"] == "儿童票"  # resourceDetails 权威
    assert by_rid[102]["full_name"] == "圆明园通票+智旅手册儿童票"  # chip name 优先


def test_parse_round_price_day_inherits_people_from_shelf_resource_list():
    """price_day 行 people_property 在缺 resourceDetails 时回退到 crowd_map。"""
    raw = {
        "capturedAt": "2026-08-25T13:35:00Z",
        "extensionVersion": "test-synth",
        "poi": {"viewid": VIEWID, "name": "圆明园"},
        "requests": [
            {"url": "/restapi/soa2/21052/json/getProductShelf",
             "ok": True, "body": synthetic_shelf_only_adult()},
            {"url": "/restapi/soa2/12530/json/resourceAddInfo",
             "ok": True, "body": {
                 "data": {"resources": [
                     {"id": 102,
                      "vendorInfo": {"vendorId": 1429575, "name": "v", "brandCompanyName": "b",
                                     "licenceNo": "L"}},
                 ]}
             }},
            {"url": "/restapi/soa2/14580/json/getProductPriceCalendar",
             "ok": True, "body": synthetic_pricecal(102)},
            {"url": "/restapi/soa2/21052/getShelfResourceList",
             "ok": True, "body": synthetic_shelf_resource_list_body(
                 rid=101, people_property_id=1642413,
                 child_rid=102, child_name="圆明园通票+智旅手册儿童票",
                 people_name="儿童票", display_price=9)},
        ],
        "cookies": {},
    }
    parsed = parse_round(raw)
    days_by_rid = {d["resource_id"]: d for d in parsed["price_days"]}
    assert 102 in days_by_rid
    # price_day 行 people_property 应该来自 crowd_map（因为没 resourceDetails）
    assert days_by_rid[102]["people_property"] == "儿童票"


# ── vendor_map 部分（WAF 限速）但 shelf_lookup 全：sibling crowd 不能丢 ──


def synthetic_shelf_three_children() -> dict:
    """shelf 一次返所有 sibling crowd（实测 5208/圆明园就是这个形态）。

    rid=101 成人 / rid=102 儿童 / rid=103 老人 同 L1；shelves[].propertyIdList 不存在
    （新 API 已把 sibling 直接展开到 resources[]）。
    """
    return {
        "poiId": POI_ID,
        "spotid": VIEWID,
        "resources": [
            {
                "id": 101, "resourceId": 101,
                "fullName": "圆明园通票+智旅手册成人票",
                "productIds": [501], "level1SaleUnitId": 9002,
                "spotid": VIEWID, "displayPrice": 34,
                "minPriceRelationInfo": {"resourceId": 101, "productId": 501,
                                         "peoplePropertyName": "成人票"},
            },
            {
                "id": 102, "resourceId": 102,
                "fullName": "圆明园通票+智旅手册儿童票",
                "productIds": [502], "level1SaleUnitId": 9002,
                "spotid": VIEWID, "displayPrice": 9,
                "minPriceRelationInfo": {"resourceId": 102, "productId": 502,
                                         "peoplePropertyName": "儿童票"},
            },
            {
                "id": 103, "resourceId": 103,
                "fullName": "圆明园通票+智旅手册老人票",
                "productIds": [503], "level1SaleUnitId": 9002,
                "spotid": VIEWID, "displayPrice": 17,
                "minPriceRelationInfo": {"resourceId": 103, "productId": 503,
                                         "peoplePropertyName": "老人票"},
            },
        ],
        "shelfGroups": [{"id": "sg-1", "ticketGroups": [{"id": 9002}]}],
        "shelfTypes": [{"id": 7, "name": "门票",
                         "shelfItems": [{"shelfGroupId": "sg-1"}]}],
    }


def synthetic_addinfo_one_success() -> list[dict]:
    """扩展主动 fire addInfo：WAF 限速，19/20 返 430，1/20 返 200。

    实测 2026-08-25 m.ctrip.com 用 3s stagger 在 viewid=5208 也是这个比例。
    parser 必须把 19 个 WAF 的 rid 当成"vendor 没拿到"走 shelf fallback 出 SKU，
    不能因为 vendor_map 里有 1 个成功就把整段 fallback `elif` 跳过。
    """
    # 只为 rid=101 返回 addInfo 200，其余全部空 body（=WAF 拦的占位）
    return [
        {
            "data": {
                "resources": [{
                    "id": 101,
                    "name": "圆明园通票+智旅手册(可选人群)",
                    "vendorInfo": {"vendorId": 900001, "name": "官方旗舰店",
                                     "brandCompanyName": "Ctrip", "licenceNo": "L1",
                                     "licencePicUrl": ""},
                }],
            },
            "ResponseStatus": {"Ack": "Success"},
        },
    ]


def test_parse_round_keeps_sibling_when_addinfo_waf_blocked():
    """vendor_map 只有一个 WAF 漏网的 rid（102/103 没 vendor），但 shelf_lookup 有 3 个。

    修复前：elif shelf_lookup 分支被跳过 → 只有 1 个 SKU；sibling crowd 102/103 丢失。
    修复后：先走 vendor_map 主路径出 rid=101，再用 shelf_lookup 兜底出 102/103。
    """
    raw = {
        "capturedAt": "2026-08-25T12:00:00.000Z",
        "poi": {"viewid": VIEWID, "name": "圆明园"},
        "requests": [
            {"url": "/restapi/soa2/21052/json/getProductShelf",
             "ok": True, "body": synthetic_shelf_three_children()},
            {"url": "/restapi/soa2/12530/json/resourceAddInfo",
             "ok": True, "body": synthetic_addinfo_one_success()[0]},
        ],
        "cookies": {},
    }
    parsed = parse_round(raw)
    by_rid = {s["resource_id"]: s for s in parsed["skus"]}
    # 三个 sibling crowd child rid 都得出 SKU
    assert set(by_rid.keys()) == {101, 102, 103}, f"missing rids: {set(by_rid.keys()) ^ {101, 102, 103}}"
    # 101 有 vendor
    assert by_rid[101]["primary_vendor_id"] == 900001
    assert by_rid[101]["primary_vendor_name"] == "官方旗舰店"
    # 102/103 vendor 字段为 0 但完整有 name + people_property
    assert by_rid[102]["primary_vendor_id"] == 0
    assert by_rid[102]["people_property"] == "儿童票"
    assert by_rid[102]["full_name"] == "圆明园通票+智旅手册儿童票"
    assert by_rid[103]["people_property"] == "老人票"
    assert by_rid[103]["full_name"] == "圆明园通票+智旅手册老人票"


# ── 父 SKU 人群 tag 兜底（fix 2026-08-25）──


def test_parse_round_parent_sku_falls_back_to_shelf_mpri_when_no_rdetail_no_chip():
    """父 SKU 无 resourceDetails 也无 chip fan-out → people_property 走 shelf.mpri.peoplePropertyName 兜底。

    实测场景：天坛 110368162「天坛公园门票+下午茶(可选人群)」addInfo 响应的 name 已经
    带 chip 后缀，但 resourceDetails 没打、chip fan-out response 也没 mpri，dashboard
    之前会显示「天坛公园门票+下午茶成人票」无 chip tag。修了 _parse_shelf Step 4 后，
    父 SKU 的 mpri.peoplePropertyName 流过来，至少挂上「成人票」chip。
    """
    raw = make_round(
        include_rdetails=[],  # 父 100 故意不打 resourceDetails
        include_pricecal=[101, 102, 200],
    )
    parsed = parse_round(raw)
    by_rid = {s["resource_id"]: s for s in parsed["skus"]}
    # 父 SKU：mpri.peoplePropertyName 兜底
    assert by_rid[100]["people_property"] == "成人票"


def test_parse_round_parent_sku_chip_fanout_overrides_mpri():
    """父 SKU 走 chip fan-out 后 → crowd_map[rid].people_property 优先于 shelf.mpri。

    实测场景：chip fan-out 命中「学生票」chip 后，people_property 应是「学生票」而不是
    默认最便宜的「成人票」（mpri）。fallback 链 people_map > crowd > shelf 保证
    crowd 覆盖 shelf.mpri。
    """
    raw = {
        "capturedAt": "2026-08-25T14:00:00.000Z",
        "extensionVersion": "test-synth",
        "poi": {"viewid": VIEWID, "name": "圆明园"},
        "requests": [
            {"url": "/restapi/soa2/21052/json/getProductShelf",
             "ok": True, "body": synthetic_shelf_with_parent_sku()},
            {"url": "/restapi/soa2/12530/json/resourceAddInfo",
             "ok": True, "body": synthetic_addinfo()},
            # chip 端点：父 rid=100 + 学生 chip pid → 返回学生子 rid=201
            {"url": "/restapi/soa2/21052/getShelfResourceList",
             "ok": True, "body": synthetic_shelf_resource_list_body(
                 rid=100, people_property_id=1460674,
                 child_rid=201, child_name="圆明园通票+智旅手册学生票",
                 people_name="学生票", display_price=20)},
        ],
        "cookies": {},
    }
    parsed = parse_round(raw)
    by_rid = {s["resource_id"]: s for s in parsed["skus"]}
    # 学生子 SKU 出现
    assert 201 in by_rid
    assert by_rid[201]["people_property"] == "学生票"
    assert by_rid[201]["full_name"] == "圆明园通票+智旅手册学生票"
    # 父 SKU 没被 chip fan-out 直接覆盖（chip 返回的是 child rid 201），仍走 shelf.mpri
    assert by_rid[100]["people_property"] == "成人票"


# ── 父 rid addInfo 双键落库 + chip name 末尾人群正则 (2026-08-26 修复) ──

def _addinfo_parent_with_chips_body() -> dict:
    """父 rid 109771882 的 addInfo 响应，含两个 chip（成人 / 儿童）。"""
    return {
        "data": {"resources": [
            {"id": 109771882, "productId": 109770667,
             "name": "圆明园通票+智旅手册成人票",
             "vendorInfo": {"vendorId": 16540, "name": "携程代理",
                            "brandCompanyName": "北京旭冉假期旅游有限公司",
                            "licenceNo": "91110106563680484T",
                            "rawLicencePicUrl": None}},
            {"id": 109771882, "productId": 110268323,
             "name": "圆明园通票+智旅手册儿童票",
             "vendorInfo": {"vendorId": 16540, "name": "携程代理",
                            "brandCompanyName": "北京旭冉假期旅游有限公司",
                            "licenceNo": "91110106563680484T",
                            "rawLicencePicUrl": None}},
        ]}
    }


def test_parse_addinfos_chip_name_extracts_ppl():
    """父 rid 的 addInfo 响应里 chip 的 name 末尾「成人票/儿童票」必须解析成 ppl。

    之前 _parse_addinfos 只存 vendor + full_name，chip 行 people_property 落空。
    通过 URL 的 __chip hint 也能识别 sibling fire（productId 在响应里是 mpri 不是 chip）。
    """
    from ctrip_core.parse import _parse_addinfos
    # 模拟 sibling fire：URL 带 __chip=110268323，响应里 productId 是 mpri (109770667)
    body = _addinfo_parent_with_chips_body()
    pairs = [
        ("/restapi/soa2/12530/json/resourceAddInfo", body),
        ("/restapi/soa2/12530/json/resourceAddInfo?__chip=110268323", body),
    ]
    out = _parse_addinfos(pairs, rids={109771882, 109770667, 110268323})
    assert out[109770667]["people_property"] == "成人票"
    assert out[110268323]["people_property"] == "儿童票"
    assert out[109770667]["full_name"] == "圆明园通票+智旅手册成人票"
    assert out[110268323]["full_name"] == "圆明园通票+智旅手册儿童票"


def test_parse_addinfos_multi_chip_names_dont_overwrite():
    """同一父 rid 的多个 chip addInfo 响应按 productId 落库时，各自的 full_name 互不覆盖。

    双键落库最后写入会覆盖，但只在 chip rid 自己的 key 上覆盖，不会让 chip1.name
    变成 chip2.name。父 rid 的 key 会保留最后写入的 chip — 这是设计意图（dashboard
    主要按 chip rid 渲染）。
    """
    from ctrip_core.parse import _parse_addinfos
    # 用 URL __chip hint 区分两个 sibling fire，避免 productId 全部撞到 mpri
    body = _addinfo_parent_with_chips_body()
    pairs = [
        ("/restapi/soa2/12530/json/resourceAddInfo?__chip=109770667", body),
        ("/restapi/soa2/12530/json/resourceAddInfo?__chip=110268323", body),
    ]
    out = _parse_addinfos(pairs, rids={109771882, 109770667, 110268323})
    assert out[109770667]["full_name"].endswith("成人票")
    assert out[110268323]["full_name"].endswith("儿童票")
    assert "成人票" not in out[110268323]["full_name"]
    assert "儿童票" not in out[109770667]["full_name"]


def test_parse_price_calendars_chip_inherits_parent_price():
    """_parse_price_calendars 给 chip 行复制父 rid 的 price_day（resourceId 替换）。

    实测 Ctrip priceCal 对 chip rid 单独 query → errcode 1005，
    _parse_price_calendars 必须从 shelf_lookup 找 parent_resource_id fallback。
    """
    from ctrip_core.parse import _parse_price_calendars
    parent_rid = 109771882
    chip_rid = 109770667
    shelf_lookup = {
        parent_rid: {"resource_id": parent_rid, "parent_resource_id": None,
                     "people_property": "成人票"},
        chip_rid: {"resource_id": chip_rid, "parent_resource_id": parent_rid,
                   "people_property": None},
    }
    pricecal = {
        "data": {"priceAndStockInfos": [{
            "date": "2026-08-26",
            "packagePriceAndStockInfos": [{
                "packageId": 5939620,
                "resourcePriceAndStockInfos": [{
                    "resourceId": parent_rid,
                    "salePrice": 34, "price": 34, "marketPrice": 60,
                    "inventoryNum": 100, "available": True, "discount": 0.57,
                }],
            }],
        }]}
    }
    out = _parse_price_calendars(
        [pricecal],
        vendor_map={parent_rid: {"primary": {"vendorId": 16540}},
                    chip_rid: {"primary": {"vendorId": 16540}}},
        shelf_lookup=shelf_lookup,
    )
    by_rid = {r["resource_id"]: r for r in out}
    # 父 rid 自己的行在
    assert parent_rid in by_rid
    assert by_rid[parent_rid]["sale_price"] == 34
    # chip rid 继承父价（resource_id 替换成 chip_rid）
    assert chip_rid in by_rid
    chip_row = by_rid[chip_rid]
    assert chip_row["sale_price"] == 34
    assert chip_row["inherited_from_parent"] is True
    assert chip_row["sale_date"] == "2026-08-26"
    # winning_vendor_id 也跟着复制
    assert chip_row["winning_vendor_id"] == 16540