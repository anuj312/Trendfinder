# dashboard_impl.py
#
# TurboTrades Dashboard implementation (NO AUTH here)
# - Exposes:
#     server               -> Dash WSGI server (mounted by FastAPI wrapper at /dash)
#     openinterest         -> optioninterest module (mounted by FastAPI wrapper at /openinterest)
#     async _startup()     -> start threads + openinterest startup
#     async _shutdown()    -> openinterest shutdown
#
# Adds: Heatmap (Treemap) below Top 15 Gainers/Losers
# - Color: %Change (from Open)
# - Tile size: Turnover proxy = LTP * VolumeTraded
# - Sector order: sorted by sector average momentum mean (DirR)
# - Includes ALL sectors & ALL stocks exactly as in SECTOR_DEFINITIONS
#   (so duplicates like NIFTY_50 are included too)

import os
import time
import threading
import logging
from collections import deque
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Dict, Any, Tuple, List
from urllib.parse import unquote
from zoneinfo import ZoneInfo
from pathlib import Path
import json

import pandas as pd

import dash
from dash import dcc, html, Input, Output, State
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

from kiteconnect import KiteConnect, KiteTicker

# OpenInterest FastAPI app (mounted by wrapper)
import optioninterest as openinterest
from heatmap_impl import build_market_heatmap_figure
from dash import dcc, html, Input, Output, State, ctx

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("turbotrades.dashboard")


# =============================================================================
# CONFIG
# =============================================================================
BASE = "/dash/"
IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("KITE_API_KEY", "").strip()
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "").strip()
if not API_KEY or not ACCESS_TOKEN:
    raise RuntimeError("Missing KITE_API_KEY / KITE_ACCESS_TOKEN environment variables.")

SEED_SLEEP_SEC = float(os.getenv("SEED_SLEEP_SEC", "0.35"))
LOOKBACK_SESSIONS = 20

# Hot Now window
HOT_WINDOW_SEC = 5 * 60
HOT_SAMPLE_SEC = 5
HOT_HISTORY_MAX_SEC = HOT_WINDOW_SEC + 10 * 60

# Hot Now filters
HOT_MIN_RET_PCT = float(os.getenv("HOT_MIN_RET_PCT", "0.25"))
HOT_MIN_RANGE_PCT = float(os.getenv("HOT_MIN_RANGE_PCT", "0.40"))

HVHR_N = int(os.getenv("HVHR_N", "20"))
HVHR_RFACTOR_Q = float(os.getenv("HVHR_RFACTOR_Q", "0.85"))

# PCR (NFO)
PCR_STRIKES_AROUND_ATM = int(os.getenv("PCR_STRIKES_AROUND_ATM", "12"))
PCR_CACHE_TTL_SEC = int(os.getenv("PCR_CACHE_TTL_SEC", "20"))
PCR_QUOTE_CHUNK = int(os.getenv("PCR_QUOTE_CHUNK", "180"))
NIFTY_SPOT_SYMBOL = os.getenv("NIFTY_SPOT_SYMBOL", "NSE:NIFTY 50")

# Background compute cadence
COMPUTE_CORE_EVERY_SEC = float(os.getenv("COMPUTE_CORE_EVERY_SEC", "2.0"))
COMPUTE_HOT_EVERY_SEC = float(os.getenv("COMPUTE_HOT_EVERY_SEC", "5.0"))
COMPUTE_PCR_EVERY_SEC = float(os.getenv("COMPUTE_PCR_EVERY_SEC", "5.0"))
COMPUTE_SLEEP_SEC = float(os.getenv("COMPUTE_SLEEP_SEC", "0.20"))

SECTOR_PLOT_H_PX = int(os.getenv("SECTOR_PLOT_H_PX", "360"))

# Pacing curve
PACE_CURVE_READY = False
PACE_CUM_FRAC_MIN: List[float] = []
PACE_BUILD_STARTED = False
PACE_LOCK = threading.Lock()
PACE_CACHE_PATH = Path("/tmp/pace_curve_cache.json")

# Recency
RECENCY_WINDOW_SEC = int(os.getenv("RECENCY_WINDOW_SEC", "900"))
RECENCY_WEIGHT = float(os.getenv("RECENCY_WEIGHT", "0.50"))

RECENCY_WINDOWS = [
    (300,  0.40),   # 5  min
    (900,  0.35),   # 15 min
    (1800, 0.25),   # 30 min
]


# =============================================================================
# KITE INIT
# =============================================================================
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)


# =============================================================================
# SECTORS / SYMBOLS
# =============================================================================
SECTOR_DEFINITIONS = {
    "METAL": [
        "ADANIENT", "APLAPOLLO", "BHARATFORG", "COALINDIA",
        "HINDALCO", "HINDZINC", "JSWSTEEL",
        "JINDALSTEL", "NMDC", "NATIONALUM",
        "SAIL", "TATASTEEL", "VEDL"
    ],
    "REALTY": [
        "PHOENIXLTD", "GODREJPROP", "LODHA",
        "OBEROIRLTY", "DLF", "PRESTIGE",
        "NBCC", "RVNL", "HUDCO"
    ],
    "ENERGY": [
        "RELIANCE", "ONGC", "IOC", "BPCL", "OIL",
        "NTPC", "POWERGRID", "POWERINDIA",
        "TATAPOWER", "TORNTPOWER", "JSWENERGY",
        "ADANIGREEN", "ADANIENSOL",
        "NHPC", "IREDA", "SUZLON", "INOXWIND",
        "WAAREEENER", "PREMIERENE",
        "PETRONET", "GAIL", "HINDPETRO"
    ],
    "AUTO": [
        "BOSCHLTD", "TIINDIA", "HEROMOTOCO",
        "M&M", "EICHERMOT", "EXIDEIND",
        "BAJAJ-AUTO", "ASHOKLEY",
        "MARUTI", "TVSMOTOR",
        "MOTHERSON", "SONACOMS",
        "UNOMINDA", "TMPV", "HYUNDAI", "AMBER"
    ],
    "IT": [
        "INFY", "TCS", "HCLTECH", "WIPRO",
        "TECHM", "LTM", "MPHASIS",
        "KPITTECH", "COFORGE", "PERSISTENT",
        "TATAELXSI", "OFSS", "CAMS",
        "TATATECH", "NAUKRI", "KAYNES"
    ],
    "PHARMA": [
        "CIPLA", "ALKEM", "BIOCON", "DRREDDY",
        "MANKIND", "TORNTPHARM", "ZYDUSLIFE",
        "DIVISLAB", "LUPIN", "PPLPHARMA",
        "LAURUSLABS", "FORTIS",
        "AUROPHARMA", "GLENMARK",
        "SUNPHARMA", "SYNGENE",
        "MAXHEALTH", "APOLLOHOSP"
    ],
    "FMCG": [
        "HINDUNILVR", "ITC", "NESTLEIND",
        "BRITANNIA", "DABUR", "MARICO",
        "COLPAL", "GODREJCP",
        "TATACONSUM", "PATANJALI",
        "UNITDSPR", "RADICO"
        "VBL", "DMART", "NYKAA",
        "ETERNAL", "SWIGGY",
        "TITAN", "TRENT", "VMM",
        "KALYANKJIL", "JUBLFOOD",
        "ASIANPAINT"
    ],
    "CEMENT": [
        "ULTRACEMCO", "SHREECEM",
        "AMBUJACEM", "DALBHARAT",
        "GRASIM", "ASTRAL",
        "PIDILITIND", "SUPREMEIND"
    ],
    "FINSERVICE": [
        "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG",
        "ICICIPRULI", "ICICIGI", "SBILIFE",
        "HDFCLIFE", "LICI", "LICHSGFIN",
        "PNBHOUSING", "MUTHOOTFIN",
        "MANAPPURAM", "CHOLAFIN",
        "PFC", "RECLTD","MOTILALOFS",
        "HDFCAMC", "360ONE",
        "KFINTECH", "NUVAMA",
        "PAYTM", "POLICYBZR",
        "IIFL", "SBICARD",
        "JIOFIN", "SHRIRAMFIN",
        "SAMMAANCAP", "ANGELONE",
        "BSE", "CDSL", "MCX", "IRFC"
    ],
    "BANK": [
        "HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK",
        "IDFCFIRSTB", "FEDERALBNK", "INDUSINDBK",
        "AUBANK", "BANDHANBNK", "RBLBANK",
    ],
    "PSUBANK": [
        "SBIN", "PNB", "BANKBARODA", "CANBK",
        "UNIONBANK", "BANKINDIA", "INDIANB",
    ],
    "TELECOM": [
        "BHARTIARTL", "INDUSTOWER",
        "HAVELLS", "KEI", "POLYCAB",
        "CROMPTON", "VOLTAS",
        "PGEL", "DIXON", "SRF"
    ],
    "LOGISTICS": [
        "CONCOR", "DELHIVERY", "INDIGO",
        "INDHOTEL", "IRCTC",
        "BLUESTARCO", "GMRAIRPORT",
        "PAGEIND", "UPL", "ADANIPORTS"
    ],
    "DEFENCE": [
        "ABB", "BDL", "BEL", "BHEL",
        "CGPOWER", "CUMMINSIND",
        "HAL", "LT", "MAZDOCK",
        "SIEMENS", "SOLARINDS"
    ],
    "NIFTY_50": [
        "ADANIENT", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE",
        "BAJAJFINSV", "BEL", "BHARTIARTL", "BPCL", "CIPLA", "COALINDIA",
        "DRREDDY", "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
        "HINDALCO", "HINDUNILVR", "ICICIBANK", "INFY", "INDIGO", "ITC",
        "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI",
        "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE",
        "SBILIFE", "SHRIRAMFIN", "SBIN", "SUNPHARMA", "TCS", "TATACONSUM",
        "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
        "TMPV", "ETERNAL"
    ],
}

ALL_SYMBOLS = sorted(set(sum(SECTOR_DEFINITIONS.values(), [])))

# Load instruments (NSE) and map to tokens/names
ins = pd.DataFrame(kite.instruments("NSE"))
ins = ins[ins["tradingsymbol"].isin(ALL_SYMBOLS)].copy()
symbol_to_token: Dict[str, int] = dict(zip(ins["tradingsymbol"], ins["instrument_token"]))
symbol_to_name: Dict[str, str] = (
    dict(zip(ins["tradingsymbol"], ins["name"])) if "name" in ins.columns else {s: "" for s in ALL_SYMBOLS}
)
TOKENS = sorted(symbol_to_token.values())


# =============================================================================
# LIVE / STATE (tick thread writes these)
# =============================================================================
LOCK = threading.Lock()

LAST_PRICE: Dict[int, float] = {}
DAY_VOL: Dict[int, float] = {}
LAST_OHLC: Dict[int, dict] = {}

LAST_TICK_TS = 0.0
LAST_TICK_DT: Optional[datetime] = None
TOTAL_TICKS = 0

TPS_WINDOW_SEC = 1.0
TPS_BUCKETS = deque()

HOT_HISTORY: Dict[int, deque] = {}  # token -> deque[(epoch, ltp, cumvol)]

EOD_SNAPSHOT: Dict[int, Dict[str, Any]] = {}
DAILY_STATS: Dict[int, Dict[str, Optional[float]]] = {}

DAILY_SEED_STARTED = False
DAILY_SEED_DONE = False
DAILY_SEED_PROGRESS = {"done": 0, "total": len(TOKENS)}
DAILY_SEED_ERRORS = 0


# =============================================================================
# U-SHAPED PACING CURVE
# =============================================================================
U_CURVE_READY = False
U_CUM_FRAC: List[float] = []


def _build_u_shaped_cum_curve(total_mins: int = 375, a: float = 0.65, b: float = 0.65) -> List[float]:
    """
    Smooth U-shaped expected intraday volume curve.
    Uses a Beta-like shape: w(x) ∝ x^(a-1) * (1-x)^(b-1)
    For a=b<1 => U-shape (more vol near open/close, less mid-day).
    Returns cumulative fractions, last = 1.0
    """
    weights: List[float] = []
    for i in range(total_mins):
        x = (i + 0.5) / total_mins
        w = (x ** (a - 1.0)) * ((1.0 - x) ** (b - 1.0))
        weights.append(float(w))

    s = sum(weights) + 1e-12
    cum: List[float] = []
    run = 0.0
    for w in weights:
        run += w
        cum.append(run / s)

    cum[-1] = 1.0
    return cum


def init_u_curve_once():
    global U_CURVE_READY, U_CUM_FRAC
    if U_CURVE_READY:
        return
    U_CUM_FRAC = _build_u_shaped_cum_curve(total_mins=375, a=0.65, b=0.65)
    U_CURVE_READY = True


def market_is_open_ist(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


def _record_tick_batch(count: int, last_dt: Optional[datetime]):
    global LAST_TICK_TS, LAST_TICK_DT, TOTAL_TICKS
    now = time.time()
    TOTAL_TICKS += int(count)

    TPS_BUCKETS.append((now, int(count)))
    cutoff = now - TPS_WINDOW_SEC
    while TPS_BUCKETS and TPS_BUCKETS[0][0] < cutoff:
        TPS_BUCKETS.popleft()

    LAST_TICK_TS = now
    LAST_TICK_DT = last_dt or datetime.now()


def _get_tps() -> float:
    if not TPS_BUCKETS:
        return 0.0
    return sum(c for _, c in TPS_BUCKETS) / TPS_WINDOW_SEC


def _hot_history_push(token: int, epoch: float, ltp: float, cumvol: Optional[float]):
    dq = HOT_HISTORY.get(token)
    if dq is None:
        dq = deque()
        HOT_HISTORY[token] = dq

    if dq and (epoch - dq[-1][0]) < HOT_SAMPLE_SEC:
        last_epoch, _, last_vol = dq[-1]
        dq[-1] = (last_epoch, float(ltp), float(cumvol) if cumvol is not None else last_vol)
    else:
        dq.append((float(epoch), float(ltp), float(cumvol) if cumvol is not None else None))

    cutoff = epoch - HOT_HISTORY_MAX_SEC
    while dq and dq[0][0] < cutoff:
        dq.popleft()


# =============================================================================
# RECENCY HELPERS (uses existing HOT_HISTORY)
# =============================================================================

def _get_price_at_cutoff(
    series: List[Tuple[float, float, Optional[float]]],
    cutoff_epoch: float,
) -> Optional[float]:
    """Last known price AT or BEFORE cutoff_epoch."""
    result = None
    for t, p, v in series:
        if float(t) <= cutoff_epoch:
            result = float(p)
        else:
            break
    return result


def _get_vol_at_cutoff(
    series: List[Tuple[float, float, Optional[float]]],
    cutoff_epoch: float,
) -> Optional[float]:
    """Last known cumulative volume AT or BEFORE cutoff_epoch."""
    result = None
    for t, p, v in series:
        if float(t) <= cutoff_epoch:
            if v is not None:
                result = float(v)
        else:
            break
    return result


def _compute_recency_factors(
    token: int,
    current_ltp: float,
    current_cumvol: Optional[float],
    avg_vol_20: float,
    window_sec: int = 900,
) -> Dict[str, float]:
    """
    Compute recent price move % and recent volume pace
    from HOT_HISTORY for a given look-back window.
    """
    fallback = {
        "recent_pct"   : 0.0,
        "recent_rvolm" : 1.0,
        "recency_score": 1.0,
        "has_data"     : 0.0,
    }

    with LOCK:
        dq = HOT_HISTORY.get(token)
        if not dq or len(dq) < 2:
            return fallback
        series = list(dq)   # snapshot — release lock before heavy work

    now_epoch = float(series[-1][0])
    cutoff    = now_epoch - float(window_sec)

    base_price = _get_price_at_cutoff(series, cutoff)
    if base_price is None:
        base_price = float(series[0][1])
    if not base_price or float(base_price) <= 0:
        return fallback

    recent_pct = (
        (float(current_ltp) - float(base_price))
        / (float(base_price) + 1e-9)
        * 100.0
    )

    base_vol   = _get_vol_at_cutoff(series, cutoff)
    recent_vol = None
    if base_vol is not None and current_cumvol is not None:
        recent_vol = max(0.0, float(current_cumvol) - float(base_vol))

    window_mins     = float(window_sec) / 60.0
    expected_recent = float(avg_vol_20) * (window_mins / 375.0)
    recent_rvolm    = (
        float(recent_vol) / (expected_recent + 1e-9)
        if recent_vol is not None and expected_recent > 0
        else 1.0
    )

    return {
        "recent_pct"   : float(recent_pct),
        "recent_rvolm" : float(recent_rvolm),
        "recency_score": 1.0,
        "has_data"     : 1.0,
    }


def _compute_recency_multiplier(
    pct_open: float,
    recent_pct: float,
    recent_rvolm: float,
    window_sec: int = 900,
) -> float:
    """
    Returns a multiplier [0.05 - 1.0] that penalizes stale moves.

    Logic:
      - If stock moved a lot recently   -> multiplier near 1.0 (fresh)
      - If stock is sideways recently   -> multiplier near 0.0 (stale)
      - If recent vol is high           -> boost multiplier slightly
      - If recent vol is dead           -> drag multiplier down

    Two components:
      1. Price recency  : how much of the session move happened recently
      2. Volume recency : is volume still flowing in?
    """

    # ---- 1. Price Recency ----
    abs_session = abs(float(pct_open))
    abs_recent  = abs(float(recent_pct))

    if abs_session < 1e-6:
        price_recency = 0.5
    else:
        price_recency = min(abs_recent / (abs_session + 1e-9), 1.5)
        price_recency = max(0.0, min(1.0, price_recency))

    # ---- 2. Direction Consistency ----
    same_direction = (
        (float(pct_open) >= 0 and float(recent_pct) >= 0)
        or
        (float(pct_open) < 0 and float(recent_pct) < 0)
    )
    direction_factor = 1.0 if same_direction else 0.35

    # ---- 3. Volume Recency ----
    vol_recency = min(float(recent_rvolm) / 2.0, 1.0)
    vol_recency = max(0.10, vol_recency)

    # ---- Combine ----
    recency_multiplier = (
        (0.65 * price_recency * direction_factor)
        + (0.35 * vol_recency)
    )

    return float(max(0.05, min(1.0, recency_multiplier)))


def _compute_recency_multiplier_multi(
    token: int,
    pct_open: float,
    current_ltp: float,
    current_cumvol: Optional[float],
    avg_vol_20: float,
) -> float:
    """
    Multi-window recency: checks 5-min, 15-min, 30-min windows
    and returns a weighted average multiplier.
    Avoids over-penalising brief consolidations.
    """
    total_weight = 0.0
    weighted_sum = 0.0

    for window_sec, weight in RECENCY_WINDOWS:
        rf = _compute_recency_factors(
            token          = token,
            current_ltp    = current_ltp,
            current_cumvol = current_cumvol,
            avg_vol_20     = avg_vol_20,
            window_sec     = window_sec,
        )

        if float(rf["has_data"]) == 0.0:
            continue

        mult = _compute_recency_multiplier(
            pct_open     = pct_open,
            recent_pct   = float(rf["recent_pct"]),
            recent_rvolm = float(rf["recent_rvolm"]),
            window_sec   = window_sec,
        )

        weighted_sum += mult * weight
        total_weight += weight

    if total_weight <= 0:
        return 1.0  # no data -> no penalty

    return float(weighted_sum / total_weight)


# =============================================================================
# TICK PROCESSING
# =============================================================================
def update_from_tick(tick: dict):
    token  = tick["instrument_token"]
    ltp    = tick.get("last_price")
    cumvol = tick.get("volume_traded")
    ohlc   = tick.get("ohlc") or {}
    ts     = tick.get("exchange_timestamp") or datetime.now()

    if ltp is None:
        return None

    LAST_PRICE[token] = float(ltp)
    if cumvol is not None:
        DAY_VOL[token] = float(cumvol)
    if ohlc:
        LAST_OHLC[token] = ohlc

    _hot_history_push(token, time.time(), float(ltp), float(cumvol) if cumvol is not None else None)
    return ts


# =============================================================================
# PACING CURVE
# =============================================================================
def _pace_reference_tokens_all_sectors(max_per_sector: int = 2, max_total: int = 30) -> List[int]:
    """
    Render-safe: pick representative tokens across ALL sectors, capped.
    """
    picked_syms: List[str] = []

    for _sector, syms in SECTOR_DEFINITIONS.items():
        cands = [s for s in syms if s in symbol_to_token]
        if not cands:
            continue
        picked_syms.extend(cands[:max_per_sector])

    out: List[int] = []
    seen = set()
    for s in picked_syms:
        tok = symbol_to_token.get(s)
        if not tok or tok in seen:
            continue
        out.append(tok)
        seen.add(tok)
        if len(out) >= max_total:
            break

    return out if out else TOKENS[:5]


def _build_learned_pace_curve_from_history(days_back: int = 30) -> List[float]:
    """
    Build a 375-minute cumulative expected volume fraction curve from real 5-min candles.
    Output: list length 375, last value = 1.0
    """
    total_mins = 375
    bins_5m    = total_mins // 5  # 75

    toks    = _pace_reference_tokens_all_sectors(max_per_sector=2, max_total=20)
    vol_sum = [0.0] * bins_5m
    vol_n   = 0

    to_dt   = datetime.now(IST)
    from_dt = to_dt - timedelta(days=days_back)
    today   = datetime.now(IST).date()

    for tok in toks:
        candles = kite.historical_data(
            instrument_token=tok,
            from_date=from_dt,
            to_date=to_dt,
            interval="5minute",
            continuous=False,
            oi=False,
        )
        df = pd.DataFrame(candles)
        if df.empty:
            time.sleep(0.35)
            continue

        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()

        for d, g in df.groupby(df.index.date):
            if market_is_open_ist() and d == today:
                continue
            g = g.sort_index()
            if len(g) < 50:
                continue

            vols = g["volume"].astype(float).tolist()
            vols = vols[:bins_5m] + [0.0] * max(0, bins_5m - len(vols))
            for i in range(bins_5m):
                vol_sum[i] += float(vols[i])
            vol_n += 1

        time.sleep(0.35)

    if vol_n <= 3:
        raise RuntimeError("Not enough intraday history to build learned pacing curve")

    vol_avg    = [v / vol_n for v in vol_sum]
    total      = sum(vol_avg) + 1e-12
    weights_5m = [v / total for v in vol_avg]

    w_min: List[float] = []
    for w in weights_5m:
        w_min.extend([w / 5.0] * 5)

    cum: List[float] = []
    run = 0.0
    for w in w_min:
        run += w
        cum.append(run)

    cum[-1] = 1.0
    return cum


def _load_pace_cache_today() -> Optional[List[float]]:
    try:
        if not PACE_CACHE_PATH.exists():
            return None
        data = json.loads(PACE_CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("date") != str(datetime.now(IST).date()):
            return None
        curve = data.get("curve")
        if not isinstance(curve, list) or len(curve) != 375:
            return None
        return [float(x) for x in curve]
    except Exception:
        return None


def _save_pace_cache_today(curve: List[float]):
    try:
        PACE_CACHE_PATH.write_text(
            json.dumps({"date": str(datetime.now(IST).date()), "curve": curve}),
            encoding="utf-8",
        )
    except Exception:
        pass


def start_pace_curve_builder_once():
    """
    Render-safe: build in background so startup stays fast.
    Sets PACE_CURVE_READY and PACE_CUM_FRAC_MIN when finished.
    """
    global PACE_BUILD_STARTED, PACE_CURVE_READY, PACE_CUM_FRAC_MIN

    if PACE_BUILD_STARTED:
        return
    PACE_BUILD_STARTED = True

    def _run():
        global PACE_CURVE_READY, PACE_CUM_FRAC_MIN

        cached = _load_pace_cache_today()
        if cached:
            with PACE_LOCK:
                PACE_CUM_FRAC_MIN = cached
                PACE_CURVE_READY  = True
            log.info("PACE curve loaded from cache (%s)", str(PACE_CACHE_PATH))
            return

        try:
            curve = _build_learned_pace_curve_from_history(days_back=30)
            with PACE_LOCK:
                PACE_CUM_FRAC_MIN = curve
                PACE_CURVE_READY  = True
            _save_pace_cache_today(curve)
            log.info("PACE curve built from intraday history (len=%s)", len(curve))
        except Exception as e:
            log.warning("PACE curve build failed -> using fallback pacing. err=%r", e)

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# DAILY STATS SEED
# =============================================================================
def compute_20d_daily_stats_and_eod(token: int, days_back: int = 220) -> Dict[str, Any]:
    to_dt   = datetime.now()
    from_dt = to_dt - timedelta(days=days_back)

    candles = kite.historical_data(
        instrument_token=token,
        from_date=from_dt,
        to_date=to_dt,
        interval="day",
        continuous=False,
        oi=False,
    )

    df = pd.DataFrame(candles)
    if df.empty or len(df) < LOOKBACK_SESSIONS + 2:
        return {"avg_vol_20": None, "avg_range_20": None, "avg_abs_oc_ret_20": None, "eod": None}

    df["date"] = pd.to_datetime(df["date"])
    df["d"]    = df["date"].dt.date
    today_ist  = datetime.now(IST).date()

    if market_is_open_ist() and df.iloc[-1]["d"] == today_ist:
        df = df.iloc[:-1].copy()

    if len(df) < LOOKBACK_SESSIONS + 1:
        return {"avg_vol_20": None, "avg_range_20": None, "avg_abs_oc_ret_20": None, "eod": None}

    last = df.iloc[-1]
    prev = df.iloc[-2]

    eod = {
        "date"      : last["d"],
        "open"      : float(last["open"]),
        "high"      : float(last["high"]),
        "low"       : float(last["low"]),
        "close"     : float(last["close"]),
        "volume"    : float(last["volume"]),
        "prev_close": float(prev["close"]),
    }

    df_stats = df.tail(LOOKBACK_SESSIONS).copy()
    df_stats["range"]      = (df_stats["high"] - df_stats["low"]).astype(float)
    df_stats["oc_ret_pct"] = (df_stats["close"] - df_stats["open"]) / df_stats["open"] * 100.0
    df_stats = df_stats.dropna()

    return {
        "avg_vol_20"       : float(df_stats["volume"].mean())           if not df_stats.empty else None,
        "avg_range_20"     : float(df_stats["range"].mean())            if not df_stats.empty else None,
        "avg_abs_oc_ret_20": float(df_stats["oc_ret_pct"].abs().mean()) if not df_stats.empty else None,
        "eod": eod,
    }


def seed_daily_stats_once(per_req_sleep: float = SEED_SLEEP_SEC):
    global DAILY_SEED_STARTED, DAILY_SEED_DONE, DAILY_SEED_ERRORS
    if DAILY_SEED_STARTED:
        return
    DAILY_SEED_STARTED = True

    def _run():
        global DAILY_SEED_DONE, DAILY_SEED_ERRORS
        DAILY_SEED_PROGRESS["total"] = len(TOKENS)
        DAILY_SEED_PROGRESS["done"]  = 0

        for i, tok in enumerate(TOKENS, start=1):
            try:
                st = compute_20d_daily_stats_and_eod(tok)
            except Exception:
                DAILY_SEED_ERRORS += 1
                st = {"avg_vol_20": None, "avg_range_20": None, "avg_abs_oc_ret_20": None, "eod": None}

            with LOCK:
                DAILY_STATS[tok] = {
                    "avg_vol_20"       : st.get("avg_vol_20"),
                    "avg_range_20"     : st.get("avg_range_20"),
                    "avg_abs_oc_ret_20": st.get("avg_abs_oc_ret_20"),
                }
                if st.get("eod"):
                    EOD_SNAPSHOT[tok] = st["eod"]

            DAILY_SEED_PROGRESS["done"] = i
            time.sleep(per_req_sleep)

        DAILY_SEED_DONE = True

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# PCR (NFO)
# =============================================================================
NFO_INS_DF: Optional[pd.DataFrame] = None
NFO_LOAD_STARTED = False
NFO_LOAD_ERR: Optional[str] = None
PCR_CACHE: Dict[str, Tuple[dict, float]] = {}


def load_nfo_instruments_once():
    global NFO_LOAD_STARTED
    if NFO_LOAD_STARTED:
        return
    NFO_LOAD_STARTED = True

    def _run():
        global NFO_INS_DF, NFO_LOAD_ERR
        try:
            df = pd.DataFrame(kite.instruments("NFO"))
            df = df[df["instrument_type"].isin(["CE", "PE"])].copy()
            df = df[df["name"] == "NIFTY"].copy()
            df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
            globals()["NFO_INS_DF"] = df
            log.info("Loaded NFO instruments (NIFTY only): %s rows", len(df))
        except Exception as e:
            globals()["NFO_LOAD_ERR"] = repr(e)
            log.exception("Failed to load NFO instruments")

    threading.Thread(target=_run, daemon=True).start()


def _chunk(lst: List[str], n: int):
    for i in range(0, len(lst), n):
        yield lst[i: i + n]


def _quote_many(keys: List[str], chunk_size: int = PCR_QUOTE_CHUNK) -> dict:
    out = {}
    for ch in _chunk(keys, chunk_size):
        out.update(kite.quote(ch))
    return out


def _infer_strike_step(strikes: pd.Series) -> float:
    s = sorted(set(float(x) for x in strikes.dropna().tolist()))
    if len(s) < 3:
        return 50.0
    diffs = [b - a for a, b in zip(s, s[1:]) if (b - a) > 0]
    if not diffs:
        return 50.0
    diffs.sort()
    return float(diffs[len(diffs) // 2])


def compute_real_nifty_oi_pcr(strikes_around_atm: int = PCR_STRIKES_AROUND_ATM) -> Optional[dict]:
    cache_key = f"NIFTY:oi:{strikes_around_atm}"
    cached    = PCR_CACHE.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]

    if NFO_LOAD_ERR or NFO_INS_DF is None:
        return None

    try:
        spot = float(kite.ltp([NIFTY_SPOT_SYMBOL])[NIFTY_SPOT_SYMBOL]["last_price"])
    except Exception:
        return None

    dfu = NFO_INS_DF
    if dfu is None or dfu.empty:
        return None

    expiry = min(dfu["expiry"].tolist()) if len(dfu) else None
    if not expiry:
        return None

    dfe = dfu[dfu["expiry"] == expiry].copy()
    if dfe.empty:
        return None

    step = _infer_strike_step(dfe["strike"])
    atm  = round(spot / step) * step

    lo  = atm - strikes_around_atm * step
    hi  = atm + strikes_around_atm * step
    dfe = dfe[(dfe["strike"] >= lo) & (dfe["strike"] <= hi)].copy()
    if dfe.empty:
        return None

    ce = dfe[dfe["instrument_type"] == "CE"]
    pe = dfe[dfe["instrument_type"] == "PE"]

    ce_keys = ["NFO:" + s for s in ce["tradingsymbol"].tolist()]
    pe_keys = ["NFO:" + s for s in pe["tradingsymbol"].tolist()]
    keys    = ce_keys + pe_keys
    if not keys:
        return None

    try:
        q = _quote_many(keys, chunk_size=PCR_QUOTE_CHUNK)
    except Exception:
        return None

    ce_oi = sum(float(q.get(k, {}).get("oi") or 0.0) for k in ce_keys)
    pe_oi = sum(float(q.get(k, {}).get("oi") or 0.0) for k in pe_keys)
    pcr   = pe_oi / (ce_oi + 1e-9)

    data = {
        "underlying": "NIFTY",
        "expiry"    : str(expiry),
        "spot"      : spot,
        "atm"       : atm,
        "step"      : step,
        "range"     : [float(lo), float(hi)],
        "ce_oi"     : float(ce_oi),
        "pe_oi"     : float(pe_oi),
        "pcr"       : float(pcr),
        "strikes"   : int(len(dfe)),
        "updated_at": datetime.now(IST).strftime("%H:%M:%S"),
    }

    PCR_CACHE[cache_key] = (data, time.time() + PCR_CACHE_TTL_SEC)
    return data


def pcr_label_from_value(pcr: float) -> str:
    if pcr >= 1.40:
        return "STRONG BUY"
    if pcr >= 1.10:
        return "BUY"
    if pcr >= 0.90:
        return "NEUTRAL"
    if pcr >= 0.60:
        return "SELL"
    return "STRONG SELL"


# =============================================================================
# SNAPSHOTS + BACKGROUND COMPUTE CACHE
# =============================================================================
CACHE_LOCK = threading.Lock()
CACHE: Dict[str, Any] = {
    "sector_agg"   : {},
    "top15_gainers": [],
    "top15_losers" : [],
    "hvhr_gainers" : [],
    "hvhr_losers"  : [],
    "hot_gainers"  : [],
    "hot_losers"   : [],
    "heatmap_rows" : [],
    "sentiment"    : {"adv": 0, "dec": 0, "unch": 0, "total": 0, "score": 0.0, "label": "NEUTRAL"},
    "pcr"          : None,
    "updated"      : {"core": 0.0, "hot": 0.0, "pcr": 0.0},
}


def _snapshot_state(include_hot: bool = False) -> Dict[str, Any]:
    with LOCK:
        snap = {
            "price" : dict(LAST_PRICE),
            "vol"   : dict(DAY_VOL),
            "ohlc"  : dict(LAST_OHLC),
            "eod"   : dict(EOD_SNAPSHOT),
            "daily" : dict(DAILY_STATS),
            "tokens": list(TOKENS),
        }
        if include_hot:
            snap["hot"] = {tok: list(dq) for tok, dq in HOT_HISTORY.items()}
    return snap


def _get_live_or_eod_state_from_snap(token: int, snap: Dict[str, Any]) -> Optional[Tuple[float, float, dict]]:
    ltp       = snap["price"].get(token)
    vol_today = snap["vol"].get(token)
    ohlc      = snap["ohlc"].get(token) or {}

    if (
        ltp       is not None
        and vol_today is not None
        and ohlc.get("open")  is not None
        and ohlc.get("close") is not None
    ):
        return float(ltp), float(vol_today), ohlc

    e = (snap.get("eod") or {}).get(token)
    if not e or e.get("prev_close") is None:
        return None

    ohlc_eod = {"open": e["open"], "high": e["high"], "low": e["low"], "close": e["prev_close"]}
    return float(e["close"]), float(e["volume"]), ohlc_eod


def _time_factor_ist_for_rvol(now_ist: Optional[datetime] = None) -> float:
    """
    Returns expected *cumulative* volume fraction for the current time in IST.

    Priority:
      1) Learned curve (PACE_CUM_FRAC_MIN) if ready
      2) U-shaped fallback curve (U_CUM_FRAC) if ready
      3) Linear fallback (mins_passed / 375)

    Output is clamped to [0.01, 1.0]
    """
    now_ist = now_ist or datetime.now(IST)

    m_open  = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    m_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    total_mins = 375

    if now_ist <= m_open:
        mins_passed = 1
    elif now_ist >= m_close:
        mins_passed = total_mins
    else:
        mins_passed = int((now_ist - m_open).total_seconds() // 60)
        mins_passed = max(1, min(total_mins, mins_passed))

    idx = mins_passed - 1

    with PACE_LOCK:
        if PACE_CURVE_READY and len(PACE_CUM_FRAC_MIN) == total_mins:
            tf = float(PACE_CUM_FRAC_MIN[idx])
        elif U_CURVE_READY and len(U_CUM_FRAC) == total_mins:
            tf = float(U_CUM_FRAC[idx])
        else:
            tf = mins_passed / float(total_mins)

    return max(0.01, min(1.0, float(tf)))


# =============================================================================
# RFACTOR (recency-aware)
# =============================================================================
def _compute_rfactor_row_snap(token: int, snap: Dict[str, Any]) -> Optional[Dict[str, float]]:
    state_ = _get_live_or_eod_state_from_snap(token, snap)
    if not state_:
        return None

    ltp, vol_today, ohlc = state_
    prev_close = ohlc.get("close")
    day_open   = ohlc.get("open")
    day_high   = ohlc.get("high")
    day_low    = ohlc.get("low")

    if prev_close is None or day_open is None or day_high is None or day_low is None:
        return None

    prev_close = float(prev_close)
    day_open   = float(day_open)
    day_high   = float(day_high)
    day_low    = float(day_low)
    ltp        = float(ltp)

    if prev_close <= 0 or day_open <= 0 or ltp <= 0:
        return None

    gap_pct  = ((day_open - prev_close) / prev_close) * 100.0
    pct_open = ((ltp - day_open) / day_open) * 100.0

    st = (snap.get("daily") or {}).get(token) or {}
    avg_vol_20        = st.get("avg_vol_20")
    avg_range_20      = st.get("avg_range_20")
    avg_abs_oc_ret_20 = st.get("avg_abs_oc_ret_20")

    if not avg_vol_20 or not avg_range_20 or not avg_abs_oc_ret_20:
        return None

    eps = 1e-9

    # ---- Paced RVOL ----
    tf           = _time_factor_ist_for_rvol(datetime.now(IST))
    expected_vol = float(avg_vol_20) * tf
    rvolm        = float(vol_today) / (expected_vol + eps)

    # ---- Range expansion ----
    range_today  = max(0.0, day_high - day_low)
    range_factor = range_today / (float(avg_range_20) + eps)

    # ---- Price move from open ----
    move_factor = abs(float(pct_open)) / (float(avg_abs_oc_ret_20) + eps)

    # ---- Base RFactor ----
    rfactor_val = rvolm * range_factor * move_factor

    # ---- Freshness (position in day range) ----
    range_span        = max(day_high - day_low, eps)
    position_in_range = (ltp - day_low) / range_span
    position_in_range = max(0.0, min(1.0, position_in_range))

    if pct_open >= 0:
        freshness = position_in_range ** 3
    else:
        freshness = (1.0 - position_in_range) ** 3

    rfactor_val *= freshness

    # ================================================================
    # ---- RECENCY MULTIPLIER ----
    # Penalizes stocks that moved in the morning and went sideways
    # ================================================================
    recency_mult = 1.0  # default: no penalty if no HOT data yet

    if market_is_open_ist():
        recency_mult = _compute_recency_multiplier_multi(
            token          = token,
            pct_open       = pct_open,
            current_ltp    = ltp,
            current_cumvol = vol_today,
            avg_vol_20     = float(avg_vol_20),
        )

    # Blend: RECENCY_WEIGHT=0 -> pure session; =1 -> fully recency-penalised
    rfactor_final = rfactor_val * (
        (1.0 - RECENCY_WEIGHT)
        + (RECENCY_WEIGHT * recency_mult)
    )

    dirr = (1.0 if pct_open >= 0 else -1.0) * rfactor_final

    return {
        "gap_pct"     : float(gap_pct),
        "pct_open"    : float(pct_open),
        "rfactor"     : float(rfactor_final),
        "rfactor_raw" : float(rfactor_val),     # pre-recency — useful for debug
        "recency_mult": float(recency_mult),    # 0.05=stale, 1.0=fresh
        "dirr"        : float(dirr),
        "ltp"         : float(ltp),
        "day_open"    : float(day_open),
        "vol_today"   : float(vol_today),
    }


# =============================================================================
# MARKET SENTIMENT
# =============================================================================
def _compute_market_sentiment_proxy_snap(snap: Dict[str, Any]) -> Dict[str, Any]:
    adv = dec = unch = 0
    for tok in snap.get("tokens") or []:
        st = _get_live_or_eod_state_from_snap(tok, snap)
        if not st:
            continue
        ltp, _, ohlc = st
        op = ohlc.get("open")
        if op is None:
            continue
        try:
            opf = float(op)
            ltp = float(ltp)
        except Exception:
            continue
        if opf <= 0 or ltp <= 0:
            continue
        pct_open = (ltp - opf) / opf * 100.0
        if pct_open > 0:
            adv += 1
        elif pct_open < 0:
            dec += 1
        else:
            unch += 1

    total = adv + dec + unch
    score = (adv - dec) / total if total > 0 else 0.0

    if score >= 0.20:
        label = "BULLISH"
    elif score <= -0.20:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    return {"adv": adv, "dec": dec, "unch": unch, "total": total, "score": float(score), "label": label}


# =============================================================================
# SECTOR AGGREGATES
# =============================================================================
def _compute_sector_aggregates_from_rr_with_daily(
    rr_by_tok: Dict[int, Dict[str, float]],
    daily_map: Dict[int, Dict[str, Optional[float]]],
) -> Dict[str, Dict[str, float]]:
    """
    Sector bars metrics:
      DirR = signed mean(DirR)
      RVOLmNetSum = Σbuy RVOLm - Σsell RVOLm
      RVOLmNetMean = RVOLmNetSum / N
    """
    tf  = _time_factor_ist_for_rvol(datetime.now(IST))
    out: Dict[str, Dict[str, float]] = {}

    for sector, syms in SECTOR_DEFINITIONS.items():
        dirr_vals: List[float] = []
        buy_sum = sell_sum = 0.0
        buy_n   = sell_n   = 0

        for s in syms:
            tok = symbol_to_token.get(s)
            if not tok:
                continue
            rr = rr_by_tok.get(tok)
            if not rr:
                continue

            dirr_vals.append(float(rr["dirr"]))

            st         = daily_map.get(tok) or {}
            avg_vol_20 = st.get("avg_vol_20")
            vol_today  = rr.get("vol_today")
            pct_open   = rr.get("pct_open")

            try:
                if pct_open is None or avg_vol_20 is None or vol_today is None:
                    continue
                if float(avg_vol_20) <= 0:
                    continue

                expected = float(avg_vol_20) * float(tf)
                rvolm    = float(vol_today) / (expected + 1e-9)

                if float(pct_open) >= 0:
                    buy_sum  += rvolm
                    buy_n    += 1
                else:
                    sell_sum += rvolm
                    sell_n   += 1
            except Exception:
                continue

        n_total    = buy_n + sell_n
        dirr_mean  = (sum(dirr_vals) / len(dirr_vals)) if dirr_vals else 0.0
        net_sum    = float(buy_sum  - sell_sum)
        gross_sum  = float(buy_sum  + sell_sum)
        net_mean   = float(net_sum  / n_total) if n_total > 0 else 0.0
        gross_mean = float(gross_sum / n_total) if n_total > 0 else 0.0

        out[sector] = {
            "DirR"          : float(dirr_mean),
            "RVOLmBuySum"   : float(buy_sum),
            "RVOLmSellSum"  : float(sell_sum),
            "RVOLmNetSum"   : float(net_sum),
            "RVOLmGrossSum" : float(gross_sum),
            "RVOLmNetMean"  : float(net_mean),
            "RVOLmGrossMean": float(gross_mean),
            "N"             : float(n_total),
            "BuyN"          : float(buy_n),
            "SellN"         : float(sell_n),
        }

    return out


def _quantile_threshold(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    q  = min(max(float(q), 0.0), 1.0)
    vs = sorted(values)
    if len(vs) == 1:
        return float(vs[0])
    idx = int(round(q * (len(vs) - 1)))
    idx = min(max(idx, 0), len(vs) - 1)
    return float(vs[idx])


def _compute_hot_row_from_series(series: List[Tuple[float, float, Optional[float]]]) -> Optional[dict]:
    if not series or len(series) < 2:
        return None

    now_epoch = float(series[-1][0])
    cutoff    = now_epoch - float(HOT_WINDOW_SEC)

    base = None
    for t, p, v in series:
        if float(t) <= cutoff:
            base = (float(t), p, v)
        else:
            break
    if base is None:
        base = (float(series[0][0]), series[0][1], series[0][2])

    base_t, base_p, base_v = base
    _, last_p, last_v = series[-1]

    if base_p is None or float(base_p) <= 0 or last_p is None:
        return None

    prices = [float(p) for (t, p, _v) in series if float(t) >= base_t and p is not None]
    if len(prices) < 2:
        return None

    lo  = float(min(prices))
    hi  = float(max(prices))
    rng = float(hi - lo)

    base_pf        = float(base_p)
    range_pct      = (rng / (base_pf + 1e-9)) * 100.0
    up_spike_pct   = (hi - base_pf) / (base_pf + 1e-9) * 100.0
    down_spike_pct = (lo - base_pf) / (base_pf + 1e-9) * 100.0
    spike_pct      = up_spike_pct if abs(up_spike_pct) >= abs(down_spike_pct) else down_spike_pct

    vol_win = None
    if base_v is not None and last_v is not None:
        vol_win = float(last_v) - float(base_v)
        if vol_win < 0:
            vol_win = None

    return {"range_pct": float(range_pct), "spike_pct": float(spike_pct), "vol_win": vol_win}


# =============================================================================
# BACKGROUND COMPUTE LOOP
# =============================================================================
_compute_started = False


def start_compute_loop_once():
    global _compute_started
    if _compute_started:
        return
    _compute_started = True

    def _run():
        last_core = last_hot = last_pcr = 0.0

        while True:
            now = time.time()

            # ---- CORE ----
            if (now - last_core) >= COMPUTE_CORE_EVERY_SEC:
                try:
                    snap = _snapshot_state(include_hot=False)

                    rr_by_tok:    Dict[int, Dict[str, float]] = {}
                    rows_basic:   List[dict] = []
                    rfactor_vals: List[float] = []

                    for sym in ALL_SYMBOLS:
                        tok = symbol_to_token.get(sym)
                        if not tok:
                            continue
                        rr = _compute_rfactor_row_snap(tok, snap)
                        if not rr:
                            continue

                        rr_by_tok[tok] = rr
                        rows_basic.append({
                            "Symbol" : sym,
                            "%Change": round(float(rr["pct_open"]), 2),
                            "RFactor": round(float(rr["rfactor"]),  2),
                            "Vol"    : int(rr["vol_today"]),
                        })
                        rfactor_vals.append(float(rr["rfactor"]))

                    gainers = [r for r in rows_basic if float(r["%Change"]) > 0]
                    losers  = [r for r in rows_basic if float(r["%Change"]) < 0]
                    gainers.sort(key=lambda r: float(r["RFactor"]), reverse=True)
                    losers.sort(key=lambda r:  float(r["RFactor"]), reverse=True)
                    top15_gainers = gainers[:15]
                    top15_losers  = losers[:15]

                    thr = _quantile_threshold(rfactor_vals, float(HVHR_RFACTOR_Q)) if rfactor_vals else None
                    if thr is None:
                        hvhr_gainers, hvhr_losers = [], []
                    else:
                        bucket   = [r for r in rows_basic if float(r["RFactor"]) >= float(thr)]
                        bucket_g = [r for r in bucket if float(r["%Change"]) > 0]
                        bucket_l = [r for r in bucket if float(r["%Change"]) < 0]
                        bucket_g.sort(key=lambda r: (int(r["Vol"]), float(r["RFactor"])), reverse=True)
                        bucket_l.sort(key=lambda r: (int(r["Vol"]), float(r["RFactor"])), reverse=True)
                        hvhr_gainers = bucket_g[: int(HVHR_N)]
                        hvhr_losers  = bucket_l[: int(HVHR_N)]

                    sector_agg = _compute_sector_aggregates_from_rr_with_daily(
                        rr_by_tok = rr_by_tok,
                        daily_map = (snap.get("daily") or {}),
                    )
                    sentiment = _compute_market_sentiment_proxy_snap(snap)

                    sector_order = sorted(
                        SECTOR_DEFINITIONS.keys(),
                        key=lambda sec: float((sector_agg.get(sec) or {}).get("DirR") or 0.0),
                        reverse=True,
                    )

                    heat_rows: List[dict] = []
                    for sec in sector_order:
                        sym_scored: List[Tuple[float, str]] = []
                        for sym in SECTOR_DEFINITIONS.get(sec, []):
                            tok = symbol_to_token.get(sym)
                            if not tok:
                                continue
                            rr = rr_by_tok.get(tok)
                            if not rr:
                                continue
                            sym_scored.append((float(rr.get("dirr") or 0.0), sym))
                        sym_scored.sort(key=lambda x: x[0], reverse=True)

                        for _dirr, sym in sym_scored:
                            tok = symbol_to_token.get(sym)
                            if not tok:
                                continue
                            rr = rr_by_tok.get(tok)
                            if not rr:
                                continue
                            ltp_     = float(rr["ltp"])
                            vol_     = float(rr["vol_today"])
                            turnover = ltp_ * vol_
                            if turnover <= 0:
                                continue
                            heat_rows.append({
                                "sector_key"  : sec,
                                "sector_label": sec.replace("_", " ").upper(),
                                "symbol"      : sym,
                                "pct"         : float(rr["pct_open"]),
                                "dirr"        : float(rr["dirr"]),
                                "value"       : float(turnover),
                            })

                    with CACHE_LOCK:
                        CACHE["sector_agg"]      = sector_agg
                        CACHE["top15_gainers"]   = top15_gainers
                        CACHE["top15_losers"]    = top15_losers
                        CACHE["hvhr_gainers"]    = hvhr_gainers
                        CACHE["hvhr_losers"]     = hvhr_losers
                        CACHE["sentiment"]       = sentiment
                        CACHE["heatmap_rows"]    = heat_rows
                        CACHE["updated"]["core"] = now

                except Exception:
                    log.exception("compute loop: CORE crashed")
                last_core = now

            # ---- HOT NOW ----
            if (now - last_hot) >= COMPUTE_HOT_EVERY_SEC:
                try:
                    snap = _snapshot_state(include_hot=True)
                    hot  = snap.get("hot") or {}

                    rows      = []
                    min_spike = float(HOT_MIN_RET_PCT)
                    min_rng   = float(HOT_MIN_RANGE_PCT)

                    for sym in ALL_SYMBOLS:
                        tok = symbol_to_token.get(sym)
                        if not tok:
                            continue
                        series = hot.get(tok)
                        if not series:
                            continue
                        hr = _compute_hot_row_from_series(series)
                        if not hr:
                            continue
                        spike     = float(hr["spike_pct"])
                        range_pct = float(hr["range_pct"])
                        if abs(spike) < min_spike or range_pct < min_rng:
                            continue
                        rows.append({
                            "Symbol"    : sym,
                            "_spike"    : spike,
                            "_abs_spike": abs(spike),
                            "SPIKE%"    : round(spike, 2),
                            "RNG5%"     : round(range_pct, 2),
                            "DAY RNG%"  : None,
                        })

                    gain = [r for r in rows if float(r["_spike"]) > 0]
                    loss = [r for r in rows if float(r["_spike"]) < 0]
                    gain.sort(key=lambda r: (float(r["_abs_spike"]), float(r["RNG5%"])), reverse=True)
                    loss.sort(key=lambda r: (float(r["_abs_spike"]), float(r["RNG5%"])), reverse=True)

                    hot_gainers = [{k: v for k, v in r.items() if not k.startswith("_")} for r in gain[:15]]
                    hot_losers  = [{k: v for k, v in r.items() if not k.startswith("_")} for r in loss[:15]]

                    with CACHE_LOCK:
                        CACHE["hot_gainers"]    = hot_gainers
                        CACHE["hot_losers"]     = hot_losers
                        CACHE["updated"]["hot"] = now

                except Exception:
                    log.exception("compute loop: HOT crashed")
                last_hot = now

            # ---- PCR ----
            if (now - last_pcr) >= COMPUTE_PCR_EVERY_SEC:
                try:
                    p = compute_real_nifty_oi_pcr(strikes_around_atm=PCR_STRIKES_AROUND_ATM)
                    with CACHE_LOCK:
                        CACHE["pcr"]            = p
                        CACHE["updated"]["pcr"] = now
                except Exception:
                    log.exception("compute loop: PCR crashed")
                last_pcr = now

            time.sleep(COMPUTE_SLEEP_SEC)

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# TICKER
# =============================================================================
_started = False


def start_ticker_once():
    global _started
    if _started:
        return
    _started = True

    def _run():
        while True:
            try:
                kws = KiteTicker(API_KEY, ACCESS_TOKEN)

                def on_connect(ws, _):
                    log.info("WS CONNECTED")
                    ws.subscribe(TOKENS)
                    ws.set_mode(ws.MODE_FULL, TOKENS)

                def on_ticks(ws, ticks):
                    try:
                        last_dt = None
                        with LOCK:
                            for t in ticks:
                                ts = update_from_tick(t)
                                if ts and (last_dt is None or ts > last_dt):
                                    last_dt = ts
                            _record_tick_batch(len(ticks), last_dt)
                    except Exception:
                        log.exception("on_ticks crashed")

                kws.on_connect = on_connect
                kws.on_ticks   = on_ticks
                kws.connect(threaded=True)

                while True:
                    time.sleep(2)

            except Exception:
                log.exception("Ticker loop crashed; restarting in 5s")
                time.sleep(5)

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# DASH APP
# =============================================================================
dash_app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],  # <-- add this
    requests_pathname_prefix=BASE,
    routes_pathname_prefix="/",
    assets_folder=os.path.join(os.path.dirname(__file__), "assets"),
    suppress_callback_exceptions=True,
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
)
server = dash_app.server


# =============================================================================
# UI: Shared components
# =============================================================================
def dial_component(prefix: str, title: str):
    return html.Div(
        html.Div(
            [
                html.Div(
                    [
                        html.Div([html.Div(className=f"dial-arc dial-arc-{prefix}")], className="dial-arc-clip"),
                        html.Div(id=f"{prefix}-needle", className="dial-needle", style={"--rot": "0deg"}),
                        html.Div(className="dial-center"),
                        html.Div(["STRONG", html.Br(), "SELL"], className="dial-label dial-ss"),
                        html.Div("SELL", className="dial-label dial-s"),
                        html.Div("NEUTRAL", className="dial-label dial-n"),
                        html.Div("BUY", className="dial-label dial-b"),
                        html.Div(["STRONG", html.Br(), "BUY"], className="dial-label dial-sb"),
                    ],
                    className="dial-arc-wrap",
                ),
                html.Div(title, className="dial-title"),
                html.Div("—", id=f"{prefix}-sub", className="dial-sub"),
            ],
            className=f"dial-card dial-{prefix}",
        )
    )


def _extract_sector_from_path(pn: str) -> Optional[str]:
    pn = (pn or "").strip()
    if "/sector/" not in pn:
        return None
    sector = unquote(pn.split("/sector/", 1)[1]).strip("/").upper()
    return sector or None


def _sector_modal_coldefs_desktop():
    return [
        {
            "field": "Symbol",
            "headerName": "STOCK",
            "minWidth": 120,
            "flex": 1,
            "cellRenderer": "SymbolCell",
        },
        {
            "field": "Company",
            "headerName": "COMPANY",
            "minWidth": 180,
            "flex": 2,
            "cellRenderer": "CompanyLinkCell",
        },
        {
            "field": "DirR",
            "headerName": "MOMENTUM",
            "minWidth": 110,
            "flex": 1,
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
        },
        {
            "field": "Price",
            "headerName": "PRICE",
            "minWidth": 110,
            "flex": 1,
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
        },
        {
            "field": "%Change",
            "headerName": "%CHG",
            "minWidth": 110,
            "flex": 1,
            "type": "rightAligned",
            "cellRenderer": "Pct2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
        },
        {
            "field": "Gap%",
            "headerName": "GAP %",
            "minWidth": 100,
            "flex": 1,
            "type": "rightAligned",
            "cellRenderer": "Pct2Cell",
        },
        {
            "field": "RVOLm",
            "headerName": "RVOLm",
            "minWidth": 100,
            "flex": 1,
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
        },
    ]


def _sector_modal_coldefs_mobile():
    return [
        {
            "field": "Symbol",
            "headerName": "STOCK",
            "minWidth": 92,
            "flex": 2,
            "cellRenderer": "SymbolCell",
        },
        {
            "field": "DirR",
            "headerName": "MOMENTUM",
            "minWidth": 88,
            "flex": 1,
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
        },
        {
            "field": "Price",
            "headerName": "Price",
            "minWidth": 72,
            "flex": 1,
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
        },
        {
            "field": "%Change",
            "headerName": "%CHG",
            "minWidth": 72,
            "flex": 1,
            "type": "rightAligned",
            "cellRenderer": "Pct2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
        },
        {
            "field": "RVOLm",
            "headerName": "RVOLm",
            "minWidth": 76,
            "flex": 1,
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
        },
    ]


def sector_modal_component():
    grid_opts_desktop = {
        "getRowId": {"function": "params.data.Symbol"},
        "animateRows": True,
        "alwaysShowVerticalScroll": True,
        "domLayout": "normal",
        "onGridReady": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 120);"},
        "onGridSizeChanged": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 120);"},
    }

    grid_opts_mobile = {
        "getRowId": {"function": "params.data.Symbol"},
        "animateRows": True,
        "alwaysShowVerticalScroll": False,
        "domLayout": "normal",
        "onGridReady": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 80);"},
        "onGridSizeChanged": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 80);"},
    }

    header = html.Div(
        [
            html.Div(id="sector-modal-title", className="tt-modal-title", children="SECTOR"),
            dcc.Link(
                dbc.Button(
                    "Close",
                    color="secondary",
                    outline=True,
                    size="sm",
                    className="tt-modal-close-btn",
                ),
                href=BASE,
                refresh=False,
            ),
        ],
        className="d-flex justify-content-between align-items-center w-100",
    )

    return dbc.Modal(
        [
            dbc.ModalHeader(header, close_button=False),
            dbc.ModalBody(
                html.Div(
                    [
                        # DESKTOP GRID
                        html.Div(
                            dag.AgGrid(
                                id="sector-modal-grid",
                                className="ag-theme-alpine-dark tt-modal-grid",
                                columnDefs=_sector_modal_coldefs_desktop(),
                                rowData=[],
                                defaultColDef={
                                    "sortable": True,
                                    "filter": True,
                                    "resizable": True,
                                    "flex": 1,
                                },
                                dashGridOptions=grid_opts_desktop,
                                style={"height": "65vh", "width": "100%"},
                            ),
                            className="desktop-only",
                        ),
                        # MOBILE GRID
                        html.Div(
                            dag.AgGrid(
                                id="sector-modal-grid-m",
                                className="ag-theme-alpine-dark tt-modal-grid",
                                columnDefs=_sector_modal_coldefs_mobile(),
                                rowData=[],
                                defaultColDef={
                                    "sortable": True,
                                    "filter": False,
                                    "resizable": True,
                                    "flex": 1,
                                },
                                dashGridOptions=grid_opts_mobile,
                                style={"height": "72vh", "width": "100%"},
                            ),
                            className="mobile-only",
                        ),
                    ]
                )
            ),
        ],
        id="sector-modal",
        is_open=False,
        size="xl",
        centered=True,
        fullscreen="md-down",
        backdrop=True,
        keyboard=True,
    )


# =============================================================================
# PAGES
# =============================================================================
def sectors_page():
    top15_cols_desktop = [
        {
            "field": "Symbol",
            "headerName": "STOCK",
            "cellRenderer": "SymbolCell",
            "minWidth": 140,
            "flex": 2,
            "headerClass": "h-left",
            "cellClass": "c-left",
        },
        {
            "field": "%Change",
            "headerName": "%CHG",
            "cellRenderer": "PctPill",
            "minWidth": 110,
            "flex": 1,
            "headerClass": "ag-right-aligned-header",
            "cellClass": "ag-right-aligned-cell",
        },
        {
            "field": "RFactor",
            "headerName": "MOMENTUM",
            "cellRenderer": "RfactorPill",
            "minWidth": 110,
            "flex": 1,
            "headerClass": "ag-right-aligned-header",
            "cellClass": "ag-right-aligned-cell",
        },
        {
            "field": "Vol",
            "headerName": "VOLUME",
            "cellRenderer": "VolPill",
            "minWidth": 120,
            "flex": 1,
            "headerClass": "ag-right-aligned-header",
            "cellClass": "ag-right-aligned-cell",
        },
    ]

    top15_cols_mobile = [
        {
            "field": "Symbol",
            "headerName": "STOCK",
            "cellRenderer": "SymbolCell",
            "minWidth": 88,
            "flex": 2,
            "headerClass": "h-left",
            "cellClass": "c-left",
        },
        {
            "field": "%Change",
            "headerName": "%CHG",
            "cellRenderer": "PctPill",
            "minWidth": 70,
            "flex": 1,
            "headerClass": "ag-right-aligned-header",
            "cellClass": "ag-right-aligned-cell",
        },
        {
            "field": "RFactor",
            "headerName": "MOMENTUM",
            "cellRenderer": "RfactorPill",
            "minWidth": 74,
            "flex": 1,
            "headerClass": "ag-right-aligned-header",
            "cellClass": "ag-right-aligned-cell",
        },
        {
            "field": "Vol",
            "headerName": "VOL",
            "cellRenderer": "VolPill",
            "minWidth": 78,
            "flex": 1,
            "headerClass": "ag-right-aligned-header",
            "cellClass": "ag-right-aligned-cell",
        },
    ]

    grid_options_desktop = {
        "getRowId": {"function": "params.data.Symbol"},
        "animateRows": False,
        "rowHeight": 40,
        "headerHeight": 40,
        "domLayout": "normal",
    }

    grid_options_mobile = {
        "getRowId": {"function": "params.data.Symbol"},
        "animateRows": False,
        "rowHeight": 40,
        "headerHeight": 38,
        "domLayout": "normal",
        "onGridReady": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 80);"},
        "onGridSizeChanged": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 80);"},
    }

    def build_grid(grid_id: str, height: str, coldefs: list, grid_opts: dict):
        return dag.AgGrid(
            id=grid_id,
            className="ag-theme-alpine-dark grid-wrap",
            columnDefs=coldefs,
            rowData=[],
            defaultColDef={"sortable": True, "resizable": True, "flex": 1},
            dashGridOptions=grid_opts,
            style={"height": height, "width": "100%"},
        )

    return html.Div(
        [
            dcc.Interval(id="refresh_sectors", interval=5000, n_intervals=0),

            dbc.Row(
                [
                    dbc.Col(html.H4("Sectors", className="page-title page-title--pill mb-0"), width="auto"),
                    dbc.Col(
    html.Div(
        [
            # DESKTOP: keep radio buttons
            html.Div(
                dbc.RadioItems(
                    id="sectors-sort",
                    options=[
                        {"label": "Sort: RVOLm",      "value": "RVOLm"},
                        {"label": "Sort: RVOLm Mean", "value": "RVOLmMean"},
                        {"label": "Sort: Momentum",   "value": "DirR"},
                    ],
                    value="DirR",
                    inline=True,
                    className="sectors-sort ms-2",
                ),
                className="desktop-only",
            ),

            # MOBILE: dropdown with all 3
            html.Div(
                dbc.Select(
                    id="sectors-sort-dd",
                    options=[
                        {"label": "RVOLm",      "value": "RVOLm"},
                        {"label": "RVOLm Mean", "value": "RVOLmMean"},
                        {"label": "Momentum",   "value": "DirR"},
                    ],
                    value="DirR",
                    size="sm",
                    className="sectors-sort-dd",
                ),
                className="mobile-only",
            ),
        ]
    ),
    width=True,
),
                ],
                className="align-items-center g-2 mb-2",
            ),

            html.Div(id="sector-bars", className="sector-bars-wrap"),
            html.Hr(),

            # DESKTOP VIEW
            html.Div(
                dbc.Row(
                    [
                        dbc.Col(
                            [
                                html.H6("Top 15 Gainers", className="tt-top15-title tt-top15-gainers"),
                                build_grid("top15-gainers-grid", "350px", top15_cols_desktop, grid_options_desktop),
                            ],
                            md=6,
                        ),
                        dbc.Col(
                            [
                                html.H6("Top 15 Losers", className="tt-top15-title tt-top15-losers"),
                                build_grid("top15-losers-grid", "350px", top15_cols_desktop, grid_options_desktop),
                            ],
                            md=6,
                        ),
                    ],
                    className="g-3",
                ),
                className="desktop-only",
            ),

            # MOBILE VIEW (TABS)
            html.Div(
                dbc.Tabs(
                    [
                        dbc.Tab(
                            label="Top 15 Gainers",
                            children=build_grid("top15-gainers-grid-m", "60vh", top15_cols_mobile, grid_options_mobile),
                        ),
                        dbc.Tab(
                            label="Top 15 Losers",
                            children=build_grid("top15-losers-grid-m", "60vh", top15_cols_mobile, grid_options_mobile),
                        ),
                    ],
                    className="top15-tabs",
                ),
                className="mobile-only",
            ),

            html.Hr(),

            # HEATMAP
            html.H6("Heatmap"),
            dcc.Graph(
                id="market-heatmap",
                config={"displayModeBar": True, "displaylogo": False, "responsive": True},
                style={"height": "75vh", "width": "100%"},
            ),

            html.Hr(),

            # DIALS
            dbc.Row(
                [
                    dbc.Col(dial_component("sentiment", "BIAS"), md=6),
                    dbc.Col(dial_component("pcr", "PCR"), md=6),
                ],
                className="g-3",
            ),
        ],
        className="page-wrap",
    )


def top15_buy_sell_rvolm_rows(n: int = 15):
    snap = _snapshot_state(include_hot=False)
    tf   = _time_factor_ist_for_rvol(datetime.now(IST))
    buy  = []
    sell = []

    for sym in ALL_SYMBOLS:
        tok = symbol_to_token.get(sym)
        if not tok:
            continue

        st = _get_live_or_eod_state_from_snap(tok, snap)
        if not st:
            continue

        ltp, vol_today, ohlc = st
        op = (ohlc or {}).get("open")
        if op is None:
            continue

        try:
            ltp       = float(ltp)
            vol_today = float(vol_today)
            op        = float(op)
        except Exception:
            continue

        if op <= 0:
            continue

        st20 = (snap.get("daily") or {}).get(tok) or {}
        avg_vol_20 = st20.get("avg_vol_20")
        try:
            avg_vol_20 = float(avg_vol_20) if avg_vol_20 is not None else None
        except Exception:
            avg_vol_20 = None

        if not avg_vol_20 or avg_vol_20 <= 0:
            continue

        pct_open = (ltp - op) / op * 100.0
        expected = avg_vol_20 * tf
        rvolm    = vol_today / (expected + 1e-9)

        row = {
            "Symbol" : sym,
            "%Change": round(float(pct_open), 2),
            "RVOLm"  : round(float(rvolm), 2),
            "Vol"    : int(vol_today),
        }

        if pct_open >= 0:
            buy.append(row)
        else:
            sell.append(row)

    buy.sort(key=lambda x:  float(x.get("RVOLm") or 0.0), reverse=True)
    sell.sort(key=lambda x: float(x.get("RVOLm") or 0.0), reverse=True)
    return buy[:n], sell[:n]


def volm_page():
    cols = [
        {"colId": "stock", "field": "Symbol",  "headerName": "STOCK",  "cellRenderer": "SymbolCell",
         "minWidth": 140, "maxWidth": 170, "suppressSizeToFit": True,
         "headerClass": "h-left", "cellClass": "c-left"},
        {"colId": "pct",   "field": "%Change", "headerName": "%CHG",   "cellRenderer": "PctPill",
         "minWidth": 140, "maxWidth": 150, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right",
         "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "rvolm", "field": "RVOLm",   "headerName": "RVOLm",  "cellRenderer": "Num2Cell",
         "minWidth": 120, "maxWidth": 140, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right",
         "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "vol",   "field": "Vol",     "headerName": "VOLUME", "cellRenderer": "VolPill",
         "minWidth": 150, "maxWidth": 190, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right",
         "cellClass": "ag-right-aligned-cell cell-num c-right"},
    ]

    grid_opts = {
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": False,
        "animateRows": False,
        "suppressMenuHide": False,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    return html.Div(
        [
            dcc.Interval(id="refresh_volm", interval=5000, n_intervals=0),

            dbc.Row(
                [
                    dbc.Col(
                        dcc.Link("← Back", href=BASE, className="stat-chip", style={"textDecoration": "none"}),
                        width="auto",
                    ),
                    dbc.Col(html.H4("Volm (RVOLm)", className="page-title mb-0"), width=True),
                ],
                className="align-items-center g-2 mb-2",
            ),

            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.H6("Top 15 BUY (by RVOLm)", className="mt-1"),
                            dag.AgGrid(
                                id="volm-buy-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 56vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6("Top 15 SELL (by RVOLm)", className="mt-1"),
                            dag.AgGrid(
                                id="volm-sell-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 56vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                ],
                className="g-2",
            ),
        ],
        className="page-wrap",
    )


# =============================================================================
# DASH ROOT LAYOUT
# =============================================================================
dash_app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Location(id="url"),
        dcc.Store(id="page-store"),
        dcc.Interval(id="top_refresh", interval=1000, n_intervals=0),

        html.Div(
    dbc.Row(
        [
            # BRAND
            dbc.Col(
                html.Div(
                    [html.Img(src=dash.get_asset_url("turbotrades.svg"), className="tt-logo")],
                    className="tt-brand",
                ),
                width="auto",
                className="top-brand-col",
            ),

            # CHIPS
            dbc.Col(
                html.Div(id="top-stats"),
                width=True,
                className="top-stats-col",
                style={"minWidth": 0},  # IMPORTANT: allows flex shrink / prevents wrap bugs
            ),

            # THEME TOGGLE
            dbc.Col(
                html.Button(
                    [
                        html.I(className="bi bi-sun icon-sun", **{"aria-hidden": "true"}),
                        html.I(className="bi bi-moon-stars icon-moon", **{"aria-hidden": "true"}),
                    ],
                    id="themeToggle",
                    className="theme-toggle",
                    type="button",
                    title="Toggle theme",
                    **{"aria-label": "Toggle theme"},
                ),
                width="auto",
                className="top-theme-col",
            ),

            # LOGOFF
            dbc.Col(
                dbc.Button(
                    "LogOff",
                    href="/auth/logout",
                    external_link=True,
                    color="danger",
                    outline=True,
                    size="sm",
                    className="tt-logout-btn",
                    style={"fontWeight": "700"},
                ),
                width="auto",
                className="top-logoff-col",
            ),
        ],
        className="topbar-row align-items-center g-2",
    ),
    className="topbar-wrap",
),

        html.Div(id="app-body"),
        sector_modal_component(),
    ],
)


# =============================================================================
# ROUTER
# =============================================================================
def _classify_page(pathname: str) -> str:
    pn = (pathname or "").strip() or "/"
    volm_paths = {"/volm", "/volm/", f"{BASE}volm", f"{BASE}volm/"}
    oi_paths   = {"/openinterest", "/openinterest/", f"{BASE}openinterest", f"{BASE}openinterest/"}

    if pn in volm_paths:
        return "volm"
    if pn in oi_paths:
        return "openinterest"
    return "sectors"


@dash_app.callback(
    Output("app-body", "children"),
    Output("page-store", "data"),
    Input("url", "pathname"),
    State("page-store", "data"),
)
def route(pathname, current_page):
    page = _classify_page(pathname)
    if current_page == page:
        return dash.no_update, current_page

    if page == "volm":
        return volm_page(), "volm"

    if page == "openinterest":
        return html.Iframe(
            src="/openinterest",
            style={
                "width": "100%",
                "height": "calc(100vh - 140px)",
                "border": "0",
                "borderRadius": "16px",
            },
        ), "openinterest"

    return sectors_page(), "sectors"


# =============================================================================
# TOP CHIPS
# =============================================================================
def _oi_inference_chip():
    try:
        with openinterest.state_lock:
            s = dict(openinterest.state)
    except Exception:
        s = {}

    baseline_ok = (s.get("baseline_price") is not None) and (s.get("baseline_oi") is not None)
    bt_raw      = (s.get("buildup_type") or "NO_CLEAR")
    bt          = bt_raw.replace("_", " ")
    bias        = (s.get("bias") or "NEUTRAL").upper()
    label       = s.get("label") or ""

    if not baseline_ok:
        return html.Div("OI: WAITING BASELINE", className="stat-chip", title=label)

    text = f"OI: {bt} • {bias}"

    if bias == "BULLISH":
        style = {"color": "var(--good)", "borderColor": "rgba(46, 213, 115, 0.55)"}
    elif bias == "BEARISH":
        style = {"color": "var(--bad)", "borderColor": "rgba(255, 71, 87, 0.55)"}
    else:
        style = {}

    return html.Div(text, className="stat-chip", style=style, title=label)


@dash_app.callback(Output("top-stats", "children"), Input("top_refresh", "n_intervals"))
def update_top_stats(_):
    updated_str = datetime.now(IST).strftime("%H:%M:%S")

    with LOCK:
        offline  = (time.time() - LAST_TICK_TS) > 10 if LAST_TICK_TS else True
        tot      = TOTAL_TICKS
        d_done   = DAILY_SEED_DONE
        d_done_n = int(DAILY_SEED_PROGRESS.get("done", 0) or 0)
        d_total  = int(DAILY_SEED_PROGRESS.get("total", 0) or 0)
        d_err    = int(DAILY_SEED_ERRORS or 0)

    with CACHE_LOCK:
        sm = dict(CACHE.get("sentiment") or {})
        pn = CACHE.get("pcr")

    sent_label = str(sm.get("label") or "NEUTRAL").upper()
    sent_score = float(sm.get("score") or 0.0)
    adv        = int(sm.get("adv",  0) or 0)
    dec        = int(sm.get("dec",  0) or 0)
    unch       = int(sm.get("unch", 0) or 0)

    if sent_label == "BULLISH":
        sent_style = {"color": "var(--good)", "borderColor": "rgba(46, 213, 115, 0.55)"}
    elif sent_label == "BEARISH":
        sent_style = {"color": "var(--bad)", "borderColor": "rgba(255, 71, 87, 0.55)"}
    else:
        sent_style = {}

    sentiment_chip = html.Div(
        f"BIAS: {sent_label} ({sent_score:+.2f}) • {adv} ↑ • {dec} ↓",
        className="stat-chip",
        style=sent_style,
        title=f"Adv {adv} • Dec {dec} • Unch {unch}",
    )

    if pn and pn.get("pcr") is not None:
        pcr     = float(pn["pcr"])
        pcr_lbl = pcr_label_from_value(pcr)

        if pcr_lbl in ("BUY", "STRONG BUY"):
            pcr_style = {"color": "var(--good)", "borderColor": "rgba(46, 213, 115, 0.55)"}
        elif pcr_lbl in ("SELL", "STRONG SELL"):
            pcr_style = {"color": "var(--bad)", "borderColor": "rgba(255, 71, 87, 0.55)"}
        else:
            pcr_style = {}

        pcr_chip = html.Div(
            f"PCR: {pcr:.2f} ({pcr_lbl})",
            className="stat-chip",
            style=pcr_style,
            title=f"Expiry {pn.get('expiry')} • ATM {pn.get('atm')} • Time {pn.get('updated_at')}",
        )
    else:
        pcr_chip = html.Div("PCR: LOADING", className="stat-chip")

    chips = [
    dbc.Badge("Offline" if offline else "Live", color=("danger" if offline else "success"), className="stat-badge"),
    html.A(
        "Volm",
        href=f"{BASE}volm",
        target="_blank",
        className="stat-chip",
        style={"textDecoration": "none", "marginLeft": "8px", "cursor": "pointer"},
    ),
    _oi_inference_chip(),
    sentiment_chip,
    # pcr_chip,  # <-- hidden from top nav
]

    if not d_done:
        chips.append(
            dbc.Badge(
                f"Seeding {d_done_n}/{d_total} (err {d_err})",
                color="warning",
                className="stat-badge",
                style={"marginLeft": "8px"},
            )
        )

    chips += [
        html.Div(f"Ticks {tot:,}", className="stat-chip"),
        html.Div(f"Time {updated_str}", className="stat-chip"),
    ]

    return html.Div(chips, className="top-stats-wrap")


@dash_app.callback(
    Output("sector-bars", "children"),
    Input("refresh_sectors", "n_intervals"),
    Input("sectors-sort", "value"),
    Input("sectors-sort-dd", "value"),
)
def render_sector_bars(_n, sort_by_radio, sort_by_dd):
    try:
        trig = ctx.triggered_id

        # Pick from the control that actually changed
        if trig == "sectors-sort":
            sort_by = sort_by_radio
        elif trig == "sectors-sort-dd":
            sort_by = sort_by_dd
        else:
            sort_by = sort_by_radio or sort_by_dd

        sort_by = (sort_by or "DirR").strip()

        # -----------------------------
        # Metric selection
        # -----------------------------
        if sort_by == "DirR":
            metric = "DirR"
        elif sort_by == "RVOLmMean":
            metric = "RVOLmNetMean"
        else:
            metric = "RVOLmNetSum"

        # -----------------------------
        # Load cached aggregates
        # -----------------------------
        with CACHE_LOCK:
            agg = dict(CACHE.get("sector_agg") or {})

        items = sorted(
            agg.items(),
            key=lambda kv: float(kv[1].get(metric, 0.0) or 0.0),
            reverse=True,
        )
        if not items:
            return html.Div("Loading sector bars…", className="hint")

        vals = [float(m.get(metric, 0.0) or 0.0) for _, m in items]
        n = len(vals)

        # -----------------------------
        # Helpers
        # -----------------------------
        def pct(sorted_list, p: float) -> float:
            if not sorted_list:
                return 0.0
            i = int(p * (len(sorted_list) - 1))
            i = max(0, min(len(sorted_list) - 1, i))
            return float(sorted_list[i])

        def fmt_dec(x: float) -> str:
            x = float(x)
            if abs(x) < 5e-7:
                x = 0.0
            return f"{x:.2f}"

        # -----------------------------
        # Make UP/DOWN more visible:
        # Asymmetric percentile caps (separate + and -)
        # -----------------------------
        pos_abs = sorted([v for v in vals if v > 0])          # positive values
        neg_abs = sorted([abs(v) for v in vals if v < 0])     # absolute of negative values
        
        if metric == "DirR":
         pos_cap = max(pos_abs) if pos_abs else 0.30
         neg_cap = max(neg_abs) if neg_abs else 0.30

        if metric == "DirR":
           CAP_Q   = 0.99   # was 0.82 (too aggressive)
           CAP_MUL = 1.00
           MIN_CAP = 0.05
           GAMMA   = 0.85   # closer to linear so big differences show
        else:
            CAP_Q   = 0.88
            CAP_MUL = 1.20
            MIN_CAP = 0.50
            GAMMA   = 0.62

        pos_cap = max(pct(pos_abs, CAP_Q) * CAP_MUL, MIN_CAP) if pos_abs else MIN_CAP
        neg_cap = max(pct(neg_abs, CAP_Q) * CAP_MUL, MIN_CAP) if neg_abs else MIN_CAP
        
                # -----------------------------
        # Display DirR as 1x,2x,3x...
        # (only changes labels, not sorting/heights)
        # -----------------------------
        if metric == "DirR":
            X_MAX = 6.0  # strongest sector will be around ~6x (tweak if you want)
            cap_abs = max(float(pos_cap), float(neg_cap), 1e-6)
            x_unit = cap_abs / X_MAX  # 1x equals this DirR value

            def fmt_tick(v: float) -> str:
                return f"{(float(v) / x_unit):+.0f}x"

            def fmt_tip(v: float) -> str:
                return f"{(float(v) / x_unit):+.0f}x"
        else:
            fmt_tick = fmt_dec
            fmt_tip  = fmt_dec

        # Axis bounds from caps (NOT from true max)
        tick_max = float(pos_cap)
        tick_min = -float(neg_cap)
        axis_span = float(tick_max - tick_min) or 1.0

        # Zero-line position in %
        zero_pct = ((tick_max - 0.0) / axis_span) * 100.0
        zero_pct = max(0.0, min(100.0, zero_pct))

        plot_h = SECTOR_PLOT_H_PX
        pos_px = plot_h * (zero_pct / 100.0)
        neg_px = plot_h - pos_px

        pos_dom = max(0.0, tick_max)
        neg_dom = max(0.0, -tick_min)
        eps = 1e-12

        # Axis tick labels
        ticks = [tick_max, tick_max / 2.0, 0.0, tick_min / 2.0, tick_min]
        axis_ticks = []
        for tv in ticks:
            top_pct = ((tick_max - float(tv)) / axis_span) * 100.0
            axis_ticks.append(
                html.Div(
                    fmt_tick(tv),
                    className="sector-axis-tick",
                    style={"top": f"{top_pct:.2f}%"},
                )
            )

        axis = html.Div(axis_ticks, className="sector-hist-axis", style={"height": f"{plot_h}px"})
        children = [axis, html.Div(className="sector-hist-zero-line")]

        # Bigger minimum so “small” bars don’t look like dots
        bar_min_px = 6.0

        def to_px(val: float) -> float:
            if val >= 0:
                if pos_dom <= 0 or pos_px <= 0:
                    return 0.0
                x = val / (pos_dom + eps)  # 0..1
                x = max(0.0, min(1.0, x))
                x = x ** GAMMA
                return max(0.0, min(pos_px, x * pos_px))
            else:
                if neg_dom <= 0 or neg_px <= 0:
                    return 0.0
                x = (-val) / (neg_dom + eps)  # 0..1
                x = max(0.0, min(1.0, x))
                x = x ** GAMMA
                return max(0.0, min(neg_px, x * neg_px))

        for sector, m in items:
            val = float(m.get(metric, 0.0) or 0.0)
            disp = sector.replace("_", " ").upper()
            val_str = fmt_tip(val)

            # Clip separately for + and -
            clipped = (val > pos_cap) or (val < -neg_cap)
            if val >= 0:
                val_eff = min(val, pos_cap)
            else:
                val_eff = max(val, -neg_cap)

            bar_px = to_px(val_eff)
            if 0 < bar_px < bar_min_px:
                bar_px = bar_min_px

            children.append(
                dcc.Link(
                    href=f"{BASE}sector/{sector}",
                    className="sector-hist-link",
                    refresh=False,
                    children=html.Div(
                        [
                            html.Div(
                                [
                                    html.Div(disp, className="sector-hist-tip-name"),
                                    html.Div(val_str, className="sector-hist-tip-val"),
                                ],
                                className="sector-hist-tooltip",
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        className=("sector-hist-bar pos" if val_eff >= 0 else "sector-hist-bar neg"),
                                        style={"height": f"{bar_px:.2f}px"},
                                    ),
                                    # Optional cap marker (only if you added CSS)
                                    html.Div(
                                        className="sector-bar-cap",
                                        style={"top": "6px"} if val_eff >= 0 else {"bottom": "6px"},
                                    ) if clipped else None,
                                ],
                                className="sector-hist-track",
                                style={
                                    "height": f"{plot_h}px",
                                    "overflow": "hidden",
                                    "position": "relative",
                                    "marginBottom": "16px",
                                },
                            ),
                            html.Div(disp, className="sector-hist-name", style={"marginTop": "4px"}),
                        ],
                        className="sector-hist-col",
                        title=f"{metric} {val_str}",
                    ),
                )
            )

        return html.Div(
            children,
            className="sector-hist-plot",
            style={"--zero": f"{zero_pct:.2f}%", "--axisW": "68px"},
        )

    except Exception as e:
        log.exception("render_sector_bars crashed")
        return html.Div(
            f"Sector bars error: {type(e).__name__}: {e}",
            className="hint",
            style={"color": "red", "padding": "20px", "fontSize": "14px"},
        )


# =============================================================================
# SECTOR MODAL
# =============================================================================
def sector_rows_sorted(sector: str, sort_by: str = "RFactor"):
    rows = []
    tf   = _time_factor_ist_for_rvol(datetime.now(IST))
    snap = _snapshot_state(include_hot=False)

    for s in SECTOR_DEFINITIONS.get(sector, []):
        tok = symbol_to_token.get(s)
        if not tok:
            continue

        rr = _compute_rfactor_row_snap(tok, snap)
        if not rr:
            continue

        pct_open = float(rr["pct_open"])
        gap_pct  = float(rr["gap_pct"])
        ltp      = float(rr["ltp"])

        st        = (snap.get("daily") or {}).get(tok) or {}
        avg_vol_20 = st.get("avg_vol_20")
        vol_today  = rr.get("vol_today")

        rvolm = None
        try:
            if avg_vol_20 and vol_today is not None and float(avg_vol_20) > 0:
                expected = float(avg_vol_20) * float(tf)
                rvolm    = float(vol_today) / (expected + 1e-9)
        except Exception:
            rvolm = None

        rows.append({
            "Symbol" : s,
            "Company": symbol_to_name.get(s, ""),
            "DirR"   : float(rr["dirr"]),
            "Price"  : ltp,
            "%Change": pct_open,
            "Gap%"   : gap_pct,
            "RVOLm"  : rvolm,
            "RFactor": float(rr["rfactor"]),
        })

    if not rows:
        return []

    sb = (sort_by or "").strip().upper()
    if sb in ("RVOL", "RVOLM"):
        key = "RVOLm"
    elif sb in ("DIRR", "DIR R"):
        key = "DirR"
    elif sb in ("%CHANGE", "%CHG", "CHG"):
        key = "%Change"
    else:
        key = "RFactor"

    def sort_val(x):
        v = x.get(key)
        return float(v) if v is not None else float("-inf")

    rows.sort(key=sort_val, reverse=True)
    return rows


@dash_app.callback(
    Output("sector-modal", "is_open"),
    Output("sector-modal-title", "children"),
    Output("sector-modal-grid", "rowData"),
    Output("sector-modal-grid-m", "rowData"),
    Input("url", "pathname"),
    Input("top_refresh", "n_intervals"),
)
def sync_sector_modal(pathname, _tick):
    sector = _extract_sector_from_path(pathname)
    if sector and sector in SECTOR_DEFINITIONS:
        rows  = sector_rows_sorted(sector, sort_by="RFactor")
        title = sector.replace("_", " ").title()
        return True, title, rows, rows
    return False, "Sector", [], []


# =============================================================================
# DIALS + LEADERBOARDS
# =============================================================================
def _state_class(label: str) -> str:
    L = (label or "").upper().strip()
    L = " ".join(L.split())
    if L == "STRONG SELL": return "state-ss"
    if L == "SELL":        return "state-sell"
    if L == "NEUTRAL":     return "state-neutral"
    if L == "BUY":         return "state-buy"
    if L == "STRONG BUY":  return "state-sb"
    if L == "BEARISH":     return "state-sell"
    if L == "BULLISH":     return "state-buy"
    return "state-neutral"


def _fmt_oi_compact(v: Optional[float]) -> str:
    if v is None:
        return "—"
    n = float(v)
    a = abs(n)
    if a >= 1e7: return f"{n/1e7:.2f}Cr"
    if a >= 1e5: return f"{n/1e5:.2f}L"
    if a >= 1e3: return f"{n/1e3:.2f}K"
    return str(int(round(n)))


@dash_app.callback(
    Output("sentiment-needle", "style"),
    Output("sentiment-sub", "children"),
    Output("pcr-needle", "style"),
    Output("pcr-sub", "children"),
    Input("refresh_sectors", "n_intervals"),
)
def update_dials(_):
    with CACHE_LOCK:
        sm = dict(CACHE.get("sentiment") or {})
        pn = CACHE.get("pcr")

    score      = float(sm.get("score") or 0.0)
    sent_angle = max(-90.0, min(90.0, score * 90.0))
    sent_style = {"--rot": f"{sent_angle:.2f}deg"}
    sent_label = str(sm.get("label") or "NEUTRAL")

    sent_sub = html.Span(
        [
            html.Span(sent_label, className=f"dial-state {_state_class(sent_label)}"),
            html.Span(f"{score:+.2f} • {sm.get('adv',0)} ↑ • {sm.get('dec',0)} ↓", className="dial-meta"),
        ],
        className="dial-sub-inner",
    )

    if pn and pn.get("pcr") is not None:
        pcr         = float(pn["pcr"])
        label       = pcr_label_from_value(pcr)
        pcr_clamped = max(0.0, min(2.0, pcr))
        pcr_angle   = (pcr_clamped - 1.0) * 90.0
        pcr_style   = {"--rot": f"{pcr_angle:.2f}deg"}
        pe_txt      = _fmt_oi_compact(pn.get("pe_oi"))
        ce_txt      = _fmt_oi_compact(pn.get("ce_oi"))

        pcr_sub = html.Span(
            [
                html.Span(label, className=f"dial-state {_state_class(label)}"),
                html.Span(f"PCR {pcr:.2f} • PE {pe_txt} • CE {ce_txt}", className="dial-meta"),
            ],
            className="dial-sub-inner",
        )
    else:
        pcr_style = {"--rot": "0deg"}
        pcr_sub   = html.Span(
            [
                html.Span("LOADING", className="dial-state state-neutral"),
                html.Span("PCR", className="dial-meta"),
            ],
            className="dial-sub-inner",
        )

    return sent_style, sent_sub, pcr_style, pcr_sub


@dash_app.callback(
    Output("top15-gainers-grid",   "rowData"),
    Output("top15-losers-grid",    "rowData"),
    Output("top15-gainers-grid-m", "rowData"),
    Output("top15-losers-grid-m",  "rowData"),
    Input("refresh_sectors", "n_intervals"),
)
def update_rfactor_leaderboards(_):
    with CACHE_LOCK:
        g = list(CACHE.get("top15_gainers") or [])
        l = list(CACHE.get("top15_losers")  or [])
    return g, l, g, l


@dash_app.callback(
    Output("volm-buy-grid",  "rowData"),
    Output("volm-sell-grid", "rowData"),
    Input("refresh_volm", "n_intervals"),
)
def update_volm_grids(_):
    return top15_buy_sell_rvolm_rows(n=15)


@dash_app.callback(
    Output("market-heatmap", "figure"),
    Input("refresh_sectors", "n_intervals"),
)
def update_market_heatmap(_):
    with CACHE_LOCK:
        rows = list(CACHE.get("heatmap_rows") or [])
    return build_market_heatmap_figure(rows)


# =============================================================================
# STARTUP/SHUTDOWN FOR WRAPPER
# =============================================================================
async def _startup():
    init_u_curve_once()
    start_pace_curve_builder_once()
    seed_daily_stats_once(per_req_sleep=SEED_SLEEP_SEC)
    start_ticker_once()
    load_nfo_instruments_once()
    start_compute_loop_once()
    await openinterest.on_startup()


async def _shutdown():
    await openinterest.on_shutdown()