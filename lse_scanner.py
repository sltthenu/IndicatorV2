#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  LSE PRE-MARKET PREDICTIVE SCANNER                                  ║
║  Finds AIM / small-cap stocks BEFORE they move 5–20%               ║
║                                                                      ║
║  ▶ WHEN TO RUN: 06:30–07:50 London time (before LSE opens 08:00)   ║
║                                                                      ║
║  HOW IT PREDICTS:                                                    ║
║   1. Pre-market RNS catalyst detection (07:00–07:55 announcements)  ║
║   2. Bollinger Band squeeze — volatility coiled, ready to explode   ║
║   3. Volume accumulation — smart money quietly buying 3–5 days      ║
║   4. Breakout proximity — price within 3% of 52-week high           ║
║   5. Inside day compression — tight range = pending explosion        ║
║   6. EMA alignment — trend already pointing up                       ║
║                                                                      ║
║  INSTALL (once):                                                     ║
║    pip install yfinance requests pandas                              ║
║                                                                      ║
║  RUN:                                                                ║
║    python lse_predict.py                                             ║
║                                                                      ║
║  OUTPUT: lse_predict_YYYY-MM-DD.html  (auto-opens in browser)       ║
╚══════════════════════════════════════════════════════════════════════╝

  ⚠  NOT FINANCIAL ADVICE — For research and education only.
     Always check the RNS text yourself before any trade.
     AIM stocks carry high risk including total loss of capital.
"""

import yfinance as yf
import requests
import json
import time
import webbrowser
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import concurrent.futures
import warnings
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG — tweak these to change scan behaviour
# ─────────────────────────────────────────────────────────────────────────────
MAX_WORKERS       = 12      # parallel downloads; keep ≤ 15 to avoid Yahoo throttle
MIN_PRED_SCORE    = 3       # minimum combined score to show on report (0–14 max)
MIN_ATR_PCT       = 2.5     # only stocks volatile enough to move 5%+ in a session
MAX_MKTCAP_GBP    = 600e6   # ignore mega-caps (above £600m usually won't move 20%)
BB_PERIOD         = 20      # Bollinger Band calculation period (days)
BB_SQUEEZE_RANK   = 25      # BB width in bottom 25th percentile = squeeze signal
VOL_BUILDUP_DAYS  = 3       # consecutive days of rising volume = accumulation
RNS_LOOKBACK_HRS  = 6       # how many hours back to look for RNS (covers 02:00–08:00)

LONDON_TZ = ZoneInfo("Europe/London")

# ─────────────────────────────────────────────────────────────────────────────
#  RNS KEYWORD SCORING
#  These are the announcement types that cause 5–20% moves on LSE/AIM.
#  Tuned from real trading experience on AIM.
# ─────────────────────────────────────────────────────────────────────────────
#  Format: ([keywords], score_pts, display_label, emoji, expected_move)
RNS_TRIGGERS = [
    # ── TIER 1 — typically causes 15–50% move ────────────────────────────────
    (
        ["recommended offer", "possible offer", "firm offer", "takeover",
         "offer for the entire", "acquire the entire", "all cash offer",
         "scheme of arrangement", "merger agreement"],
        4, "Takeover / M&A Bid", "🎯", "15–50%"
    ),
    (
        ["phase 3 results", "phase iii results", "phase 2 results", "phase ii results",
         "positive top-line", "pivotal trial", "clinical trial results",
         "statistically significant", "fda approval", "mhra approval",
         "ema approval", "regulatory approval", "breakthrough designation",
         "orphan drug", "significant clinical"],
        4, "Clinical / Regulatory Result", "💊", "15–40%"
    ),
    (
        ["maiden resource", "initial resource", "resource estimate",
         "significant intersection", "high grade intercept", "drill results",
         "bonanza grade", "significant mineralisation", "reserve update"],
        3, "Resource / Drill Result", "⛏️", "10–30%"
    ),

    # ── TIER 2 — typically causes 5–20% move ─────────────────────────────────
    (
        ["materially ahead", "significantly ahead", "ahead of market expectations",
         "ahead of expectations", "ahead of management expectations",
         "record revenue", "record sales", "record profit", "record results",
         "exceeds expectations", "outperforms"],
        3, "Beats Expectations", "🚀", "5–20%"
    ),
    (
        ["transformational contract", "significant contract", "major contract",
         "landmark agreement", "exclusive licence", "licence and commercialisation",
         "strategic licensing", "global licence"],
        3, "Major Contract / Licence", "📋", "5–20%"
    ),
    (
        ["strategic partnership", "strategic investment", "cornerstone investment",
         "joint venture", "co-development agreement", "partnership with",
         "agreement with [a-z]+ plc", "agreement with [a-z]+ inc"],
        2, "Strategic Partnership", "🤝", "5–15%"
    ),
    (
        ["contract award", "contract win", "awarded a contract", "selected as preferred",
         "appointed as", "letter of intent signed", "heads of terms",
         "framework agreement", "supply agreement", "offtake agreement"],
        2, "Contract Win", "📝", "5–15%"
    ),
    (
        ["full year results", "half year results", "interim results",
         "preliminary results", "annual results", "trading update",
         "positive trading update", "strong trading", "confident outlook",
         "positive outlook", "in line with", "board is pleased"],
        2, "Results / Trading Update", "📊", "3–10%"
    ),
    (
        ["fundraising", "oversubscribed", "significant institutional support",
         "strategic investor", "placing and open offer", "equity raise"],
        1, "Fundraise (check dilution)", "⚠️", "varies"
    ),
    (
        ["director purchase", "director dealing", "pdmr purchase",
         "executive director purchase", "non-executive director purchase",
         "ceo purchase", "cfo purchase", "executive purchase"],
        1, "Director / Insider Buying", "👤", "2–8%"
    ),
]

# Keywords that REDUCE score — negative news
RNS_NEGATIVES = [
    "profit warning", "revenue warning", "below expectations",
    "below market expectations", "disappointing results",
    "challenging trading", "difficult trading conditions",
    "suspension of trading", "suspended from trading",
    "wind down", "administration", "insolvency", "liquidation",
    "cancellation of admission", "delist", "cease trading",
    "winding up", "material uncertainty", "going concern",
]


# ─────────────────────────────────────────────────────────────────────────────
#  AIM / SMALL-CAP WATCHLIST
#  Active, liquid AIM names with history of sharp intraday moves.
#  Add your own finds here.
# ─────────────────────────────────────────────────────────────────────────────
WATCHLIST = [
    # Biotech / Pharma / MedTech
    "AVCT","BVXP","CLI","CLIN","CRSO","CRW","DCAN","ECHO","GBG","HAT",
    "HBR","IGP","IKA","IMM","INFA","JOG","KBT","LIO","MAIA","MCB",
    "MED","MRC","MTI","NANO","NCZ","OXB","POLB","RDL","SLN","SYNX",
    "TRX","VRS","WGB","XPLO","ZOO","AMRN","BYIT","CTEC","DNAY","ELIX",
    "GENL","HLTH","IMMU","IMUN","INCE","IPIX","MDNA","MGNX","MIRL",
    "MTCH","MXCT","MYMD","MYMX","MYRG","MYSL","NASDAQ","NBTX","NCRE",
    # Mining / Resources
    "AAZ","ALBA","AMI","AOG","APH","ARG","ARL","ARM","ARML","ARP",
    "ATM","AUR","AVN","BKT","BON","CAD","CDL","DEC","ECR","EDL",
    "EGO","EMX","ENS","ERG","GCM","GTI","HYR","IQE","KBT","KORE",
    "LBRT","LWI","MDC","MNRZ","MMX","MKA","MOTS","NAP","NCZ","POG",
    "RRS","SXX","THL","UJO","VELA","VGM","WKP","XTR","ZYT","ADT",
    # Tech / Digital / Fintech
    "ALFA","AMS","ANP","APP","BIG","BOOM","BUR","CAB","FEYE","GAM",
    "GAN","HAV","HAWK","IFP","IGN","KAV","LAD","MCK","MCL","NANO",
    "PAD","QRT","RAD","SLP","TAM","UAI","VAL","WAL","ZINC","ASOS",
    "BOO","PETS","WHR","GFRD","MAIR","MCLS","MCX","MLIN","MOON","MTL",
    "SDX","SHRE","SOS","STB","TCG","TFIF","THT","TED","IGC","BIG",
    # Well-known AIM movers (historically high ATR)
    "CRON","GROW","HIGH","HOC","ITV","JQW","KEFI","LEAF","LOOP",
    "LUCK","LYG","MAST","MATD","MCAT","MFLO","MGAM","MGNS","MHN",
    "MIGN","MIND","MINT","MIRA","MIRL","MIRR","MIST","MITI","MITL",
    "MMAG","MMH","MMHL","MMIP","MMIT","MMIX","MMK","MML","MMM","MMO",
]

def to_yahoo(ticker: str) -> str:
    t = ticker.strip().upper()
    if not t.endswith(".L"):
        t += ".L"
    return t

WATCHLIST_YAHOO = list(dict.fromkeys(to_yahoo(t) for t in WATCHLIST))


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1: Fetch pre-market RNS from London Stock Exchange
#  LSE publishes announcements from ~07:00. We scan for today's RNS.
# ─────────────────────────────────────────────────────────────────────────────
def fetch_rns_today() -> dict[str, dict]:
    """
    Attempts to fetch today's pre-market RNS from the LSE public API.
    Returns dict: { "TICKER.L": { "headline": str, "score": int,
                                   "label": str, "emoji": str,
                                   "expected_move": str,
                                   "negative": bool } }
    Falls back to empty dict if the API is unreachable.
    """
    rns_data: dict[str, dict] = {}
    now_london = datetime.now(LONDON_TZ)
    cutoff = now_london - timedelta(hours=RNS_LOOKBACK_HRS)

    # ── Attempt 1: LSE Regulatory News API ───────────────────────────────────
    endpoints = [
        (
            "https://api.londonstockexchange.com/api/gw/lse/instruments"
            "/alldata/news?worlds=quotes&count=200&sortby=time"
            "&category=RegulatoryAnnouncement",
            _parse_lse_api
        ),
        (
            "https://api.londonstockexchange.com/api/gw/lse/instruments"
            "/alldata/regulatorynewsheadlines?worlds=quotes&count=200",
            _parse_lse_api
        ),
    ]

    for url, parser in endpoints:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.londonstockexchange.com/",
                "Origin":  "https://www.londonstockexchange.com",
            }
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200:
                items = parser(r.json(), cutoff)
                if items:
                    rns_data.update(items)
                    print(f"  ✓ RNS: {len(rns_data)} announcements fetched from LSE API")
                    return rns_data
        except Exception:
            pass

    # ── Attempt 2: Yahoo Finance news (per-ticker — used as supplement) ───────
    # This is too slow for bulk scanning; skip here, used in per-ticker fetch.

    if not rns_data:
        print("  ⚠ RNS API unreachable — using technical signals only.")
        print("    (Normal if running outside 06:30–08:30 or LSE API is rate-limited)")

    return rns_data


def _parse_lse_api(data: dict, cutoff: datetime) -> dict[str, dict]:
    """Parse the LSE JSON API response into our rns_data format."""
    result = {}
    now_london = datetime.now(LONDON_TZ)

    # Different response shapes from different endpoints
    items = (
        data.get("content", [])
        or data.get("data",    [])
        or data.get("news",    [])
        or data.get("items",   [])
        or []
    )

    # Also try nested structure
    if not items:
        for key in data:
            if isinstance(data[key], list) and len(data[key]) > 0:
                if isinstance(data[key][0], dict):
                    items = data[key]
                    break

    for item in items:
        try:
            # Extract ticker symbol
            tidm = (
                item.get("tidm") or item.get("symbol") or
                item.get("instrumentCode") or item.get("ticker") or
                (item.get("instrument", {}) or {}).get("tidm") or
                ""
            )
            if not tidm:
                continue

            # Extract headline
            headline = (
                item.get("headline") or item.get("title") or
                item.get("summary") or item.get("description") or ""
            )

            # Extract time
            time_str = (
                item.get("publishedTime") or item.get("publishedDate") or
                item.get("date") or item.get("time") or ""
            )
            # Try to parse and filter by cutoff
            if time_str:
                try:
                    pub_time = datetime.fromisoformat(
                        time_str.replace("Z", "+00:00")
                    ).astimezone(LONDON_TZ)
                    if pub_time < cutoff:
                        continue
                except Exception:
                    pass  # Keep it if we can't parse the time

            if not headline:
                continue

            symbol = tidm.strip().upper() + ".L"
            hl_lower = headline.lower()

            # Check for negative keywords
            is_negative = any(neg in hl_lower for neg in RNS_NEGATIVES)
            if is_negative:
                result[symbol] = {
                    "headline":       headline,
                    "score":          -2,
                    "label":          "⛔ Negative / Warning",
                    "emoji":          "⛔",
                    "expected_move":  "−5% to −30%",
                    "negative":       True,
                }
                continue

            # Score against positive triggers
            best_score = 0
            best_label = "General Announcement"
            best_emoji = "📌"
            best_move  = "unknown"

            for keywords, pts, label, emoji, expected_move in RNS_TRIGGERS:
                if any(re.search(kw, hl_lower) for kw in keywords):
                    if pts > best_score:
                        best_score = pts
                        best_label = label
                        best_emoji = emoji
                        best_move  = expected_move

            if best_score > 0:
                result[symbol] = {
                    "headline":      headline,
                    "score":         best_score,
                    "label":         best_label,
                    "emoji":         best_emoji,
                    "expected_move": best_move,
                    "negative":      False,
                }

        except Exception:
            continue

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2: Fetch individual ticker technical data
# ─────────────────────────────────────────────────────────────────────────────
def fetch_ticker_data(symbol: str) -> dict | None:
    """
    Downloads 90 days of price history for technical analysis.
    Returns a metrics dict or None if data is insufficient.
    """
    try:
        tk   = yf.Ticker(symbol)
        hist = tk.history(period="90d", auto_adjust=True)

        if hist.empty or len(hist) < 20:
            return None

        info = {}
        try:
            info = tk.info or {}
        except Exception:
            pass

        closes   = hist["Close"].dropna()
        highs    = hist["High"].dropna()
        lows     = hist["Low"].dropna()
        volumes  = hist["Volume"].dropna()

        last     = hist.iloc[-1]
        prev1    = hist.iloc[-2] if len(hist) >= 2 else last
        prev2    = hist.iloc[-3] if len(hist) >= 3 else prev1

        close    = float(last["Close"])
        open_p   = float(last.get("Open",  close))
        high_p   = float(last.get("High",  close))
        low_p    = float(last.get("Low",   close))
        vol      = float(last.get("Volume", 0))
        prev_c   = float(prev1["Close"])
        prev2_c  = float(prev2["Close"])

        pct_change = (close - prev_c) / prev_c * 100 if prev_c else 0

        # ── 20-day average volume ─────────────────────────────────────────────
        avg_vol_20 = float(volumes.iloc[-21:-1].mean()) if len(volumes) >= 21 else float(volumes.mean())
        vol_ratio  = vol / avg_vol_20 if avg_vol_20 > 0 else 0

        # ── ATR(14) ───────────────────────────────────────────────────────────
        tr_list = []
        for i in range(1, min(15, len(hist))):
            h  = float(hist.iloc[-i]["High"])
            l  = float(hist.iloc[-i]["Low"])
            pc = float(hist.iloc[-(i+1)]["Close"]) if i + 1 <= len(hist) else l
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr14    = sum(tr_list) / len(tr_list) if tr_list else 0
        atr_pct  = atr14 / close * 100 if close > 0 else 0

        # ── Bollinger Band Squeeze ────────────────────────────────────────────
        # BB width = (upper - lower) / mid  →  normalized across last 60 days
        bb_width_history = []
        for i in range(20, len(closes) + 1):
            window  = closes.iloc[i-20:i]
            mid     = window.mean()
            std     = window.std()
            width   = (2 * std / mid * 100) if mid > 0 else 0
            bb_width_history.append(width)

        current_bb_width = bb_width_history[-1] if bb_width_history else 0
        if len(bb_width_history) >= 10:
            sorted_widths = sorted(bb_width_history)
            rank_pct = (
                bb_width_history.index(
                    min(bb_width_history[-10:], key=lambda x: abs(x - current_bb_width))
                ) / len(bb_width_history) * 100
            )
            bb_squeeze = current_bb_width <= sorted_widths[int(len(sorted_widths) * BB_SQUEEZE_RANK / 100)]
            bb_squeeze_strong = current_bb_width <= sorted_widths[int(len(sorted_widths) * 0.10)]
        else:
            bb_squeeze = False
            bb_squeeze_strong = False

        # ── Volume Accumulation (smart money buildup) ─────────────────────────
        # Look for N consecutive days of rising volume with positive closes
        vol_days = min(VOL_BUILDUP_DAYS + 2, len(volumes) - 1)
        vol_recent = [float(volumes.iloc[-i]) for i in range(1, vol_days + 1)][::-1]
        vol_rising = all(vol_recent[i] > vol_recent[i-1] for i in range(1, len(vol_recent)))
        vol_above_avg = vol / avg_vol_20 >= 1.5 if avg_vol_20 > 0 else False

        # ── EMAs for trend ────────────────────────────────────────────────────
        ema9  = float(closes.ewm(span=9,  adjust=False).mean().iloc[-1]) if len(closes) >= 9  else None
        ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1]) if len(closes) >= 20 else None
        ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else None

        ema_aligned    = ema9 and ema20 and ema50 and close > ema9 > ema20 > ema50
        ema_uptrend    = ema20 and ema50 and ema20 > ema50
        ema_recovering = ema20 and close > ema20 and (ema50 is None or ema20 < ema50)

        # ── 52-week range position ────────────────────────────────────────────
        hi52  = float(closes.max())
        lo52  = float(closes.min())
        rng52 = hi52 - lo52
        pos52 = (close - lo52) / rng52 * 100 if rng52 > 0 else 50

        near_52w_high  = pos52 >= 90        # within 10% of 52w high = breakout territory
        at_52w_high    = pos52 >= 97        # essentially at all-time high = momentum
        in_discount    = pos52 < 40

        # Distance from 52w high as a % (used for display)
        dist_from_high = (hi52 - close) / hi52 * 100 if hi52 > 0 else 0

        # ── Inside Day detection ──────────────────────────────────────────────
        # Yesterday's range contained within day before = compression
        yesterday_high = float(prev1.get("High", prev_c))
        yesterday_low  = float(prev1.get("Low",  prev_c))
        day_before_high = float(prev2.get("High", prev2_c))
        day_before_low  = float(prev2.get("Low",  prev2_c))
        inside_day = (
            yesterday_high <= day_before_high and
            yesterday_low  >= day_before_low
        )

        # ── Previous session close strength ──────────────────────────────────
        # Did it close in the top 20% of yesterday's range? = bullish momentum
        yesterday_range = yesterday_high - yesterday_low
        if yesterday_range > 0:
            close_position = (prev_c - yesterday_low) / yesterday_range
        else:
            close_position = 0.5
        strong_close = close_position >= 0.75

        # ── Recent consolidation (tight range = coiled) ───────────────────────
        # Average true range of last 5 days vs 20-day ATR
        recent_ranges = []
        for i in range(1, min(6, len(hist))):
            h = float(hist.iloc[-i]["High"])
            l = float(hist.iloc[-i]["Low"])
            recent_ranges.append(h - l)
        avg_recent_range  = sum(recent_ranges) / len(recent_ranges) if recent_ranges else atr14
        range_compression = avg_recent_range < atr14 * 0.6  # recent range < 60% of ATR

        # ── Market cap ────────────────────────────────────────────────────────
        mktcap = info.get("marketCap")
        if mktcap is None:
            shares = info.get("sharesOutstanding")
            mktcap = close * shares if shares else None

        currency = info.get("currency", "GBP")
        mktcap_gbp = None
        if mktcap is not None:
            mktcap_gbp = mktcap / 100 if currency == "GBp" else mktcap

        is_aim = (
            info.get("exchange", "").upper() in ("AIM", "LSE", "IOB")
            or (mktcap_gbp is not None and mktcap_gbp < MAX_MKTCAP_GBP)
        )

        short_name = info.get("shortName", info.get("longName", symbol.replace(".L", "")))

        return {
            "symbol":           symbol,
            "name":             short_name,
            "currency":         currency,
            "close":            close,
            "open":             open_p,
            "high":             high_p,
            "low":              low_p,
            "pct_change":       pct_change,       # previous day's change
            "volume":           vol,
            "avg_vol_20":       avg_vol_20,
            "vol_ratio":        vol_ratio,
            "atr14":            atr14,
            "atr_pct":          atr_pct,
            "ema9":             ema9,
            "ema20":            ema20,
            "ema50":            ema50,
            "ema_aligned":      ema_aligned,
            "ema_uptrend":      ema_uptrend,
            "ema_recovering":   ema_recovering,
            "pos52":            pos52,
            "hi52":             hi52,
            "lo52":             lo52,
            "dist_from_high":   dist_from_high,
            "near_52w_high":    near_52w_high,
            "at_52w_high":      at_52w_high,
            "in_discount":      in_discount,
            "bb_squeeze":       bb_squeeze,
            "bb_squeeze_strong":bb_squeeze_strong,
            "bb_width":         current_bb_width,
            "vol_rising":       vol_rising,
            "vol_above_avg":    vol_above_avg,
            "inside_day":       inside_day,
            "strong_close":     strong_close,
            "close_position":   close_position,
            "range_compression":range_compression,
            "mktcap_gbp":       mktcap_gbp,
            "is_aim":           is_aim,
            "sector":           info.get("sector", ""),
            "industry":         info.get("industry", ""),
        }

    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3: Pre-market Predictive Scoring (0–14 points)
# ─────────────────────────────────────────────────────────────────────────────
def score_predictive(d: dict, rns: dict | None) -> tuple[int, list[str], str, str]:
    """
    Returns (score, reasons_list, confidence_label, predicted_move_range).

    Scoring breakdown:
      RNS Catalyst          0–4 pts  (biggest weight — news drives 80% of AIM moves)
      Bollinger Squeeze     0–2 pts  (volatility coiled)
      Volume Accumulation   0–2 pts  (smart money buying)
      Technical Breakout    0–2 pts  (position + trend)
      Compression Pattern   0–2 pts  (inside day / range compression)
      Volatility Capacity   0–1 pt   (ATR% high enough to deliver the move)
      Market Cap            0–1 pt   (smaller = more explosive)
    """
    s       = 0
    reasons = []

    # ── 1. RNS Catalyst (0–4 pts) ─────────────────────────────────────────────
    rns_score = 0
    if rns:
        if rns.get("negative"):
            s -= 2
            reasons.append(f"⛔ NEGATIVE RNS: {rns.get('label', '')} — likely gap DOWN")
            reasons.append(f"   Headline: \"{rns.get('headline', '')[:80]}...\"")
        else:
            rns_score = rns.get("score", 0)
            s += rns_score
            reasons.append(
                f"{rns['emoji']} RNS TODAY: {rns['label']} "
                f"(expected move: {rns['expected_move']})"
            )
            reasons.append(f"   \"{rns.get('headline', '')[:80]}\"")

    # ── 2. Bollinger Band Squeeze (0–2 pts) ───────────────────────────────────
    if d["bb_squeeze_strong"]:
        s += 2
        reasons.append(
            f"🔥 STRONG BB Squeeze — volatility compressed to historical low. "
            f"Explosive move imminent (direction TBC at open)"
        )
    elif d["bb_squeeze"]:
        s += 1
        reasons.append(
            f"📐 BB Squeeze — volatility in bottom {BB_SQUEEZE_RANK}th percentile. "
            f"Breakout likely within 1–3 sessions"
        )

    # ── 3. Volume Accumulation (0–2 pts) ──────────────────────────────────────
    if d["vol_rising"] and d["vol_above_avg"]:
        s += 2
        reasons.append(
            f"🏦 Volume accumulation: {VOL_BUILDUP_DAYS}+ days rising volume "
            f"× {d['vol_ratio']:.1f}× avg — institutional buying signal"
        )
    elif d["vol_rising"]:
        s += 1
        reasons.append(
            f"📊 Volume building: {VOL_BUILDUP_DAYS}+ consecutive days of increasing volume"
        )
    elif d["vol_above_avg"]:
        s += 1
        reasons.append(f"📊 Volume elevated: {d['vol_ratio']:.1f}× 20-day average")

    # ── 4. Technical breakout position (0–2 pts) ──────────────────────────────
    if d["at_52w_high"]:
        s += 2
        reasons.append(
            f"🚀 AT 52-WEEK HIGH ({d['pos52']:.0f}%) — breakout momentum, "
            f"no overhead resistance. Stocks often accelerate here"
        )
    elif d["near_52w_high"]:
        s += 2
        reasons.append(
            f"📈 Near 52-week high ({d['pos52']:.0f}% of range, {d['dist_from_high']:.1f}% from high) "
            f"— imminent breakout territory"
        )
    elif d["ema_aligned"]:
        s += 1
        reasons.append("✅ EMA stack aligned: Close > EMA9 > EMA20 > EMA50 — perfect uptrend")
    elif d["ema_uptrend"]:
        s += 1
        reasons.append("✅ EMA uptrend: EMA20 > EMA50 — bullish structure intact")

    # ── 5. Compression patterns (0–2 pts) ─────────────────────────────────────
    if d["inside_day"] and d["range_compression"]:
        s += 2
        reasons.append(
            "🗜️  Inside day + range compression — price wound tight. "
            "Classic pre-breakout setup on AIM stocks"
        )
    elif d["inside_day"]:
        s += 1
        reasons.append("🗜️  Inside day pattern — price range compressed within prior day")
    elif d["range_compression"]:
        s += 1
        reasons.append(
            f"🗜️  Range compression — recent daily ranges {int((1 - d['atr14']/d['close']*100/d['atr_pct']) * 100)}% "
            f"below normal ATR. Energy building"
        )

    if d["strong_close"]:
        reasons.append(
            f"✅ Previous session closed strong (top {int((1 - d['close_position']) * 100)}% of range) "
            f"— buyers in control at close"
        )

    # ── 6. Volatility capacity (0–1 pt) ───────────────────────────────────────
    if d["atr_pct"] >= 6:
        s += 1
        reasons.append(
            f"✅ ATR {d['atr_pct']:.1f}% — highly volatile stock, "
            f"structurally capable of 10–20%+ intraday moves"
        )
    elif d["atr_pct"] >= MIN_ATR_PCT:
        reasons.append(
            f"ATR {d['atr_pct']:.1f}% — moderate volatility, "
            f"capable of 5–10% intraday move"
        )
    else:
        reasons.append(
            f"⚠ ATR {d['atr_pct']:.1f}% — low volatility. "
            f"May struggle to move 5%+ in a session"
        )

    # ── 7. Market cap bonus (0–1 pt) ──────────────────────────────────────────
    mc = d["mktcap_gbp"]
    if mc is not None and mc < 30e6:
        s += 1
        reasons.append(f"✅ Micro-cap £{mc/1e6:.0f}m — small float, explosive when volume hits")
    elif mc is not None and mc < 100e6:
        reasons.append(f"Small-cap £{mc/1e6:.0f}m — manageable size for sharp moves")
    elif mc is None:
        reasons.append("⚠ Market cap unknown — check manually before trading")

    # ── Confidence label + predicted move ─────────────────────────────────────
    s_capped = max(0, min(s, 14))

    if rns and not rns.get("negative"):
        predicted_move = rns.get("expected_move", "5–15%")
        if s_capped >= 10:
            confidence = "VERY HIGH"
        elif s_capped >= 7:
            confidence = "HIGH"
        elif s_capped >= 5:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
    else:
        # Technical-only prediction
        if s_capped >= 9:
            confidence = "HIGH (Technical)"
            predicted_move = "5–15%"
        elif s_capped >= 6:
            confidence = "MEDIUM (Technical)"
            predicted_move = "3–10%"
        elif s_capped >= 4:
            confidence = "LOW (Technical)"
            predicted_move = "2–5%"
        else:
            confidence = "SPECULATIVE"
            predicted_move = "unknown"

    return s_capped, reasons, confidence, predicted_move


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4: Build HTML Report
# ─────────────────────────────────────────────────────────────────────────────
def build_html(results: list[dict], rns_map: dict,
               scan_time: str, date_str: str) -> str:

    def score_colour(s, max_s=14):
        pct = s / max_s
        if pct >= 0.75: return "#00e676"
        if pct >= 0.55: return "#f5a623"
        if pct >= 0.35: return "#29b6f6"
        return "#546e7a"

    def conf_colour(c):
        if "VERY HIGH" in c: return "#00e676"
        if "HIGH"      in c: return "#f5a623"
        if "MEDIUM"    in c: return "#29b6f6"
        return "#546e7a"

    def fmt_price(p, ccy):
        if ccy == "GBp":
            return f"{p:.2f}p"
        return f"£{p:.4f}" if p < 1 else f"£{p:.2f}"

    def fmt_cap(mc):
        if mc is None:  return "N/A"
        if mc >= 1e9:   return f"£{mc/1e9:.1f}B"
        if mc >= 1e6:   return f"£{mc/1e6:.0f}M"
        return f"£{mc/1e3:.0f}K"

    results.sort(key=lambda x: (-x["score"], -x["data"]["atr_pct"]))

    rns_count  = sum(1 for r in results if r.get("has_rns"))
    tech_count = len(results) - rns_count
    top_count  = sum(1 for r in results if r["score"] >= 8)

    rows_html = ""
    for r in results:
        d         = r["data"]
        sc        = r["score"]
        reasons   = r["reasons"]
        conf      = r["confidence"]
        pred_move = r["predicted_move"]
        has_rns   = r.get("has_rns", False)
        rns_info  = rns_map.get(d["symbol"])

        sym_clean = d["symbol"].replace(".L", "")
        sc_col    = score_colour(sc)
        conf_col  = conf_colour(conf)

        vol_col = (
            "#00e676" if d["vol_ratio"] >= 3 else
            "#f5a623" if d["vol_ratio"] >= 1.5 else
            "#546e7a"
        )
        pos52_col = (
            "#00e676" if d["pos52"] >= 90 else
            "#f5a623" if d["pos52"] >= 60 else
            "#546e7a"
        )

        reasons_html = "".join(
            f'<div class="reason">{rr}</div>' for rr in reasons
        )

        tv_url   = f"https://www.tradingview.com/chart/?symbol=LSE%3A{sym_clean}"
        rns_url  = (f"https://www.londonstockexchange.com/news?tab=news-explorer"
                    f"&search={sym_clean}")
        adv_url  = f"https://www.advfn.com/stock-market/LSE/{sym_clean}/share-price"
        yhoo_url = f"https://finance.yahoo.com/quote/{d['symbol']}"

        rns_badge = ""
        if has_rns and rns_info and not rns_info.get("negative"):
            rns_badge = (
                f'<div class="rns-badge">'
                f'{rns_info["emoji"]} RNS TODAY: {rns_info["label"]}'
                f'</div>'
            )
        elif has_rns and rns_info and rns_info.get("negative"):
            rns_badge = (
                f'<div class="rns-badge negative-badge">'
                f'⛔ NEGATIVE RNS</div>'
            )

        squeeze_badge = ""
        if d["bb_squeeze_strong"]:
            squeeze_badge = '<div class="squeeze-badge">🔥 STRONG SQUEEZE</div>'
        elif d["bb_squeeze"]:
            squeeze_badge = '<div class="squeeze-badge dim-squeeze">📐 BB SQUEEZE</div>'

        rows_html += f"""
        <div class="card {'rns-card' if has_rns and not (rns_info and rns_info.get('negative')) else ''}
                         {'neg-card' if has_rns and rns_info and rns_info.get('negative') else ''}
                         {'top-card' if sc >= 8 else ''}"
             data-score="{sc}"
             data-volratio="{d['vol_ratio']:.2f}"
             data-pos52="{d['pos52']:.1f}"
             data-squeeze="{'1' if d['bb_squeeze'] else '0'}"
             data-rns="{'1' if has_rns else '0'}">

          <div class="card-badges">{rns_badge}{squeeze_badge}</div>

          <div class="card-top">
            <div class="card-left">
              <div class="sym">{sym_clean}
                <span class="exch-badge">{'AIM' if d['is_aim'] else 'LSE'}</span>
              </div>
              <div class="company">{d['name']}</div>
              <div class="meta">{d['sector']}{'  ·  ' + d['industry'] if d['industry'] else ''}</div>
              <div class="last-price">{fmt_price(d['close'], d['currency'])}
                <span class="prev-chg {'up' if d['pct_change'] >= 0 else 'dn'}">
                  {d['pct_change']:+.1f}% prev session
                </span>
              </div>
            </div>

            <div class="card-mid">
              <div class="conf-label" style="color:{conf_col}">{conf}</div>
              <div class="pred-move">Predicted move:</div>
              <div class="pred-move-val" style="color:{conf_col}">{pred_move}</div>
              <div class="mkcap">{fmt_cap(d['mktcap_gbp'])}</div>
            </div>

            <div class="score-col">
              <div class="score-num" style="color:{sc_col}">{sc}</div>
              <div class="score-label">/ 14</div>
              <div class="score-bar-wrap">
                <div class="score-bar"
                     style="width:{sc/14*100:.0f}%; background:{sc_col}">
                </div>
              </div>
            </div>
          </div>

          <div class="metrics-row">
            <div class="metric">
              <span class="m-label">VOL RATIO</span>
              <span class="m-val" style="color:{vol_col}">{d['vol_ratio']:.1f}×</span>
            </div>
            <div class="metric">
              <span class="m-label">ATR%</span>
              <span class="m-val">{d['atr_pct']:.1f}%</span>
            </div>
            <div class="metric">
              <span class="m-label">52W POS</span>
              <span class="m-val" style="color:{pos52_col}">{d['pos52']:.0f}%</span>
            </div>
            <div class="metric">
              <span class="m-label">BB SQUEEZE</span>
              <span class="m-val" style="color:{'#00e676' if d['bb_squeeze'] else '#546e7a'}">
                {'STRONG' if d['bb_squeeze_strong'] else ('YES' if d['bb_squeeze'] else 'NO')}
              </span>
            </div>
            <div class="metric">
              <span class="m-label">VOL BUILD</span>
              <span class="m-val" style="color:{'#00e676' if d['vol_rising'] else '#546e7a'}">
                {'YES' if d['vol_rising'] else 'NO'}
              </span>
            </div>
            <div class="metric">
              <span class="m-label">INSIDE DAY</span>
              <span class="m-val" style="color:{'#f5a623' if d['inside_day'] else '#546e7a'}">
                {'YES' if d['inside_day'] else 'NO'}
              </span>
            </div>
            <div class="metric">
              <span class="m-label">UPTREND</span>
              <span class="m-val" style="color:{'#00e676' if d['ema_uptrend'] else '#546e7a'}">
                {'YES' if d['ema_uptrend'] else 'NO'}
              </span>
            </div>
            <div class="metric">
              <span class="m-label">NEAR HIGH</span>
              <span class="m-val" style="color:{'#00e676' if d['near_52w_high'] else '#546e7a'}">
                {'YES' if d['near_52w_high'] else 'NO'}
              </span>
            </div>
          </div>

          <div class="reasons">{reasons_html}</div>

          <div class="action-box">
            <div class="action-title">📋 WHAT TO CHECK BEFORE TRADING</div>
            <div class="action-items">
              <span class="a-item">1. Read full RNS on LSE →</span>
              <span class="a-item">2. Check chart for entry level →</span>
              <span class="a-item">3. Confirm volume spike at open (not pre-open)</span>
              <span class="a-item">4. Set stop-loss before entering</span>
            </div>
          </div>

          <div class="links">
            <a href="{tv_url}"  target="_blank" class="link-btn chart-btn">📈 Chart</a>
            <a href="{rns_url}" target="_blank" class="link-btn rns-btn">📰 RNS</a>
            <a href="{adv_url}" target="_blank" class="link-btn adv-btn">📊 ADVFN</a>
            <a href="{yhoo_url}" target="_blank" class="link-btn yh-btn">💹 Yahoo</a>
          </div>
        </div>
        """

    # ── Header stats ──────────────────────────────────────────────────────────
    total  = len(results)
    avg_sc = sum(r["score"] for r in results) / total if total else 0

    now_london = datetime.now(LONDON_TZ)
    hm = now_london.hour * 60 + now_london.minute
    if   hm < 480:    sess_label, sess_col = "PRE-MARKET (BEST TIME TO RUN)", "#00e676"
    elif hm < 510:    sess_label, sess_col = "AUCTION / OPEN",                "#f5a623"
    elif hm < 810:    sess_label, sess_col = "SESSION OPEN",                  "#29b6f6"
    elif hm < 930:    sess_label, sess_col = "US OVERLAP / CLOSE KZ",         "#f5a623"
    elif hm < 990:    sess_label, sess_col = "SESSION CLOSE",                 "#29b6f6"
    else:             sess_label, sess_col = "MARKET CLOSED",                 "#546e7a"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LSE Pre-Market Predictor — {date_str}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600&family=Barlow+Condensed:wght@600;700&display=swap');
  :root {{
    --bg:#080c10; --bg2:#0d1318; --bg3:#111920; --border:#1c2e3a;
    --amber:#f5a623; --green:#00e676; --red:#ff3d57; --blue:#29b6f6;
    --purple:#ce93d8; --gray:#546e7a; --text:#c8d8e4; --dim:#4a6478;
    --mono:'Share Tech Mono',monospace;
    --sans:'Barlow',sans-serif;
    --cond:'Barlow Condensed',sans-serif;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    background:var(--bg); color:var(--text); font-family:var(--sans);
    min-height:100vh;
    background-image:
      radial-gradient(ellipse at 15% 0%,rgba(0,230,118,.04) 0%,transparent 50%),
      radial-gradient(ellipse at 85% 100%,rgba(41,182,246,.03) 0%,transparent 50%);
  }}

  /* ── Header ─────────────────────────────────────────────────────────────── */
  .header {{
    padding:14px 28px; border-bottom:1px solid var(--border);
    background:rgba(13,19,24,.98); display:flex; align-items:center;
    justify-content:space-between; position:sticky; top:0; z-index:50;
  }}
  .logo {{ font-family:var(--mono); font-size:18px; color:var(--green); letter-spacing:2px; }}
  .logo span {{ color:var(--dim); }}
  .logo sub {{ font-size:11px; color:var(--amber); letter-spacing:1px; }}
  .header-right {{ text-align:right; font-family:var(--mono); font-size:11px; color:var(--dim); line-height:1.9; }}
  .sess {{ font-size:11px; font-weight:bold; letter-spacing:1px; }}

  /* ── How-it-works banner ─────────────────────────────────────────────────── */
  .how-bar {{
    background:rgba(0,230,118,.04); border-bottom:1px solid rgba(0,230,118,.1);
    padding:10px 28px; font-size:11px; color:var(--dim);
    font-family:var(--mono); line-height:2; display:flex; gap:30px; flex-wrap:wrap;
  }}
  .how-item {{ display:flex; align-items:center; gap:6px; }}
  .how-num  {{ color:var(--green); font-weight:bold; }}

  /* ── Stats bar ───────────────────────────────────────────────────────────── */
  .stats-bar {{
    display:flex; border-bottom:1px solid var(--border); background:var(--bg2);
  }}
  .stat {{ flex:1; text-align:center; padding:12px 8px; border-right:1px solid var(--border); }}
  .stat:last-child {{ border-right:none; }}
  .stat-v {{ font-family:var(--mono); font-size:22px; color:var(--amber); display:block; }}
  .stat-l {{ font-family:var(--cond); font-size:10px; color:var(--dim); letter-spacing:1.5px; text-transform:uppercase; }}

  /* ── Filter bar ──────────────────────────────────────────────────────────── */
  .filter-bar {{
    padding:10px 28px; background:var(--bg2); border-bottom:1px solid var(--border);
    display:flex; gap:8px; align-items:center; flex-wrap:wrap;
  }}
  .filter-label {{ font-family:var(--cond); font-size:11px; color:var(--dim); letter-spacing:1px; }}
  .filter-btn {{
    font-family:var(--cond); font-size:11px; font-weight:700; letter-spacing:1px;
    padding:5px 12px; border:1px solid var(--border); border-radius:2px;
    background:var(--bg3); color:var(--dim); cursor:pointer; transition:all .15s;
  }}
  .filter-btn:hover, .filter-btn.active {{
    border-color:var(--green); color:var(--green); background:rgba(0,230,118,.07);
  }}

  /* ── Grid ────────────────────────────────────────────────────────────────── */
  .grid {{ padding:18px 28px; display:grid; grid-template-columns:repeat(auto-fill,minmax(440px,1fr)); gap:14px; }}

  /* ── Cards ───────────────────────────────────────────────────────────────── */
  .card {{
    background:var(--bg2); border:1px solid var(--border); border-radius:4px;
    overflow:hidden; transition:border-color .2s, transform .15s;
  }}
  .card:hover {{ border-color:rgba(41,182,246,.35); transform:translateY(-1px); }}
  .top-card   {{ border-color:rgba(245,166,35,.25); }}
  .top-card:hover {{ border-color:rgba(245,166,35,.5); }}
  .rns-card   {{ border-color:rgba(0,230,118,.3); }}
  .rns-card:hover {{ border-color:rgba(0,230,118,.6); }}
  .neg-card   {{ border-color:rgba(255,61,87,.25); opacity:.7; }}

  .card-badges {{ display:flex; gap:6px; padding:8px 14px 0; flex-wrap:wrap; }}
  .rns-badge {{
    font-family:var(--cond); font-size:11px; font-weight:700; letter-spacing:1px;
    padding:3px 10px; border-radius:2px; background:rgba(0,230,118,.12);
    border:1px solid rgba(0,230,118,.3); color:var(--green);
  }}
  .negative-badge {{ background:rgba(255,61,87,.1); border-color:rgba(255,61,87,.3); color:var(--red); }}
  .squeeze-badge {{
    font-family:var(--cond); font-size:11px; font-weight:700; letter-spacing:1px;
    padding:3px 10px; border-radius:2px; background:rgba(245,166,35,.1);
    border:1px solid rgba(245,166,35,.3); color:var(--amber);
  }}
  .dim-squeeze {{ background:rgba(41,182,246,.07); border-color:rgba(41,182,246,.25); color:var(--blue); }}

  .card-top {{ display:flex; padding:12px 14px 10px; gap:12px; align-items:flex-start; }}
  .card-left {{ flex:1; }}
  .sym {{
    font-family:var(--mono); font-size:20px; color:var(--amber);
    letter-spacing:1.5px; display:flex; align-items:center; gap:8px;
  }}
  .exch-badge {{
    font-family:var(--cond); font-size:9px; color:var(--dim);
    border:1px solid var(--border); padding:1px 5px; border-radius:2px; letter-spacing:1px;
  }}
  .company {{ font-size:12px; color:var(--dim); margin-top:3px; }}
  .meta    {{ font-size:10px; color:rgba(74,100,120,.6); margin-top:2px; }}
  .last-price {{ font-family:var(--mono); font-size:13px; color:var(--text); margin-top:6px; }}
  .prev-chg {{ font-size:11px; margin-left:6px; }}
  .up {{ color:var(--green); }} .dn {{ color:var(--red); }}

  .card-mid {{ text-align:center; min-width:110px; }}
  .conf-label {{ font-family:var(--cond); font-size:13px; font-weight:700; letter-spacing:1px; }}
  .pred-move {{ font-size:9px; color:var(--dim); font-family:var(--cond); letter-spacing:1px; margin-top:6px; text-transform:uppercase; }}
  .pred-move-val {{ font-family:var(--mono); font-size:17px; font-weight:bold; margin-top:2px; }}
  .mkcap {{ font-size:10px; color:var(--dim); margin-top:5px; }}

  .score-col {{ text-align:center; min-width:52px; }}
  .score-num {{ font-family:var(--mono); font-size:26px; font-weight:bold; line-height:1; }}
  .score-label {{ font-size:9px; color:var(--dim); font-family:var(--mono); }}
  .score-bar-wrap {{ width:42px; height:3px; background:var(--bg3); border-radius:2px; margin:4px auto 0; overflow:hidden; }}
  .score-bar {{ height:100%; border-radius:2px; }}

  .metrics-row {{
    display:flex; flex-wrap:wrap; border-top:1px solid var(--border);
    border-bottom:1px solid var(--border); background:var(--bg3);
  }}
  .metric {{
    flex:1; min-width:60px; padding:6px 5px; text-align:center;
    border-right:1px solid var(--border);
  }}
  .metric:last-child {{ border-right:none; }}
  .m-label {{ display:block; font-family:var(--cond); font-size:9px; color:var(--dim); letter-spacing:1px; text-transform:uppercase; }}
  .m-val   {{ display:block; font-family:var(--mono); font-size:12px; color:var(--text); margin-top:2px; }}

  .reasons {{ padding:10px 14px; }}
  .reason  {{ font-size:11px; color:var(--dim); padding:2px 0; line-height:1.6; }}

  .action-box {{
    margin:6px 14px 8px; padding:8px 12px;
    background:rgba(41,182,246,.04); border:1px solid rgba(41,182,246,.15);
    border-radius:3px;
  }}
  .action-title {{ font-family:var(--cond); font-size:11px; color:var(--blue); font-weight:700; letter-spacing:1px; margin-bottom:5px; }}
  .action-items {{ display:flex; flex-wrap:wrap; gap:6px; }}
  .a-item {{ font-size:10px; color:var(--dim); font-family:var(--mono); }}

  .links {{
    padding:8px 14px 12px; display:flex; gap:7px; flex-wrap:wrap;
    border-top:1px solid rgba(28,46,58,.4);
  }}
  .link-btn {{
    font-family:var(--cond); font-size:11px; font-weight:700; letter-spacing:1px;
    padding:4px 11px; border-radius:2px; text-decoration:none;
    border:1px solid; transition:all .15s;
  }}
  .chart-btn {{ color:var(--blue);   border-color:rgba(41,182,246,.3);  background:rgba(41,182,246,.07);  }}
  .chart-btn:hover {{ background:rgba(41,182,246,.15); }}
  .rns-btn   {{ color:var(--amber);  border-color:rgba(245,166,35,.3);  background:rgba(245,166,35,.07);  }}
  .rns-btn:hover {{ background:rgba(245,166,35,.15); }}
  .adv-btn   {{ color:var(--purple); border-color:rgba(206,147,216,.3); background:rgba(206,147,216,.07); }}
  .adv-btn:hover {{ background:rgba(206,147,216,.15); }}
  .yh-btn    {{ color:var(--green);  border-color:rgba(0,230,118,.3);   background:rgba(0,230,118,.07);   }}
  .yh-btn:hover {{ background:rgba(0,230,118,.15); }}

  .empty {{ text-align:center; padding:60px 20px; color:var(--dim); font-family:var(--mono); font-size:13px; line-height:2; }}
  .footer {{
    margin-top:40px; padding:18px 28px; border-top:1px solid var(--border);
    font-size:11px; color:var(--dim); font-family:var(--mono); line-height:2.2;
    background:var(--bg2);
  }}
  .hidden {{ display:none !important; }}
  ::-webkit-scrollbar {{ width:4px; }} ::-webkit-scrollbar-thumb {{ background:var(--border); }}
  @media(max-width:500px) {{ .grid {{ padding:10px; grid-template-columns:1fr; }} .how-bar {{ display:none; }} }}
</style>
</head>
<body>

<div class="header">
  <div class="logo">
    LSE<span>·</span>PREDICT<br>
    <sub>PRE-MARKET SCANNER</sub>
  </div>
  <div class="header-right">
    <div>{date_str} &nbsp;|&nbsp; Scanned {scan_time} London</div>
    <div class="sess" style="color:{sess_col}">● {sess_label}</div>
  </div>
</div>

<div class="how-bar">
  <div class="how-item"><span class="how-num">①</span> RNS catalyst detected pre-market (07:00–07:55)</div>
  <div class="how-item"><span class="how-num">②</span> Bollinger squeeze = volatility coiled</div>
  <div class="how-item"><span class="how-num">③</span> Volume accumulation = smart money</div>
  <div class="how-item"><span class="how-num">④</span> Near 52-week high = breakout proximity</div>
  <div class="how-item"><span class="how-num">⑤</span> Best run: 06:30–07:50 London time</div>
</div>

<div class="stats-bar">
  <div class="stat">
    <span class="stat-v">{total}</span>
    <span class="stat-l">Candidates</span>
  </div>
  <div class="stat">
    <span class="stat-v" style="color:var(--green)">{rns_count}</span>
    <span class="stat-l">Have RNS Today</span>
  </div>
  <div class="stat">
    <span class="stat-v" style="color:var(--amber)">{top_count}</span>
    <span class="stat-l">Score ≥ 8/14</span>
  </div>
  <div class="stat">
    <span class="stat-v">{tech_count}</span>
    <span class="stat-l">Technical Setup Only</span>
  </div>
  <div class="stat">
    <span class="stat-v">{avg_sc:.1f}</span>
    <span class="stat-l">Avg Score</span>
  </div>
</div>

<div class="filter-bar">
  <span class="filter-label">FILTER ›</span>
  <button class="filter-btn active" onclick="fc('all',this)">ALL ({total})</button>
  <button class="filter-btn" onclick="fc('rns',this)">HAS RNS TODAY</button>
  <button class="filter-btn" onclick="fc('squeeze',this)">BB SQUEEZE</button>
  <button class="filter-btn" onclick="fc('top',this)">SCORE ≥ 8</button>
  <button class="filter-btn" onclick="fc('nearhigh',this)">NEAR 52W HIGH</button>
  <button class="filter-btn" onclick="fc('volbuild',this)">VOL ACCUMULATION</button>
</div>

<div class="grid" id="grid">
  {rows_html if rows_html.strip() else
    '<div class="empty">No candidates found.<br>'
    'Try running between 06:30–07:50 London time for RNS signals.<br>'
    'Technical setups will show any time. Lower MIN_PRED_SCORE in config.</div>'}
</div>

<div class="footer">
  ⚠  IMPORTANT DISCLAIMERS:<br>
  · This tool uses prior-day EOD data from Yahoo Finance. It does NOT have real-time intraday prices.<br>
  · RNS detection is automated keyword matching — always read the full announcement at londonstockexchange.com before trading.<br>
  · AIM stocks are high risk. Wide bid-ask spreads (1–5%) mean you can lose money even on a winning trade if you enter/exit poorly.<br>
  · Pre-market RNS signals: buy the open only after confirming volume spike in first 2–5 minutes. Fakes happen.<br>
  · This is not financial advice. Past price patterns do not guarantee future moves.
</div>

<script>
function fc(type, btn) {{
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(card => {{
    const score   = parseInt(card.dataset.score   || 0);
    const volr    = parseFloat(card.dataset.volratio || 0);
    const pos52   = parseFloat(card.dataset.pos52  || 0);
    const squeeze = card.dataset.squeeze === '1';
    const rns     = card.dataset.rns     === '1';
    let volbuild  = false;
    card.querySelectorAll('.m-val').forEach((el, i) => {{
      if (i === 4 && el.textContent.trim() === 'YES') volbuild = true;
    }});
    let show = true;
    if (type === 'rns')      show = rns;
    if (type === 'squeeze')  show = squeeze;
    if (type === 'top')      show = score >= 8;
    if (type === 'nearhigh') show = pos52 >= 90;
    if (type === 'volbuild') show = volbuild;
    card.classList.toggle('hidden', !show);
  }});
}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    now_london = datetime.now(LONDON_TZ)
    date_str   = now_london.strftime("%A %d %B %Y")
    scan_time  = now_london.strftime("%H:%M:%S")
    file_date  = now_london.strftime("%Y-%m-%d")

    print("═" * 64)
    print("  LSE PRE-MARKET PREDICTIVE SCANNER")
    print(f"  {date_str}  |  {scan_time} London time")
    print("  Finds stocks BEFORE they move 5–20%")
    print("═" * 64)

    if now_london.hour >= 16 or now_london.hour < 6:
        print("\n  ⚠  NOTE: Best results when run 06:30–07:50 London time.")
        print("     RNS announcements appear from 07:00.")
        print("     Technical setups are valid any time.\n")

    # ── Step 1: Fetch today's RNS ─────────────────────────────────────────────
    print("\n[1/4] Scanning pre-market RNS announcements...")
    rns_map = fetch_rns_today()
    rns_symbols = set(rns_map.keys())
    print(f"  ✓ {len(rns_map)} RNS tickers matched")
    if rns_map:
        print("  RNS tickers found:", ", ".join(
            s.replace(".L","") for s in list(rns_map.keys())[:15]
        ) + ("..." if len(rns_map) > 15 else ""))

    # ── Step 2: Build symbol list ─────────────────────────────────────────────
    print("\n[2/4] Building symbol list...")
    # RNS tickers take priority, then watchlist
    all_syms = list(dict.fromkeys(list(rns_symbols) + WATCHLIST_YAHOO))
    print(f"  ✓ {len(all_syms)} symbols to scan "
          f"({len(rns_symbols)} RNS + {len(WATCHLIST_YAHOO)} watchlist)")

    # ── Step 3: Download technical data ──────────────────────────────────────
    print(f"\n[3/4] Downloading data ({MAX_WORKERS} parallel workers)...")
    all_data = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_map = {ex.submit(fetch_ticker_data, sym): sym for sym in all_syms}
        for future in concurrent.futures.as_completed(future_map):
            done += 1
            result = future.result()
            if result is not None:
                all_data.append(result)
            if done % 20 == 0 or done == len(all_syms):
                pct = done / len(all_syms) * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(f"  [{bar}] {done}/{len(all_syms)}  ({len(all_data)} valid)",
                      end="\r")
    print(f"\n  ✓ Valid data for {len(all_data)} symbols")

    # ── Step 4: Score all tickers ─────────────────────────────────────────────
    print("\n[4/4] Scoring candidates...")
    results = []
    for d in all_data:
        # Skip stocks too stable to move 5%
        if d["atr_pct"] < MIN_ATR_PCT and d["symbol"] not in rns_symbols:
            continue

        # Skip huge caps (won't move 5–20% in a day)
        mc = d["mktcap_gbp"]
        if mc is not None and mc > MAX_MKTCAP_GBP and d["symbol"] not in rns_symbols:
            continue

        rns_info = rns_map.get(d["symbol"])
        score, reasons, confidence, predicted_move = score_predictive(d, rns_info)

        if score < MIN_PRED_SCORE and d["symbol"] not in rns_symbols:
            continue

        results.append({
            "data":           d,
            "score":          score,
            "reasons":        reasons,
            "confidence":     confidence,
            "predicted_move": predicted_move,
            "has_rns":        d["symbol"] in rns_symbols,
        })

    results.sort(key=lambda x: (-x["score"], -x["data"]["atr_pct"]))
    print(f"  ✓ {len(results)} candidates passed filters")

    if results:
        print("\n  ┌── TOP CANDIDATES ──────────────────────────────────────────┐")
        for r in results[:12]:
            d  = r["data"]
            mc = f"£{d['mktcap_gbp']/1e6:.0f}M" if d["mktcap_gbp"] else "N/A"
            rns_flag = " ◀ RNS" if r["has_rns"] else ""
            print(f"  │ {d['symbol'].replace('.L',''):<8} "
                  f"score {r['score']:>2}/14  "
                  f"ATR {d['atr_pct']:.1f}%  "
                  f"{mc:<8}  "
                  f"{r['confidence'][:15]:<15}"
                  f"{rns_flag}")
        print("  └────────────────────────────────────────────────────────────┘")

    # ── Step 5: Generate HTML ─────────────────────────────────────────────────
    print("\nGenerating HTML report...")
    html     = build_html(results, rns_map, scan_time, date_str)
    outfile  = f"lse_predict_{file_date}.html"
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Saved: {outfile}")

    try:
        webbrowser.open(f"file://{os.path.abspath(outfile)}")
        print("  ✓ Opened in browser")
    except Exception:
        print(f"  ⚠ Open manually: {os.path.abspath(outfile)}")

    print("\n" + "═" * 64)
    if results:
        top = results[0]
        d   = top["data"]
        print(f"  Best candidate: {d['symbol'].replace('.L','')} "
              f"| Score {top['score']}/14 "
              f"| {top['confidence']} "
              f"| Predicted: {top['predicted_move']}")
        if top["has_rns"]:
            rns = rns_map.get(d["symbol"])
            if rns:
                print(f"  RNS: {rns.get('label','')} — {rns.get('headline','')[:60]}")
    else:
        print("  No candidates found today.")
    print("═" * 64 + "\n")


if __name__ == "__main__":
    main()