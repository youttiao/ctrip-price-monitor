"""子人群票种展开 — 合成数据单测（不依赖 _captures/，CI 永远跑）。

验证：
1. _parse_shelf 抽出 people_property fallback（resourceDetails 是权威源覆盖）
2. _parse_resource_details 抽出 peopleProperty
3. parse_round 输出每行 sku 带 people_property
4. _build_sku_name 直接用 shelf.fullName / vendor 段名字
5. price_day 行带 people_property（来自 resourceDetails > shelf fallback）

注意：父 SKU 与子人群之间没有可靠的 API 链接（children 的 mpri_rid 指向自己，
与 parent 不共享 l1/productIds/fullName 前缀），所以 parent_resource_id 暂不填。
父子在 dashboard 通过 shelfType 自然聚合。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctrip_core.parse import (
    parse_round,
    _parse_shelf,
    _parse_resource_details,
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

def test_parse_shelf_parent_sku_no_people():
    """父 SKU（productIds > 1）→ people_property=None，自身无 parent。"""
    shelf = synthetic_shelf_with_parent_sku()
    lookup = _parse_shelf(shelf, VIEWID)
    parent = lookup[100]
    assert parent["parent_resource_id"] is None
    assert parent["people_property"] is None
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
    # 父 SKU：无 people_property
    assert 100 in by_rid
    assert by_rid[100]["parent_resource_id"] is None
    assert by_rid[100]["people_property"] is None
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