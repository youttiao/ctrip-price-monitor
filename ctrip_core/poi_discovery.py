"""POI 发现：从 URL 抽取 viewId、从名称搜索候选 viewId。

不动 soa2 调用逻辑；只做规则化抽取 + 字符串匹配。
"""
from __future__ import annotations
import re
from urllib.parse import urlparse, parse_qs


# ?viewId=N  /  /sight/N.html  /  /N  /  /poi/N  都能抽
_VIEWID_PATTERNS = [
    re.compile(r"[?&]viewId=(\d{2,7})", re.IGNORECASE),
    re.compile(r"/sight/(\d{2,7})(?:[./?#]|$)", re.IGNORECASE),
    re.compile(r"/poi/(\d{2,7})(?:[./?#]|$)", re.IGNORECASE),
    re.compile(r"/spots?/(\d{2,7})(?:[./?#]|$)", re.IGNORECASE),
    re.compile(r"/(\d{4,7})(?:\.html?)?(?:[?#]|$)"),
]


def extract_viewid_from_url(url: str) -> int | None:
    """从任意 Ctrip H5 URL 抽 viewId。失败返回 None。

    支持：
      https://m.ctrip.com/restapi/soa2/14509/json/GetSightOverview?viewId=233
      https://m.ctrip.com/webapp/sight/233.html
      https://m.ctrip.com/webapp/poi/231.html
      https://piao.ctrip.com/sight/5208.html
    """
    if not url:
        return None
    # 先按整 URL 匹配（query 部分）
    for pat in _VIEWID_PATTERNS[:1]:
        m = pat.search(url)
        if m:
            return int(m.group(1))
    # 拆 path 再匹配（避免 query 中的 viewId 命中后跳过 path 段）
    try:
        u = urlparse(url.strip())
        path = u.path
        for pat in _VIEWID_PATTERNS[1:]:
            m = pat.search(path)
            if m:
                return int(m.group(1))
        # 再退一步：query 里可能有多个 viewId-like key
        qs = parse_qs(u.query)
        for k in ("viewId", "viewid", "view_id", "sightId"):
            if k in qs and qs[k]:
                try:
                    return int(qs[k][0])
                except (ValueError, TypeError):
                    continue
    except Exception:
        return None
    return None


def canonicalize_poi_name(raw: str) -> str:
    """清洗：去前后空白、常见括号内容、统一 '××公园' / '××景区' 形式。

    不做语义归一化（用户输入什么就给什么），只去无关空白。
    """
    if not raw:
        return ""
    return " ".join(raw.strip().split())