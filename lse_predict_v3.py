#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  LSE PRE-MARKET PREDICTIVE SCANNER  v3.0  — BUGFIX RELEASE            ║
║  Finds AIM / small-cap stocks BEFORE they move 5–20%                  ║
║                                                                          ║
║  ▶ WHEN TO RUN: 06:30–07:55 London time (before LSE opens 08:00)      ║
║                                                                          ║
║  BUGS FIXED vs v2.0:                                                     ║
║   ✓ BUG1: 52W range now uses 252 days (1 year), not 120 days           ║
║   ✓ BUG2: inside_day calculation was off by one day                     ║
║   ✓ BUG3: strong_close was measuring wrong candle and wrong formula     ║
║   ✓ BUG4: vol_rising was checking 5 days instead of VOL_BUILDUP_DAYS   ║
║   ✓ BUG5: range_compression compared H-L to ATR (apples-to-oranges)   ║
║   ✓ BUG6: downtrend stocks leaked through with score=3 from noise      ║
║   ✓ BUG7: mktcap_gbp divided by 100 wrongly for GBp stocks            ║
║   ✓ BUG8: EMA truthiness check fails for near-zero penny stock prices  ║
║   ✓ BUG9: RNS negatives matched too many innocent announcements        ║
║   ✓ BUG10: ATR_STOP_TIGHT defined after function that used it          ║
║   ✓ BUG11: no "minimum one real signal" gate — noise added up to score ║
║                                                                          ║
║  INSTALL:                                                                ║
║    pip install yfinance requests pandas                                  ║
║                                                                          ║
║  RUN:                                                                    ║
║    python lse_predict_v3.py                                              ║
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
import webbrowser
import os
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import concurrent.futures
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG — all tuning knobs in one place
# ─────────────────────────────────────────────────────────────────────────────
MAX_WORKERS          = 12       # parallel downloads; keep ≤ 15
MIN_PRED_SCORE       = 4        # raised from 3 — requires more evidence
MIN_ATR_PCT          = 2.0      # minimum ATR% to be worth scanning
MAX_MKTCAP_GBP       = 600e6    # ignore large-caps (won't move 20%)
MIN_AVG_DAILY_GBP    = 30_000   # liquidity gate: £30k avg daily turnover minimum
BB_SQUEEZE_RANK      = 25       # BB width in bottom 25th percentile = squeeze
VOL_BUILDUP_DAYS     = 3        # number of consecutive rising-volume days required
RNS_LOOKBACK_HRS     = 8        # hours back to check RNS (covers overnight)

# BUG10 FIX: All stop-loss constants defined here — before any function that uses them
ATR_STOP_MULT        = 1.5      # recommended stop = entry − (ATR × 1.5)
ATR_STOP_MULT_TIGHT  = 1.0      # tight stop = entry − (ATR × 1.0)
SWING_LOW_LOOKBACK   = 10       # days for swing-low support calculation
MAX_STOP_PCT         = 12.0     # cap stop distance at 12%

# Position sizing display (illustrative — NOT executed)
ACCOUNT_RISK_PCT     = 1.0      # % of account to risk per trade
EXAMPLE_ACCOUNT_GBP  = 10_000   # example account size

LONDON_TZ = ZoneInfo("Europe/London")

# ─────────────────────────────────────────────────────────────────────────────
#  RNS TRIGGER SCORING
#  Format: ([keywords], pts, label, emoji, expected_move)
# ─────────────────────────────────────────────────────────────────────────────
RNS_TRIGGERS = [
    # TIER 1: 15–50% typical move
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
         "orphan drug", "primary endpoint met", "met its primary endpoint",
         "significant clinical benefit"],
        4, "Clinical / Regulatory Result", "💊", "15–40%"
    ),
    (
        ["maiden resource", "initial resource", "resource estimate",
         "significant intersection", "high grade intercept", "drill results",
         "bonanza grade", "significant mineralisation", "reserve update",
         "jorc resource", "ni 43-101 resource"],
        3, "Resource / Drill Result", "⛏️", "10–30%"
    ),
    # TIER 2: 5–20% typical move
    (
        ["materially ahead", "significantly ahead", "ahead of market expectations",
         "ahead of expectations", "ahead of management expectations",
         "record revenue", "record sales", "record profit", "record results",
         "exceeds expectations", "well ahead of expectations"],
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
         "letter of intent signed", "heads of terms signed",
         "framework agreement signed", "supply agreement", "offtake agreement"],
        2, "Contract Win", "📝", "5–15%"
    ),
    (
        ["full year results", "half year results", "interim results",
         "preliminary results", "annual results", "positive trading update",
         "strong trading performance", "confident outlook", "board is pleased to report"],
        2, "Results / Trading Update", "📊", "3–10%"
    ),
    (
        ["director purchase", "pdmr purchase",
         "executive director purchase", "ceo purchase", "cfo purchase"],
        1, "Director / Insider Buying", "👤", "2–8%"
    ),
]

# BUG9 FIX: Removed over-broad terms: "rpl", "financial conduct authority",
# "convertible loan" (many legitimate deals), "director loan" (ambiguous)
# Kept only terms that are unambiguously negative in isolation
RNS_NEGATIVES = [
    # Operational warnings
    "profit warning", "revenue warning", "below expectations",
    "below market expectations", "disappointing results",
    "challenging trading conditions", "shortfall in revenue",
    "below board expectations", "below management expectations",
    # Dilution events with clear negative signal
    "placing at a discount of", "deeply discounted placing",
    "distressed placing", "emergency placing",
    # Corporate distress — unambiguous
    "suspension of trading", "suspended from trading",
    "administration", "insolvency", "liquidation",
    "cancellation of admission", "cease trading",
    "winding up petition", "material uncertainty related to going concern",
    "breach of banking covenant",
    # Serious regulatory / legal
    "fca investigation into", "criminal investigation into",
    "serious fraud office",
]

# ─────────────────────────────────────────────────────────────────────────────
#  CURATED WATCHLIST — valid AIM tickers with history of sharp moves
# ─────────────────────────────────────────────────────────────────────────────
WATCHLIST = [
    # Biotech / Pharma / MedTech (AIM-listed, active)
    "AVCT","BVXP","CLI","CLIN","CRSO","CRW","GBG","HAT","HBR","IGP",
    "IMM","JOG","KBT","LIO","MCB","MED","MRC","MTI","NANO","NCZ",
    "OXB","POLB","RDL","SLN","TRX","VRS","WGB","CTEC","GENL","MDNA",
    # Mining / Resources (AIM-listed, active)
    "AAZ","ALBA","AMI","AOG","APH","ARG","ARL","ARM","ARP","ATM",
    "AUR","AVN","BKT","CAD","ECR","EDL","EGO","EMX","ERG","GCM",
    "GTI","HYR","IQE","KORE","LWI","MDC","MMX","MKA","MOTS","NAP",
    "POG","RRS","SXX","THL","UJO","VGM","WKP","XTR","ZYT","KEFI",
    # Tech / Digital / Fintech (AIM-listed)
    "ALFA","AMS","ANP","APP","BIG","BUR","CAB","GAM","GAN","HAV",
    "HAWK","IGN","KAV","MCK","PAD","QRT","RAD","SLP","TAM","ASOS",
    "BOO","PETS","WHR","GFRD","SDX","STB","TCG","TED","LOOP",
    # Known AIM movers with strong intraday history
    "HOC","LUCK","MAST","MATD","HIGH","GROW","JQW",
    "MIND","MINT","MIRA","CRON","LEAF",
]

def to_yahoo(t: str) -> str:
    t = t.strip().upper()
    return t if t.endswith(".L") else t + ".L"

WATCHLIST_YAHOO = list(dict.fromkeys(to_yahoo(t) for t in WATCHLIST))


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1: Fetch RNS — RSS feed first, JSON API fallback
# ─────────────────────────────────────────────────────────────────────────────
def fetch_rns_today() -> dict[str, dict]:
    rns_data: dict[str, dict] = {}
    now_london = datetime.now(LONDON_TZ)
    cutoff = now_london - timedelta(hours=RNS_LOOKBACK_HRS)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Referer": "https://www.londonstockexchange.com/",
    }

    # Attempt 1: RSS feed (more stable structure than JSON API)
    rss_urls = [
        "https://www.londonstockexchange.com/exchange/news/market-news/market-news-home.html?rss=true",
        "https://api.londonstockexchange.com/api/gw/lse/regulatory-news/rss",
        "https://www.londonstockexchange.com/news?tab=regulatory-news&rss=true",
    ]
    for url in rss_urls:
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200 and ("<rss" in r.text or "<feed" in r.text):
                items = _parse_rss(r.text, cutoff)
                if items:
                    rns_data.update(items)
                    print(f"  ✓ RNS RSS: {len(rns_data)} announcements")
                    return rns_data
        except Exception:
            pass

    # Attempt 2: JSON API fallback
    api_urls = [
        ("https://api.londonstockexchange.com/api/gw/lse/instruments/alldata/news"
         "?worlds=quotes&count=200&sortby=time&category=RegulatoryAnnouncement"),
        ("https://api.londonstockexchange.com/api/gw/lse/instruments"
         "/alldata/regulatorynewsheadlines?worlds=quotes&count=200"),
    ]
    for url in api_urls:
        try:
            r = requests.get(url, headers={**headers, "Accept": "application/json"},
                             timeout=12)
            if r.status_code == 200:
                items = _parse_json_api(r.json(), cutoff)
                if items:
                    rns_data.update(items)
                    print(f"  ✓ RNS JSON API: {len(rns_data)} announcements")
                    return rns_data
        except Exception:
            pass

    print("  ⚠ RNS feed unreachable — technical signals only.")
    print("    Normal outside 06:30–08:30 or if LSE is rate-limiting.")
    return rns_data


def _parse_rss(xml_text: str, cutoff: datetime) -> dict[str, dict]:
    result = {}
    try:
        root = ET.fromstring(xml_text)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        for item in items:
            title_el = item.find("title") or item.find("atom:title", ns)
            headline = (title_el.text or "").strip() if title_el is not None else ""
            if not headline:
                continue

            # LSE RSS format: "TICKER: Company Name - Announcement"
            tidm = ""
            m = re.match(r"^([A-Z]{2,5})\s*[-:]", headline)
            if m:
                tidm = m.group(1)
            if not tidm:
                desc_el = item.find("description") or item.find("atom:summary", ns)
                desc_text = (desc_el.text or "") if desc_el is not None else ""
                m2 = re.search(r"\b([A-Z]{2,5})\.L\b", desc_text)
                if m2:
                    tidm = m2.group(1)
            if not tidm:
                continue

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
    result = {}
    items = (data.get("content") or data.get("data") or
             data.get("news") or data.get("items") or [])
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
                (item.get("instrument") or {}).get("tidm") or ""
            )
            headline = (
                item.get("headline") or item.get("title") or
                item.get("summary") or item.get("description") or ""
            )
            if not tidm or not headline:
                continue

            time_str = (item.get("publishedTime") or item.get("publishedDate") or
                        item.get("date") or item.get("time") or "")
            if time_str:
                try:
                    pt = datetime.fromisoformat(
                        time_str.replace("Z", "+00:00")
                    ).astimezone(LONDON_TZ)
                    if pt < cutoff:
                        continue
                except Exception:
                    pass

            _score_and_store(tidm.strip().upper(), headline, result)
        except Exception:
            continue
    return result


def _score_and_store(tidm: str, headline: str, result: dict):
    """Score a headline against RNS triggers and store in result dict."""
    symbol   = tidm.upper() + ".L"
    hl_lower = headline.lower()

    # BUG9 FIX: tightened negative terms — only unambiguous negatives
    is_negative = any(neg in hl_lower for neg in RNS_NEGATIVES)
    if is_negative:
        result[symbol] = {
            "headline": headline, "score": -3,
            "label": "⛔ Negative / Warning", "emoji": "⛔",
            "expected_move": "−5% to −30%", "negative": True,
        }
        return

    best_score, best_label, best_emoji, best_move = 0, "General", "📌", "unknown"
    for keywords, pts, label, emoji, move in RNS_TRIGGERS:
        if any(re.search(kw, hl_lower) for kw in keywords):
            if pts > best_score:
                best_score, best_label, best_emoji, best_move = pts, label, emoji, move

    if best_score > 0:
        result[symbol] = {
            "headline": headline, "score": best_score,
            "label": best_label, "emoji": best_emoji,
            "expected_move": best_move, "negative": False,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2: Fetch ticker technical data
# ─────────────────────────────────────────────────────────────────────────────
def fetch_ticker_data(symbol: str) -> dict | None:
    """
    Download 1 year of history for proper 52W metrics + all technical signals.
    Pre-market: hist.iloc[-1] = yesterday (most recent complete session).
    """
    try:
        tk   = yf.Ticker(symbol)
        # BUG1 FIX: Use 1 year (252 trading days) for accurate 52W metrics
        hist = tk.history(period="1y", auto_adjust=True)

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

        # Pre-market context:
        # hist.iloc[-1] = yesterday (most recent complete trading day)
        # hist.iloc[-2] = 2 days ago
        # hist.iloc[-3] = 3 days ago
        last  = hist.iloc[-1]   # yesterday — most recent complete session
        prev1 = hist.iloc[-2] if len(hist) >= 2 else last   # 2 days ago
        prev2 = hist.iloc[-3] if len(hist) >= 3 else prev1  # 3 days ago

        close   = float(last["Close"])
        open_p  = float(last.get("Open",   close))
        high_p  = float(last.get("High",   close))
        low_p   = float(last.get("Low",    close))
        vol     = float(last.get("Volume", 0))
        prev_c  = float(prev1["Close"])
        prev2_c = float(prev2["Close"])

        # Yesterday's % change (yesterday vs day-before)
        pct_change = (close - prev_c) / prev_c * 100 if prev_c else 0

        # ── Currency & price in GBP ───────────────────────────────────────────
        currency  = info.get("currency", "GBP")
        # For GBp (pence) stocks: price is in pence, convert to pounds for £ calcs
        price_gbp = close / 100.0 if currency == "GBp" else close

        # ── Average daily turnover (20d, excludes yesterday) ─────────────────
        avg_vol_20 = (float(volumes.iloc[-21:-1].mean())
                      if len(volumes) >= 21 else float(volumes.iloc[:-1].mean()))
        avg_daily_gbp = avg_vol_20 * price_gbp
        vol_ratio     = vol / avg_vol_20 if avg_vol_20 > 0 else 0.0

        # ── Estimated bid/ask spread (heuristic by liquidity tier) ────────────
        if   avg_daily_gbp >= 500_000: est_spread_pct = 0.5
        elif avg_daily_gbp >= 150_000: est_spread_pct = 1.5
        elif avg_daily_gbp >= 50_000:  est_spread_pct = 2.5
        elif avg_daily_gbp >= 20_000:  est_spread_pct = 4.0
        else:                           est_spread_pct = 6.0  # effectively untradeable

        # ── ATR(14) — correct direction: i=1 is yesterday, i+1 is day before ─
        tr_list = []
        for i in range(1, min(15, len(hist))):
            h  = float(hist.iloc[-i]["High"])
            l  = float(hist.iloc[-i]["Low"])
            pc = float(hist.iloc[-(i + 1)]["Close"])
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr14   = sum(tr_list) / len(tr_list) if tr_list else 0.0
        atr_pct = atr14 / close * 100.0 if close > 0 else 0.0

        # ── Market cap — BUG7 FIX ─────────────────────────────────────────────
        # Yahoo Finance returns marketCap in GBP (not pence) for UK stocks,
        # regardless of whether the stock trades in GBp.
        # Cross-check with shares × price_gbp when available.
        mktcap_gbp = None
        shares_out = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        if shares_out and price_gbp > 0:
            # Most reliable: price we know × shares Yahoo tells us
            mktcap_gbp = float(shares_out) * price_gbp
        else:
            raw_mc = info.get("marketCap")
            if raw_mc:
                # Yahoo gives marketCap in GBP for UK stocks — do NOT divide by 100
                mktcap_gbp = float(raw_mc)

        is_aim = (
            info.get("exchange", "").upper() in ("AIM", "LSE", "IOB")
            or (mktcap_gbp is not None and mktcap_gbp < MAX_MKTCAP_GBP)
        )

        # ── STOP LOSS CALCULATIONS (all constants defined at module top) ───────
        atr_stop_dist      = atr14 * ATR_STOP_MULT
        atr_stop_pct       = min(atr_stop_dist / close * 100, MAX_STOP_PCT) if close > 0 else 0
        atr_stop_price     = close * (1 - atr_stop_pct / 100)

        atr_tight_pct      = min(atr14 * ATR_STOP_MULT_TIGHT / close * 100, MAX_STOP_PCT) if close > 0 else 0
        atr_tight_price    = close * (1 - atr_tight_pct / 100)

        lookback_n         = min(SWING_LOW_LOOKBACK, len(lows))
        swing_lows_list    = [float(lows.iloc[-i]) for i in range(1, lookback_n + 1)]
        swing_low_raw      = min(swing_lows_list) * 0.995  # 0.5% buffer below swing low
        swing_low_pct      = min((close - swing_low_raw) / close * 100, MAX_STOP_PCT) if close > 0 else 0
        swing_low_price    = close * (1 - swing_low_pct / 100)

        # Choose recommended stop: prefer swing low if it's tighter but still
        # outside the bid-ask spread (i.e., a real stop, not inside the spread)
        min_stop_pct = est_spread_pct * 2.0  # stop must be at least 2× spread away
        if swing_low_pct < atr_stop_pct and swing_low_pct >= min_stop_pct:
            rec_stop_price  = swing_low_price
            rec_stop_pct    = swing_low_pct
            rec_stop_method = "Swing Low"
        else:
            rec_stop_price  = atr_stop_price
            rec_stop_pct    = atr_stop_pct
            rec_stop_method = f"ATR×{ATR_STOP_MULT}"

        # Position sizing
        max_risk_gbp  = EXAMPLE_ACCOUNT_GBP * ACCOUNT_RISK_PCT / 100
        stop_dist_gbp = price_gbp * rec_stop_pct / 100
        if stop_dist_gbp > 0 and price_gbp > 0:
            max_shares   = int(max_risk_gbp / stop_dist_gbp)
            position_gbp = max_shares * price_gbp
        else:
            max_shares   = 0
            position_gbp = 0.0

        # ── Bollinger Band Squeeze ────────────────────────────────────────────
        bb_widths = []
        for i in range(20, len(closes) + 1):
            w = closes.iloc[i - 20:i]
            mid = w.mean()
            std = w.std()
            bb_widths.append((2 * std / mid * 100) if mid > 0 else 0)

        current_bb = bb_widths[-1] if bb_widths else 0
        if len(bb_widths) >= 20:
            sorted_w         = sorted(bb_widths)
            bb_squeeze        = current_bb <= sorted_w[int(len(sorted_w) * BB_SQUEEZE_RANK / 100)]
            bb_squeeze_strong = current_bb <= sorted_w[int(len(sorted_w) * 0.10)]
        else:
            bb_squeeze = bb_squeeze_strong = False

        # ── Volume accumulation ───────────────────────────────────────────────
        # BUG4 FIX: use exactly VOL_BUILDUP_DAYS, not VOL_BUILDUP_DAYS + 2
        n_vol     = min(VOL_BUILDUP_DAYS, len(volumes) - 1)
        # Build list oldest→newest: volumes.iloc[-(n+1)] ... volumes.iloc[-1]
        vol_seq   = [float(volumes.iloc[-(n_vol - i)]) for i in range(n_vol + 1)]
        # vol_seq[0] = n days ago (oldest), vol_seq[-1] = yesterday (newest)
        vol_rising    = all(vol_seq[i] > vol_seq[i - 1] for i in range(1, len(vol_seq)))
        vol_above_avg = vol_ratio >= 1.5  # yesterday was 1.5× the 20-day average

        # ── RSI(14) — avoids chasing already-exhausted moves ─────────────────
        if len(closes) >= 15:
            deltas = closes.diff().dropna()
            gains  = deltas.clip(lower=0)
            losses = (-deltas).clip(lower=0)
            avg_g  = gains.rolling(14).mean().iloc[-1]
            avg_l  = losses.rolling(14).mean().iloc[-1]
            rsi14  = 100 - (100 / (1 + avg_g / avg_l)) if avg_l > 0 else 100.0
        else:
            rsi14  = 50.0  # neutral if not enough data

        # ── EMAs ──────────────────────────────────────────────────────────────
        # BUG8 FIX: use explicit `is not None` checks, not truthiness
        ema9  = float(closes.ewm(span=9,  adjust=False).mean().iloc[-1]) if len(closes) >= 9  else None
        ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1]) if len(closes) >= 20 else None
        ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else None

        ema_aligned = (
            ema9 is not None and ema20 is not None and ema50 is not None
            and close > ema9 > ema20 > ema50
        )
        ema_uptrend = (
            ema20 is not None and ema50 is not None and ema20 > ema50
        )
        # Is the close above its medium-term trend line?
        above_ema20 = ema20 is not None and close > ema20
        above_ema50 = ema50 is not None and close > ema50

        # ── 52-week range (BUG1 FIX: now uses full 1-year history) ───────────
        hi52         = float(closes.max())
        lo52         = float(closes.min())
        rng52        = hi52 - lo52
        pos52        = (close - lo52) / rng52 * 100 if rng52 > 0 else 50.0
        dist_hi_pct  = (hi52 - close) / hi52 * 100 if hi52 > 0 else 0.0

        near_52w_high     = pos52 >= 90   # within top 10% of 52W range
        at_52w_high       = pos52 >= 97   # at or making a new 52W high
        distribution_risk = at_52w_high   # on AIM, at 52W high = possible distribution
        in_discount       = pos52 < 35    # in lower third of 52W range

        # ── Inside day — BUG2 FIX ─────────────────────────────────────────────
        # "yesterday" = hist.iloc[-1] = last; "day before" = hist.iloc[-2] = prev1
        # (was wrongly using prev1 vs prev2 — one day behind)
        yest_high  = high_p          # last["High"] — yesterday
        yest_low   = low_p           # last["Low"]  — yesterday
        d_bef_high = float(prev1.get("High", prev_c))  # day before yesterday
        d_bef_low  = float(prev1.get("Low",  prev_c))
        inside_day = (yest_high <= d_bef_high and yest_low >= d_bef_low)

        # ── Strong close — BUG3 FIX ───────────────────────────────────────────
        # Did yesterday close in the top 25% of ITS OWN session range?
        yest_range    = yest_high - yest_low
        close_position = (close - yest_low) / yest_range if yest_range > 0 else 0.5
        strong_close   = close_position >= 0.75

        # ── Range compression — BUG5 FIX ─────────────────────────────────────
        # Compare recent 5-day average H-L range to 20-day average H-L range
        # (NOT against ATR, which includes gaps and is always larger)
        recent_hl = [
            float(hist.iloc[-i]["High"]) - float(hist.iloc[-i]["Low"])
            for i in range(1, min(6, len(hist)))
        ]
        longterm_hl = [
            float(hist.iloc[-i]["High"]) - float(hist.iloc[-i]["Low"])
            for i in range(1, min(21, len(hist)))
        ]
        avg_recent_hl  = sum(recent_hl)   / len(recent_hl)   if recent_hl   else 0
        avg_longterm_hl= sum(longterm_hl) / len(longterm_hl) if longterm_hl else 0
        # Range compression = recent daily swings are 30%+ tighter than 20-day average
        range_compression = (
            avg_recent_hl < avg_longterm_hl * 0.70 and avg_longterm_hl > 0
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
            "rsi14":                rsi14,
            "ema9":                 ema9,
            "ema20":                ema20,
            "ema50":                ema50,
            "ema_aligned":          ema_aligned,
            "ema_uptrend":          ema_uptrend,
            "above_ema20":          above_ema20,
            "above_ema50":          above_ema50,
            "pos52":                pos52,
            "hi52":                 hi52,
            "lo52":                 lo52,
            "dist_hi_pct":          dist_hi_pct,
            "near_52w_high":        near_52w_high,
            "at_52w_high":          at_52w_high,
            "distribution_risk":    distribution_risk,
            "in_discount":          in_discount,
            "bb_squeeze":           bb_squeeze,
            "bb_squeeze_strong":    bb_squeeze_strong,
            "bb_width":             current_bb,
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
            # Stop loss
            "atr_stop_price":       atr_stop_price,
            "atr_stop_pct":         atr_stop_pct,
            "atr_tight_price":      atr_tight_price,
            "atr_tight_pct":        atr_tight_pct,
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
#  STEP 3: Predictive Scoring (0–16 points)
# ─────────────────────────────────────────────────────────────────────────────
def score_predictive(d: dict, rns: dict | None) -> tuple[int, list[str], str, str, float | None]:
    """
    Returns (score, reasons, confidence, predicted_move, rr_ratio).

    BUG6 FIX: Added trend-direction penalties so downtrend stocks don't
    accumulate score from compression/ATR noise alone.

    BUG11 FIX: Added minimum-signal gate — stock must have at least one
    genuine quality signal (BB squeeze, volume buildup, or near 52W high)
    unless it has a positive RNS.
    """
    s       = 0
    reasons = []
    predicted_move_midpoint = None

    # ── 1. Trend direction gate (applied first) ────────────────────────────────
    # BUG6 FIX: penalise stocks in confirmed downtrends
    # These penalties make it very hard to pass MIN_PRED_SCORE without real signals
    if not d["above_ema20"] and not d["above_ema50"]:
        s -= 2
        reasons.append(
            "⛔ DOWNTREND: Close below both EMA20 and EMA50 — "
            "price in confirmed downtrend, avoid unless strong RNS catalyst"
        )
    elif not d["above_ema20"]:
        s -= 1
        reasons.append("⚠ Below EMA20 — short-term downtrend. Needs RNS or strong volume to reverse")

    # Extra penalty if stock is deep in the lower half of its 52W range AND in downtrend
    if d["in_discount"] and not d["above_ema50"]:
        s -= 1
        reasons.append(
            f"⚠ In lower 35% of 52W range ({d['pos52']:.0f}%) AND below EMA50 — "
            f"structural downtrend, falling knife risk"
        )

    # ── 2. RNS Catalyst (0–4 pts) ─────────────────────────────────────────────
    has_positive_rns = False
    if rns:
        if rns.get("negative"):
            s -= 3
            reasons.append(f"⛔ NEGATIVE RNS: {rns.get('label', '')} — likely gap DOWN")
            reasons.append(f"   Headline: \"{rns.get('headline', '')[:90]}\"")
        else:
            has_positive_rns = True
            rns_score = rns.get("score", 0)
            s += rns_score
            reasons.append(
                f"{rns['emoji']} RNS TODAY: {rns['label']} "
                f"(expected: {rns['expected_move']})"
            )
            reasons.append(f"   \"{rns.get('headline', '')[:90]}\"")
            em = rns.get("expected_move", "")
            nums = re.findall(r"\d+", em)
            if len(nums) >= 2:
                predicted_move_midpoint = (int(nums[0]) + int(nums[1])) / 2.0

    # ── 3. Bollinger Band Squeeze (0–2 pts) ───────────────────────────────────
    bb_signal = False
    if d["bb_squeeze_strong"]:
        s += 2
        bb_signal = True
        reasons.append(
            "🔥 STRONG BB Squeeze — volatility compressed to 10th percentile. "
            "Explosive move imminent (direction confirmed at open)"
        )
    elif d["bb_squeeze"]:
        s += 1
        bb_signal = True
        reasons.append(
            f"📐 BB Squeeze — volatility in bottom {BB_SQUEEZE_RANK}th percentile. "
            "Breakout likely within 1–3 sessions"
        )

    # ── 4. Volume Accumulation (0–2 pts) ──────────────────────────────────────
    vol_signal = False
    if d["vol_rising"] and d["vol_above_avg"]:
        s += 2
        vol_signal = True
        reasons.append(
            f"🏦 Volume accumulation: {VOL_BUILDUP_DAYS} consecutive days rising × "
            f"{d['vol_ratio']:.1f}× avg — institutional buying signal"
        )
    elif d["vol_rising"]:
        s += 1
        vol_signal = True
        reasons.append(f"📊 Volume building: {VOL_BUILDUP_DAYS} consecutive days of rising volume")
    elif d["vol_above_avg"]:
        s += 1
        vol_signal = True
        reasons.append(f"📊 Volume elevated: {d['vol_ratio']:.1f}× 20-day average")

    # ── 5. Technical breakout position (0–2 pts, AIM-aware) ───────────────────
    pos_signal = False
    if d["at_52w_high"] and not d["distribution_risk"]:
        s += 2
        pos_signal = True
        reasons.append(
            f"🚀 AT 52W HIGH ({d['pos52']:.0f}%) — momentum, no overhead resistance"
        )
    elif d["near_52w_high"] and not d["distribution_risk"]:
        s += 2
        pos_signal = True
        reasons.append(
            f"📈 Near 52W high ({d['pos52']:.0f}%, {d['dist_hi_pct']:.1f}% from top) "
            f"— approaching breakout territory"
        )
    elif d["at_52w_high"] and d["distribution_risk"]:
        # On AIM, at 52W high with distribution risk = neutral, not bullish
        reasons.append(
            f"⚠ AT 52W HIGH ({d['pos52']:.0f}%) — but AIM distribution risk: "
            "check L2 for large sellers before entering"
        )
    elif d["ema_aligned"]:
        s += 1
        pos_signal = True
        reasons.append("✅ EMA stack: Close > EMA9 > EMA20 > EMA50 — clean uptrend alignment")
    elif d["ema_uptrend"]:
        s += 1
        pos_signal = True
        reasons.append("✅ EMA20 > EMA50 — medium-term bullish structure")

    # ── 6. Compression patterns (0–2 pts) ─────────────────────────────────────
    if d["inside_day"] and d["range_compression"]:
        s += 2
        reasons.append(
            "🗜️  Inside day + range compression — price wound tight vs 20-day average. "
            "Classic pre-breakout coil"
        )
    elif d["inside_day"]:
        s += 1
        reasons.append(
            "🗜️  Inside day — yesterday's range sits inside prior session's range. "
            "Compression signal"
        )
    elif d["range_compression"]:
        s += 1
        reasons.append(
            "🗜️  Range compression — recent daily ranges 30%+ tighter than 20-day average"
        )

    # Strong close adds context but no extra points (avoid double-counting with inside_day)
    if d["strong_close"]:
        reasons.append(
            f"✅ Strong close: yesterday closed in top {int((1 - d['close_position']) * 100)}% "
            "of session range — buyers controlled end of day"
        )

    # ── 7. RSI filter — avoid overbought / note oversold ─────────────────────
    rsi = d["rsi14"]
    if rsi > 75:
        s -= 1
        reasons.append(
            f"⚠ RSI {rsi:.0f} — overbought. Risk of short-term pullback before continuation"
        )
    elif rsi < 30:
        reasons.append(
            f"📉 RSI {rsi:.0f} — oversold territory. May bounce but needs catalyst"
        )
    else:
        reasons.append(f"RSI {rsi:.0f} — neutral momentum")

    # ── 8. Volatility capacity (0–1 pt) ───────────────────────────────────────
    if d["atr_pct"] >= 6.0:
        s += 1
        reasons.append(
            f"✅ ATR {d['atr_pct']:.1f}% — highly volatile, capable of 10–20%+ daily moves"
        )
    elif d["atr_pct"] >= MIN_ATR_PCT:
        reasons.append(
            f"ATR {d['atr_pct']:.1f}% — moderate volatility, 5–10% moves achievable"
        )

    # ── 9. Market cap context (0–1 pt) ────────────────────────────────────────
    mc = d["mktcap_gbp"]
    if mc is not None and mc < 30e6:
        s += 1
        reasons.append(f"✅ Micro-cap £{mc/1e6:.1f}m — small float, explosive when volume hits")
    elif mc is not None and mc < 100e6:
        reasons.append(f"Small-cap £{mc/1e6:.0f}m — manageable size for sharp moves")
    elif mc is None:
        reasons.append("⚠ Market cap unknown — verify before trading")

    # ── 10. Liquidity & spread information ────────────────────────────────────
    adgbp  = d["avg_daily_gbp"]
    spread = d["est_spread_pct"]
    if adgbp >= 200_000:
        reasons.append(f"✅ Liquid: £{adgbp/1e3:.0f}k/day — tradeable with limit orders")
    elif adgbp >= 50_000:
        reasons.append(f"⚠ Moderate liquidity: £{adgbp/1e3:.0f}k/day — use limits, not market orders")
    else:
        reasons.append(
            f"🚫 ILLIQUID: £{adgbp/1e3:.0f}k/day avg. "
            f"Estimated spread {spread:.1f}% — extremely difficult to trade without slippage"
        )
    if spread >= 2.5:
        reasons.append(
            f"⚠ Wide spread ~{spread:.1f}%: need {spread * 2:.1f}%+ move just to cover entry/exit costs"
        )

    # ── 11. BUG11 FIX: minimum one quality signal gate ────────────────────────
    # If NO genuine signal (no RNS, no BB squeeze, no volume, no 52W proximity),
    # cap the score at MIN_PRED_SCORE - 1 so the stock cannot pass the filter
    has_quality_signal = has_positive_rns or bb_signal or vol_signal or pos_signal
    s_capped = max(-8, min(s, 16))
    if not has_quality_signal:
        s_capped = min(s_capped, MIN_PRED_SCORE - 1)
        reasons.append(
            "⚠ No primary signal (no RNS, no BB squeeze, no volume buildup, "
            "not near 52W high) — score capped, not recommended"
        )

    # ── Confidence label + predicted move ─────────────────────────────────────
    if rns and not rns.get("negative"):
        predicted_move = rns.get("expected_move", "5–15%")
        if   s_capped >= 10: confidence = "VERY HIGH"
        elif s_capped >= 7:  confidence = "HIGH"
        elif s_capped >= 5:  confidence = "MEDIUM"
        else:                confidence = "LOW"
    else:
        if   s_capped >= 9:  confidence, predicted_move = "HIGH (Technical)",   "5–15%"
        elif s_capped >= 6:  confidence, predicted_move = "MEDIUM (Technical)", "3–10%"
        elif s_capped >= 4:  confidence, predicted_move = "LOW (Technical)",    "2–5%"
        else:                confidence, predicted_move = "SPECULATIVE",        "unknown"

    # ── R/R ratio ─────────────────────────────────────────────────────────────
    rr_ratio = None
    if predicted_move_midpoint and d["rec_stop_pct"] > 0:
        rr_ratio = round(predicted_move_midpoint / d["rec_stop_pct"], 1)

    return s_capped, reasons, confidence, predicted_move, rr_ratio


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4: Build HTML Report
# ─────────────────────────────────────────────────────────────────────────────
def build_html(results: list[dict], rns_map: dict, scan_time: str, date_str: str) -> str:

    def score_colour(s):
        if s >= 10: return "#00e676"
        if s >= 7:  return "#f5a623"
        if s >= 4:  return "#29b6f6"
        return "#546e7a"

    def conf_colour(c):
        if "VERY HIGH" in c: return "#00e676"
        if "HIGH"      in c: return "#f5a623"
        if "MEDIUM"    in c: return "#29b6f6"
        return "#546e7a"

    def rr_colour(rr):
        if rr is None:  return "#546e7a"
        if rr >= 3.0:   return "#00e676"
        if rr >= 2.0:   return "#f5a623"
        return "#ff3d57"

    def fmt_p(p, ccy):
        if ccy == "GBp": return f"{p:.2f}p"
        return f"£{p:.4f}" if p < 1 else f"£{p:.2f}"

    def fmt_mc(mc):
        if mc is None:  return "N/A"
        if mc >= 1e9:   return f"£{mc/1e9:.1f}B"
        if mc >= 1e6:   return f"£{mc/1e6:.0f}M"
        return f"£{mc/1e3:.0f}K"

    def fmt_gbp(v):
        if v >= 1e6:   return f"£{v/1e6:.1f}M"
        if v >= 1000:  return f"£{v/1e3:.0f}K"
        return f"£{v:.0f}"

    results.sort(key=lambda x: (-x["score"], -(x.get("rr_ratio") or 0), -x["data"]["atr_pct"]))

    rns_count  = sum(1 for r in results if r.get("has_rns") and not (rns_map.get(r["data"]["symbol"]) or {}).get("negative"))
    tech_count = len(results) - rns_count
    top_count  = sum(1 for r in results if r["score"] >= 8)
    good_rr    = sum(1 for r in results if (r.get("rr_ratio") or 0) >= 2)

    now_l = datetime.now(LONDON_TZ)
    hm = now_l.hour * 60 + now_l.minute
    if   hm < 480: sess_label, sess_col = "PRE-MARKET — OPTIMAL TIME", "#00e676"
    elif hm < 510: sess_label, sess_col = "AUCTION / OPEN",            "#f5a623"
    elif hm < 810: sess_label, sess_col = "SESSION OPEN",              "#29b6f6"
    elif hm < 930: sess_label, sess_col = "US OVERLAP",                "#f5a623"
    else:          sess_label, sess_col = "MARKET CLOSED",             "#546e7a"

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

        sym = d["symbol"].replace(".L", "")
        sc_col   = score_colour(sc)
        conf_col = conf_colour(conf)
        rr_col   = rr_colour(rr)

        vol_col   = "#00e676" if d["vol_ratio"] >= 3 else "#f5a623" if d["vol_ratio"] >= 1.5 else "#546e7a"
        pos52_col = "#00e676" if d["pos52"] >= 90   else "#f5a623" if d["pos52"] >= 55    else "#546e7a" if d["pos52"] >= 35 else "#ff3d57"
        spread_col= "#ff3d57" if d["est_spread_pct"] >= 4 else "#f5a623" if d["est_spread_pct"] >= 2 else "#00e676"
        liq_col   = "#00e676" if d["avg_daily_gbp"] >= 200_000 else "#f5a623" if d["avg_daily_gbp"] >= 50_000 else "#ff3d57"
        rsi_col   = "#ff3d57" if d["rsi14"] > 70 else "#f5a623" if d["rsi14"] < 35 else "#29b6f6"
        trend_col = "#00e676" if d["ema_aligned"] else "#f5a623" if d["ema_uptrend"] else "#ff3d57"

        reasons_html = "".join(f'<div class="reason">{rr2}</div>' for rr2 in reasons)

        tv_url   = f"https://www.tradingview.com/chart/?symbol=LSE%3A{sym}"
        rns_url  = f"https://www.londonstockexchange.com/news?tab=news-explorer&search={sym}"
        adv_url  = f"https://www.advfn.com/stock-market/LSE/{sym}/share-price"
        yhoo_url = f"https://finance.yahoo.com/quote/{d['symbol']}"

        rns_badge = ""
        if has_rns and rns_info and not rns_info.get("negative"):
            rns_badge = f'<div class="badge rns-badge">{rns_info["emoji"]} RNS TODAY: {rns_info["label"]}</div>'
        elif has_rns and rns_info and rns_info.get("negative"):
            rns_badge = '<div class="badge neg-badge">⛔ NEGATIVE RNS</div>'

        squeeze_badge = ""
        if d["bb_squeeze_strong"]: squeeze_badge = '<div class="badge sqz-badge">🔥 STRONG SQUEEZE</div>'
        elif d["bb_squeeze"]:      squeeze_badge = '<div class="badge sqz-dim">📐 BB SQUEEZE</div>'

        illiq_badge = '<div class="badge illiq-badge">⚠ LOW LIQUIDITY</div>' if d["avg_daily_gbp"] < 50_000 else ""
        dt_badge    = '<div class="badge dt-badge">📉 DOWNTREND</div>' if not d["above_ema20"] else ""
        dist_badge  = '<div class="badge dist-badge">⚠ DIST ZONE</div>' if d["distribution_risk"] else ""

        rr_display = f"{rr:.1f}:1" if rr is not None else "N/A"
        pos_sz     = f"{d['max_shares']:,} shares ≈ {fmt_gbp(d['position_gbp'])}" if d["max_shares"] > 0 else "N/A"

        # Trend label
        if d["ema_aligned"]:  trend_label = "ALIGNED ↑"
        elif d["ema_uptrend"]: trend_label = "UPTREND"
        elif d["above_ema20"]: trend_label = "MIXED"
        else:                   trend_label = "DOWNTREND"

        rows_html += f"""
<div class="card {'rns-card' if has_rns and not (rns_info or {}).get('negative') else ''}
                  {'neg-card' if has_rns and (rns_info or {}).get('negative') else ''}
                  {'top-card' if sc >= 8 else ''}"
     data-score="{sc}" data-rr="{rr or 0}"
     data-rns="{'1' if has_rns and not (rns_info or {}).get('negative') else '0'}"
     data-squeeze="{'1' if d['bb_squeeze'] else '0'}"
     data-liquid="{'1' if d['avg_daily_gbp'] >= 50_000 else '0'}"
     data-volbuild="{'1' if d['vol_rising'] else '0'}">

  <div class="card-badges">{rns_badge}{squeeze_badge}{illiq_badge}{dt_badge}{dist_badge}</div>

  <div class="card-top">
    <div class="card-left">
      <div class="sym">{sym} <span class="exch">{'AIM' if d['is_aim'] else 'LSE'}</span></div>
      <div class="company">{d['name']}</div>
      <div class="sector-line">{d['sector']}{'  ·  ' + d['industry'] if d['industry'] else ''}</div>
      <div class="price-line">{fmt_p(d['close'], d['currency'])}
        <span class="chg {'up' if d['pct_change'] >= 0 else 'dn'}">{d['pct_change']:+.1f}% prev</span>
      </div>
      <div class="meta-line">{fmt_mc(d['mktcap_gbp'])} &nbsp;|&nbsp; £{d['avg_daily_gbp']/1e3:.0f}k/day liq</div>
    </div>

    <div class="card-mid">
      <div class="conf-label" style="color:{conf_col}">{conf}</div>
      <div class="pred-lbl">Predicted:</div>
      <div class="pred-val" style="color:{conf_col}">{pred_move}</div>
      <div class="rr-block">
        <span class="rr-lbl">R/R</span>
        <span class="rr-val" style="color:{rr_col}">{rr_display}</span>
      </div>
    </div>

    <div class="score-col">
      <div class="score-num" style="color:{sc_col}">{sc}</div>
      <div class="score-denom">/ 16</div>
      <div class="score-bar-wrap">
        <div class="score-bar" style="width:{max(0,sc)/16*100:.0f}%;background:{sc_col}"></div>
      </div>
    </div>
  </div>

  <!-- STOP LOSS BOX -->
  <div class="stop-box">
    <div class="stop-title">🛑 STOP LOSS LEVELS <span class="stop-warn">(EOD prices — adjust to live L2 at open)</span></div>
    <div class="stop-grid">
      <div class="stop-item recommended">
        <div class="stop-lbl">RECOMMENDED ({d['rec_stop_method']})</div>
        <div class="stop-price">{fmt_p(d['rec_stop_price'], d['currency'])}</div>
        <div class="stop-pct">−{d['rec_stop_pct']:.1f}% from last close</div>
      </div>
      <div class="stop-item">
        <div class="stop-lbl">TIGHT (ATR×1.0)</div>
        <div class="stop-price">{fmt_p(d['atr_tight_price'], d['currency'])}</div>
        <div class="stop-pct">−{d['atr_tight_pct']:.1f}%</div>
      </div>
      <div class="stop-item">
        <div class="stop-lbl">SWING LOW ({SWING_LOW_LOOKBACK}d)</div>
        <div class="stop-price">{fmt_p(d['swing_low_price'], d['currency'])}</div>
        <div class="stop-pct">−{d['swing_low_pct']:.1f}%</div>
      </div>
      <div class="stop-item">
        <div class="stop-lbl">WIDE (ATR×{ATR_STOP_MULT})</div>
        <div class="stop-price">{fmt_p(d['atr_stop_price'], d['currency'])}</div>
        <div class="stop-pct">−{d['atr_stop_pct']:.1f}%</div>
      </div>
    </div>
    <div class="pos-row">
      <span class="ps-lbl">£{EXAMPLE_ACCOUNT_GBP:,} acct @ {ACCOUNT_RISK_PCT}% risk →</span>
      <span class="ps-val">{pos_sz}</span>
      <span class="ps-spread">Spread est: <span style="color:{spread_col}">{d['est_spread_pct']:.1f}%</span></span>
    </div>
  </div>

  <!-- METRICS ROW -->
  <div class="metrics-row">
    <div class="metric">
      <span class="ml">VOL RATIO</span>
      <span class="mv" style="color:{vol_col}">{d['vol_ratio']:.1f}×</span>
    </div>
    <div class="metric">
      <span class="ml">ATR%</span>
      <span class="mv">{d['atr_pct']:.1f}%</span>
    </div>
    <div class="metric">
      <span class="ml">RSI(14)</span>
      <span class="mv" style="color:{rsi_col}">{d['rsi14']:.0f}</span>
    </div>
    <div class="metric">
      <span class="ml">52W POS</span>
      <span class="mv" style="color:{pos52_col}">{d['pos52']:.0f}%</span>
    </div>
    <div class="metric">
      <span class="ml">BB SQZ</span>
      <span class="mv" style="color:{'#00e676' if d['bb_squeeze'] else '#546e7a'}">
        {'STRONG' if d['bb_squeeze_strong'] else 'YES' if d['bb_squeeze'] else 'NO'}
      </span>
    </div>
    <div class="metric">
      <span class="ml">VOL BLD</span>
      <span class="mv" style="color:{'#00e676' if d['vol_rising'] else '#546e7a'}">
        {'YES' if d['vol_rising'] else 'NO'}
      </span>
    </div>
    <div class="metric">
      <span class="ml">TREND</span>
      <span class="mv" style="color:{trend_col}">{trend_label}</span>
    </div>
    <div class="metric">
      <span class="ml">LIQUIDITY</span>
      <span class="mv" style="color:{liq_col}">{fmt_gbp(d['avg_daily_gbp'])}</span>
    </div>
  </div>

  <div class="reasons">{reasons_html}</div>

  <div class="checklist">
    <div class="cl-title">📋 PRE-TRADE CHECKLIST</div>
    <div class="cl-items">
      <span>1. Read full RNS on LSE</span>
      <span>2. Check L2 spread at 07:55</span>
      <span>3. Confirm volume spike in first 3 mins of open</span>
      <span>4. Set stop BEFORE entering</span>
      <span>5. Check for placing / dilution in RNS</span>
    </div>
  </div>

  <div class="links">
    <a href="{tv_url}"   target="_blank" class="lb chart-lb">📈 Chart</a>
    <a href="{rns_url}"  target="_blank" class="lb rns-lb">📰 RNS</a>
    <a href="{adv_url}"  target="_blank" class="lb adv-lb">📊 ADVFN</a>
    <a href="{yhoo_url}" target="_blank" class="lb yh-lb">💹 Yahoo</a>
  </div>
</div>"""

    total  = len(results)
    avg_sc = sum(r["score"] for r in results) / total if total else 0

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LSE Pre-Market v3 — {date_str}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600&family=Barlow+Condensed:wght@600;700&display=swap');
:root{{
  --bg:#080c10;--bg2:#0d1318;--bg3:#111920;--border:#1c2e3a;
  --amber:#f5a623;--green:#00e676;--red:#ff3d57;--blue:#29b6f6;
  --purple:#ce93d8;--dim:#4a6478;--text:#c8d8e4;
  --mono:'Share Tech Mono',monospace;--sans:'Barlow',sans-serif;--cond:'Barlow Condensed',sans-serif;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);
  background-image:radial-gradient(ellipse at 15% 0%,rgba(0,230,118,.04) 0%,transparent 50%),
  radial-gradient(ellipse at 85% 100%,rgba(41,182,246,.03) 0%,transparent 50%);}}
.header{{padding:14px 28px;border-bottom:1px solid var(--border);background:rgba(13,19,24,.98);
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50;}}
.logo{{font-family:var(--mono);font-size:18px;color:var(--green);letter-spacing:2px}}
.logo sub{{font-size:10px;color:var(--amber);letter-spacing:1px}}
.v3{{font-family:var(--cond);font-size:10px;color:var(--blue);border:1px solid var(--blue);
  padding:2px 6px;border-radius:2px;margin-left:8px;vertical-align:middle}}
.hdr-r{{text-align:right;font-family:var(--mono);font-size:11px;color:var(--dim);line-height:1.9}}
.sess{{font-size:11px;font-weight:bold;letter-spacing:1px}}
.how-bar{{background:rgba(0,230,118,.04);border-bottom:1px solid rgba(0,230,118,.1);
  padding:10px 28px;font-size:11px;color:var(--dim);font-family:var(--mono);
  line-height:2;display:flex;gap:30px;flex-wrap:wrap}}
.how-n{{color:var(--green);font-weight:bold}}
.stats-bar{{display:flex;border-bottom:1px solid var(--border);background:var(--bg2)}}
.stat{{flex:1;text-align:center;padding:12px 8px;border-right:1px solid var(--border)}}
.stat:last-child{{border-right:none}}
.stat-v{{font-family:var(--mono);font-size:22px;color:var(--amber);display:block}}
.stat-l{{font-family:var(--cond);font-size:10px;color:var(--dim);letter-spacing:1.5px;text-transform:uppercase}}
.filter-bar{{padding:10px 28px;background:var(--bg2);border-bottom:1px solid var(--border);
  display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.fl{{font-family:var(--cond);font-size:11px;color:var(--dim);letter-spacing:1px}}
.fb{{font-family:var(--cond);font-size:11px;font-weight:700;letter-spacing:1px;
  padding:5px 12px;border:1px solid var(--border);border-radius:2px;
  background:var(--bg3);color:var(--dim);cursor:pointer;transition:all .15s}}
.fb:hover,.fb.active{{border-color:var(--green);color:var(--green);background:rgba(0,230,118,.07)}}
.grid{{padding:18px 28px;display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:16px}}
.card{{background:var(--bg2);border:1px solid var(--border);border-radius:4px;
  overflow:hidden;transition:border-color .2s,transform .15s}}
.card:hover{{border-color:rgba(41,182,246,.35);transform:translateY(-1px)}}
.top-card{{border-color:rgba(245,166,35,.3)}}
.top-card:hover{{border-color:rgba(245,166,35,.6)}}
.rns-card{{border-color:rgba(0,230,118,.35)}}
.rns-card:hover{{border-color:rgba(0,230,118,.7)}}
.neg-card{{border-color:rgba(255,61,87,.25);opacity:.65}}
.card-badges{{display:flex;gap:6px;padding:8px 14px 0;flex-wrap:wrap}}
.badge{{font-family:var(--cond);font-size:11px;font-weight:700;letter-spacing:1px;
  padding:3px 10px;border-radius:2px;border:1px solid}}
.rns-badge{{background:rgba(0,230,118,.12);border-color:rgba(0,230,118,.3);color:var(--green)}}
.neg-badge{{background:rgba(255,61,87,.1);border-color:rgba(255,61,87,.3);color:var(--red)}}
.sqz-badge{{background:rgba(245,166,35,.1);border-color:rgba(245,166,35,.3);color:var(--amber)}}
.sqz-dim{{background:rgba(41,182,246,.07);border-color:rgba(41,182,246,.25);color:var(--blue)}}
.illiq-badge{{background:rgba(255,61,87,.08);border-color:rgba(255,61,87,.25);color:var(--red)}}
.dt-badge{{background:rgba(255,61,87,.08);border-color:rgba(255,61,87,.2);color:#ff7094}}
.dist-badge{{background:rgba(206,147,216,.08);border-color:rgba(206,147,216,.25);color:var(--purple)}}
.card-top{{display:flex;padding:12px 14px 10px;gap:12px;align-items:flex-start}}
.card-left{{flex:1}}
.sym{{font-family:var(--mono);font-size:20px;color:var(--amber);letter-spacing:1.5px;
  display:flex;align-items:center;gap:8px}}
.exch{{font-family:var(--cond);font-size:9px;color:var(--dim);border:1px solid var(--border);
  padding:1px 5px;border-radius:2px;letter-spacing:1px}}
.company{{font-size:12px;color:var(--dim);margin-top:3px}}
.sector-line{{font-size:10px;color:rgba(74,100,120,.6);margin-top:2px}}
.price-line{{font-family:var(--mono);font-size:13px;color:var(--text);margin-top:6px}}
.chg{{font-size:11px;margin-left:6px}}
.up{{color:var(--green)}}.dn{{color:var(--red)}}
.meta-line{{font-size:10px;color:var(--dim);margin-top:4px;font-family:var(--mono)}}
.card-mid{{text-align:center;min-width:115px}}
.conf-label{{font-family:var(--cond);font-size:13px;font-weight:700;letter-spacing:1px}}
.pred-lbl{{font-size:9px;color:var(--dim);font-family:var(--cond);letter-spacing:1px;
  margin-top:6px;text-transform:uppercase}}
.pred-val{{font-family:var(--mono);font-size:17px;font-weight:bold;margin-top:2px}}
.rr-block{{margin-top:8px}}
.rr-lbl{{font-family:var(--cond);font-size:9px;color:var(--dim);letter-spacing:1px;display:block}}
.rr-val{{font-family:var(--mono);font-size:18px;font-weight:bold}}
.score-col{{text-align:center;min-width:52px}}
.score-num{{font-family:var(--mono);font-size:26px;font-weight:bold;line-height:1}}
.score-denom{{font-size:9px;color:var(--dim);font-family:var(--mono)}}
.score-bar-wrap{{width:42px;height:3px;background:var(--bg3);border-radius:2px;margin:4px auto 0;overflow:hidden}}
.score-bar{{height:100%;border-radius:2px}}
.stop-box{{margin:0 14px;padding:10px 12px 8px;
  background:rgba(255,61,87,.04);border:1px solid rgba(255,61,87,.2);border-radius:3px}}
.stop-title{{font-family:var(--cond);font-size:12px;font-weight:700;letter-spacing:1px;
  color:#ff7094;margin-bottom:8px;display:flex;align-items:center;gap:10px}}
.stop-warn{{font-size:9px;color:var(--dim);font-weight:400;letter-spacing:0}}
.stop-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:8px}}
.stop-item{{background:var(--bg3);border:1px solid var(--border);border-radius:3px;
  padding:6px 8px;text-align:center}}
.stop-item.recommended{{background:rgba(255,61,87,.08);border-color:rgba(255,61,87,.35)}}
.stop-lbl{{font-family:var(--cond);font-size:9px;color:var(--dim);letter-spacing:.5px;display:block;margin-bottom:3px}}
.stop-price{{font-family:var(--mono);font-size:13px;color:#ff7094;font-weight:bold}}
.stop-pct{{font-family:var(--mono);font-size:10px;color:var(--red);margin-top:2px}}
.stop-item.recommended .stop-price{{font-size:15px;color:var(--red)}}
.pos-row{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
  padding-top:6px;border-top:1px solid rgba(255,61,87,.1);
  font-family:var(--mono);font-size:10px;color:var(--dim)}}
.ps-val{{color:var(--text);font-weight:bold}}
.ps-spread{{margin-left:auto}}
.metrics-row{{display:flex;flex-wrap:wrap;border-top:1px solid var(--border);
  border-bottom:1px solid var(--border);background:var(--bg3);margin-top:10px}}
.metric{{flex:1;min-width:60px;padding:6px 5px;text-align:center;border-right:1px solid var(--border)}}
.metric:last-child{{border-right:none}}
.ml{{display:block;font-family:var(--cond);font-size:9px;color:var(--dim);
  letter-spacing:1px;text-transform:uppercase}}
.mv{{display:block;font-family:var(--mono);font-size:11px;color:var(--text);margin-top:2px}}
.reasons{{padding:10px 14px}}
.reason{{font-size:11px;color:var(--dim);padding:2px 0;line-height:1.6}}
.checklist{{margin:6px 14px 8px;padding:8px 12px;
  background:rgba(41,182,246,.04);border:1px solid rgba(41,182,246,.15);border-radius:3px}}
.cl-title{{font-family:var(--cond);font-size:11px;color:var(--blue);font-weight:700;
  letter-spacing:1px;margin-bottom:5px}}
.cl-items{{display:flex;flex-wrap:wrap;gap:8px;font-size:10px;color:var(--dim);font-family:var(--mono)}}
.links{{padding:8px 14px 12px;display:flex;gap:7px;flex-wrap:wrap;
  border-top:1px solid rgba(28,46,58,.4)}}
.lb{{font-family:var(--cond);font-size:11px;font-weight:700;letter-spacing:1px;
  padding:4px 11px;border-radius:2px;text-decoration:none;border:1px solid;transition:all .15s}}
.chart-lb{{color:var(--blue);border-color:rgba(41,182,246,.3);background:rgba(41,182,246,.07)}}
.chart-lb:hover{{background:rgba(41,182,246,.15)}}
.rns-lb{{color:var(--amber);border-color:rgba(245,166,35,.3);background:rgba(245,166,35,.07)}}
.rns-lb:hover{{background:rgba(245,166,35,.15)}}
.adv-lb{{color:var(--purple);border-color:rgba(206,147,216,.3);background:rgba(206,147,216,.07)}}
.adv-lb:hover{{background:rgba(206,147,216,.15)}}
.yh-lb{{color:var(--green);border-color:rgba(0,230,118,.3);background:rgba(0,230,118,.07)}}
.yh-lb:hover{{background:rgba(0,230,118,.15)}}
.empty{{text-align:center;padding:60px 20px;color:var(--dim);font-family:var(--mono);
  font-size:13px;line-height:2}}
.footer{{margin-top:40px;padding:18px 28px;border-top:1px solid var(--border);
  font-size:11px;color:var(--dim);font-family:var(--mono);line-height:2.2;background:var(--bg2)}}
.hidden{{display:none!important}}
::-webkit-scrollbar{{width:4px}}::-webkit-scrollbar-thumb{{background:var(--border)}}
@media(max-width:540px){{.grid{{padding:10px;grid-template-columns:1fr}}
  .stop-grid{{grid-template-columns:repeat(2,1fr)}}}}
</style>
</head>
<body>

<div class="header">
  <div class="logo">
    LSE·PREDICT <span class="v3">v3.0</span><br>
    <sub>AIM SCANNER · STOP LOSS ENGINE · 11 BUGS FIXED</sub>
  </div>
  <div class="hdr-r">
    <div>{date_str} &nbsp;|&nbsp; Scanned {scan_time} London</div>
    <div class="sess" style="color:{sess_col}">● {sess_label}</div>
  </div>
</div>

<div class="how-bar">
  <div><span class="how-n">①</span> RNS catalyst (RSS→JSON fallback)</div>
  <div><span class="how-n">②</span> BB squeeze: volatility coiled</div>
  <div><span class="how-n">③</span> Volume accumulation: smart money</div>
  <div><span class="how-n">④</span> RSI filter: avoids overbought</div>
  <div><span class="how-n">⑤</span> Trend gate: downtrends penalised</div>
  <div><span class="how-n">⑥</span> Stop loss + R/R per ticker</div>
  <div><span class="how-n">⑦</span> Liq gate: £{MIN_AVG_DAILY_GBP/1e3:.0f}k+/day required</div>
</div>

<div class="stats-bar">
  <div class="stat"><span class="stat-v">{total}</span><span class="stat-l">Candidates</span></div>
  <div class="stat"><span class="stat-v" style="color:var(--green)">{rns_count}</span><span class="stat-l">Positive RNS</span></div>
  <div class="stat"><span class="stat-v" style="color:var(--amber)">{top_count}</span><span class="stat-l">Score ≥ 8/16</span></div>
  <div class="stat"><span class="stat-v" style="color:var(--green)">{good_rr}</span><span class="stat-l">R/R ≥ 2:1</span></div>
  <div class="stat"><span class="stat-v">{avg_sc:.1f}</span><span class="stat-l">Avg Score</span></div>
</div>

<div class="filter-bar">
  <span class="fl">FILTER ›</span>
  <button class="fb active" onclick="fc('all',this)">ALL ({total})</button>
  <button class="fb" onclick="fc('rns',this)">HAS RNS</button>
  <button class="fb" onclick="fc('squeeze',this)">BB SQUEEZE</button>
  <button class="fb" onclick="fc('top',this)">SCORE ≥ 8</button>
  <button class="fb" onclick="fc('goodrr',this)">R/R ≥ 2:1</button>
  <button class="fb" onclick="fc('liquid',this)">LIQUID ≥ £50K</button>
  <button class="fb" onclick="fc('volbuild',this)">VOL BUILD</button>
</div>

<div class="grid" id="grid">
  {rows_html or '<div class="empty">No candidates found.<br>Try running 06:30–07:55 London.<br>Lower MIN_PRED_SCORE in CONFIG if needed.</div>'}
</div>

<div class="footer">
  ⚠  DISCLAIMERS (v3.0 — 11 bugs fixed vs v2.0):<br>
  · All prices are prior-day EOD from Yahoo Finance. Not real-time.<br>
  · Stop loss levels are estimates. AIM stocks gap — stops may fill far below your target.<br>
  · Check the live Level 2 at 07:55 before using any stop price.<br>
  · Spread estimates are heuristic. Real AIM spreads can be 2–8% at open.<br>
  · Position sizing is illustrative for a £{EXAMPLE_ACCOUNT_GBP:,} account at {ACCOUNT_RISK_PCT}% risk — adjust to your own size.<br>
  · R/R uses the midpoint of predicted move vs recommended stop — directional guide only.<br>
  · NOT FINANCIAL ADVICE. AIM stocks can and do go to zero.
</div>

<script>
function fc(type,btn){{
  document.querySelectorAll('.fb').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(card=>{{
    const score   = parseInt(card.dataset.score||0);
    const rr      = parseFloat(card.dataset.rr||0);
    const rns     = card.dataset.rns==='1';
    const squeeze = card.dataset.squeeze==='1';
    const liquid  = card.dataset.liquid==='1';
    const volbld  = card.dataset.volbuild==='1';
    let show=true;
    if(type==='rns')     show=rns;
    if(type==='squeeze') show=squeeze;
    if(type==='top')     show=score>=8;
    if(type==='goodrr')  show=rr>=2;
    if(type==='liquid')  show=liquid;
    if(type==='volbuild')show=volbld;
    card.classList.toggle('hidden',!show);
  }});
}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    now_l      = datetime.now(LONDON_TZ)
    date_str   = now_l.strftime("%A %d %B %Y")
    scan_time  = now_l.strftime("%H:%M:%S")
    file_date  = now_l.strftime("%Y-%m-%d")

    print("═" * 66)
    print("  LSE PRE-MARKET SCANNER v3.0 — 11 bugs fixed")
    print(f"  {date_str}  |  {scan_time} London")
    print("═" * 66)

    if now_l.hour >= 16 or now_l.hour < 6:
        print("\n  ⚠  Best run 06:30–07:55 London. RNS from 07:00.")

    # 1. RNS
    print("\n[1/4] Fetching RNS (RSS → JSON fallback)...")
    rns_map     = fetch_rns_today()
    rns_symbols = set(rns_map.keys())
    print(f"  ✓ {len(rns_map)} RNS tickers")
    if rns_map:
        sample = ", ".join(s.replace(".L","") for s in list(rns_map)[:15])
        print(f"  Found: {sample}{'...' if len(rns_map)>15 else ''}")

    # 2. Symbol list
    print("\n[2/4] Building symbol list...")
    all_syms = list(dict.fromkeys(list(rns_symbols) + WATCHLIST_YAHOO))
    print(f"  ✓ {len(all_syms)} symbols ({len(rns_symbols)} RNS + {len(WATCHLIST_YAHOO)} watchlist)")

    # 3. Download (1-year history now)
    print(f"\n[3/4] Downloading 1-year history ({MAX_WORKERS} parallel workers)...")
    all_data = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fmap = {ex.submit(fetch_ticker_data, s): s for s in all_syms}
        for fut in concurrent.futures.as_completed(fmap):
            done += 1
            res = fut.result()
            if res is not None:
                all_data.append(res)
            if done % 20 == 0 or done == len(all_syms):
                pct = done / len(all_syms) * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(f"  [{bar}] {done}/{len(all_syms)} ({len(all_data)} valid)", end="\r")
    print(f"\n  ✓ Valid data: {len(all_data)} symbols")

    # 4. Score + filter
    print("\n[4/4] Scoring (with trend-direction gate + minimum signal gate)...")
    results        = []
    filtered_illiq = 0
    filtered_trend = 0

    for d in all_data:
        sym = d["symbol"]

        # Liquidity gate
        if d["avg_daily_gbp"] < MIN_AVG_DAILY_GBP and sym not in rns_symbols:
            filtered_illiq += 1
            continue

        # ATR gate
        if d["atr_pct"] < MIN_ATR_PCT and sym not in rns_symbols:
            continue

        # Market cap gate
        mc = d["mktcap_gbp"]
        if mc is not None and mc > MAX_MKTCAP_GBP and sym not in rns_symbols:
            continue

        rns_info = rns_map.get(sym)
        score, reasons, confidence, predicted_move, rr_ratio = score_predictive(d, rns_info)

        # Score threshold gate
        if score < MIN_PRED_SCORE and sym not in rns_symbols:
            continue

        results.append({
            "data":           d,
            "score":          score,
            "reasons":        reasons,
            "confidence":     confidence,
            "predicted_move": predicted_move,
            "rr_ratio":       rr_ratio,
            "has_rns":        sym in rns_symbols,
        })

    results.sort(key=lambda x: (-x["score"], -(x.get("rr_ratio") or 0), -x["data"]["atr_pct"]))
    print(f"  ✓ {len(results)} candidates passed all filters")
    print(f"  ✓ {filtered_illiq} removed (illiquid < £{MIN_AVG_DAILY_GBP/1e3:.0f}k/day)")

    if results:
        print("\n  ┌── TOP CANDIDATES ─────────────────────────────────────────────┐")
        for r in results[:12]:
            d   = r["data"]
            mc  = f"£{d['mktcap_gbp']/1e6:.0f}M" if d["mktcap_gbp"] else "N/A  "
            rr  = f"R/R {r['rr_ratio']:.1f}:1" if r["rr_ratio"] else "R/R N/A  "
            sl  = f"SL −{d['rec_stop_pct']:.1f}%"
            fl  = " ◀ RNS" if r["has_rns"] else ""
            trn = "↑" if d["ema_aligned"] else "→" if d["ema_uptrend"] else "↓"
            print(f"  │ {d['symbol'].replace('.L',''):<8} "
                  f"{r['score']:>2}/16  ATR {d['atr_pct']:.1f}%  "
                  f"{sl:<9} {rr:<11} {mc:<7} {trn}{fl}")
        print("  └────────────────────────────────────────────────────────────────┘")

    # 5. HTML output
    print("\nGenerating HTML...")
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
        trn = "UPTREND ↑" if d["ema_aligned"] else "MIXED →" if d["ema_uptrend"] else "DOWNTREND ↓"
        print(f"  Best: {d['symbol'].replace('.L','')} | Score {top['score']}/16 | "
              f"{top['confidence']} | SL −{d['rec_stop_pct']:.1f}% | {rr} | {trn}")
        if top["has_rns"]:
            ri = rns_map.get(d["symbol"])
            if ri: print(f"  RNS: {ri.get('label','')} — {ri.get('headline','')[:65]}")
    else:
        print("  No candidates found. Try lowering MIN_PRED_SCORE in CONFIG.")
    print("═" * 66 + "\n")


if __name__ == "__main__":
    main()
