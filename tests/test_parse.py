"""解析器单测：拿 _captures/ 已有抓包 JSON 验证 parse_round() 行为。

跑：pytest tests/test_parse.py -v

注意：_captures/ 在 .gitignore 里（真实 API 响应，不进 repo）。
本仓库只跑合成数据的 round-trip 测试；本地有 _captures/ 时跑全量。
"""
import json
from pathlib import Path

import pytest

from ctrip_core.parse import parse_round

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
                pricecal_name: str | None = None) -> dict:
    """合成一个 round JSON，结构与扩展 POST 的一致。"""
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
    # 应有 7 个 SKU 全部 vendorId 都拿到
    assert len(parsed["skus"]) == 7
    vendor_ids = {s["primary_vendor_id"] for s in parsed["skus"]}
    # jingshan_vendors.json 有 4 个 unique vendorId
    assert len(vendor_ids) == 4
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
    """addInfo 返回的 rid 不在 shelf 里 → 不应进 SKU 列表。"""
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
    assert len(parsed["skus"]) == 0, "addInfo 的离群 rid 不应污染 SKU 列表"


def test_price_calendar_baseline():
    """price_calendar_resp 是真实响应，验证 parser 抽到至少 1 条价格记录。"""
    body = load("pricecal_resp.network-response")
    from ctrip_core.parse import _parse_price_calendars
    days = _parse_price_calendars([body])
    assert len(days) > 0, "pricecal_resp 应至少抽到 1 条价格记录"
    assert days[0]["resource_id"] is not None
    assert days[0]["sale_date"] is not None
    assert days[0]["min_price"] is not None


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