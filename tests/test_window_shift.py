"""POI 详情页窗口切换 — 按北京时间 18:00 平移到 T+1~T+8。

覆盖 `_resolve_window` 与 `_window_label` / `_window_note` 三个纯函数。
不依赖 DB / 网络。
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.routes.pages import _resolve_window, _window_label, _window_note


def _bj(year, month, day, hour):
    """构造一个带 Asia/Shanghai tzinfo 的 naive 输入，便于断言切换边界。"""
    return datetime(year, month, day, hour, 0, 0, tzinfo=None)


def test_resolve_window_before_cutoff_returns_seven_days():
    """17:59 还是今天的窗口：T+0 → T+6。"""
    assert _resolve_window(_bj(2026, 8, 25, 17)) == (0, 7)
    assert _resolve_window(_bj(2026, 8, 25, 0)) == (0, 7)
    assert _resolve_window(_bj(2026, 8, 25, 9)) == (0, 7)


def test_resolve_window_at_cutoff_shifts_to_tomorrow():
    """18:00 整点（晚场截止）即跳到 T+1 起 8 天。"""
    assert _resolve_window(_bj(2026, 8, 25, 18)) == (1, 8)


def test_resolve_window_after_cutoff_keeps_shifted():
    """18:00 之后任何时刻：依然 T+1~T+8。"""
    assert _resolve_window(_bj(2026, 8, 25, 19)) == (1, 8)
    assert _resolve_window(_bj(2026, 8, 25, 23)) == (1, 8)
    # 次日凌晨 hour 又回到 <18，规则自然回到 7 天窗口（不是 8 天延续）


def test_resolve_window_next_morning_resets_to_seven():
    """切换后的第二天凌晨：hour 又落到 <18，窗口回到 7 天（T+0~T+6）。"""
    assert _resolve_window(_bj(2026, 8, 26, 2)) == (0, 7)
    assert _resolve_window(_bj(2026, 8, 26, 9)) == (0, 7)


def test_resolve_window_without_arg_uses_bj_time(monkeypatch):
    """不传 now 时用 Asia/Shanghai 当前时刻。

    把内置 datetime.now 替成可控的 fake，验证默认分支真的会读 BJ 时区的小时。
    """
    import web.routes.pages as pages_mod

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                raise AssertionError("默认分支应当显式带 tz")
            return datetime(2026, 8, 25, 19, 30, tzinfo=tz)

    monkeypatch.setattr(pages_mod, "datetime", _FakeDatetime)
    assert _resolve_window() == (1, 8)


def test_window_label_default_seven():
    assert _window_label(0, 7) == "T+0~T+6"


def test_window_label_shifted_eight():
    assert _window_label(1, 8) == "T+1~T+8"


def test_window_note_only_when_shifted():
    assert _window_note(0) == ""
    assert _window_note(1) == "18:00 后跳过今天"
    # 防御性：未来如果扩展到 start_offset=2 也仍返回提示
    assert _window_note(2) == "18:00 后跳过今天"
