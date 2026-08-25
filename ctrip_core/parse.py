"""解析 raw round JSON → 标准化结果。

输入：扩展 POST 给 /api/ingest/round 的 round JSON（含 requests[] 数组，
每个元素是 {url, ok, body}，body 是 soa2 响应解析后的字典）。

输出：{
    captured_at: ISO8601,
    viewid: int,
    poi_name: str,
    skus: [{resource_id, primary_vendor_id, primary_vendor_name,
            primary_vendor_brand, primary_vendor_licence,
            primary_vendor_licence_pic, display_price, full_name,
            shelf_type_id, shelf_type_name, spotid, market_price,
            first_booking_date, sale_count, raw_resource}, ...],
    price_days: [{resource_id, sale_date, min_price, sale_price,
                  inventory, available, package_id, raw}, ...]
}

关键发现（来自 _captures 实测）：
- getProductShelf 响应里 `resources[].shelfTypeId/shelfTypeName` 总是 null
- 关联路径：resourceId → level1SaleUnitId → ticketGroup.id（shelfGroups[].ticketGroups[].id）
             → 父 shelfGroup.id → shelfTypes[].shelfItems[].shelfGroupId → shelfTypes[].id
- resourceAddInfo 响应里 `resources[].id` 等于 vendorInfo.resourceId；`productId` 是另一个字段
"""
from __future__ import annotations
import json
from typing import Optional
from . import selectors as S


def parse_round(raw_round: dict) -> dict:
    """一次扫描 → 标准化结果。"""
    captured_at = raw_round["capturedAt"]
    poi = raw_round["poi"]
    viewid = int(poi["viewid"])

    # 用 endpoint 路径部分做匹配（request.url 是相对路径如 "/restapi/soa2/21052/json/getProductShelf"）
    shelf_path = S.SHELF_URL.replace(S.BASE_URL, "")
    addinfo_path = S.ADDINFO_URL.replace(S.BASE_URL, "")
    price_cal_path = S.PRICE_CAL_URL.replace(S.BASE_URL, "")
    resource_detail_path = S.RESOURCE_DETAIL_PATH
    shelf_resource_list_path = S.SHELF_RESOURCE_LIST_PATH

    shelf_body = _find_body(raw_round, shelf_path)
    addinfo_bodies = _find_bodies(raw_round, addinfo_path)
    price_cal_bodies = _find_bodies(raw_round, price_cal_path)
    rdetail_bodies = _find_bodies(raw_round, resource_detail_path)
    srl_bodies = _find_bodies(raw_round, shelf_resource_list_path)

    # 1. 解析 shelf → 构建 rid → shelfType 映射（同时抽 minPriceRelationInfo 派生 parent / people）
    shelf_lookup = _parse_shelf(shelf_body, viewid) if shelf_body else {}

    # 2. 解析 addInfo → rid → vendor 映射
    vendor_map = _parse_addinfos(addinfo_bodies, set(shelf_lookup.keys()))

    # 3. 解析 resourceDetails → rid → peopleProperty（人群标签权威源）
    people_map = _parse_resource_details(rdetail_bodies)

    # 3.5 解析 getShelfResourceList → rid → {name, people_property}。官方页点 chip 时
    #     才触发；扩展 proactive fire 同 (rid, peoplePropertyId) 抓一次，把 sibling crowd
    #     child rid 的正确 name + 人群标签带回来。idList 是单 chip 粒度，所以一个 chip 一 body。
    crowd_map = _parse_shelf_resource_list(srl_bodies)

    # 4. 解析 priceCalendar → 每日价（注入 vendor_map 让每行带 winning_vendor_id + people_map 带人群）
    price_days = _parse_price_calendars(price_cal_bodies, vendor_map, people_map, shelf_lookup, crowd_map)

    # 5. 组装 SKU 列表
    skus = []
    covered = set()
    if vendor_map:
        # 正常路径：addInfo + shelf 都有，按 vendor 主键 join
        for rid, info in vendor_map.items():
            shelf = shelf_lookup.get(rid, {})
            crowd = crowd_map.get(rid) or {}
            # 人群标签：resourceDetails > getShelfResourceList > shelf.minPriceRelationInfo fallback
            people = (people_map.get(rid)
                      or crowd.get("people_property")
                      or shelf.get("people_property"))
            parent_rid = shelf.get("parent_resource_id")
            # full_name: getShelfResourceList.name 优先（chip 后缀），其次 vendor 段，最后 shelf
            srl_name = crowd.get("name")
            skus.append({
                "resource_id": rid,
                "primary_vendor_id": info["primary"]["vendorId"],
                "primary_vendor_name": info["primary"]["name"],
                "primary_vendor_brand": info["primary"]["brandCompanyName"],
                "primary_vendor_licence": info["primary"]["licenceNo"],
                "primary_vendor_licence_pic": info["primary"].get("licencePicUrl"),
                "display_price": shelf.get("display_price"),
                "full_name": _build_sku_name(shelf_lookup, rid, people,
                                              srl_name or info.get("full_name")),
                "shelf_type_id": shelf.get("shelf_type_id"),
                "shelf_type_name": shelf.get("shelf_type_name"),
                "spotid": shelf.get("spotid") or viewid,
                "market_price": shelf.get("market_price"),
                "first_booking_date": shelf.get("first_booking_date"),
                "sale_count": shelf.get("sale_count"),
                "parent_resource_id": parent_rid,
                "people_property": people,
                "raw_resource": shelf.get("raw"),
            })
            covered.add(rid)

    # Fallback / 补全：shelf 里出现但 vendor_map 没有的 rid（addInfo WAF 限速漏抓 → 没 vendor）
    # 仍然出 SKU 行，vendor 字段为 None，让 dashboard 能看到货架结构和人群分布。
    # 注意：之前是 `elif shelf_lookup`，意味着 vendor_map 只要有 ≥1 项就跳过整段 — sibling
    # crowd child rid（e.g. 110268325）会被悄悄丢弃。已修。
    for rid, shelf in shelf_lookup.items():
        if rid in covered:
            continue
        crowd = crowd_map.get(rid) or {}
        people = (people_map.get(rid)
                  or crowd.get("people_property")
                  or shelf.get("people_property"))
        parent_rid = shelf.get("parent_resource_id")
        srl_name = crowd.get("name")
        skus.append({
            "resource_id": rid,
            "primary_vendor_id": 0,  # 未抓到 addInfo 的占位
            "primary_vendor_name": None,
            "primary_vendor_brand": None,
            "primary_vendor_licence": None,
            "primary_vendor_licence_pic": None,
            "display_price": shelf.get("display_price"),
            "full_name": _build_sku_name(shelf_lookup, rid, people,
                                          srl_name or shelf.get("full_name")),
            "shelf_type_id": shelf.get("shelf_type_id"),
            "shelf_type_name": shelf.get("shelf_type_name"),
            "spotid": shelf.get("spotid") or viewid,
            "market_price": shelf.get("market_price"),
            "first_booking_date": shelf.get("first_booking_date"),
            "sale_count": shelf.get("sale_count"),
            "parent_resource_id": parent_rid,
            "people_property": people,
                "raw_resource": shelf.get("raw"),
            })

    # 5.5 getShelfResourceList sibling crowd child rid — 不在 shelf.resources[] 的 rid
    #     在前两轮 vendor_map / shelf_lookup 路径都不会被发；这里补齐它们为独立 SKU 行，
    #     vendor 字段空（暂时）但 full_name + people_property 完整。dashboard 后续能靠
    #     hourly round + resourceDetails 补 vendor。
    covered = {s["resource_id"] for s in skus}
    for rid, crowd in crowd_map.items():
        if rid in covered:
            continue
        srl_name = crowd.get("name")
        people = crowd.get("people_property") or people_map.get(rid)
        skus.append({
            "resource_id": rid,
            "primary_vendor_id": 0,
            "primary_vendor_name": None,
            "primary_vendor_brand": None,
            "primary_vendor_licence": None,
            "primary_vendor_licence_pic": None,
            "display_price": None,
            "full_name": srl_name or f"rid {rid}",
            "shelf_type_id": None,
            "shelf_type_name": None,
            "spotid": viewid,
            "market_price": None,
            "first_booking_date": None,
            "sale_count": None,
            "parent_resource_id": None,
            "people_property": people,
            "raw_resource": crowd.get("raw"),
        })

    return {
        "captured_at": captured_at,
        "viewid": viewid,
        "poi_name": poi.get("name"),
        "skus": skus,
        "price_days": price_days,
    }


def _build_sku_name(shelf_lookup: dict[int, dict], rid: int,
                    people_property: str | None,
                    fallback_full_name: str | None) -> str:
    """组装 sku_snapshot.full_name：直接用 shelf.fullName / vendor 段名字。
    父 · 人群 前缀组合 API 上无可靠父子链路（children minPriceRelationInfo
    指向自己而非 parent），所以不强行拼。都没有则用 rid。
    """
    own = (fallback_full_name or (shelf_lookup.get(rid) or {}).get("full_name") or "").strip()
    return own or (people_property or "").strip() or f"rid {rid}"


# ── shelf 解析：构建 rid → {display_price, full_name, shelf_type_id, ...} ──

def _parse_shelf(shelf_body: dict, viewid: int) -> dict[int, dict]:
    """从 getProductShelf 响应构建 rid → 元数据。

    四步：
    1. 遍历 resources[] 收集本 POI 的 rid + level1SaleUnitId
    2. 遍历 shelfGroups[] 构建 ticketGroupId → shelfGroupId 映射
    3. 遍历 shelfTypes[] 构建 shelfGroupId → shelfType(name, id)
    4. 从 resources[].minPriceRelationInfo 抽 peoplePropertyName 作 fallback（resourceDetails
       在 parse_round 阶段覆盖）。父 SKU（productIds > 1）people_property=None（自己
       是容器，没有具体人群）。children 的 mpri.resourceId 指向 self，与父无 API 链接，
       所以 parent_resource_id 暂不填 — 父子在 dashboard 通过 shelfType 自然聚合。
    """
    # Step 1: rid → level1SaleUnitId
    rid_to_l1: dict[int, dict] = {}
    for r in shelf_body.get("resources", []) or []:
        if r.get("spotid") != viewid:
            continue
        rid = r.get("resourceId")
        if not rid:
            continue
        rid_to_l1[rid] = {
            "resource_id": rid,
            "level1_sale_unit_id": r.get("level1SaleUnitId"),
            "product_ids": r.get("productIds") or [],
            # 兼容新旧字段：历史 raw 用 fullName，2026-08 起新 raw 用 name（API 调整）
            "full_name": r.get("fullName") or r.get("name"),
            "spotid": r.get("spotid"),
            "display_price": r.get("displayPrice"),
            "market_price": ((r.get("marketPriceInfo") or {}).get("price")),
            "first_booking_date": r.get("firstBookingDate"),
            "sale_count": ((r.get("statisticInfo") or {}).get("saleCount")),
            "raw": r,
        }

    # Step 2: ticketGroupId → shelfGroupId
    # shelfGroups[].ticketGroups[].id == level1SaleUnitId
    l1_to_shelf_group: dict[int, str] = {}
    for sg in shelf_body.get("shelfGroups", []) or []:
        sg_id = sg.get("id")
        if not sg_id:
            continue
        for tg in sg.get("ticketGroups", []) or []:
            tg_id = tg.get("id")
            if tg_id:
                l1_to_shelf_group[tg_id] = sg_id

    # Step 3: shelfGroupId → (shelfTypeId, shelfTypeName)
    shelf_group_to_type: dict[str, tuple[int, str]] = {}
    for st in shelf_body.get("shelfTypes", []) or []:
        st_id = st.get("id")
        st_name = st.get("name")
        for item in st.get("shelfItems", []) or []:
            sg_id = item.get("shelfGroupId")
            if sg_id and st_id is not None:
                shelf_group_to_type[sg_id] = (st_id, st_name)

    # 关联 rid → shelfType
    for rid, info in rid_to_l1.items():
        l1 = info.get("level1_sale_unit_id")
        if l1 and l1 in l1_to_shelf_group:
            sg_id = l1_to_shelf_group[l1]
            if sg_id in shelf_group_to_type:
                st_id, st_name = shelf_group_to_type[sg_id]
                info["shelf_type_id"] = st_id
                info["shelf_type_name"] = st_name

    # Step 4: 从 minPriceRelationInfo 抽 people_property fallback（resourceDetails 是权威源）。
    # 父 SKU（productIds > 1）自己就是容器，不带具体人群 — 不挂 people。
    # children 的 minPriceRelationInfo.resourceId 实测指向"本 L1 的可选人群入口 rid"
    # （= 上层父 rid，不是 self）。多个 sibling 共用同一 mpri.resourceId，无法用其反推
    # 唯一父子 — 故 parent_resource_id 暂不填，父子在 dashboard 通过 shelfType 自然聚合。
    for rid, info in rid_to_l1.items():
        r_raw = info.get("raw") or {}
        mpri = r_raw.get("minPriceRelationInfo") or {}
        product_ids = info.get("product_ids") or []
        if len(product_ids) > 1:
            info["people_property"] = None
        else:
            info["people_property"] = mpri.get("peoplePropertyName")
        info["parent_resource_id"] = None

    return rid_to_l1


# ── addInfo 解析：rid → vendor ──

def _parse_addinfos(bodies: list[dict], rids: set[int]) -> dict[int, dict]:
    """从 resourceAddInfo 响应构建 rid → {primary: vendorInfo}。

    响应结构（实测）：
    {
      "data": {
        "resources": [{
          "id": <resourceId>,
          "productId": <pid>,
          "vendorInfo": {vendorId, name, brandCompanyName, licenceNo,
                         rawLicencePicUrl, ...},
          "vendorInfos": [...]  # 复数 = 多 vendorId（组合产品）罕见
        }]
      }
    }
    """
    out = {}
    for b in bodies:
        if not b:
            continue
        for r in (b.get("data") or {}).get("resources", []) or []:
            rid = r.get("id")
            # 当 rids 为空（无 shelf 数据，server-scraper 场景），保留所有 vendor。
            # 否则只保留 shelf_lookup 里有匹配的 rid。
            if rids and rid not in rids:
                continue
            vi = r.get("vendorInfo") or {}
            if not vi or not vi.get("vendorId"):
                continue
            out[rid] = {
                "primary": {
                    "vendorId": vi.get("vendorId"),
                    "name": vi.get("name"),
                    "brandCompanyName": vi.get("brandCompanyName"),
                    "licenceNo": vi.get("licenceNo"),
                    "licencePicUrl": vi.get("rawLicencePicUrl") or vi.get("licencePicUrl"),
                },
                "full_name": r.get("name"),
            }
    return out


# ── priceCalendar 解析：(rid, date) → {price, inventory, available} ──

def _parse_price_calendars(bodies: list[dict],
                            vendor_map: dict[int, dict] | None = None,
                            people_map: dict[int, str] | None = None,
                            shelf_lookup: dict[int, dict] | None = None,
                            crowd_map: dict[int, dict] | None = None) -> list[dict]:
    """从 getProductPriceCalendar 响应构建每日价行。

    响应结构（实测 data/sample_price_calendar.json）：
    {
      "data": {
        "priceAndStockInfos": [
          {
            "date": "2026-08-25",
            "minPrice": 39,                       # daily min across packages
            "packagePriceAndStockInfos": [
              {
                "packageId": 54434100,
                "minPrice": 39,                   # package-level min
                "resourcePriceAndStockInfos": [
                  {
                    "resourceId": 54434101,
                    "salePrice": 39,              # 实际售价
                    "price": 39,                  # 与 salePrice 一致（每日挂牌价）
                    "marketPrice": 60.0,          # 门市价/原价
                    "inventoryNum": 100,          # 库存
                    "available": true,            # 当日是否可售
                    "discount": 0.35              # 折扣率
                  }
                ]
              }
            ]
          }
        ]
      }
    }

    输出每个 resourcePriceAndStockInfos 一行：
      resource_id, sale_date, package_id 来自所在层
      sale_price  ← salePrice (实价)
      min_price   ← None  (留 None；daily minPrice 在 day 层，未下沉到每行，
                          如需可由 dashboard 端聚合 day.minPrice)
      inventory   ← inventoryNum
      available   ← bool(available)
      market_price ← marketPrice (新增)
      discount     ← discount    (新增)
      raw          ← 整个 resource obj (含 marketPrice/discount 等所有字段)
    """
    out = []
    for b in bodies:
        if not b:
            continue
        data = b.get("data") or {}
        # 兼容旧 shape：priceAndStockInfos 不存在时静默返回空，不崩。
        days = data.get("priceAndStockInfos")
        if not days:
            continue
        for day in days:
            sale_date = day.get("date")
            for pkg in day.get("packagePriceAndStockInfos", []) or []:
                package_id = pkg.get("packageId")
                for r in pkg.get("resourcePriceAndStockInfos", []) or []:
                    rid = r.get("resourceId")
                    primary = (vendor_map or {}).get(rid, {}).get("primary") or {}
                    # 人群标签：resourceDetails > getShelfResourceList > shelf.minPriceRelationInfo
                    people = (people_map or {}).get(rid)
                    if not people and crowd_map:
                        people = (crowd_map.get(rid) or {}).get("people_property")
                    if not people and shelf_lookup:
                        people = shelf_lookup.get(rid, {}).get("people_property")
                    out.append({
                        "resource_id": rid,
                        "sale_date": sale_date,
                        "min_price": None,
                        "sale_price": r.get("salePrice"),
                        "inventory": r.get("inventoryNum"),
                        "available": bool(r.get("available")),
                        "package_id": package_id,
                        "market_price": r.get("marketPrice"),
                        "discount": r.get("discount"),
                        # winning_vendor_id 来自 addInfo 的 vendorMap：每个
                        # resource 绑定到一个 vendor；该 resource 在某日的
                        # 最低价 = 该 vendor 当日「胜出」。三色 cell 用它：
                        # 绿 = my_vids, 红 = watched_shelf ∉ my_vids,
                        # 灰 = has_vid 且 shelf 未关注。
                        "winning_vendor_id": primary.get("vendorId"),
                        "winning_vendor_name": primary.get("name"),
                        "people_property": people,
                        "raw": r,
                    })
    return out


# ── resourceDetails 解析：rid → peopleProperty ──

def _parse_resource_details(bodies: list[dict]) -> dict[int, str]:
    """从 resourceDetails 响应构建 rid → peopleProperty。

    每个 body 是单个 rid 的响应，data.peopleProperty 形如 "成人票"/"儿童票"/
    "老人票"/"不限人群"。当同一 rid 多次被抓，后到的覆盖（resourceDetails 是
    单 rid 接口，多次只可能来自不同抓轮，本轮以最后一次为准）。
    """
    out: dict[int, str] = {}
    for b in bodies:
        if not b:
            continue
        d = b.get("data") or {}
        rid = d.get("resourceId")
        pp = (d.get("peopleProperty") or "").strip()
        if rid is not None and pp:
            out[rid] = pp
    return out


# ── getShelfResourceList 解析：rid → {name, people_property}（chip 端点 sibling crowd） ──

def _parse_shelf_resource_list(bodies: list[dict]) -> dict[int, dict]:
    """从 getShelfResourceList 响应构建 rid → {name, people_property}。

    每个 chip 点击会 fire 这个端点，返回的 resources[].name 自带 crowd 后缀
    （"圆明园通票+智旅手册儿童票"），resources[].minPriceRelationInfo.peoplePropertyName
    给出人群标签。同 rid 多次出现后到的覆盖（chip 单粒度，但扩展 proactive fire 可能
    重复触发）。

    重要：sibling crowd 的 rid 不会出现在 shelf.resources[]（shelf 只返默认 crowd），
    所以这是补全 (可选人群) 子资源 rid 的唯一权威源。Server scraper 抓不到这个
    端点（要 w-payload-source），只能靠扩展。
    """
    out: dict[int, dict] = {}
    for b in bodies:
        if not b:
            continue
        for r in (b.get("resources") or []):
            rid = r.get("resourceId")
            if rid is None:
                continue
            mpri = r.get("minPriceRelationInfo") or {}
            name = (r.get("name") or "").strip() or None
            pp = (mpri.get("peoplePropertyName") or "").strip() or None
            out[int(rid)] = {"name": name, "people_property": pp, "raw": r}
    return out


# ── helpers ──

def _extract_body(r: dict) -> Optional[dict]:
    """从 request entry 取 JSON body。

    支持两种 shape：
    1) {"body": <已解析 JSON>}（脚本直接构造）
    2) {"response": {"bodyText": "<JSON string>"}}（Chrome 扩展抓的）
    """
    if "body" in r and isinstance(r["body"], (dict, list)):
        return r["body"]
    resp = r.get("response") or {}
    text = resp.get("bodyText")
    if isinstance(text, str):
        try:
            v = json.loads(text)
            return v if isinstance(v, (dict, list)) else None
        except Exception:
            return None
    return None


def _find_body(rr: dict, url_substr: str) -> Optional[dict]:
    for r in rr.get("requests", []):
        if url_substr in r.get("url", ""):
            return _extract_body(r)
    return None


def _find_bodies(rr: dict, url_substr: str) -> list[dict]:
    out = []
    for r in rr.get("requests", []):
        if url_substr in r.get("url", ""):
            b = _extract_body(r)
            if b is not None:
                out.append(b)
    return out


# ── 辅助：从 raw_round 抽取 cookies（用于服务器后台 scraper）──

def extract_cookies(raw_round: dict) -> dict[str, str]:
    """从 raw_round.cookies 字段返回 cookies dict（仅保留 REQUIRED）。"""
    raw = raw_round.get("cookies") or {}
    return {k: v for k, v in raw.items() if k in S.REQUIRED_COOKIES}


# ── rank_history 计算 ──

def compute_rank_history(conn, round_pk: int, viewid: int) -> list[dict]:
    """对当前 round 的每个 (shelf_type) 计算 RANK，返回写入 rank_history 的行。

    conn: sqlite3 connection（要求 row_factory=Row）
    """
    my_vids = {r["vendor_id"] for r in conn.execute(
        "SELECT vendor_id FROM my_vendors WHERE is_active=1").fetchall()}

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
                "shelf_type_name": r["shelf_type_name"],
                "full_name": r["full_name"],
            })
    return out