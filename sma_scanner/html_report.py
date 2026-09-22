"""Self-contained HTML report for scan results.

One file, no external assets or CDN - it renders correctly offline and can
be dropped on any static host or opened straight from disk.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .scanner import ScanResult

_CSS = """
:root{--bg:#0f1216;--card:#171b21;--line:#252b33;--fg:#e6e9ee;--dim:#98a2b3;
--accent:#4da3ff;--good:#3ddc97;--warn:#ffb454;--bad:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;padding:32px 24px;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto}
h1{margin:0 0 4px;font-size:24px;letter-spacing:-.3px}
.sub{color:var(--dim);font-size:13px;margin-bottom:24px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 18px;min-width:120px;flex:1}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:10px 12px;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
th{background:#1b2129;color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.5px;cursor:pointer;user-select:none;position:sticky;top:0}
th:hover{color:var(--accent)}
th:first-child,td:first-child{text-align:left}
tbody tr{border-top:1px solid var(--line)}
tbody tr:hover{background:#1c222b}
.sym{font-weight:600}
.r-strong{color:var(--good);font-weight:600}
.r-normal{color:var(--fg)}
.r-weak{color:var(--warn)}
.empty{padding:28px;text-align:center;color:var(--dim)}
.note{margin-top:16px;color:var(--dim);font-size:12px;line-height:1.7}
.legend{margin-top:14px;color:var(--dim);font-size:12px}
.legend b{color:var(--fg)}
footer{margin-top:28px;padding-top:14px;border-top:1px solid var(--line);
color:var(--dim);font-size:12px}
"""

_JS = """
document.querySelectorAll('th').forEach((th,i)=>{th.onclick=()=>{
const t=th.closest('table'),tb=t.tBodies[0],rows=[...tb.rows],
dir=th.dataset.dir==='asc'?-1:1;
th.dataset.dir=dir===1?'asc':'desc';
rows.sort((a,b)=>{const x=a.cells[i].dataset.v||a.cells[i].textContent,
y=b.cells[i].dataset.v||b.cells[i].textContent;
const nx=parseFloat(x),ny=parseFloat(y);
if(!isNaN(nx)&&!isNaN(ny))return (nx-ny)*dir;
return String(x).localeCompare(String(y))*dir;});
rows.forEach(r=>tb.appendChild(r));};});
"""


def _num(value: Any, spec: str = ".2f") -> str:
    if value is None:
        return "-"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return escape(str(value))
    if f != f:
        return "-"
    return format(f, spec)


def _ratio_class(ratio: Any) -> str:
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return "r-normal"
    if r != r:
        return "r-normal"
    if r >= 1.5:
        return "r-strong"
    if r < 0.8:
        return "r-weak"
    return "r-normal"


def write_html(
    res: ScanResult,
    path: str,
    *,
    title: str = "SMA Breakout Scan",
    generated: datetime | None = None,
    extra_note: str = "",
) -> str:
    """Render the scan as a standalone HTML file. Returns the path written."""
    out = Path(str(path)).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    generated = generated or datetime.now()
    matches = res.matches

    cards = [
        ("Matches", str(res.n_matches)),
        ("Universe", str(res.universe_size)),
        ("Evaluated", str(res.evaluated)),
        ("Data source", escape(res.data_source or "-")),
    ]

    head = "".join(
        f'<div class="card"><div class="k">{escape(k)}</div><div class="v">{v}</div></div>'
        for k, v in cards
    )

    if matches:
        cols = [
            ("Symbol", "sym"), ("Price", "price"), ("SMA20", "sma_fast"),
            ("SMA50", "sma_slow"), ("Spread %", "spread_pct"), ("Crossed", "cross_date"),
            ("Bars ago", "bars_since_cross"), ("Vol x avg", "volume_ratio"),
            ("SMA200", "sma_200"),
        ]
        thead = "".join(
            f'<th data-dir="desc">{escape(label)}</th>' for label, _ in cols
        )
        body_rows: List[str] = []
        for m in matches:
            mt = m.metrics
            ratio = mt.get("volume_ratio")
            ratio_txt = (
                "-" if ratio is None or ratio != ratio else f"{float(ratio):.2f}x"
            )
            cells = []
            for label, key in cols:
                val = m.symbol if key == "sym" else mt.get(key)
                if key == "sym":
                    cells.append(f'<td class="sym" data-v="{escape(m.symbol)}">'
                                 f'{escape(m.symbol)}</td>')
                elif key == "volume_ratio":
                    cls = _ratio_class(ratio)
                    cells.append(f'<td class="{cls}" data-v="{_num(val, ".4f")}">'
                                 f'{ratio_txt}</td>')
                elif key == "bars_since_cross":
                    cells.append(f'<td data-v="{_num(val, ".0f")}">{_num(val, ".0f")}</td>')
                elif key == "cross_date":
                    txt = str(val)[:10] if val is not None else "-"
                    cells.append(f'<td data-v="{escape(txt)}">{escape(txt)}</td>')
                else:
                    cells.append(f'<td data-v="{_num(val)}">{_num(val)}</td>')
            body_rows.append("<tr>" + "".join(cells) + "</tr>")
        table = (
            f'<table><thead><tr>{thead}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table>'
        )
    else:
        table = '<div class="empty">No symbols matched the configured conditions.</div>'

    note = (
        "<b>Vol x avg</b> = breakout-day volume / average volume of the 20 bars "
        "before it. Above 1.0x means the cross came on above-normal participation; "
        "below 0.8x suggests weak conviction."
    )
    if extra_note:
        note += f"<br>{escape(extra_note)}"

    filters = escape(", ".join(res.filters_used) or "(none)")
    html = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><div class='wrap'>"
        f"<h1>{escape(title)}</h1>"
        f"<div class='sub'>Generated {generated.strftime('%Y-%m-%d %H:%M:%S')} "
        f"&middot; filters: {filters}</div>"
        f"<div class='cards'>{head}</div>"
        f"{table}"
        f"<div class='legend'>{note}</div>"
        "<div class='note'>Click any column header to sort. Signals are computed "
        "from daily bars; if the report is generated before the market close, the "
        "current day's bar is still forming.</div>"
        "<footer>Screening tool output - not investment advice.</footer>"
        "</div>"
        f"<script>{_JS}</script></body></html>"
    )
    out.write_text(html, encoding="utf-8")
    return str(out)
