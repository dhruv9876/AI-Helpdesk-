"""Small query helpers shared by dashboard pages."""
from sqlalchemy import case

from app.models import Ticket, TicketPriority

PRIORITY_RANK = case(
    {TicketPriority.urgent: 4, TicketPriority.high: 3, TicketPriority.medium: 2, TicketPriority.low: 1},
    value=Ticket.priority,
    else_=0,
)


def bar_chart(points: list[dict], width: int = 1180, height: int = 230) -> dict:
    """Geometry for a simple SVG bar chart (no JavaScript, no chart library).

    `points` is [{"label": "5", "title": "5 Nov: 3 tickets", "count": 3}, ...]. Returns coordinates
    the template draws as <rect>/<text>. Attributes (not style=) are used so the strict CSP is happy.
    """
    left, right, top, bottom = 34, 10, 24, 30
    n = max(len(points), 1)
    peak = max([p["count"] for p in points] + [4])
    slot = (width - left - right) / n
    bar_w = slot * 0.62
    plot_h = height - top - bottom
    bars = []
    for i, p in enumerate(points):
        h = round(plot_h * p["count"] / peak, 1)
        bars.append({
            **p,
            "x": round(left + i * slot + (slot - bar_w) / 2, 1),
            "y": round(top + plot_h - h, 1),
            "w": round(bar_w, 1),
            "h": h,
            "cx": round(left + i * slot + slot / 2, 1),
            "last": i == n - 1,
        })
    return {"w": width, "h": height, "bars": bars, "base": top + plot_h, "peak": peak, "left": left, "right": width - right,
            "label_y": height - 9, "mid_y": round(top + plot_h / 2, 1)}


def hbar_rows(pairs: list[tuple[str, int]]) -> list[dict]:
    peak = max([n for _, n in pairs] + [1])
    return [{"label": label, "count": n, "pct": round(100 * n / peak, 1)} for label, n in pairs]
