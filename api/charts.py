"""Small inline SVG charts (no JavaScript). Colours come from CSS variables, so
they follow the page's light/dark theme."""
from html import escape


def line(points, height=120, money=False, max_value=None):
    """points: [(label, value)] oldest first. Returns an SVG string ('' if fewer than 2 values)."""
    pts = [(str(l), float(v)) for l, v in points if v is not None]
    if len(pts) < 2:
        return ""
    w, h, pad_l, pad_b, pad_t = 600, height, 64, 22, 10
    vals = [v for _, v in pts]
    lo = min(0.0, min(vals))
    hi = max_value if max_value is not None else max(vals)
    if hi <= lo:
        hi = lo + 1
    n = len(pts)

    def x(i):
        return pad_l + (w - pad_l - 8) * i / (n - 1)

    def y(v):
        return pad_t + (h - pad_t - pad_b) * (1 - (v - lo) / (hi - lo))

    def fmt(v):
        return f"£{v:,.0f}" if money else f"{v:,.0f}"

    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(pts))
    dots = "".join(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.5"><title>{escape(l[:10])}: {escape(fmt(v))}'
                   f'</title></circle>' for i, (l, v) in enumerate(pts))
    grid = "".join(f'<line x1="{pad_l}" x2="{w - 8}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                   f'<text x="{pad_l - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{escape(fmt(v))}</text>'
                   for v in (lo, (lo + hi) / 2, hi))
    labels = (f'<text x="{pad_l}" y="{h - 4}">{escape(pts[0][0][:10])}</text>'
              f'<text x="{w - 8}" y="{h - 4}" text-anchor="end">{escape(pts[-1][0][:10])}</text>')
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" role="img">'
            f'{grid}{labels}<path d="{path}" class="line"/>{dots}</svg>')
