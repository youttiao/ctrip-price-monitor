"""解析器单测：拿 _captures/ 已有抓包 JSON 验证 parse_round() 行为。

跑：pytest tests/test_parse.py -v

注意：_captures/ 在 .gitignore 里（真实 API 响应，不进 repo）。
本仓库只跑合成数据的 round-trip 测试；本地有 _captures/ 时跑全量。
"""
import json
from pathlib import Path

import pytest

from ctrip_core.parse import parse_round, _parse_resource_details, _build_sku_name

CAPTURES = Path(__file__).resolve().parents[1] / "_captures"
HAS_CAPTURES = CAPTURES.exists() and any(CAPTURES.glob("*.json"))


pytestmark = pytest.mark.skipif(
    not HAS_CAPTURES,
    reason="_captures/ not in repo (CI only runs synthetic tests)",
)


def load(name: str):
    return json.loads((CAPTURES / name).read_text(encoding="utf-8"))


def build_round(viewid: int, poi_name: str, shelf_name: str,
                addinfo_name: str | None = None,
                pricecal_name: str | None = None,
                rdetail_name: str | None = None,
                synthetic_rdetails: list[dict] | None = None) -> dict:
    """合成一个 round JSON，结构与扩展 POST 的一致。

    rdetail_name: 加载 _captures/ 里真实 resourceDetails 响应（带 wrapper）
    synthetic_rdetails: 合成 (rid, peopleProperty) 对，直接构造响应 body
    """
    requests = []
    if shelf_name:
        requests.append({
            "url": "/restapi/soa2/21052/json/getProductShelf",
            "ok": True,
            "body": load(shelf_name),
        })
    if addinfo_name:
        requests.append({
            "url": "/restapi/soa2/12530/json/resourceAddInfo",
            "ok": True,
            "body": load(addinfo_name),
        })
    if pricecal_name:
        requests.append({
            "url": "/restapi/soa2/14580/json/getProductPriceCalendar",
            "ok": True,
            "body": load(pricecal_name),
        })
    if rdetail_name:
        requests.append({
            "url": "/restapi/soa2/12314/json/resourceDetails",
            "ok": True,
            "body": load(rdetail_name),
        })
    if synthetic_rdetails:
        for rid, people in synthetic_rdetails:
            requests.append({
                "url": "/restapi/soa2/12314/json/resourceDetails",
                "ok": True,
                "body": {
                    "data": {
                        "resourceId": rid,
                        "peopleProperty": people,
                        "name": f"synthetic rid {rid}",
                    }
                },
            })
    return {
        "capturedAt": "2026-08-24T13:35:00Z",
        "extensionVersion": "test-1.0",
        "poi": {"viewid": viewid, "name": poi_name},
        "requests": requests,
        "cookies": {},
    }


# ── 景山（5170）：shelf 响应 + 8 个 SKU 实测 ──

def test_jingshan_parses_seven_skus():
    """jingshan_shelf_resp 应解析出 ≥1 个本 POI SKU（使用合成 addInfo 对齐 jingshan vendors）。"""
    # jingshan_vendors.json 提供 7 个 (rid, vendorId) 实测对
    jingshan_v = load("jingshan_vendors.json")
    synthetic_addinfo = {
        "data": {"resources": [{
            "id": e["rid"],
            "vendorInfo": {
                "vendorId": e["vendorId"],
                "name": e["name"],
                "brandCompanyName": e["brandCompanyName"],
                "licenceNo": e["licenceNo"],
                "rawLicencePicUrl": None,
            }
        } for e in jingshan_v]}
    }
    round_data = {
        "capturedAt": "2026-08-24T13:35:00Z",
        "extensionVersion": "test-1.0",
        "poi": {"viewid": 5170, "name": "景山公园"},
        "requests": [
            {"url": "/restapi/soa2/21052/json/getProductShelf",
             "ok": True,
             "body": load("jingshan_shelf_resp.network-response")},
            {"url": "/restapi/soa2/12530/json/resourceAddInfo",
             "ok": True,
             "body": synthetic_addinfo},
        ],
    }
    parsed = parse_round(round_data)
    assert parsed["viewid"] == 5170
    assert parsed["poi_name"] == "景山公园"
    # shelf 有 7 个 vendor-backed rid + 1 个 type=7 旅拍 resourceId=None fallback
    # → 共 8 SKU（旅拍没有 addInfo，vendor_id=0 占位 → 5 个 unique vendorId 含 0）
    assert len(parsed["skus"]) == 8
    vendor_ids = {s["primary_vendor_id"] for s in parsed["skus"]}
    # jingshan_vendors.json 有 4 个 unique vendorId（+ 0 = 旅拍 fallback）
    assert len(vendor_ids) == 5
    assert 67255 in vendor_ids  # 北京讯程
    assert 1248157 in vendor_ids  # 北京票景通


def test_jingshan_shelf_type_mapping():
    """验证 rid 通过 level1SaleUnitId 正确映射到 shelfType。"""
    round_data = build_round(
        viewid=5170, poi_name="景山公园",
        shelf_name="jingshan_shelf_resp.network-response",
    )
    parsed = parse_round(round_data)
    # 即便没有 addInfo，shelf_lookup 仍会被构建
    # 通过 addinfo 名 list 我们手动跑一次
    from ctrip_core.parse import _parse_shelf
    shelf_body = round_data["requests"][0]["body"]
    lookup = _parse_shelf(shelf_body, viewid=5170)
    assert len(lookup) > 0
    # rid 82458035 → shelfType 应为 791887（故宫全景讲解）
    # 来源：jingshan_skus.json 基线
    info = lookup.get(82458035)
    if info:
        # 这个 rid 在 shelf 响应里 → 应有 shelfTypeId
        assert info.get("shelf_type_id") is not None, \
            f"rid 82458035 mapped to shelf_type_id={info.get('shelf_type_id')}"


def test_jingshan_filters_other_poi():
    """周边 POI 的资源应被过滤掉（spotid != 5170）。"""
    from ctrip_core.parse import _parse_shelf
    shelf_body = load("jingshan_shelf_resp.network-response")
    lookup = _parse_shelf(shelf_body, viewid=5170)
    for rid, info in lookup.items():
        assert info["spotid"] == 5170, \
            f"rid {rid} leaked from another POI: spotid={info['spotid']}"


def test_addinfo_only_emits_present_rids():
    """addInfo 返回的 rid 不在 shelf 里 → 不应进 SKU 列表。

    shelf 自身含 7 个 rid 仍会进 skus (这是正常的); addInfo 的 999999999 必须不在结果里。
    """
    # 给一个 addInfo 含未知 rid
    round_data = {
        "capturedAt": "2026-08-24T13:35:00Z",
        "extensionVersion": "test-1.0",
        "poi": {"viewid": 5170, "name": "景山公园"},
        "requests": [
            {
                "url": "/restapi/soa2/21052/json/getProductShelf",
                "ok": True,
                "body": load("jingshan_shelf_resp.network-response"),
            },
            {
                "url": "/restapi/soa2/12530/json/resourceAddInfo",
                "ok": True,
                "body": {
                    "data": {"resources": [{
                        "id": 999999999,  # 不在 shelf 里
                        "vendorInfo": {"vendorId": 12345, "name": "X",
                                       "brandCompanyName": "Y",
                                       "licenceNo": "Z"},
                    }]},
                },
            },
        ],
    }
    parsed = parse_round(round_data)
    sku_rids = {s["resource_id"] for s in parsed["skus"]}
    assert 999999999 not in sku_rids, "addInfo 的离群 rid 不应污染 SKU 列表"


def test_price_calendar_baseline():
    """price_calendar_resp 是真实响应，验证 parser 抽到至少 1 条价格记录。

    注: 每行 min_price 故意为 None (见 parse.py:303-304 注释);
    daily minPrice 在 day 层聚合, 不下沉到每行。
    """
    body = load("pricecal_resp.network-response")
    from ctrip_core.parse import _parse_price_calendars
    days = _parse_price_calendars([body])
    assert len(days) > 0, "pricecal_resp 应至少抽到 1 条价格记录"
    assert days[0]["resource_id"] is not None
    assert days[0]["sale_date"] is not None
    assert days[0]["min_price"] is None  # by design; see parse.py:334


# ── 5 个 POI 都应能解析 ──

@pytest.mark.parametrize("poi_name,viewid,shelf_file", [
    ("天坛公园", 233, "getProductShelf_resp.network-response"),
    ("景山公园", 5170, "jingshan_shelf_resp.network-response"),
    ("雍和宫", 5153, "yonghegong_shelf_resp.network-response"),
    ("颐和园", 231, "yiheyuan_shelf_resp.network-response"),
    ("圆明园", 5208, "yuanmingyuan_shelf_resp.network-response"),
])
def test_all_5_pois_parse(poi_name, viewid, shelf_file):
    """5 POI 的 shelf 响应都能解析出至少一个 SKU。"""
    shelf_body = load(shelf_file)
    from ctrip_core.parse import _parse_shelf
    lookup = _parse_shelf(shelf_body, viewid=viewid)
    assert len(lookup) >= 5, f"{poi_name} 只解析到 {len(lookup)} 个 SKU"
    # 至少应有 50% 的 rid 有 shelfType
    with_shelftype = sum(1 for v in lookup.values() if v.get("shelf_type_id"))
    ratio = with_shelftype / len(lookup)
    assert ratio >= 0.4, f"{poi_name} shelfType 命中率仅 {ratio:.0%}"


# ── shelfTypes 缺失 fallback：API 灰度/WAF 降级返空时，关联链从 shelfGroups.groupId 兜底 ──

def test_shelf_types_empty_falls_back_to_shelf_groups():
    """getProductShelf 响应 shelfTypes=[] 时，rid 仍能被归到一个非零 shelfType。

    实测场景：2026-08-25 圆明园 (5208) round 221 抓到的 soa2 响应
    shelfTypes 数组为 0，shelfGroups 正常有 groupId/groupName — 此时 dashboard
    会把所有 rid 落到 shelf 0 → "（未分组）"。修复见 parse.py Step 3.5。
    """
    # 直接用真实 VPS 上抓到的 round 221 shelf body（已存到 /tmp fixture）
    import json as _json
    from ctrip_core.parse import _parse_shelf
    path = "/tmp/5208_shelf_no_types.json"
    if not Path(path).exists():
        pytest.skip(f"需 {path}，手动从 VPS raw round 拉一次")
    shelf = _json.loads(Path(path).read_text())
    assert (shelf.get("shelfTypes") or []) == [], "fixture 应是 shelfTypes 为空的真实 case"
    lookup = _parse_shelf(shelf, viewid=5208)
    assert len(lookup) >= 10, f"5208 round 应解析到 ≥10 个 SKU, 实际 {len(lookup)}"
    # 所有 rid 都应有 shelf_type_id 且非 0（兜底成功）
    for rid, info in lookup.items():
        assert info.get("shelf_type_id"), \
            f"rid {rid} 在 fallback 后仍无 shelf_type_id"
        assert info["shelf_type_id"] != 0, \
            f"rid {rid} 落到 shelf 0（未分组），fallback 失败"
    # 至少应有 2 个不同 shelf_type（圆明园真实情况有 6 个）
    uniq = {info["shelf_type_id"] for info in lookup.values()}
    assert len(uniq) >= 2, f"fallback 后只 1 个 shelfType，groupId 没派上用场: {uniq}"


# ── resourceId 缺失 fallback：SKU 只有 id 没 resourceId 时不丢 ──

def test_null_resource_id_falls_back_to_id():
    """部分 SKU（type=7 旅拍 / type=11 儿童票）resources[].resourceId=None、id 非空。

    之前 Step 1 直接 `if not rid: continue`，dashboard 看不到这些 SKU。修法：rid
    退到 id；id 也为 None 的 type=14 占位（"需通过公众号购票入园"）依然被跳过。
    """
    from ctrip_core.parse import _parse_shelf
    shelf = {
        "shelfTypes": [],
        "shelfGroups": [
            {"id": "sg1", "ticketGroups": [{"id": 1, "subTicketGroups": [{"tokens": []}]}]},
        ],
        "resources": [
            # 正常：resourceId + id
            {"id": 100, "resourceId": 100, "spotid": 231, "fullName": "正常SKU",
             "level1SaleUnitId": 1, "displayPrice": 99},
            # 真实 SKU：只 id 没 resourceId（type=7 旅拍）
            {"id": 200, "resourceId": None, "spotid": 231, "fullName": "旅拍写真",
             "level1SaleUnitId": 1, "displayPrice": 90, "type": 7},
            # 占位（type=14）：id 也没，应被跳过
            {"id": None, "resourceId": None, "spotid": 231, "fullName": "占位",
             "type": 14},
            # 其它 POI 的 rid（spotid 不同），应被过滤
            {"id": 999, "resourceId": 999, "spotid": 999, "fullName": "其它POI",
             "displayPrice": 1},
        ],
    }
    lookup = _parse_shelf(shelf, viewid=231)
    assert 100 in lookup, "正常 rid 应在"
    assert 200 in lookup, "只 id 没 resourceId 的真实 SKU 应 fallback 到 id=200，不丢"
    assert None not in lookup
    assert 999 not in lookup, "其它 POI rid 应被过滤"
    assert lookup[200]["full_name"] == "旅拍写真"
    assert lookup[200]["level1_sale_unit_id"] == 1
    assert lookup[200]["display_price"] == 90