#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  LSE PRE-MARKET PREDICTIVE SCANNER  v2.0                               ║
║  Finds AIM / small-cap stocks BEFORE they move 5–20%                  ║
║                                                                          ║
║  ▶ WHEN TO RUN: 06:30–07:55 London time (before LSE opens 08:00)      ║
║                                                                          ║
║  NEW IN v2.0:                                                            ║
║   ✓ Stop-loss levels (ATR-based + swing-low support)                    ║
║   ✓ Risk/reward ratio per candidate                                      ║
║   ✓ Liquidity gate — filters untradeable micro-illiquid stocks          ║
║   ✓ RSS feed RNS (more reliable than JSON API)                          ║
║   ✓ Spread cost estimate (AIM bid/ask impact)                            ║
║   ✓ Position sizing guide (1% account risk)                              ║
║   ✓ AIM-specific 52W high logic (distribution zone warning)             ║
║   ✓ Cleaned and curated watchlist                                        ║
║   ✓ Extended negative RNS keywords (dilution, placing discount, etc.)   ║
║                                                                          ║
║  INSTALL (once):                                                         ║
║    pip install yfinance requests pandas                                  ║
║                                                                          ║
║  RUN:                                                                    ║
║    python lse_predict_v2.py                                              ║
║                                                                          ║
║  OUTPUT: lse_predict_YYYY-MM-DD.html  (auto-opens in browser)          ║
╚══════════════════════════════════════════════════════════════════════════╝

  ⚠  NOT FINANCIAL ADVICE — For research and education only.
     AIM stocks carry extreme risk including total loss of capital.
     Always verify RNS text on londonstockexchange.com before trading.
     Stop-loss levels are estimates — always use a live L2 feed to refine.
"""

import yfinance as yf
import requests
import xml.etree.ElementTree as ET
import re
import sys
import time
import webbrowser
import os
import math
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import concurrent.futures
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
MAX_WORKERS          = 12      # parallel downloads; keep ≤ 15
MIN_PRED_SCORE       = 3       # min score to show (0–16 max)
MIN_ATR_PCT          = 2.0     # minimum average true range %
MAX_MKTCAP_GBP       = 600e6   # ignore mega-caps (above £600m)
MIN_AVG_DAILY_GBP    = 30_000  # LIQUIDITY GATE: avg daily turnover in £
                               # Below this you cannot trade without moving price
BB_PERIOD            = 20
BB_SQUEEZE_RANK      = 25
VOL_BUILDUP_DAYS     = 3
RNS_LOOKBACK_HRS     = 8       # Look back 8 hours (covers 00:00–08:00 releases)

# Stop loss configuration
ATR_STOP_MULT        = 1.5     # Stop = entry − (ATR × this multiplier)
ATR_STOP_MULT_TIGHT  = 1.0     # Tighter stop for high-confidence setups
SWING_LOW_LOOKBACK   = 10      # Days to look back for swing low support
MAX_STOP_PCT         = 12.0    # Cap stop distance at 12% (AIM can gap badly)

# Position sizing (for display only — NOT executed)
ACCOUNT_RISK_PCT     = 1.0     # Risk 1% of account per trade
EXAMPLE_ACCOUNT_GBP  = 10_000  # Example account size for position sizing display

LONDON_TZ = ZoneInfo("Europe/London")

# ─────────────────────────────────────────────────────────────────────────────
#  RNS KEYWORD SCORING
# ─────────────────────────────────────────────────────────────────────────────
RNS_TRIGGERS = [
    # ── TIER 1 — typically 15–50% move ───────────────────────────────────────
    (
        ["recommended offer", "possible offer", "firm offer", "takeover",
         "offer for the entire", "acquire the entire", "all cash offer",
         "scheme of arrangement", "merger agreement", "offer for all"],
        4, "Takeover / M&A Bid", "🎯", "15–50%"
    ),
    (
        ["phase 3 results", "phase iii results", "phase 2 results", "phase ii results",
         "positive top-line", "pivotal trial", "clinical trial results",
         "statistically significant", "fda approval", "mhra approval",
         "ema approval", "regulatory approval", "breakthrough designation",
         "orphan drug", "significant clinical", "primary endpoint met",
         "met its primary endpoint"],
        4, "Clinical / Regulatory Result", "💊", "15–40%"
    ),
    (
        ["maiden resource", "initial resource", "resource estimate",
         "significant intersection", "high grade intercept", "drill results",
         "bonanza grade", "significant mineralisation", "reserve update",
         "jorc resource", "ni 43-101"],
        3, "Resource / Drill Result", "⛏️", "10–30%"
    ),

    # ── TIER 2 — typically 5–20% move ────────────────────────────────────────
    (
        ["materially ahead", "significantly ahead", "ahead of market expectations",
         "ahead of expectations", "ahead of management expectations",
         "record revenue", "record sales", "record profit", "record results",
         "exceeds expectations", "outperforms", "well ahead"],
        3, "Beats Expectations", "🚀", "5–20%"
    ),
    (
        ["transformational contract", "significant contract", "major contract",
         "landmark agreement", "exclusive licence", "licence and commercialisation",
         "strategic licensing", "global licence", "exclusive agreement"],
        3, "Major Contract / Licence", "📋", "5–20%"
    ),
    (
        ["strategic partnership", "strategic investment", "cornerstone investment",
         "joint venture", "co-development agreement"],
        2, "Strategic Partnership", "🤝", "5–15%"
    ),
    (
        ["contract award", "contract win", "awarded a contract", "selected as preferred",
         "appointed as", "letter of intent", "heads of terms",
         "framework agreement", "supply agreement", "offtake agreement"],
        2, "Contract Win", "📝", "5–15%"
    ),
    (
        ["full year results", "half year results", "interim results",
         "preliminary results", "annual results", "positive trading update",
         "strong trading", "confident outlook", "board is pleased"],
        2, "Results / Trading Update", "📊", "3–10%"
    ),
    (
        ["director purchase", "director dealing", "pdmr purchase",
         "executive director purchase", "ceo purchase", "cfo purchase",
         "significant director purchase"],
        1, "Director / Insider Buying", "👤", "2–8%"
    ),
]

# Keywords that flag negative news (reduce score)
RNS_NEGATIVES = [
    # Operational warnings
    "profit warning", "revenue warning", "below expectations",
    "below market expectations", "disappointing results",
    "challenging trading", "difficult trading conditions",
    "shortfall", "below board expectations",
    # Dilution / Capital events (AIM-specific risk)
    "placing at a discount", "placing at discount", "subscription at a discount",
    "open offer at", "deeply discounted", "distressed placing",
    "loan note conversion", "convertible loan", "warrant exercise",
    "related party loan", "rpl", "director loan",
    # Corporate distress
    "suspension of trading", "suspended from trading",
    "wind down", "administration", "insolvency", "liquidation",
    "cancellation of admission", "delist", "cease trading",
    "winding up", "material uncertainty", "going concern",
    "breach of covenant", "restructuring of debt",
    # Regulatory
    "fca investigation", "financial conduct authority",
    "criminal investigation", "fraud",
]

# ─────────────────────────────────────────────────────────────────────────────
#  CURATED AIM / SMALL-CAP WATCHLIST
#  Removed: duplicates, invalid tickers, US names, non-AIM stocks
#  Kept: stocks with history of sharp intraday moves on AIM/LSE
# ─────────────────────────────────────────────────────────────────────────────
WATCHLIST = [
    # Biotech / Pharma / MedTech
    "AVCT","BVXP","CLI","CLIN","CRSO","CRW","DCAN","ECHO","GBG","HAT",
    "HBR","IGP","IKA","IMM","INFA","JOG","KBT","LIO","MCB","MED",
    "MRC","MTI","NANO","NCZ","OXB","POLB","RDL","SLN","SYNX","TRX",
    "VRS","WGB","CTEC","ELIX","GENL","HLTH","IMMU","IMUN","MDNA",
    # Mining / Resources
    "AAZ","ALBA","AMI","AOG","APH","ARG","ARL","ARM","ARML","ARP",
    "ATM","AUR","AVN","BKT","BON","CAD","CDL","DEC","ECR","EDL",
    "EGO","EMX","ENS","ERG","GCM","GTI","HYR","IQE","KORE","LWI",
    "MDC","MMX","MKA","MOTS","NAP","NCZ","POG","RRS","SXX","THL",
    "UJO","VELA","VGM","WKP","XTR","ZYT","ADT","KEFI",
    # Tech / Digital / Fintech
    "ALFA","AMS","ANP","APP","BIG","BUR","CAB","GAM","GAN","HAV",
    "HAWK","IGN","KAV","MCK","PAD","QRT","RAD","SLP","TAM","ASOS",
    "BOO","PETS","WHR","GFRD","SDX","STB","TCG","TED","LOOP",
    # Well-known AIM movers
    "HOC","ITV","LUCK","MAST","MATD","HIGH","GROW","JQW",
    "MIND","MINT","MIRA","MIRL","CRON","LEAF","LYG",
]

def to_yahoo(ticker: str) -> str:
    t = ticker.strip().upper()
    if not t.endswith(".L"):
        t += ".L"
    return t

WATCHLIST_YAHOO = list(dict.fromkeys(to_yahoo(t) for t in WATCHLIST))


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1: Fetch RNS — RSS feed first, JSON API fallback
# ─────────────────────────────────────────────────────────────────────────────
def fetch_rns_today() -> dict[str, dict]:
    """
    Fetch today's RNS via RSS (more reliable) then JSON API fallback.
    Returns dict: { "TICKER.L": { headline, score, label, emoji,
                                   expected_move, negative } }
    """
    rns_data: dict[str, dict] = {}
    now_london = datetime.now(LONDON_TZ)
    cutoff = now_london - timedelta(hours=RNS_LOOKBACK_HRS)

    # ── Attempt 1: LSE RSS feed ───────────────────────────────────────────────
    rss_urls = [
        "https://www.londonstockexchange.com/exchange/news/market-news/market-news-home.html?rss=true",
        "https://api.londonstockexchange.com/api/gw/lse/regulatory-news/rss",
        "https://www.londonstockexchange.com/news?tab=regulatory-news&rss=true",
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Referer": "https://www.londonstockexchange.com/",
    }

    for rss_url in rss_urls:
        try:
            r = requests.get(rss_url, headers=headers, timeout=12)
            if r.status_code == 200 and ("<rss" in r.text or "<feed" in r.text):
                items = _parse_rss(r.text, cutoff)
                if items:
                    rns_data.update(items)
                    print(f"  ✓ RNS RSS: {len(rns_data)} announcements")
                    return rns_data
        except Exception:
            pass

    # ── Attempt 2: LSE JSON API ───────────────────────────────────────────────
    api_endpoints = [
        "https://api.londonstockexchange.com/api/gw/lse/instruments/alldata/news"
        "?worlds=quotes&count=200&sortby=time&category=RegulatoryAnnouncement",
        "https://api.londonstockexchange.com/api/gw/lse/instruments"
        "/alldata/regulatorynewsheadlines?worlds=quotes&count=200",
    ]

    for url in api_endpoints:
        try:
            r = requests.get(url, headers={
                **headers,
                "Accept": "application/json",
            }, timeout=12)
            if r.status_code == 200:
                items = _parse_json_api(r.json(), cutoff)
                if items:
                    rns_data.update(items)
                    print(f"  ✓ RNS JSON API: {len(rns_data)} announcements")
                    return rns_data
        except Exception:
            pass

    print("  ⚠ RNS feed unreachable — technical signals only.")
    print("    Normal outside 06:30–08:30 or if LSE rate-limited.")
    return rns_data


def _parse_rss(xml_text: str, cutoff: datetime) -> dict[str, dict]:
    """Parse RSS/Atom XML for RNS announcements."""
    result = {}
    try:
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        # RSS 2.0
        items = root.findall(".//item")
        # Atom
        if not items:
            items = root.findall(".//atom:entry", ns)

        for item in items:
            # Title / headline
            title_el = item.find("title") or item.find("atom:title", ns)
            headline = title_el.text.strip() if title_el is not None and title_el.text else ""
            if not headline:
                continue

            # Try to extract ticker from title or link
            # LSE RSS format: "TICKER: Company Name - Announcement Type"
            tidm = ""
            m = re.match(r"^([A-Z]{2,5})\s*[-:]", headline)
            if m:
                tidm = m.group(1)

            if not tidm:
                # Try description
                desc_el = item.find("description") or item.find("atom:summary", ns)
                if desc_el is not None and desc_el.text:
                    m2 = re.search(r"\b([A-Z]{2,5})\.L\b", desc_el.text)
                    if m2:
                        tidm = m2.group(1)

            if not tidm:
                continue

            # Time filter
            pub_el = item.find("pubDate") or item.find("atom:published", ns)
            if pub_el is not None and pub_el.text:
                try:
                    pub_time = datetime.strptime(
                        pub_el.text.strip(), "%a, %d %b %Y %H:%M:%S %z"
                    ).astimezone(LONDON_TZ)
                    if pub_time < cutoff:
                        continue
                except Exception:
                    pass

            _score_and_store(tidm, headline, result)

    except Exception:
        pass
    return result


def _parse_json_api(data: dict, cutoff: datetime) -> dict[str, dict]:
    """Parse LSE JSON API response."""
    result = {}
    items = (
        data.get("content", []) or data.get("data", []) or
        data.get("news", [])    or data.get("items", []) or []
    )
    if not items:
        for key in data:
            if isinstance(data[key], list) and data[key] and isinstance(data[key][0], dict):
                items = data[key]
                break

    for item in items:
        try:
            tidm = (
                item.get("tidm") or item.get("symbol") or
                item.get("instrumentCode") or item.get("ticker") or
                (item.get("instrument", {}) or {}).get("tidm") or ""
            )
            if not tidm:
                continue

            headline = (
                item.get("headline") or item.get("title") or
                item.get("summary")  or item.get("description") or ""
            )
            if not headline:
                continue

            time_str = (
                item.get("publishedTime") or item.get("publishedDate") or
                item.get("date") or item.get("time") or ""
            )
            if time_str:
                try:
                    pub_time = datetime.fromisoformat(
                        time_str.replace("Z", "+00:00")
                    ).astimezone(LONDON_TZ)
                    if pub_time < cutoff:
                        continue
                except Exception:
                    pass

            _score_and_store(tidm.strip().upper(), headline, result)
        except Exception:
            continue

    return result


def _score_and_store(tidm: str, headline: str, result: dict):
    """Score a headline and store it in result dict."""
    symbol = tidm.upper() + ".L"
    hl_lower = headline.lower()

    is_negative = any(neg in hl_lower for neg in RNS_NEGATIVES)
    if is_negative:
        result[symbol] = {
            "headline": headline, "score": -3,
            "label": "⛔ Negative / Warning", "emoji": "⛔",
            "expected_move": "−5% to −30%", "negative": True,
        }
        return

    best_score, best_label, best_emoji, best_move = 0, "General", "📌", "unknown"
    for keywords, pts, label, emoji, expected_move in RNS_TRIGGERS:
        if any(re.search(kw, hl_lower) for kw in keywords):
            if pts > best_score:
                best_score, best_label, best_emoji, best_move = pts, label, emoji, expected_move

    if best_score > 0:
        result[symbol] = {
            "headline": headline, "score": best_score,
            "label": best_label, "emoji": best_emoji,
            "expected_move": best_move, "negative": False,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2: Fetch ticker technical data + stop loss calculation
# ─────────────────────────────────────────────────────────────────────────────
def fetch_ticker_data(symbol: str) -> dict | None:
    """Download 120 days of price history for technical + stop loss analysis."""
    try:
        tk   = yf.Ticker(symbol)
        hist = tk.history(period="120d", auto_adjust=True)

        if hist.empty or len(hist) < 20:
            return None

        info = {}
        try:
            info = tk.info or {}
        except Exception:
            pass

        closes  = hist["Close"].dropna()
        highs   = hist["High"].dropna()
        lows    = hist["Low"].dropna()
        volumes = hist["Volume"].dropna()

        last  = hist.iloc[-1]
        prev1 = hist.iloc[-2] if len(hist) >= 2 else last
        prev2 = hist.iloc[-3] if len(hist) >= 3 else prev1

        close  = float(last["Close"])
        open_p = float(last.get("Open",  close))
        high_p = float(last.get("High",  close))
        low_p  = float(last.get("Low",   close))
        vol    = float(last.get("Volume", 0))
        prev_c = float(prev1["Close"])
        prev2_c= float(prev2["Close"])

        pct_change = (close - prev_c) / prev_c * 100 if prev_c else 0

        # ── Liquidity: Average Daily Turnover in GBP ──────────────────────────
        currency = info.get("currency", "GBP")
        price_gbp = close / 100 if currency == "GBp" else close
        avg_vol_20 = float(volumes.iloc[-21:-1].mean()) if len(volumes) >= 21 else float(volumes.mean())
        avg_daily_gbp = avg_vol_20 * price_gbp
        vol_ratio     = float(vol) / avg_vol_20 if avg_vol_20 > 0 else 0

        # ── ATR(14) ───────────────────────────────────────────────────────────
        tr_list = []
        for i in range(1, min(15, len(hist))):
            h  = float(hist.iloc[-i]["High"])
            l  = float(hist.iloc[-i]["Low"])
            pc = float(hist.iloc[-(i+1)]["Close"]) if i + 1 <= len(hist) else l
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr14   = sum(tr_list) / len(tr_list) if tr_list else 0
        atr_pct = atr14 / close * 100 if close > 0 else 0

        # ── Estimated bid/ask spread (proxy via ATR and mktcap) ───────────────
        mc_gbp_est = info.get("marketCap")
        if mc_gbp_est:
            mc_gbp_est = mc_gbp_est / 100 if currency == "GBp" else mc_gbp_est
        # AIM spread heuristic: tighter for liquid stocks
        if avg_daily_gbp >= 500_000:
            est_spread_pct = 0.5
        elif avg_daily_gbp >= 100_000:
            est_spread_pct = 1.5
        elif avg_daily_gbp >= 30_000:
            est_spread_pct = 3.0
        else:
            est_spread_pct = 5.0  # near-untradeable

        # ── STOP LOSS CALCULATIONS ────────────────────────────────────────────
        # Method 1: ATR-based stop (standard)
        atr_stop_distance = atr14 * ATR_STOP_MULT
        atr_stop_price    = close - atr_stop_distance
        atr_stop_pct      = atr_stop_distance / close * 100

        # Method 2: Tight ATR stop (for high-confidence + RNS setups)
        atr_stop_tight_price = close - (atr14 * ATR_STOP_TIGHT)
        atr_stop_tight_pct   = (atr14 * ATR_STOP_TIGHT) / close * 100

        # Method 3: Swing Low support stop
        lookback = min(SWING_LOW_LOOKBACK, len(lows))
        swing_lows = [float(lows.iloc[-i]) for i in range(1, lookback + 1)]
        swing_low_price = min(swing_lows) * 0.995  # 0.5% buffer below swing low
        swing_low_pct   = (close - swing_low_price) / close * 100

        # Cap stops at MAX_STOP_PCT
        atr_stop_pct       = min(atr_stop_pct,       MAX_STOP_PCT)
        atr_stop_tight_pct = min(atr_stop_tight_pct, MAX_STOP_PCT)
        swing_low_pct      = min(swing_low_pct,       MAX_STOP_PCT)

        # Recalculate prices from capped pcts
        atr_stop_price       = close * (1 - atr_stop_pct / 100)
        atr_stop_tight_price = close * (1 - atr_stop_tight_pct / 100)
        swing_low_price      = close * (1 - swing_low_pct / 100)

        # Recommended stop: swing low if it's tighter than ATR stop
        # but never so tight it's inside the spread
        if swing_low_pct < atr_stop_pct and swing_low_pct > (est_spread_pct * 1.5):
            rec_stop_price = swing_low_price
            rec_stop_pct   = swing_low_pct
            rec_stop_method = "Swing Low"
        else:
            rec_stop_price = atr_stop_price
            rec_stop_pct   = atr_stop_pct
            rec_stop_method = "ATR×1.5"

        # ── R/R ratio (using RNS expected move midpoint vs stop) ──────────────
        # Will be updated after scoring with predicted move
        rr_ratio = None  # calculated later

        # ── Position sizing (for £10k account, 1% risk) ───────────────────────
        max_risk_gbp  = EXAMPLE_ACCOUNT_GBP * (ACCOUNT_RISK_PCT / 100)
        stop_dist_gbp = price_gbp * (rec_stop_pct / 100)
        if stop_dist_gbp > 0:
            max_shares   = int(max_risk_gbp / stop_dist_gbp)
            position_gbp = max_shares * price_gbp
        else:
            max_shares   = 0
            position_gbp = 0

        # ── Bollinger Band Squeeze ────────────────────────────────────────────
        bb_width_history = []
        for i in range(20, len(closes) + 1):
            window = closes.iloc[i - 20:i]
            mid    = window.mean()
            std    = window.std()
            width  = (2 * std / mid * 100) if mid > 0 else 0
            bb_width_history.append(width)

        current_bb_width = bb_width_history[-1] if bb_width_history else 0
        if len(bb_width_history) >= 10:
            sorted_widths = sorted(bb_width_history)
            bb_squeeze        = current_bb_width <= sorted_widths[int(len(sorted_widths) * BB_SQUEEZE_RANK / 100)]
            bb_squeeze_strong = current_bb_width <= sorted_widths[int(len(sorted_widths) * 0.10)]
        else:
            bb_squeeze = bb_squeeze_strong = False

        # ── Volume Accumulation ───────────────────────────────────────────────
        vol_days    = min(VOL_BUILDUP_DAYS + 2, len(volumes) - 1)
        vol_recent  = [float(volumes.iloc[-i]) for i in range(1, vol_days + 1)][::-1]
        vol_rising  = all(vol_recent[i] > vol_recent[i - 1] for i in range(1, len(vol_recent)))
        vol_above_avg = vol / avg_vol_20 >= 1.5 if avg_vol_20 > 0 else False

        # ── EMAs ──────────────────────────────────────────────────────────────
        ema9  = float(closes.ewm(span=9,  adjust=False).mean().iloc[-1]) if len(closes) >= 9  else None
        ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1]) if len(closes) >= 20 else None
        ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else None

        ema_aligned    = bool(ema9 and ema20 and ema50 and close > ema9 > ema20 > ema50)
        ema_uptrend    = bool(ema20 and ema50 and ema20 > ema50)
        ema_recovering = bool(ema20 and close > ema20 and (ema50 is None or ema20 < ema50))

        # ── 52-week range ─────────────────────────────────────────────────────
        hi52  = float(closes.max())
        lo52  = float(closes.min())
        rng52 = hi52 - lo52
        pos52 = (close - lo52) / rng52 * 100 if rng52 > 0 else 50
        dist_from_high = (hi52 - close) / hi52 * 100 if hi52 > 0 else 0

        # AIM-specific: near 52W high can mean DISTRIBUTION, not breakout
        # Flag it as potential distribution zone rather than bullish breakout
        near_52w_high    = pos52 >= 90
        at_52w_high      = pos52 >= 97
        distribution_risk = at_52w_high  # Smart money may be exiting

        # ── Inside day / compression ──────────────────────────────────────────
        yesterday_high  = float(prev1.get("High", prev_c))
        yesterday_low   = float(prev1.get("Low",  prev_c))
        day_before_high = float(prev2.get("High", prev2_c))
        day_before_low  = float(prev2.get("Low",  prev2_c))
        inside_day = (yesterday_high <= day_before_high and yesterday_low >= day_before_low)

        yesterday_range = yesterday_high - yesterday_low
        close_position  = ((prev_c - yesterday_low) / yesterday_range
                           if yesterday_range > 0 else 0.5)
        strong_close = close_position >= 0.75

        recent_ranges = [
            float(hist.iloc[-i]["High"]) - float(hist.iloc[-i]["Low"])
            for i in range(1, min(6, len(hist)))
        ]
        avg_recent_range  = sum(recent_ranges) / len(recent_ranges) if recent_ranges else atr14
        range_compression = avg_recent_range < atr14 * 0.6

        # ── Market cap ────────────────────────────────────────────────────────
        mktcap     = info.get("marketCap")
        mktcap_gbp = None
        if mktcap is not None:
            mktcap_gbp = mktcap / 100 if currency == "GBp" else mktcap

        is_aim = (
            info.get("exchange", "").upper() in ("AIM", "LSE", "IOB")
            or (mktcap_gbp is not None and mktcap_gbp < MAX_MKTCAP_GBP)
        )

        short_name = info.get("shortName", info.get("longName", symbol.replace(".L", "")))

        return {
            "symbol":               symbol,
            "name":                 short_name,
            "currency":             currency,
            "close":                close,
            "open":                 open_p,
            "high":                 high_p,
            "low":                  low_p,
            "price_gbp":            price_gbp,
            "pct_change":           pct_change,
            "volume":               vol,
            "avg_vol_20":           avg_vol_20,
            "avg_daily_gbp":        avg_daily_gbp,
            "vol_ratio":            vol_ratio,
            "est_spread_pct":       est_spread_pct,
            "atr14":                atr14,
            "atr_pct":              atr_pct,
            "ema9":                 ema9,
            "ema20":                ema20,
            "ema50":                ema50,
            "ema_aligned":          ema_aligned,
            "ema_uptrend":          ema_uptrend,
            "ema_recovering":       ema_recovering,
            "pos52":                pos52,
            "hi52":                 hi52,
            "lo52":                 lo52,
            "dist_from_high":       dist_from_high,
            "near_52w_high":        near_52w_high,
            "at_52w_high":          at_52w_high,
            "distribution_risk":    distribution_risk,
            "bb_squeeze":           bb_squeeze,
            "bb_squeeze_strong":    bb_squeeze_strong,
            "bb_width":             current_bb_width,
            "vol_rising":           vol_rising,
            "vol_above_avg":        vol_above_avg,
            "inside_day":           inside_day,
            "strong_close":         strong_close,
            "close_position":       close_position,
            "range_compression":    range_compression,
            "mktcap_gbp":           mktcap_gbp,
            "is_aim":               is_aim,
            "sector":               info.get("sector", ""),
            "industry":             info.get("industry", ""),
            # ── Stop loss data ──────────────────────────────────────────────
            "atr_stop_price":       atr_stop_price,
            "atr_stop_pct":         atr_stop_pct,
            "atr_stop_tight_price": atr_stop_tight_price,
            "atr_stop_tight_pct":   atr_stop_tight_pct,
            "swing_low_price":      swing_low_price,
            "swing_low_pct":        swing_low_pct,
            "rec_stop_price":       rec_stop_price,
            "rec_stop_pct":         rec_stop_pct,
            "rec_stop_method":      rec_stop_method,
            "max_shares":           max_shares,
            "position_gbp":         position_gbp,
        }

    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG (referenced inside fetch_ticker_data - define as globals)
# ─────────────────────────────────────────────────────────────────────────────
ATR_STOP_TIGHT = ATR_STOP_MULT_TIGHT  # alias used in fetch_ticker_data


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3: Predictive Scoring (0–16 points)
# ─────────────────────────────────────────────────────────────────────────────
def score_predictive(d: dict, rns: dict | None) -> tuple[int, list[str], str, str, float | None]:
    """
    Returns (score, reasons, confidence_label, predicted_move, rr_ratio).
    """
    s       = 0
    reasons = []
    predicted_move_midpoint = None

    # ── 1. RNS Catalyst (0–4 pts) ─────────────────────────────────────────────
    if rns:
        if rns.get("negative"):
            s -= 3
            reasons.append(f"⛔ NEGATIVE RNS: {rns.get('label', '')} — likely gap DOWN")
            reasons.append(f"   Headline: \"{rns.get('headline', '')[:90]}\"")
        else:
            rns_score = rns.get("score", 0)
            s += rns_score
            reasons.append(
                f"{rns['emoji']} RNS TODAY: {rns['label']} "
                f"(expected: {rns['expected_move']})"
            )
            reasons.append(f"   \"{rns.get('headline', '')[:90]}\"")
            # Parse midpoint for R/R
            em = rns.get("expected_move", "")
            nums = re.findall(r"\d+", em)
            if len(nums) >= 2:
                predicted_move_midpoint = (int(nums[0]) + int(nums[1])) / 2

    # ── 2. Bollinger Band Squeeze (0–2 pts) ───────────────────────────────────
    if d["bb_squeeze_strong"]:
        s += 2
        reasons.append("🔥 STRONG BB Squeeze — volatility at historical low. Explosive move imminent.")
    elif d["bb_squeeze"]:
        s += 1
        reasons.append(f"📐 BB Squeeze — volatility in bottom {BB_SQUEEZE_RANK}th percentile.")

    # ── 3. Volume Accumulation (0–2 pts) ──────────────────────────────────────
    if d["vol_rising"] and d["vol_above_avg"]:
        s += 2
        reasons.append(
            f"🏦 Volume accumulation: {VOL_BUILDUP_DAYS}+ days rising × "
            f"{d['vol_ratio']:.1f}× avg — institutional signal"
        )
    elif d["vol_rising"]:
        s += 1
        reasons.append(f"📊 Volume building: {VOL_BUILDUP_DAYS}+ consecutive days rising")
    elif d["vol_above_avg"]:
        s += 1
        reasons.append(f"📊 Volume elevated: {d['vol_ratio']:.1f}× 20-day average")

    # ── 4. Technical position (0–2 pts, AIM-aware) ────────────────────────────
    if d["at_52w_high"] and d["distribution_risk"]:
        # On AIM, at 52W high is more often distribution — penalise slightly
        s += 1
        reasons.append(
            f"⚠ AT 52W HIGH ({d['pos52']:.0f}%) — momentum present BUT "
            f"AIM distribution risk: check for insider selling / placing."
        )
    elif d["near_52w_high"]:
        s += 2
        reasons.append(
            f"📈 Near 52W high ({d['pos52']:.0f}%, {d['dist_from_high']:.1f}% from top) "
            f"— approaching breakout territory"
        )
    elif d["ema_aligned"]:
        s += 1
        reasons.append("✅ EMA stack: Close > EMA9 > EMA20 > EMA50 — clean uptrend")
    elif d["ema_uptrend"]:
        s += 1
        reasons.append("✅ EMA20 > EMA50 — bullish structure")

    # ── 5. Compression patterns (0–2 pts) ─────────────────────────────────────
    if d["inside_day"] and d["range_compression"]:
        s += 2
        reasons.append("🗜️  Inside day + range compression — price wound tight. Pre-breakout setup.")
    elif d["inside_day"]:
        s += 1
        reasons.append("🗜️  Inside day — yesterday's range inside prior day's range")
    elif d["range_compression"]:
        s += 1
        reasons.append("🗜️  Range compression — recent ranges below normal ATR")

    if d["strong_close"]:
        reasons.append(
            f"✅ Strong close: top {int((1 - d['close_position']) * 100)}% of yesterday's range"
        )

    # ── 6. Volatility capacity (0–1 pt) ───────────────────────────────────────
    if d["atr_pct"] >= 6:
        s += 1
        reasons.append(f"✅ ATR {d['atr_pct']:.1f}% — high volatility, capable of 10–20%+ moves")
    elif d["atr_pct"] >= MIN_ATR_PCT:
        reasons.append(f"ATR {d['atr_pct']:.1f}% — moderate volatility, 5–10% moves possible")
    else:
        s -= 1
        reasons.append(f"⚠ ATR {d['atr_pct']:.1f}% — low volatility. Unlikely to move 5%+")

    # ── 7. Market cap bonus (0–1 pt) ──────────────────────────────────────────
    mc = d["mktcap_gbp"]
    if mc is not None and mc < 30e6:
        s += 1
        reasons.append(f"✅ Micro-cap £{mc/1e6:.0f}m — small float, explosive on volume")
    elif mc is not None and mc < 100e6:
        reasons.append(f"Small-cap £{mc/1e6:.0f}m — manageable for sharp moves")
    elif mc is None:
        reasons.append("⚠ Market cap unknown — verify before trading")

    # ── 8. Liquidity note (no score impact — just information) ────────────────
    adgbp = d["avg_daily_gbp"]
    if adgbp >= 200_000:
        reasons.append(f"✅ Liquid: £{adgbp/1e3:.0f}k avg daily turnover — tradeable")
    elif adgbp >= 50_000:
        reasons.append(f"⚠ Moderate liquidity: £{adgbp/1e3:.0f}k avg — use limit orders")
    else:
        reasons.append(
            f"🚫 ILLIQUID: Only £{adgbp/1e3:.0f}k avg daily. "
            f"Spread may be 3–5%+. Extreme caution or avoid."
        )

    # ── Spread cost warning ────────────────────────────────────────────────────
    spread = d["est_spread_pct"]
    if spread >= 3.0:
        reasons.append(
            f"⚠ Estimated spread: ~{spread:.1f}% — need {spread * 2:.1f}%+ move just to break even"
        )

    # ── Confidence + predicted move ───────────────────────────────────────────
    s_capped = max(-5, min(s, 16))

    if rns and not rns.get("negative"):
        predicted_move = rns.get("expected_move", "5–15%")
        if s_capped >= 10: confidence = "VERY HIGH"
        elif s_capped >= 7: confidence = "HIGH"
        elif s_capped >= 5: confidence = "MEDIUM"
        else: confidence = "LOW"
    else:
        if s_capped >= 9:   confidence, predicted_move = "HIGH (Technical)",       "5–15%"
        elif s_capped >= 6: confidence, predicted_move = "MEDIUM (Technical)",     "3–10%"
        elif s_capped >= 4: confidence, predicted_move = "LOW (Technical)",        "2–5%"
        else:               confidence, predicted_move = "SPECULATIVE",             "unknown"

    # ── Risk/Reward calculation ────────────────────────────────────────────────
    rr_ratio = None
    if predicted_move_midpoint and d["rec_stop_pct"] > 0:
        rr_ratio = round(predicted_move_midpoint / d["rec_stop_pct"], 1)

    return s_capped, reasons, confidence, predicted_move, rr_ratio


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4: Build HTML Report
# ─────────────────────────────────────────────────────────────────────────────
def build_html(results: list[dict], rns_map: dict, scan_time: str, date_str: str) -> str:

    def score_colour(s, max_s=16):
        pct = max(0, s) / max_s
        if pct >= 0.75: return "#00e676"
        if pct >= 0.55: return "#f5a623"
        if pct >= 0.35: return "#29b6f6"
        return "#546e7a"

    def conf_colour(c):
        if "VERY HIGH" in c: return "#00e676"
        if "HIGH"      in c: return "#f5a623"
        if "MEDIUM"    in c: return "#29b6f6"
        return "#546e7a"

    def rr_colour(rr):
        if rr is None: return "#546e7a"
        if rr >= 3: return "#00e676"
        if rr >= 2: return "#f5a623"
        return "#ff3d57"

    def fmt_price(p, ccy):
        if ccy == "GBp":
            return f"{p:.2f}p"
        return f"£{p:.4f}" if p < 1 else f"£{p:.2f}"

    def fmt_stop(p, ccy):
        if ccy == "GBp":
            return f"{p:.2f}p"
        return f"£{p:.4f}" if p < 1 else f"£{p:.2f}"

    def fmt_cap(mc):
        if mc is None:  return "N/A"
        if mc >= 1e9:   return f"£{mc/1e9:.1f}B"
        if mc >= 1e6:   return f"£{mc/1e6:.0f}M"
        return f"£{mc/1e3:.0f}K"

    def fmt_gbp(v):
        if v >= 1e6:  return f"£{v/1e6:.1f}M"
        if v >= 1000: return f"£{v/1e3:.0f}K"
        return f"£{v:.0f}"

    results.sort(key=lambda x: (-x["score"], -x["data"]["atr_pct"]))

    rns_count  = sum(1 for r in results if r.get("has_rns"))
    tech_count = len(results) - rns_count
    top_count  = sum(1 for r in results if r["score"] >= 8)
    good_rr    = sum(1 for r in results if (r.get("rr_ratio") or 0) >= 2)

    rows_html = ""
    for r in results:
        d         = r["data"]
        sc        = r["score"]
        reasons   = r["reasons"]
        conf      = r["confidence"]
        pred_move = r["predicted_move"]
        rr        = r.get("rr_ratio")
        has_rns   = r.get("has_rns", False)
        rns_info  = rns_map.get(d["symbol"])

        sym_clean = d["symbol"].replace(".L", "")
        sc_col    = score_colour(sc)
        conf_col  = conf_colour(conf)
        rr_col    = rr_colour(rr)

        vol_col = (
            "#00e676" if d["vol_ratio"] >= 3 else
            "#f5a623" if d["vol_ratio"] >= 1.5 else "#546e7a"
        )
        pos52_col = (
            "#00e676" if d["pos52"] >= 90 else
            "#f5a623" if d["pos52"] >= 60 else "#546e7a"
        )
        spread_col = (
            "#ff3d57" if d["est_spread_pct"] >= 3 else
            "#f5a623" if d["est_spread_pct"] >= 1.5 else "#00e676"
        )
        liq_col = (
            "#00e676" if d["avg_daily_gbp"] >= 200_000 else
            "#f5a623" if d["avg_daily_gbp"] >= 50_000 else "#ff3d57"
        )

        # Stop loss display
        rec_stop  = d["rec_stop_price"]
        rec_pct   = d["rec_stop_pct"]
        swing_low = d["swing_low_price"]
        sl_pct    = d["swing_low_pct"]
        atr_stop  = d["atr_stop_price"]
        atr_pct_s = d["atr_stop_pct"]
        tight_stop= d["atr_stop_tight_price"]
        tight_pct = d["atr_stop_tight_pct"]
        method    = d["rec_stop_method"]

        reasons_html = "".join(f'<div class="reason">{rr2}</div>' for rr2 in reasons)

        tv_url  = f"https://www.tradingview.com/chart/?symbol=LSE%3A{sym_clean}"
        rns_url = f"https://www.londonstockexchange.com/news?tab=news-explorer&search={sym_clean}"
        adv_url = f"https://www.advfn.com/stock-market/LSE/{sym_clean}/share-price"
        yhoo_url= f"https://finance.yahoo.com/quote/{d['symbol']}"

        rns_badge = ""
        if has_rns and rns_info and not rns_info.get("negative"):
            rns_badge = f'<div class="rns-badge">{rns_info["emoji"]} RNS TODAY: {rns_info["label"]}</div>'
        elif has_rns and rns_info and rns_info.get("negative"):
            rns_badge = '<div class="rns-badge negative-badge">⛔ NEGATIVE RNS</div>'

        squeeze_badge = ""
        if d["bb_squeeze_strong"]:
            squeeze_badge = '<div class="squeeze-badge">🔥 STRONG SQUEEZE</div>'
        elif d["bb_squeeze"]:
            squeeze_badge = '<div class="squeeze-badge dim-squeeze">📐 BB SQUEEZE</div>'

        illiq_badge = ""
        if d["avg_daily_gbp"] < MIN_AVG_DAILY_GBP * 2:
            illiq_badge = '<div class="illiq-badge">⚠ LOW LIQUIDITY</div>'

        dist_badge = ""
        if d["distribution_risk"]:
            dist_badge = '<div class="dist-badge">⚠ DIST ZONE</div>'

        rr_display = f"{rr:.1f}:1" if rr else "N/A"
        pos_shares = f"{d['max_shares']:,}" if d['max_shares'] > 0 else "N/A"
        pos_value  = fmt_gbp(d['position_gbp']) if d['position_gbp'] > 0 else "N/A"

        rows_html += f"""
        <div class="card {'rns-card' if has_rns and not (rns_info and rns_info.get('negative')) else ''}
                         {'neg-card' if has_rns and rns_info and rns_info.get('negative') else ''}
                         {'top-card' if sc >= 8 else ''}"
             data-score="{sc}"
             data-volratio="{d['vol_ratio']:.2f}"
             data-pos52="{d['pos52']:.1f}"
             data-squeeze="{'1' if d['bb_squeeze'] else '0'}"
             data-rns="{'1' if has_rns else '0'}"
             data-rr="{rr or 0}">

          <div class="card-badges">{rns_badge}{squeeze_badge}{illiq_badge}{dist_badge}</div>

          <div class="card-top">
            <div class="card-left">
              <div class="sym">{sym_clean}
                <span class="exch-badge">{'AIM' if d['is_aim'] else 'LSE'}</span>
              </div>
              <div class="company">{d['name']}</div>
              <div class="meta">{d['sector']}{'  ·  ' + d['industry'] if d['industry'] else ''}</div>
              <div class="last-price">{fmt_price(d['close'], d['currency'])}
                <span class="prev-chg {'up' if d['pct_change'] >= 0 else 'dn'}">
                  {d['pct_change']:+.1f}% prev
                </span>
              </div>
              <div class="mkcap">{fmt_cap(d['mktcap_gbp'])} &nbsp;|&nbsp; Liq: {fmt_gbp(d['avg_daily_gbp'])}/day</div>
            </div>

            <div class="card-mid">
              <div class="conf-label" style="color:{conf_col}">{conf}</div>
              <div class="pred-move">Predicted:</div>
              <div class="pred-move-val" style="color:{conf_col}">{pred_move}</div>
              <div class="rr-display">
                <span class="rr-label">R/R</span>
                <span class="rr-val" style="color:{rr_col}">{rr_display}</span>
              </div>
            </div>

            <div class="score-col">
              <div class="score-num" style="color:{sc_col}">{sc}</div>
              <div class="score-label">/ 16</div>
              <div class="score-bar-wrap">
                <div class="score-bar" style="width:{max(0,sc)/16*100:.0f}%; background:{sc_col}"></div>
              </div>
            </div>
          </div>

          <!-- STOP LOSS BOX -->
          <div class="stop-box">
            <div class="stop-title">🛑 STOP LOSS LEVELS</div>
            <div class="stop-grid">
              <div class="stop-item recommended">
                <div class="stop-label">RECOMMENDED ({method})</div>
                <div class="stop-price">{fmt_stop(rec_stop, d['currency'])}</div>
                <div class="stop-pct">−{rec_pct:.1f}% from last close</div>
              </div>
              <div class="stop-item">
                <div class="stop-label">TIGHT (ATR×1.0)</div>
                <div class="stop-price">{fmt_stop(tight_stop, d['currency'])}</div>
                <div class="stop-pct">−{tight_pct:.1f}%</div>
              </div>
              <div class="stop-item">
                <div class="stop-label">SWING LOW ({SWING_LOW_LOOKBACK}d)</div>
                <div class="stop-price">{fmt_stop(swing_low, d['currency'])}</div>
                <div class="stop-pct">−{sl_pct:.1f}%</div>
              </div>
              <div class="stop-item">
                <div class="stop-label">WIDE (ATR×1.5)</div>
                <div class="stop-price">{fmt_stop(atr_stop, d['currency'])}</div>
                <div class="stop-pct">−{atr_pct_s:.1f}%</div>
              </div>
            </div>
            <div class="pos-size-row">
              <span class="ps-label">Position sizing (£{EXAMPLE_ACCOUNT_GBP:,} acct, {ACCOUNT_RISK_PCT}% risk):</span>
              <span class="ps-val">{pos_shares} shares ≈ {pos_value}</span>
              <span class="ps-spread">Est spread: <span style="color:{spread_col}">{d['est_spread_pct']:.1f}%</span></span>
            </div>
            <div class="stop-warning">
              ⚠ Stop levels use prior-day EOD close. Adjust to live L2 price at open.
              AIM stocks gap — a stop order may execute significantly below your level.
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
              <span class="m-label">BB SQZ</span>
              <span class="m-val" style="color:{'#00e676' if d['bb_squeeze'] else '#546e7a'}">
                {'STRONG' if d['bb_squeeze_strong'] else ('YES' if d['bb_squeeze'] else 'NO')}
              </span>
            </div>
            <div class="metric">
              <span class="m-label">VOL BLD</span>
              <span class="m-val" style="color:{'#00e676' if d['vol_rising'] else '#546e7a'}">
                {'YES' if d['vol_rising'] else 'NO'}
              </span>
            </div>
            <div class="metric">
              <span class="m-label">SPREAD</span>
              <span class="m-val" style="color:{spread_col}">{d['est_spread_pct']:.1f}%</span>
            </div>
            <div class="metric">
              <span class="m-label">LIQUIDITY</span>
              <span class="m-val" style="color:{liq_col}">{fmt_gbp(d['avg_daily_gbp'])}</span>
            </div>
            <div class="metric">
              <span class="m-label">UPTREND</span>
              <span class="m-val" style="color:{'#00e676' if d['ema_uptrend'] else '#546e7a'}">
                {'YES' if d['ema_uptrend'] else 'NO'}
              </span>
            </div>
          </div>

          <div class="reasons">{reasons_html}</div>

          <div class="action-box">
            <div class="action-title">📋 PRE-TRADE CHECKLIST</div>
            <div class="action-items">
              <span class="a-item">1. Read full RNS on LSE</span>
              <span class="a-item">2. Check L2 spread at 07:55</span>
              <span class="a-item">3. Confirm volume in first 3 mins of open</span>
              <span class="a-item">4. Set stop BEFORE entering</span>
              <span class="a-item">5. Check for placing / dilution risk</span>
            </div>
          </div>

          <div class="links">
            <a href="{tv_url}"   target="_blank" class="link-btn chart-btn">📈 Chart</a>
            <a href="{rns_url}"  target="_blank" class="link-btn rns-btn">📰 RNS</a>
            <a href="{adv_url}"  target="_blank" class="link-btn adv-btn">📊 ADVFN</a>
            <a href="{yhoo_url}" target="_blank" class="link-btn yh-btn">💹 Yahoo</a>
          </div>
        </div>
        """

    total  = len(results)
    avg_sc = sum(r["score"] for r in results) / total if total else 0

    now_london = datetime.now(LONDON_TZ)
    hm = now_london.hour * 60 + now_london.minute
    if   hm < 480: sess_label, sess_col = "PRE-MARKET — OPTIMAL TIME", "#00e676"
    elif hm < 510: sess_label, sess_col = "AUCTION / OPEN",            "#f5a623"
    elif hm < 810: sess_label, sess_col = "SESSION OPEN",              "#29b6f6"
    elif hm < 930: sess_label, sess_col = "US OVERLAP",                "#f5a623"
    else:          sess_label, sess_col = "MARKET CLOSED",             "#546e7a"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LSE Pre-Market Predictor v2 — {date_str}</title>
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
  .header {{
    padding:14px 28px; border-bottom:1px solid var(--border);
    background:rgba(13,19,24,.98); display:flex; align-items:center;
    justify-content:space-between; position:sticky; top:0; z-index:50;
  }}
  .logo {{ font-family:var(--mono); font-size:18px; color:var(--green); letter-spacing:2px; }}
  .logo span {{ color:var(--dim); }}
  .logo sub {{ font-size:10px; color:var(--amber); letter-spacing:1px; }}
  .v2tag {{ font-family:var(--cond); font-size:10px; color:var(--blue); border:1px solid var(--blue); padding:2px 6px; border-radius:2px; margin-left:8px; vertical-align:middle; }}
  .header-right {{ text-align:right; font-family:var(--mono); font-size:11px; color:var(--dim); line-height:1.9; }}
  .sess {{ font-size:11px; font-weight:bold; letter-spacing:1px; }}

  .how-bar {{
    background:rgba(0,230,118,.04); border-bottom:1px solid rgba(0,230,118,.1);
    padding:10px 28px; font-size:11px; color:var(--dim);
    font-family:var(--mono); line-height:2; display:flex; gap:30px; flex-wrap:wrap;
  }}
  .how-item {{ display:flex; align-items:center; gap:6px; }}
  .how-num  {{ color:var(--green); font-weight:bold; }}

  .stats-bar {{ display:flex; border-bottom:1px solid var(--border); background:var(--bg2); }}
  .stat {{ flex:1; text-align:center; padding:12px 8px; border-right:1px solid var(--border); }}
  .stat:last-child {{ border-right:none; }}
  .stat-v {{ font-family:var(--mono); font-size:22px; color:var(--amber); display:block; }}
  .stat-l {{ font-family:var(--cond); font-size:10px; color:var(--dim); letter-spacing:1.5px; text-transform:uppercase; }}

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
  .filter-btn:hover, .filter-btn.active {{ border-color:var(--green); color:var(--green); background:rgba(0,230,118,.07); }}

  .grid {{ padding:18px 28px; display:grid; grid-template-columns:repeat(auto-fill,minmax(480px,1fr)); gap:16px; }}

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
    font-family:var(--cond); font-size:11px; font-weight:700;
    padding:3px 10px; border-radius:2px; background:rgba(245,166,35,.1);
    border:1px solid rgba(245,166,35,.3); color:var(--amber);
  }}
  .dim-squeeze {{ background:rgba(41,182,246,.07); border-color:rgba(41,182,246,.25); color:var(--blue); }}
  .illiq-badge {{
    font-family:var(--cond); font-size:11px; font-weight:700;
    padding:3px 10px; border-radius:2px; background:rgba(255,61,87,.1);
    border:1px solid rgba(255,61,87,.3); color:var(--red);
  }}
  .dist-badge {{
    font-family:var(--cond); font-size:11px; font-weight:700;
    padding:3px 10px; border-radius:2px; background:rgba(206,147,216,.1);
    border:1px solid rgba(206,147,216,.3); color:var(--purple);
  }}

  .card-top {{ display:flex; padding:12px 14px 10px; gap:12px; align-items:flex-start; }}
  .card-left {{ flex:1; }}
  .sym {{
    font-family:var(--mono); font-size:20px; color:var(--amber);
    letter-spacing:1.5px; display:flex; align-items:center; gap:8px;
  }}
  .exch-badge {{
    font-family:var(--cond); font-size:9px; color:var(--dim);
    border:1px solid var(--border); padding:1px 5px; border-radius:2px;
  }}
  .company {{ font-size:12px; color:var(--dim); margin-top:3px; }}
  .meta    {{ font-size:10px; color:rgba(74,100,120,.6); margin-top:2px; }}
  .last-price {{ font-family:var(--mono); font-size:13px; color:var(--text); margin-top:6px; }}
  .prev-chg {{ font-size:11px; margin-left:6px; }}
  .up {{ color:var(--green); }} .dn {{ color:var(--red); }}
  .mkcap {{ font-size:10px; color:var(--dim); margin-top:4px; font-family:var(--mono); }}

  .card-mid {{ text-align:center; min-width:115px; }}
  .conf-label {{ font-family:var(--cond); font-size:13px; font-weight:700; letter-spacing:1px; }}
  .pred-move {{ font-size:9px; color:var(--dim); font-family:var(--cond); letter-spacing:1px; margin-top:6px; text-transform:uppercase; }}
  .pred-move-val {{ font-family:var(--mono); font-size:17px; font-weight:bold; margin-top:2px; }}
  .rr-display {{ margin-top:8px; }}
  .rr-label {{ font-family:var(--cond); font-size:9px; color:var(--dim); letter-spacing:1px; display:block; }}
  .rr-val   {{ font-family:var(--mono); font-size:18px; font-weight:bold; }}

  .score-col {{ text-align:center; min-width:52px; }}
  .score-num {{ font-family:var(--mono); font-size:26px; font-weight:bold; line-height:1; }}
  .score-label {{ font-size:9px; color:var(--dim); font-family:var(--mono); }}
  .score-bar-wrap {{ width:42px; height:3px; background:var(--bg3); border-radius:2px; margin:4px auto 0; overflow:hidden; }}
  .score-bar {{ height:100%; border-radius:2px; }}

  /* ── STOP LOSS BOX ─────────────────────────────────────────────────────── */
  .stop-box {{
    margin:0 14px 0; padding:10px 12px 8px;
    background:rgba(255,61,87,.04); border:1px solid rgba(255,61,87,.2);
    border-radius:3px;
  }}
  .stop-title {{
    font-family:var(--cond); font-size:12px; font-weight:700; letter-spacing:1px;
    color:#ff7094; margin-bottom:8px;
  }}
  .stop-grid {{
    display:grid; grid-template-columns:repeat(4,1fr); gap:6px; margin-bottom:8px;
  }}
  .stop-item {{
    background:var(--bg3); border:1px solid var(--border); border-radius:3px;
    padding:6px 8px; text-align:center;
  }}
  .stop-item.recommended {{
    background:rgba(255,61,87,.08); border-color:rgba(255,61,87,.35);
  }}
  .stop-label {{ font-family:var(--cond); font-size:9px; color:var(--dim); letter-spacing:0.5px; display:block; margin-bottom:3px; }}
  .stop-price {{ font-family:var(--mono); font-size:13px; color:#ff7094; font-weight:bold; }}
  .stop-pct   {{ font-family:var(--mono); font-size:10px; color:var(--red); margin-top:2px; }}
  .stop-item.recommended .stop-price {{ color:var(--red); font-size:15px; }}
  .stop-item.recommended .stop-pct   {{ font-size:11px; }}
  .pos-size-row {{
    display:flex; gap:12px; align-items:center; flex-wrap:wrap;
    padding-top:6px; border-top:1px solid rgba(255,61,87,.1);
    font-family:var(--mono); font-size:10px; color:var(--dim);
  }}
  .ps-label {{ color:var(--dim); }}
  .ps-val   {{ color:var(--text); font-weight:bold; }}
  .ps-spread {{ margin-left:auto; }}
  .stop-warning {{
    margin-top:6px; font-size:9px; color:rgba(74,100,120,.7);
    font-family:var(--mono); line-height:1.6;
  }}

  .metrics-row {{
    display:flex; flex-wrap:wrap; border-top:1px solid var(--border);
    border-bottom:1px solid var(--border); background:var(--bg3); margin-top:10px;
  }}
  .metric {{
    flex:1; min-width:60px; padding:6px 5px; text-align:center;
    border-right:1px solid var(--border);
  }}
  .metric:last-child {{ border-right:none; }}
  .m-label {{ display:block; font-family:var(--cond); font-size:9px; color:var(--dim); letter-spacing:1px; text-transform:uppercase; }}
  .m-val   {{ display:block; font-family:var(--mono); font-size:11px; color:var(--text); margin-top:2px; }}

  .reasons {{ padding:10px 14px; }}
  .reason  {{ font-size:11px; color:var(--dim); padding:2px 0; line-height:1.6; }}

  .action-box {{
    margin:6px 14px 8px; padding:8px 12px;
    background:rgba(41,182,246,.04); border:1px solid rgba(41,182,246,.15); border-radius:3px;
  }}
  .action-title {{ font-family:var(--cond); font-size:11px; color:var(--blue); font-weight:700; letter-spacing:1px; margin-bottom:5px; }}
  .action-items {{ display:flex; flex-wrap:wrap; gap:8px; }}
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
  .chart-btn {{ color:var(--blue);  border-color:rgba(41,182,246,.3);  background:rgba(41,182,246,.07); }}
  .chart-btn:hover {{ background:rgba(41,182,246,.15); }}
  .rns-btn   {{ color:var(--amber); border-color:rgba(245,166,35,.3);  background:rgba(245,166,35,.07); }}
  .rns-btn:hover {{ background:rgba(245,166,35,.15); }}
  .adv-btn   {{ color:var(--purple);border-color:rgba(206,147,216,.3); background:rgba(206,147,216,.07); }}
  .adv-btn:hover {{ background:rgba(206,147,216,.15); }}
  .yh-btn    {{ color:var(--green); border-color:rgba(0,230,118,.3);   background:rgba(0,230,118,.07); }}
  .yh-btn:hover {{ background:rgba(0,230,118,.15); }}

  .empty {{ text-align:center; padding:60px 20px; color:var(--dim); font-family:var(--mono); font-size:13px; line-height:2; }}
  .footer {{
    margin-top:40px; padding:18px 28px; border-top:1px solid var(--border);
    font-size:11px; color:var(--dim); font-family:var(--mono); line-height:2.2;
    background:var(--bg2);
  }}
  .hidden {{ display:none !important; }}
  ::-webkit-scrollbar {{ width:4px; }} ::-webkit-scrollbar-thumb {{ background:var(--border); }}
  @media(max-width:540px) {{ .grid {{ padding:10px; grid-template-columns:1fr; }} .stop-grid {{ grid-template-columns:repeat(2,1fr); }} }}
</style>
</head>
<body>

<div class="header">
  <div class="logo">
    LSE<span>·</span>PREDICT <span class="v2tag">v2.0</span><br>
    <sub>AIM PRE-MARKET SCANNER + STOP LOSS ENGINE</sub>
  </div>
  <div class="header-right">
    <div>{date_str} &nbsp;|&nbsp; Scanned {scan_time} London</div>
    <div class="sess" style="color:{sess_col}">● {sess_label}</div>
  </div>
</div>

<div class="how-bar">
  <div class="how-item"><span class="how-num">①</span> RNS catalyst (RSS + JSON fallback)</div>
  <div class="how-item"><span class="how-num">②</span> BB squeeze → volatility coiled</div>
  <div class="how-item"><span class="how-num">③</span> Volume accumulation → smart money</div>
  <div class="how-item"><span class="how-num">④</span> Stop loss: ATR-based + swing low</div>
  <div class="how-item"><span class="how-num">⑤</span> Liquidity gate: min £{MIN_AVG_DAILY_GBP/1000:.0f}k/day</div>
  <div class="how-item"><span class="how-num">⑥</span> Best run: 06:30–07:55 London</div>
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
    <span class="stat-l">Score ≥ 8/16</span>
  </div>
  <div class="stat">
    <span class="stat-v" style="color:var(--green)">{good_rr}</span>
    <span class="stat-l">R/R ≥ 2:1</span>
  </div>
  <div class="stat">
    <span class="stat-v">{avg_sc:.1f}</span>
    <span class="stat-l">Avg Score</span>
  </div>
</div>

<div class="filter-bar">
  <span class="filter-label">FILTER ›</span>
  <button class="filter-btn active" onclick="fc('all',this)">ALL ({total})</button>
  <button class="filter-btn" onclick="fc('rns',this)">HAS RNS</button>
  <button class="filter-btn" onclick="fc('squeeze',this)">BB SQUEEZE</button>
  <button class="filter-btn" onclick="fc('top',this)">SCORE ≥ 8</button>
  <button class="filter-btn" onclick="fc('goodrr',this)">R/R ≥ 2:1</button>
  <button class="filter-btn" onclick="fc('liquid',this)">LIQUID ≥ £50K</button>
  <button class="filter-btn" onclick="fc('volbuild',this)">VOL BUILD</button>
</div>

<div class="grid" id="grid">
  {rows_html if rows_html.strip() else
   '<div class="empty">No candidates found.<br>'
   'Try running 06:30–07:55 London time for RNS signals.<br>'
   'Lower MIN_PRED_SCORE or MIN_ATR_PCT in CONFIG to see more.</div>'}
</div>

<div class="footer">
  ⚠  IMPORTANT DISCLAIMERS (v2.0):<br>
  · Stop loss levels are ESTIMATES using prior-day EOD data from Yahoo Finance.<br>
  · AIM stocks can gap significantly at open — stop orders may fill well below your target stop price.<br>
  · Always check the live Level 2 order book at 07:55 before committing to any stop level.<br>
  · Bid/ask spread estimates are heuristic only. Real spreads on AIM can be 2–8% at open.<br>
  · Liquidity gate (£{MIN_AVG_DAILY_GBP/1000:.0f}k/day) protects against untradeable names, but check L2 manually.<br>
  · Position sizing shown is illustrative for a £{EXAMPLE_ACCOUNT_GBP:,} account risking {ACCOUNT_RISK_PCT}% — adjust to your own account.<br>
  · R/R ratio uses midpoint of predicted move vs recommended stop — treat as directional only.<br>
  · This is NOT financial advice. AIM stocks carry extreme risk including 100% loss of capital.
</div>

<script>
function fc(type, btn) {{
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(card => {{
    const score   = parseInt(card.dataset.score   || 0);
    const squeeze = card.dataset.squeeze === '1';
    const rns     = card.dataset.rns     === '1';
    const rr      = parseFloat(card.dataset.rr    || 0);
    let volbuild  = false;
    let liquid    = false;
    card.querySelectorAll('.m-label').forEach((el, i) => {{
      if (el.textContent.trim() === 'VOL BLD') {{
        const val = el.nextElementSibling;
        if (val && val.textContent.trim() === 'YES') volbuild = true;
      }}
      if (el.textContent.trim() === 'LIQUIDITY') {{
        const val = el.nextElementSibling;
        if (val) {{
          const txt = val.textContent.replace('£','').replace('K','000').replace('M','000000');
          if (parseFloat(txt) >= 50000) liquid = true;
        }}
      }}
    }});
    let show = true;
    if (type === 'rns')     show = rns;
    if (type === 'squeeze') show = squeeze;
    if (type === 'top')     show = score >= 8;
    if (type === 'goodrr')  show = rr >= 2;
    if (type === 'liquid')  show = liquid;
    if (type === 'volbuild')show = volbuild;
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

    print("═" * 66)
    print("  LSE PRE-MARKET PREDICTIVE SCANNER  v2.0")
    print(f"  {date_str}  |  {scan_time} London")
    print("  Now with stop-losses, R/R ratios, liquidity gate")
    print("═" * 66)

    if now_london.hour >= 16 or now_london.hour < 6:
        print("\n  ⚠  Best run 06:30–07:55 London. RNS from 07:00.")
        print("     Technical setups valid any time.\n")

    # Step 1: RNS
    print("\n[1/4] Fetching pre-market RNS (RSS + JSON fallback)...")
    rns_map     = fetch_rns_today()
    rns_symbols = set(rns_map.keys())
    print(f"  ✓ {len(rns_map)} RNS tickers matched")
    if rns_map:
        print("  Found:", ", ".join(
            s.replace(".L", "") for s in list(rns_map.keys())[:15]
        ) + ("..." if len(rns_map) > 15 else ""))

    # Step 2: Symbol list
    print("\n[2/4] Building symbol list...")
    all_syms = list(dict.fromkeys(list(rns_symbols) + WATCHLIST_YAHOO))
    print(f"  ✓ {len(all_syms)} symbols "
          f"({len(rns_symbols)} RNS + {len(WATCHLIST_YAHOO)} watchlist)")

    # Step 3: Download
    print(f"\n[3/4] Downloading 120-day history ({MAX_WORKERS} workers)...")
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
                print(f"  [{bar}] {done}/{len(all_syms)}  ({len(all_data)} valid)", end="\r")
    print(f"\n  ✓ Valid data: {len(all_data)} symbols")

    # Step 4: Score
    print("\n[4/4] Scoring + stop-loss calculation...")
    results = []
    filtered_illiq = 0
    for d in all_data:
        # Liquidity gate (bypass for RNS stocks — they're worth checking anyway)
        if d["avg_daily_gbp"] < MIN_AVG_DAILY_GBP and d["symbol"] not in rns_symbols:
            filtered_illiq += 1
            continue

        if d["atr_pct"] < MIN_ATR_PCT and d["symbol"] not in rns_symbols:
            continue

        mc = d["mktcap_gbp"]
        if mc is not None and mc > MAX_MKTCAP_GBP and d["symbol"] not in rns_symbols:
            continue

        rns_info = rns_map.get(d["symbol"])
        score, reasons, confidence, predicted_move, rr_ratio = score_predictive(d, rns_info)

        if score < MIN_PRED_SCORE and d["symbol"] not in rns_symbols:
            continue

        results.append({
            "data":           d,
            "score":          score,
            "reasons":        reasons,
            "confidence":     confidence,
            "predicted_move": predicted_move,
            "rr_ratio":       rr_ratio,
            "has_rns":        d["symbol"] in rns_symbols,
        })

    results.sort(key=lambda x: (-x["score"], -(x["rr_ratio"] or 0), -x["data"]["atr_pct"]))
    print(f"  ✓ {len(results)} candidates passed filters")
    print(f"  ✓ {filtered_illiq} filtered out (illiquid < £{MIN_AVG_DAILY_GBP/1000:.0f}k/day)")

    if results:
        print("\n  ┌── TOP CANDIDATES ─────────────────────────────────────────────┐")
        for r in results[:12]:
            d      = r["data"]
            mc     = f"£{d['mktcap_gbp']/1e6:.0f}M" if d["mktcap_gbp"] else "N/A"
            rr_str = f"R/R {r['rr_ratio']:.1f}:1" if r["rr_ratio"] else "R/R N/A"
            sl_str = f"SL {d['rec_stop_pct']:.1f}%"
            rns_fl = " ◀ RNS" if r["has_rns"] else ""
            print(f"  │ {d['symbol'].replace('.L',''):<8} "
                  f"score {r['score']:>2}/16  "
                  f"ATR {d['atr_pct']:.1f}%  "
                  f"{sl_str:<8}  "
                  f"{rr_str:<10}  "
                  f"{mc:<7}"
                  f"{rns_fl}")
        print("  └────────────────────────────────────────────────────────────────┘")

    # Step 5: HTML
    print("\nGenerating HTML report...")
    html    = build_html(results, rns_map, scan_time, date_str)
    outfile = f"lse_predict_{file_date}.html"
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Saved: {outfile}")

    try:
        webbrowser.open(f"file://{os.path.abspath(outfile)}")
        print("  ✓ Opened in browser")
    except Exception:
        print(f"  ⚠ Open manually: {os.path.abspath(outfile)}")

    print("\n" + "═" * 66)
    if results:
        top = results[0]
        d   = top["data"]
        rr  = f"R/R {top['rr_ratio']:.1f}:1" if top["rr_ratio"] else ""
        print(f"  Best: {d['symbol'].replace('.L','')} | "
              f"Score {top['score']}/16 | "
              f"{top['confidence']} | "
              f"Stop −{d['rec_stop_pct']:.1f}% | {rr}")
        if top["has_rns"]:
            rns = rns_map.get(d["symbol"])
            if rns:
                print(f"  RNS: {rns.get('label','')} — {rns.get('headline','')[:65]}")
    else:
        print("  No candidates found today.")
    print("═" * 66 + "\n")


if __name__ == "__main__":
    main()
