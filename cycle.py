"""Date-only character simulation. No hormone measurements or contraceptive claims."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PHASES = {"follicular": "卵泡期", "ovulation": "估算排卵日", "luteal": "黄体期"}
NOTICE = "角色周期的日历估算，不代表真实排卵、激素检测或医学诊断。所谓“安全期”并不安全，不能作为避孕依据。"
SOURCES = [
    {"title": "ACOG · 周期与易孕窗口", "url": "https://www.acog.org/womens-health/faqs/fertility-awareness-based-methods-of-family-planning"},
    {"title": "NHS · 月经周期与排卵时间", "url": "https://www.nhs.uk/conditions/periods/fertility-in-the-menstrual-cycle/"},
]


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name}必须为 {low}～{high} 的整数")
    return value


def make_snapshot(state: dict, timezone: str, cycle_offset: int = 0,
                  demo: bool = False, *, today: date | None = None) -> dict:
    """One-indexed days: O=L-14, fertile O-5..O+1 (inclusive).

    Menstruation is an overlay within the ovarian cycle, not a mutually exclusive
    fertility category. Boundaries always derive from one canonical cycle model.
    """
    _integer(cycle_offset, "周期偏移", -12, 12)
    if not isinstance(demo, bool):
        raise ValueError("演示开关必须为布尔值")
    if not isinstance(state, dict):
        raise ValueError("麦生活的生活状态配置格式无效")
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("麦生活的时区无效，请检查环境与天气设置") from None
    today = today or datetime.now(tz).date()
    length = _integer(state.get("body_cycle_length_days", 28), "周期长度", 20, 45)
    period = _integer(state.get("body_cycle_period_days", 5), "经期持续天数", 1, 10)
    enabled = state.get("body_cycle_enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("麦生活身体周期开关格式无效")
    start_text = state.get("body_cycle_start_date", "")
    base = {"source": "maibot-community.mai-life", "timezone": timezone,
            "today": today.isoformat(), "length": length, "period_days": period,
            "notice": NOTICE, "sources": SOURCES, "demo": demo,
            "model": "估算排卵日＝周期第（周期长度−14）天；易孕窗口＝该日前5天至后1天。固定周期重复计算，不预测激素水平。"}
    if demo:
        anchor = today
        current_index = 0
    else:
        if not enabled:
            return {**base, "status": "disabled", "message": "请在麦生活 → 系统与配置 → 生活状态中启用身体周期。"}
        if not start_text:
            return {**base, "status": "missing_date", "message": "麦生活尚未填写周期起始日期。请在生活状态中填写第1天对应的 YYYY-MM-DD 日期。"}
        try:
            if not isinstance(start_text, str) or len(start_text) != 10:
                raise ValueError()
            anchor = date.fromisoformat(start_text)
        except (TypeError, ValueError):
            return {**base, "status": "invalid_date", "message": "麦生活的周期起始日期无效，请填写 YYYY-MM-DD。"}
        current_index = max(0, (today - anchor).days // length)
    index = current_index + cycle_offset
    if index < 0:
        return {**base, "status": "before_anchor", "message": "所选周期早于配置的起始日期。", "can_previous": False, "can_next": True}
    try:
        start = anchor + timedelta(days=index * length)
        end = start + timedelta(days=length - 1)
        next_start = end + timedelta(days=1)
    except OverflowError:
        raise ValueError("所选周期日期超出支持范围") from None
    ovulation = length - 14
    fertile_start, fertile_end = max(1, ovulation - 5), min(length, ovulation + 1)
    today_day = (today - start).days + 1
    in_view = 1 <= today_day <= length and not demo
    days = []
    for n in range(1, length + 1):
        phase = "follicular" if n < ovulation else "ovulation" if n == ovulation else "luteal"
        fertile = fertile_start <= n <= fertile_end
        days.append({"day": n, "date": (start + timedelta(days=n - 1)).isoformat(),
                     "phase": phase, "phase_label": PHASES[phase], "menstruation": n <= period,
                     "fertile": fertile, "fertility_label": "估算易孕窗口" if fertile else "较低受孕概率时段（非安全保证）",
                     "today": in_view and n == today_day})
    def span(key, label, first, last):
        return {"key": key, "label": label, "start_day": first, "end_day": last,
                "start_date": days[first - 1]["date"], "end_date": days[last - 1]["date"]}
    spans = [span("menstruation", "经期", 1, period),
             span("follicular", "卵泡期（包含经期）", 1, ovulation - 1),
             span("ovulation", "估算排卵日", ovulation, ovulation),
             span("luteal", "黄体期", ovulation + 1, length),
             span("fertile", "估算易孕窗口", fertile_start, fertile_end)]
    low_ranges = []
    if fertile_start > 1:
        low_ranges.append(span("lower", "较低受孕概率时段", 1, fertile_start - 1))
    if fertile_end < length:
        low_ranges.append(span("lower", "较低受孕概率时段", fertile_end + 1, length))
    current = next((item for item in days if item["today"]), None)
    return {**base, "status": "demo" if demo else "ready", "cycle_offset": cycle_offset,
            "start": start.isoformat(), "end": end.isoformat(), "next_start": next_start.isoformat(),
            "anchor": anchor.isoformat(), "ovulation_day": ovulation,
            "ovulation_date": days[ovulation - 1]["date"], "days": days, "spans": spans,
            "lower_ranges": low_ranges, "current": current,
            "pending": not demo and today < anchor,
            "can_previous": index > 0 and cycle_offset > -12, "can_next": cycle_offset < 12,
            "overlap_note": "经期与估算易孕窗口存在重叠，不能把经期当作安全期。" if fertile_start <= period else ""}
