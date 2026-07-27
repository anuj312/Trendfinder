# heatmap_impl.py
#
# Plotly Treemap Heatmap builder
# - Tight packing (branchvalues="total")
# - Leaf colors: deep red/green (custom)
# - Sector headers drawn in JS (assets/heatmap_fonts.js)
#
# Per sector:
#   1) SELECT: Top N by abs(%Change) so big losers are included
#   2) ORDER: Biggest tiles first (size_col DESC), then smaller
#      (OTHERS, if present, forced to last)

import os
from typing import Any, Dict, List, Tuple

import pandas as pd
import plotly.graph_objects as go


# =============================================================================
# CONFIG
# =============================================================================
HEATMAP_TOP_N_PER_SECTOR = int(os.getenv("HEATMAP_TOP_N_PER_SECTOR", "18"))
HEATMAP_ADD_OTHERS = os.getenv("HEATMAP_ADD_OTHERS", "0").strip().lower() not in ("0", "false", "no")
HEATMAP_PACKING = os.getenv("HEATMAP_PACKING", "squarify").strip()
HEATMAP_SECTOR_POWER = float(os.getenv("HEATMAP_SECTOR_POWER", "2.5"))

# abs_pct | pos_pct | turnover
HEATMAP_STOCK_SIZE_METRIC = os.getenv("HEATMAP_STOCK_SIZE_METRIC", "abs_pct").strip()
HEATMAP_MAX_STOCK_LABEL_CHARS = int(os.getenv("HEATMAP_MAX_STOCK_LABEL_CHARS", "9"))

_BG = "rgba(0,0,0,0)"   # transparent -> CSS controls visible background
_LINE = "#000000"


# =============================================================================
# HELPERS
# =============================================================================
def _empty_fig(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        margin=dict(l=6, r=6, t=6, b=6),
        title=None,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        annotations=[dict(text=msg, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper")],
    )
    return fig


def _unicode_bold_char(c: str) -> str:
    o = ord(c)
    if 65 <= o <= 90:
        return chr(0x1D400 + (o - 65))
    if 97 <= o <= 122:
        return chr(0x1D41A + (o - 97))
    if 48 <= o <= 57:
        return chr(0x1D7CE + (o - 48))
    return c


def unicode_bold(s: str) -> str:
    return "".join(_unicode_bold_char(c) for c in str(s))


def heatmap_short_symbol(sym: str, max_len: int = HEATMAP_MAX_STOCK_LABEL_CHARS) -> str:
    sym = str(sym)
    if sym == "OTHERS":
        return sym
    return sym if len(sym) <= max_len else sym[: max_len - 1] + "…"


def _hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def _lerp_color(c0: str, c1: str, t: float) -> str:
    t = max(0.0, min(1.0, float(t)))
    r0, g0, b0 = _hex_to_rgb(c0)
    r1, g1, b1 = _hex_to_rgb(c1)
    return _rgb_to_hex(_lerp(r0, r1, t), _lerp(g0, g1, t), _lerp(b0, b1, t))


def pct_to_color(pct: float, mx: float) -> str:
    """
    Deep maroon for negatives, bright green for positives, near-black around 0.
    """
    if mx <= 1e-9:
        return "#151515"

    v = float(pct)
    t = min(1.0, abs(v) / mx)

    # small moves darker, big moves pop
    t = t ** 0.65

    if v > 0:
        return _lerp_color("#0b2416", "#1fa83f", t)
    if v < 0:
        return _lerp_color("#2a1417", "#9b2f3a", t)
    return "#111111"


def _topn_plus_others_heatmap(sdf: pd.DataFrame, n: int, add_others: bool, size_col: str) -> pd.DataFrame:
    """
    sdf must already be sorted by abs_pct DESC (selection order).
    - Always keeps top-n.
    - If add_others=True, adds an "OTHERS" aggregate row for the rest.
    - If add_others=False, rest are dropped.
    """
    if n <= 0 or len(sdf) <= n:
        return sdf

    top = sdf.iloc[:n].copy()
    rest = sdf.iloc[n:].copy()

    if add_others and not rest.empty:
        wsum = float(rest[size_col].sum())
        if wsum <= 1e-9:
            w = (rest["abs_pct"] + 0.01).astype(float)
            wsum = float(w.sum())
        else:
            w = rest[size_col].astype(float)

        others_pct = float((rest["pct"].astype(float) * w).sum() / (wsum + 1e-9))
        others_dirr = float((rest["dirr"].astype(float) * w).sum() / (wsum + 1e-9))
        others_turn = float(rest["turnover"].sum())

        top = pd.concat(
            [
                top,
                pd.DataFrame([{
                    "sector_key": str(rest.iloc[0]["sector_key"]),
                    "sector_label": str(rest.iloc[0]["sector_label"]),
                    "symbol": "OTHERS",
                    "pct": others_pct,
                    "dirr": others_dirr,
                    "turnover": others_turn,
                    "abs_pct": float(rest["abs_pct"].sum()),
                    "pos_pct": float(rest["pos_pct"].sum()),
                }]),
            ],
            ignore_index=True,
        )

    return top


# =============================================================================
# MAIN FIGURE BUILDER
# =============================================================================
def build_market_heatmap_figure(rows: List[Dict[str, Any]]) -> go.Figure:
    if not rows:
        return _empty_fig("Heatmap warming up…")

    df = pd.DataFrame(rows)
    if df.empty:
        return _empty_fig("Heatmap: no data yet")

    required = {"sector_key", "sector_label", "symbol", "pct", "dirr"}
    if not required.issubset(set(df.columns)):
        return _empty_fig(f"Heatmap: missing {sorted(required - set(df.columns))}")

    if "turnover" not in df.columns:
        df["turnover"] = pd.to_numeric(df.get("value"), errors="coerce").fillna(0.0)

    df["pct"] = pd.to_numeric(df["pct"], errors="coerce")
    df["dirr"] = pd.to_numeric(df["dirr"], errors="coerce")
    df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce").fillna(0.0)

    df = df.dropna(subset=["sector_key", "sector_label", "symbol", "pct", "dirr"])
    if df.empty:
        return _empty_fig("Heatmap: waiting…")

    # sizing helper columns
    df["abs_pct"] = df["pct"].abs()
    df["pos_pct"] = df["pct"].clip(lower=0.0)

    size_col = HEATMAP_STOCK_SIZE_METRIC
    if size_col not in ("abs_pct", "pos_pct", "turnover"):
        size_col = "abs_pct"

    # Sector order: ABS(mean DirR) desc, tie: + before -
    sec_mean_dirr = df.groupby("sector_key")["dirr"].mean().to_dict()

    def _v(sec: str) -> float:
        try:
            return float(sec_mean_dirr.get(sec, 0.0) or 0.0)
        except Exception:
            return 0.0

    sector_order = sorted(sec_mean_dirr.keys(), key=lambda s: (abs(_v(s)), _v(s)), reverse=True)
    if not sector_order:
        return _empty_fig("Heatmap: no sectors")

    # Sector area by rank^power
    nsec = len(sector_order)
    sector_weight: Dict[str, float] = {}
    for i, sec in enumerate(sector_order):
        rank_val = float(max(1, nsec - i))
        sector_weight[sec] = rank_val ** float(HEATMAP_SECTOR_POWER)

    mx = float(max(0.5, df["pct"].abs().max()))

    labels: List[str] = []
    texts: List[str] = []
    ids: List[str] = []
    parents: List[str] = []
    values: List[float] = []
    colors: List[str] = []
    customdata: List[List[float]] = []  # [turnover, dirr, pct]

    top_n = int(HEATMAP_TOP_N_PER_SECTOR)
    add_others = bool(HEATMAP_ADD_OTHERS)

    for sec in sector_order:
        sdf = df[df["sector_key"] == sec].copy()
        if sdf.empty:
            continue

        # 1) SELECT by abs move so big losers are included
        sdf.sort_values("abs_pct", ascending=False, inplace=True)

        if top_n > 0:
            sdf = _topn_plus_others_heatmap(sdf, n=top_n, add_others=add_others, size_col=size_col)

        # 2) ORDER leaves so biggest tiles come first
        leaf_df = sdf.copy()
        leaf_df["__is_others"] = (leaf_df["symbol"].astype(str) == "OTHERS").astype(int)
        leaf_df.sort_values(
            by=["__is_others", size_col, "abs_pct", "pct"],
            ascending=[True, False, False, False],
            inplace=True,
        )

        sec_label = str(leaf_df.iloc[0]["sector_label"])
        sec_id = f"sec:{sec}"  # JS uses this to draw header bar
        w_sec = float(sector_weight.get(sec, 1.0))

        # sector container node
        labels.append(sec_label)
        texts.append(unicode_bold(sec_label))
        ids.append(sec_id)
        parents.append("")
        values.append(w_sec)
        colors.append(_BG)  # transparent
        customdata.append([
            float(leaf_df["turnover"].sum()),
            float(leaf_df["dirr"].mean()),
            float(leaf_df["pct"].mean()),
        ])

        # leaf sizing within sector
        w = leaf_df[size_col].astype(float)
        if float(w.sum()) <= 1e-9:
            w = (leaf_df["abs_pct"].astype(float) + 0.01)
        wsum = float(w.sum())

        for _, r in leaf_df.iterrows():
            sym = str(r["symbol"])
            wi = float(r.get(size_col) or 0.0)
            leaf_area = (wi / (wsum + 1e-9)) * w_sec

            labels.append(sym)
            texts.append(heatmap_short_symbol(sym))
            ids.append(f"{sec}:{sym}")
            parents.append(sec_id)
            values.append(float(leaf_area))
            colors.append(pct_to_color(float(r["pct"]), mx))
            customdata.append([float(r["turnover"]), float(r["dirr"]), float(r["pct"])])

    if not labels:
        return _empty_fig("Heatmap: nothing to render")

    fig = go.Figure(
        go.Treemap(
            labels=labels,
            text=texts,
            texttemplate="%{text}",
            textinfo="text",
            textfont=dict(size=10, color="#ffffff"),
            ids=ids,
            parents=parents,
            values=values,
            customdata=customdata,
            marker=dict(colors=colors, line=dict(width=2.0, color=_LINE)),
            branchvalues="total",
            sort=False,  # we control ordering ourselves
            tiling=dict(packing=HEATMAP_PACKING, pad=1),
            hovertemplate=(
                "<b>%{label}</b>"
                "<br>% chg: %{customdata[2]:.2f}%"
                "<br>Turnover: %{customdata[0]:,.0f}"
                "<br>DirR: %{customdata[1]:.2f}"
                "<extra></extra>"
            ),
            pathbar=dict(visible=False),
            root_color=_BG,
        )
    )

    fig.update_layout(
        template="plotly_dark",
        autosize=True,
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        margin=dict(l=6, r=6, t=6, b=6),
        uniformtext_minsize=9,
        uniformtext_mode="hide",
        title=None,
        hoverlabel=dict(
            bgcolor="#ffffff",
            bordercolor="#e6e6e6",
            font=dict(color="#111111", size=16),
        ),
    )
    return fig