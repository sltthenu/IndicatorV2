#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  LSE PRE-MARKET PREDICTIVE SCANNER  v4.0  — FUNDAMENTALS RELEASE      ║
║  Finds AIM / small-cap stocks BEFORE they move 5–20%                  ║
║                                                                          ║
║  NEW IN v4.0 — Fundamental Health Layer:                                ║
║   ✓ Cash runway (quarters of cash remaining at current burn rate)       ║
║   ✓ Net cash / net debt position                                        ║
║   ✓ Current ratio (can the company pay near-term bills?)                ║
║   ✓ Revenue growth (is the business actually growing?)                  ║
║   ✓ Operating cashflow (self-funding vs cash-burning?)                  ║
║   ✓ Dilution risk score (debt/equity, CLN proxy detection)              ║
║   ✓ Serial diluter flag (repeated fundraises)                           ║
║   ✓ Hard block: <2 quarters cash + no Tier-1 RNS = excluded            ║
║   ✓ Fundamental Health badge (A/B/C/D/F rating) on every card          ║
║   ✓ Combined score = Technical + Fundamental (max 22 pts)               ║
║                                                                          ║
║  ALSO FIXED IN v4.0 (carried from v3.0):                               ║
║   ✓ Stop loss calculated from ENTRY price (not yesterday's close)       ║
║   ✓ Volume direction check (rising vol + rising price = accumulation)   ║
║   ✓ RNS feed status shown on every card (confirmed / not checked)       ║
║   ✓ All 11 bugs from v2.0 retained fixed                                ║
║                                                                          ║
║  INSTALL:                                                                ║
║    pip install yfinance requests pandas                                  ║
║                                                                          ║
║  RUN:                                                                    ║
║    python lse_predict_v4.py                                              ║
║                                                                          ║
║  OUTPUT: lse_predict_YYYY-MM-DD.html  (auto-opens in browser)          ║
╚══════════════════════════════════════════════════════════════════════════╝

  ⚠  NOT FINANCIAL ADVICE — For research and education only.
     AIM stocks carry extreme risk including total loss of capital.
     Fundamental data from Yahoo Finance may be stale (quarterly lag).
     Always verify accounts on Companies House / broker research.
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
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
MAX_WORKERS           = 12
MIN_COMBINED_SCORE    = 4       # minimum combined (tech + fundamental) score to show
MIN_ATR_PCT           = 2.0     # minimum ATR% — below this stock won't move 5%+
MAX_MKTCAP_GBP        = 600e6   # ignore large-caps
MIN_AVG_DAILY_GBP     = 30_000  # liquidity gate: £30k/day minimum
BB_SQUEEZE_RANK       = 25      # BB width percentile for squeeze signal
VOL_BUILDUP_DAYS      = 3       # consecutive rising-volume days needed
RNS_LOOKBACK_HRS      = 8       # hours back to scan RNS

# Stop-loss config (ALL defined before any function — v3 BUG10 fix retained)
ATR_STOP_MULT         = 1.5     # standard stop: entry − ATR×1.5
ATR_STOP_MULT_TIGHT   = 1.0     # tight stop: entry − ATR×1.0
SWING_LOW_LOOKBACK    = 10      # days for swing-low support
MAX_STOP_PCT          = 15.0    # maximum stop distance % (AIM can gap)

# Entry price assumptions for stop calculation (v4 NEW)
# For RNS stocks, price will gap at open — use a realistic entry estimate
RNS_GAP_ESTIMATE_PCT  = 5.0     # assume 5% gap-up for Tier-2 RNS at open
RNS_GAP_T1_PCT        = 12.0    # assume 12% gap-up for Tier-1 (takeover/clinical)
TECH_ENTRY_BUFFER_PCT = 0.5     # for technical setups, add 0.5% for spread/slippage

# Position sizing (illustrative only — NOT executed)
ACCOUNT_RISK_PCT      = 1.0     # risk 1% of account per trade
EXAMPLE_ACCOUNT_GBP   = 10_000  # example account size

# Fundamental thresholds
MIN_CASH_RUNWAY_QTR   = 2.0     # hard block below 2 quarters cash (unless Tier-1 RNS)
MIN_CURRENT_RATIO     = 1.0     # below this = short-term solvency risk
MAX_DEBT_EQUITY       = 1.5     # above this = high leverage / dilution risk
MIN_REVENUE_GROWTH    = -0.20   # below −20% YoY = serious revenue decline

LONDON_TZ = ZoneInfo("Europe/London")

# ─────────────────────────────────────────────────────────────────────────────
#  RNS TRIGGERS
# ─────────────────────────────────────────────────────────────────────────────
RNS_TRIGGERS = [
    # TIER 1 — 15–50% moves
    (["recommended offer","possible offer","firm offer","takeover",
      "offer for the entire","acquire the entire","all cash offer",
      "scheme of arrangement","merger agreement","offer for all"],
     4, "Takeover / M&A Bid", "🎯", "15–50%", 1),

    (["phase 3 results","phase iii results","phase 2 results","phase ii results",
      "positive top-line","pivotal trial","clinical trial results",
      "statistically significant","fda approval","mhra approval","ema approval",
      "regulatory approval","breakthrough designation","orphan drug",
      "primary endpoint met","met its primary endpoint","significant clinical benefit"],
     4, "Clinical / Regulatory Result", "💊", "15–40%", 1),

    (["maiden resource","initial resource","resource estimate",
      "significant intersection","high grade intercept","drill results",
      "bonanza grade","significant mineralisation","reserve update",
      "jorc resource","ni 43-101 resource"],
     3, "Resource / Drill Result", "⛏️", "10–30%", 1),

    # TIER 2 — 5–20% moves
    (["materially ahead","significantly ahead","ahead of market expectations",
      "ahead of expectations","ahead of management expectations",
      "record revenue","record sales","record profit","record results",
      "exceeds expectations","well ahead of expectations"],
     3, "Beats Expectations", "🚀", "5–20%", 2),

    (["transformational contract","significant contract","major contract",
      "landmark agreement","exclusive licence","licence and commercialisation",
      "strategic licensing","global licence","exclusive agreement"],
     3, "Major Contract / Licence", "📋", "5–20%", 2),

    (["strategic partnership","strategic investment","cornerstone investment",
      "joint venture","co-development agreement"],
     2, "Strategic Partnership", "🤝", "5–15%", 2),

    (["contract award","contract win","awarded a contract","selected as preferred",
      "letter of intent signed","heads of terms signed",
      "framework agreement signed","supply agreement","offtake agreement"],
     2, "Contract Win", "📝", "5–15%", 2),

    (["full year results","half year results","interim results",
      "preliminary results","annual results","positive trading update",
      "strong trading performance","confident outlook","board is pleased to report"],
     2, "Results / Trading Update", "📊", "3–10%", 2),

    (["director purchase","pdmr purchase",
      "executive director purchase","ceo purchase","cfo purchase"],
     1, "Director / Insider Buying", "👤", "2–8%", 2),
]

# Tightened negatives (v3 BUG9 fix retained)
RNS_NEGATIVES = [
    "profit warning","revenue warning","below expectations",
    "below market expectations","disappointing results",
    "challenging trading conditions","shortfall in revenue",
    "below board expectations","below management expectations",
    "placing at a discount of","deeply discounted placing",
    "distressed placing","emergency placing",
    "suspension of trading","suspended from trading",
    "administration","insolvency","liquidation",
    "cancellation of admission","cease trading",
    "winding up petition","material uncertainty related to going concern",
    "breach of banking covenant",
    "fca investigation into","criminal investigation into","serious fraud office",
]

# ─────────────────────────────────────────────────────────────────────────────
#  WATCHLIST — curated AIM names with history of sharp moves
# ─────────────────────────────────────────────────────────────────────────────
WATCHLIST = [
    # Biotech / Pharma / MedTech
    "AVCT","BVXP","CLI","CLIN","CRSO","CRW","GBG","HAT","HBR","IGP",
    "IMM","JOG","KBT","LIO","MCB","MED","MRC","MTI","NANO","NCZ",
    "OXB","POLB","RDL","SLN","TRX","VRS","WGB","CTEC","GENL","MDNA",
    # Mining / Resources
    "AAZ","ALBA","AMI","AOG","APH","ARG","ARL","ARM","ARP","ATM",
    "AUR","AVN","BKT","CAD","ECR","EDL","EGO","EMX","ERG","GCM",
    "GTI","HYR","IQE","KORE","LWI","MDC","MMX","MKA","MOTS","NAP",
    "POG","RRS","SXX","THL","UJO","VGM","WKP","XTR","ZYT","KEFI",
    # Tech / Digital / Fintech
    "ALFA","AMS","ANP","APP","BIG","BUR","CAB","GAM","GAN","HAV",
    "HAWK","IGN","KAV","MCK","PAD","QRT","RAD","SLP","TAM","ASOS",
    "BOO","PETS","WHR","GFRD","SDX","STB","TCG","TED","LOOP",
    # Known AIM movers
    "HOC","LUCK","MAST","MATD","HIGH","GROW","JQW","MIND","MINT",
    "MIRA","CRON","LEAF",
]

def to_yahoo(t: str) -> str:
    t = t.strip().upper()
    return t if t.endswith(".L") else t + ".L"

WATCHLIST_YAHOO = list(dict.fromkeys(to_yahoo(t) for t in WATCHLIST))


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1: Fetch RNS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_rns_today() -> tuple[dict[str, dict], bool]:
    """
    Returns (rns_map, rns_feed_ok).
    rns_feed_ok = True if we actually got data from LSE, False if fell back / empty.
    """
    rns_data: dict[str, dict] = {}
    now_l  = datetime.now(LONDON_TZ)
    cutoff = now_l - timedelta(hours=RNS_LOOKBACK_HRS)

    hdrs = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"),
        "Accept":  "application/rss+xml, application/xml, text/xml, */*",
        "Referer": "https://www.londonstockexchange.com/",
    }

    # Attempt 1: RSS (most stable)
    for url in [
        "https://www.londonstockexchange.com/exchange/news/market-news/market-news-home.html?rss=true",
        "https://api.londonstockexchange.com/api/gw/lse/regulatory-news/rss",
        "https://www.londonstockexchange.com/news?tab=regulatory-news&rss=true",
    ]:
        try:
            r = requests.get(url, headers=hdrs, timeout=12)
            if r.status_code == 200 and ("<rss" in r.text or "<feed" in r.text):
                items = _parse_rss(r.text, cutoff)
                if items:
                    rns_data.update(items)
                    print(f"  ✓ RNS RSS: {len(rns_data)} announcements")
                    return rns_data, True
        except Exception:
            pass

    # Attempt 2: JSON API fallback
    for url in [
        ("https://api.londonstockexchange.com/api/gw/lse/instruments/alldata/news"
         "?worlds=quotes&count=200&sortby=time&category=RegulatoryAnnouncement"),
        ("https://api.londonstockexchange.com/api/gw/lse/instruments"
         "/alldata/regulatorynewsheadlines?worlds=quotes&count=200"),
    ]:
        try:
            r = requests.get(url, headers={**hdrs, "Accept": "application/json"}, timeout=12)
            if r.status_code == 200:
                items = _parse_json_api(r.json(), cutoff)
                if items:
                    rns_data.update(items)
                    print(f"  ✓ RNS JSON: {len(rns_data)} announcements")
                    return rns_data, True
        except Exception:
            pass

    print("  ⚠ RNS feed unreachable — technical + fundamental signals only.")
    return rns_data, False


def _parse_rss(xml_text: str, cutoff: datetime) -> dict[str, dict]:
    result = {}
    try:
        root  = ET.fromstring(xml_text)
        ns    = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for item in items:
            t_el = item.find("title") or item.find("atom:title", ns)
            headline = (t_el.text or "").strip() if t_el is not None else ""
            if not headline:
                continue
            tidm = ""
            m = re.match(r"^([A-Z]{2,5})\s*[-:]", headline)
            if m:
                tidm = m.group(1)
            if not tidm:
                d_el = item.find("description") or item.find("atom:summary", ns)
                dt   = (d_el.text or "") if d_el is not None else ""
                m2   = re.search(r"\b([A-Z]{2,5})\.L\b", dt)
                if m2: tidm = m2.group(1)
            if not tidm:
                continue
            p_el = item.find("pubDate") or item.find("atom:published", ns)
            if p_el is not None and p_el.text:
                try:
                    pt = datetime.strptime(
                        p_el.text.strip(), "%a, %d %b %Y %H:%M:%S %z"
                    ).astimezone(LONDON_TZ)
                    if pt < cutoff:
                        continue
                except Exception:
                    pass
            _score_rns(tidm, headline, result)
    except Exception:
        pass
    return result


def _parse_json_api(data: dict, cutoff: datetime) -> dict[str, dict]:
    result = {}
    items  = (data.get("content") or data.get("data") or
              data.get("news") or data.get("items") or [])
    if not items:
        for k in data:
            if isinstance(data[k], list) and data[k] and isinstance(data[k][0], dict):
                items = data[k]; break
    for item in items:
        try:
            tidm = (item.get("tidm") or item.get("symbol") or
                    item.get("instrumentCode") or item.get("ticker") or
                    (item.get("instrument") or {}).get("tidm") or "")
            hl   = (item.get("headline") or item.get("title") or
                    item.get("summary") or item.get("description") or "")
            if not tidm or not hl:
                continue
            ts = (item.get("publishedTime") or item.get("publishedDate") or
                  item.get("date") or item.get("time") or "")
            if ts:
                try:
                    pt = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).astimezone(LONDON_TZ)
                    if pt < cutoff: continue
                except Exception:
                    pass
            _score_rns(tidm.strip().upper(), hl, result)
        except Exception:
            continue
    return result


def _score_rns(tidm: str, headline: str, result: dict):
    sym   = tidm.upper() + ".L"
    hl_lo = headline.lower()
    if any(neg in hl_lo for neg in RNS_NEGATIVES):
        result[sym] = {"headline": headline, "score": -3,
                       "label": "Negative / Warning", "emoji": "⛔",
                       "expected_move": "−5% to −30%", "negative": True, "tier": 0}
        return
    best = (0, "General Announcement", "📌", "unknown", 0)
    for kws, pts, label, emoji, move, tier in RNS_TRIGGERS:
        if any(re.search(kw, hl_lo) for kw in kws):
            if pts > best[0]:
                best = (pts, label, emoji, move, tier)
    if best[0] > 0:
        result[sym] = {"headline": headline, "score": best[0],
                       "label": best[1], "emoji": best[2],
                       "expected_move": best[3], "negative": False, "tier": best[4]}


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2a: Fundamental Health Analysis
# ─────────────────────────────────────────────────────────────────────────────
def analyse_fundamentals(info: dict, currency: str, price_gbp: float) -> dict:
    """
    Score the company's financial health from Yahoo Finance info dict.

    Returns a dict with:
      fund_score       : int 0–6  (higher = healthier)
      fund_grade       : str A/B/C/D/F
      fund_flags       : list[str]  human-readable findings
      hard_block       : bool  (True = too dangerous to trade without Tier-1 RNS)
      cash_runway_qtrs : float | None
      net_cash_gbp     : float | None
      current_ratio    : float | None
      revenue_growth   : float | None
      op_cashflow_gbp  : float | None
      debt_equity      : float | None
      dilution_risk    : str  ("LOW" / "MEDIUM" / "HIGH" / "CRITICAL")
    """
    flags   = []
    score   = 0
    block   = False

    # ── Helper: convert Yahoo raw value to GBP ────────────────────────────────
    def to_gbp(val):
        """Yahoo reports financial figures in the stock's native currency unit.
           For GBp stocks that's pence — convert to pounds."""
        if val is None:
            return None
        return float(val) / 100.0 if currency == "GBp" else float(val)

    # ── 1. Cash position ──────────────────────────────────────────────────────
    total_cash  = to_gbp(info.get("totalCash"))
    total_debt  = to_gbp(info.get("totalDebt") or 0)
    net_cash    = (total_cash - total_debt) if total_cash is not None else None

    if net_cash is not None:
        if net_cash > 0:
            score += 1
            flags.append(f"✅ Net cash positive: £{net_cash/1e6:.1f}m (cash > debt)")
        else:
            flags.append(f"⚠ Net debt: £{abs(net_cash)/1e6:.1f}m — company owes more than it holds")

    # ── 2. Cash runway ────────────────────────────────────────────────────────
    op_cf       = to_gbp(info.get("operatingCashflow"))
    cash_runway = None

    if total_cash is not None and op_cf is not None:
        if op_cf < 0:
            # Burning cash — calculate quarters remaining
            quarterly_burn = abs(op_cf) / 4.0
            cash_runway    = total_cash / quarterly_burn if quarterly_burn > 0 else None
            if cash_runway is not None:
                if cash_runway < MIN_CASH_RUNWAY_QTR:
                    block = True
                    flags.append(
                        f"🚫 CRITICAL: Only {cash_runway:.1f} quarters of cash remaining "
                        f"at current burn rate — placing / dilution IMMINENT"
                    )
                elif cash_runway < 4:
                    flags.append(
                        f"⚠ Cash runway: {cash_runway:.1f} quarters — fundraise likely "
                        f"within 12 months, dilution risk HIGH"
                    )
                else:
                    score += 1
                    flags.append(
                        f"✅ Cash runway: {cash_runway:.1f} quarters at current burn"
                    )
        else:
            # Self-funding — no burn concern
            cash_runway = 999.0  # effectively infinite
            score += 2
            flags.append(
                f"✅ Cash-generative: operating cashflow £{op_cf/1e6:.1f}m — "
                f"no fundraise needed"
            )
    elif total_cash is not None:
        flags.append(f"⚠ Cashflow data unavailable — verify runway manually")

    # ── 3. Current ratio (short-term solvency) ────────────────────────────────
    current_ratio = info.get("currentRatio")
    if current_ratio is not None:
        current_ratio = float(current_ratio)
        if current_ratio >= 2.0:
            score += 1
            flags.append(f"✅ Current ratio {current_ratio:.1f}x — strong short-term liquidity")
        elif current_ratio >= MIN_CURRENT_RATIO:
            flags.append(f"Current ratio {current_ratio:.1f}x — adequate, watch for deterioration")
        else:
            flags.append(
                f"⚠ Current ratio {current_ratio:.1f}x — below 1.0: "
                f"current liabilities exceed current assets. Short-term squeeze risk"
            )

    # ── 4. Revenue growth ─────────────────────────────────────────────────────
    rev_growth = info.get("revenueGrowth")
    if rev_growth is not None:
        rev_growth = float(rev_growth)
        if rev_growth >= 0.20:
            score += 1
            flags.append(f"✅ Revenue growing +{rev_growth*100:.0f}% YoY — genuine business expansion")
        elif rev_growth >= 0.0:
            flags.append(f"Revenue flat/modest +{rev_growth*100:.0f}% YoY")
        elif rev_growth >= MIN_REVENUE_GROWTH:
            flags.append(f"⚠ Revenue declining {rev_growth*100:.0f}% YoY — business shrinking")
        else:
            flags.append(
                f"⛔ Revenue collapsing {rev_growth*100:.0f}% YoY — "
                f"serious business deterioration"
            )

    # ── 5. Debt / dilution risk ───────────────────────────────────────────────
    debt_equity = info.get("debtToEquity")
    if debt_equity is not None:
        debt_equity = float(debt_equity) / 100.0  # Yahoo returns this as %, e.g. 45 = 0.45
    
    # Proxy for CLN / convertible debt risk:
    # High debt/equity on AIM with negative cashflow = very likely has CLNs
    dilution_risk = "LOW"
    if debt_equity is not None:
        if debt_equity < 0.25:
            score += 1
            dilution_risk = "LOW"
            flags.append(f"✅ Low leverage: D/E {debt_equity:.2f} — minimal dilution risk")
        elif debt_equity < MAX_DEBT_EQUITY:
            dilution_risk = "MEDIUM"
            flags.append(
                f"⚠ Moderate leverage: D/E {debt_equity:.2f} — "
                f"check for convertible loan notes (CLNs) in filings"
            )
        else:
            dilution_risk = "HIGH"
            flags.append(
                f"🚫 HIGH leverage: D/E {debt_equity:.2f} — "
                f"probable CLN / warrant overhang. Sellers likely at every rally"
            )
    
    # Combine with cashflow — worst case
    if op_cf is not None and op_cf < 0 and debt_equity is not None and debt_equity > 1.0:
        dilution_risk = "CRITICAL"
        block = True
        flags.append(
            "🚫 CRITICAL DILUTION RISK: cash-burning + high leverage. "
            "Classic AIM serial-dilutor profile — avoid without Tier-1 RNS"
        )

    # ── 6. Earnings trend ─────────────────────────────────────────────────────
    earn_growth = info.get("earningsGrowth")
    if earn_growth is not None:
        earn_growth = float(earn_growth)
        if earn_growth >= 0.25:
            score += 1
            flags.append(f"✅ Earnings growing +{earn_growth*100:.0f}% — profitable momentum")
        elif earn_growth < -0.50:
            flags.append(f"⚠ Earnings declining {earn_growth*100:.0f}% YoY")

    # ── 7. Profitability indicator ────────────────────────────────────────────
    profit_margin = info.get("profitMargins")
    if profit_margin is not None:
        profit_margin = float(profit_margin)
        if profit_margin > 0:
            flags.append(f"✅ Profitable: {profit_margin*100:.1f}% net margin")
        elif profit_margin > -0.20:
            flags.append(f"Loss-making: {profit_margin*100:.1f}% net margin (pre-revenue/early stage)")
        else:
            flags.append(f"⚠ Deep losses: {profit_margin*100:.1f}% margin — high cash consumption")

    # ── Grade assignment ──────────────────────────────────────────────────────
    if   score >= 6: grade = "A"
    elif score >= 4: grade = "B"
    elif score >= 2: grade = "C"
    elif score >= 1: grade = "D"
    else:            grade = "F"

    if not flags:
        flags.append("⚠ Fundamental data unavailable from Yahoo Finance — check Companies House")

    return {
        "fund_score":       score,
        "fund_grade":       grade,
        "fund_flags":       flags,
        "hard_block":       block,
        "cash_runway_qtrs": cash_runway,
        "net_cash_gbp":     net_cash,
        "current_ratio":    current_ratio,
        "revenue_growth":   rev_growth,
        "op_cashflow_gbp":  op_cf,
        "debt_equity":      debt_equity,
        "dilution_risk":    dilution_risk,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2b: Technical Data Fetch
# ─────────────────────────────────────────────────────────────────────────────
def fetch_ticker_data(symbol: str) -> dict | None:
    """Download 1 year of price history + fundamentals."""
    try:
        tk   = yf.Ticker(symbol)
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

        # In pre-market context: last = yesterday's complete session
        last  = hist.iloc[-1]
        prev1 = hist.iloc[-2] if len(hist) >= 2 else last
        prev2 = hist.iloc[-3] if len(hist) >= 3 else prev1

        close   = float(last["Close"])
        open_p  = float(last.get("Open",   close))
        high_p  = float(last.get("High",   close))
        low_p   = float(last.get("Low",    close))
        vol     = float(last.get("Volume", 0))
        prev_c  = float(prev1["Close"])
        prev2_c = float(prev2["Close"])

        pct_change = (close - prev_c) / prev_c * 100 if prev_c else 0.0

        # Currency & GBP price
        currency  = info.get("currency", "GBP")
        price_gbp = close / 100.0 if currency == "GBp" else close

        # Liquidity
        avg_vol_20    = (float(volumes.iloc[-21:-1].mean())
                         if len(volumes) >= 21 else float(volumes.iloc[:-1].mean()))
        avg_daily_gbp = avg_vol_20 * price_gbp
        vol_ratio     = vol / avg_vol_20 if avg_vol_20 > 0 else 0.0

        # Spread estimate by liquidity tier
        if   avg_daily_gbp >= 500_000: est_spread_pct = 0.5
        elif avg_daily_gbp >= 150_000: est_spread_pct = 1.5
        elif avg_daily_gbp >= 50_000:  est_spread_pct = 2.5
        elif avg_daily_gbp >= 20_000:  est_spread_pct = 4.0
        else:                           est_spread_pct = 6.0

        # ATR(14)
        tr_list = []
        for i in range(1, min(15, len(hist))):
            h  = float(hist.iloc[-i]["High"])
            l  = float(hist.iloc[-i]["Low"])
            pc = float(hist.iloc[-(i + 1)]["Close"])
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr14   = sum(tr_list) / len(tr_list) if tr_list else 0.0
        atr_pct = atr14 / close * 100.0 if close > 0 else 0.0

        # Market cap — BUG7 fix: use shares × price_gbp
        mktcap_gbp = None
        shares_out = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        if shares_out and price_gbp > 0:
            mktcap_gbp = float(shares_out) * price_gbp
        else:
            raw_mc = info.get("marketCap")
            if raw_mc:
                mktcap_gbp = float(raw_mc)   # Yahoo gives this in £ for UK stocks

        is_aim = (
            info.get("exchange", "").upper() in ("AIM", "LSE", "IOB")
            or (mktcap_gbp is not None and mktcap_gbp < MAX_MKTCAP_GBP)
        )

        # ── FUNDAMENTALS ──────────────────────────────────────────────────────
        fund = analyse_fundamentals(info, currency, price_gbp)

        # ── Bollinger Band Squeeze ────────────────────────────────────────────
        bb_widths = []
        for i in range(20, len(closes) + 1):
            w   = closes.iloc[i - 20:i]
            mid = w.mean()
            std = w.std()
            bb_widths.append((2 * std / mid * 100) if mid > 0 else 0)

        current_bb        = bb_widths[-1] if bb_widths else 0
        if len(bb_widths) >= 20:
            sw                = sorted(bb_widths)
            bb_squeeze        = current_bb <= sw[int(len(sw) * BB_SQUEEZE_RANK / 100)]
            bb_squeeze_strong = current_bb <= sw[int(len(sw) * 0.10)]
        else:
            bb_squeeze = bb_squeeze_strong = False

        # ── Volume accumulation — v4: direction-aware ─────────────────────────
        # BUG4 fix retained: exactly VOL_BUILDUP_DAYS
        # v4 NEW: confirm price was also rising on those same days
        n_vol    = min(VOL_BUILDUP_DAYS, len(volumes) - 1)
        vol_seq  = [float(volumes.iloc[-(n_vol - i)]) for i in range(n_vol + 1)]
        cl_seq   = [float(closes.iloc[-(n_vol - i)])  for i in range(n_vol + 1)]
        # vol_seq[0]/cl_seq[0] = oldest, [-1] = most recent (yesterday)
        vol_rising_raw = all(vol_seq[i] > vol_seq[i - 1] for i in range(1, len(vol_seq)))
        price_rising   = all(cl_seq[i]  > cl_seq[i - 1]  for i in range(1, len(cl_seq)))
        vol_above_avg  = vol_ratio >= 1.5

        # True accumulation = volume AND price both rising (not distribution)
        vol_accumulation = vol_rising_raw and price_rising
        # Rising volume on falling price = distribution (negative signal)
        vol_distribution = vol_rising_raw and not price_rising

        # ── RSI(14) ───────────────────────────────────────────────────────────
        if len(closes) >= 15:
            d     = closes.diff().dropna()
            gains = d.clip(lower=0)
            loss  = (-d).clip(lower=0)
            ag    = gains.rolling(14).mean().iloc[-1]
            al    = loss.rolling(14).mean().iloc[-1]
            rsi14 = 100 - (100 / (1 + ag / al)) if al > 0 else 100.0
        else:
            rsi14 = 50.0

        # ── EMAs — BUG8 fix: explicit is not None ─────────────────────────────
        ema9  = float(closes.ewm(span=9,  adjust=False).mean().iloc[-1]) if len(closes) >= 9  else None
        ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1]) if len(closes) >= 20 else None
        ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else None

        ema_aligned  = (ema9 is not None and ema20 is not None and ema50 is not None
                        and close > ema9 > ema20 > ema50)
        ema_uptrend  = (ema20 is not None and ema50 is not None and ema20 > ema50)
        above_ema20  = ema20 is not None and close > ema20
        above_ema50  = ema50 is not None and close > ema50

        # ── 52W range — BUG1 fix: full 1-year history ────────────────────────
        hi52          = float(closes.max())
        lo52          = float(closes.min())
        rng52         = hi52 - lo52
        pos52         = (close - lo52) / rng52 * 100 if rng52 > 0 else 50.0
        dist_hi_pct   = (hi52 - close) / hi52 * 100 if hi52 > 0 else 0.0
        near_52w_high = pos52 >= 90
        at_52w_high   = pos52 >= 97
        in_discount   = pos52 < 35
        distribution_risk = at_52w_high

        # ── Inside day — BUG2 fix: last vs prev1 ─────────────────────────────
        yest_high  = high_p
        yest_low   = low_p
        d_bef_high = float(prev1.get("High", prev_c))
        d_bef_low  = float(prev1.get("Low",  prev_c))
        inside_day = (yest_high <= d_bef_high and yest_low >= d_bef_low)

        # ── Strong close — BUG3 fix ───────────────────────────────────────────
        yest_range     = yest_high - yest_low
        close_position = (close - yest_low) / yest_range if yest_range > 0 else 0.5
        strong_close   = close_position >= 0.75

        # ── Range compression — BUG5 fix: H-L vs 20-day H-L avg ─────────────
        recent_hl  = [float(hist.iloc[-i]["High"]) - float(hist.iloc[-i]["Low"])
                      for i in range(1, min(6, len(hist)))]
        longterm_hl= [float(hist.iloc[-i]["High"]) - float(hist.iloc[-i]["Low"])
                      for i in range(1, min(21, len(hist)))]
        avg_r_hl   = sum(recent_hl)   / len(recent_hl)   if recent_hl   else 0
        avg_l_hl   = sum(longterm_hl) / len(longterm_hl) if longterm_hl else 0
        range_compression = avg_r_hl < avg_l_hl * 0.70 and avg_l_hl > 0

        # ── STOP LOSS — v4 NEW: calculated from ESTIMATED ENTRY PRICE ─────────
        # Not from yesterday's close — the stock will gap at open on RNS
        # Entry price estimates are applied in score_predictive() after we know
        # the RNS tier. We store the raw close-based values here as fallback.
        atr_stop_pct_raw   = min(atr14 * ATR_STOP_MULT / close * 100, MAX_STOP_PCT) if close > 0 else 0
        atr_tight_pct_raw  = min(atr14 * ATR_STOP_MULT_TIGHT / close * 100, MAX_STOP_PCT) if close > 0 else 0
        swg_lows           = [float(lows.iloc[-i]) for i in range(1, min(SWING_LOW_LOOKBACK, len(lows)) + 1)]
        swing_low_pct_raw  = min((close - min(swg_lows) * 0.995) / close * 100, MAX_STOP_PCT) if close > 0 and swg_lows else 0
        swing_low_price_raw= close * (1 - swing_low_pct_raw / 100)

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
            "vol_accumulation":     vol_accumulation,
            "vol_distribution":     vol_distribution,
            "vol_above_avg":        vol_above_avg,
            "inside_day":           inside_day,
            "strong_close":         strong_close,
            "close_position":       close_position,
            "range_compression":    range_compression,
            "mktcap_gbp":           mktcap_gbp,
            "is_aim":               is_aim,
            "sector":               info.get("sector", ""),
            "industry":             info.get("industry", ""),
            # Raw stop values (adjusted for entry price in scorer)
            "atr_stop_pct_raw":     atr_stop_pct_raw,
            "atr_tight_pct_raw":    atr_tight_pct_raw,
            "swing_low_pct_raw":    swing_low_pct_raw,
            "swing_low_price_raw":  swing_low_price_raw,
            # Fundamentals
            **{f"fund_{k}": v for k, v in fund.items()},
            "fund_data": fund,
        }

    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3: Scoring  (Technical 0–16  +  Fundamental 0–6  =  Combined 0–22)
# ─────────────────────────────────────────────────────────────────────────────
def score_predictive(
    d: dict, rns: dict | None, rns_feed_ok: bool
) -> tuple[int, int, int, list[str], str, str, float | None, dict]:
    """
    Returns: (tech_score, fund_score, combined_score,
              reasons, confidence, predicted_move, rr_ratio, stop_data)
    """
    ts      = 0   # technical score
    reasons = []
    predicted_move_midpoint = None

    # ── Determine RNS tier for entry-price gap estimate ───────────────────────
    rns_tier = 0
    if rns and not rns.get("negative"):
        rns_tier = rns.get("tier", 2)

    # ── Entry price estimate (v4 NEW) ─────────────────────────────────────────
    close = d["close"]
    if rns_tier == 1:
        entry_est = close * (1 + RNS_GAP_T1_PCT / 100)
        reasons.append(f"📌 Entry estimate: +{RNS_GAP_T1_PCT}% gap = "
                        f"{_fmt_p(entry_est, d['currency'])} (Tier-1 RNS open)")
    elif rns_tier == 2:
        entry_est = close * (1 + RNS_GAP_ESTIMATE_PCT / 100)
        reasons.append(f"📌 Entry estimate: +{RNS_GAP_ESTIMATE_PCT}% gap = "
                        f"{_fmt_p(entry_est, d['currency'])} (Tier-2 RNS open)")
    else:
        entry_est = close * (1 + TECH_ENTRY_BUFFER_PCT / 100)

    # Recalculate stop levels from ENTRY PRICE (not yesterday's close)
    atr14  = d["atr14"]
    spread = d["est_spread_pct"]
    min_stop_pct = spread * 2.0  # stop must be at least 2× spread from entry

    atr_stop_pct   = min(atr14 * ATR_STOP_MULT  / entry_est * 100, MAX_STOP_PCT) if entry_est > 0 else 0
    atr_tight_pct  = min(atr14 * ATR_STOP_MULT_TIGHT / entry_est * 100, MAX_STOP_PCT) if entry_est > 0 else 0
    # Swing low is absolute price — recalculate distance from entry
    swing_low_price= d["swing_low_price_raw"]
    swing_low_pct  = min((entry_est - swing_low_price) / entry_est * 100, MAX_STOP_PCT) if entry_est > 0 else 0

    # Recommended stop
    if swing_low_pct < atr_stop_pct and swing_low_pct >= min_stop_pct:
        rec_stop_pct   = swing_low_pct
        rec_stop_price = entry_est * (1 - rec_stop_pct / 100)
        rec_stop_method= "Swing Low"
    else:
        rec_stop_pct   = atr_stop_pct
        rec_stop_price = entry_est * (1 - rec_stop_pct / 100)
        rec_stop_method= f"ATR×{ATR_STOP_MULT}"

    atr_stop_price  = entry_est * (1 - atr_stop_pct  / 100)
    atr_tight_price = entry_est * (1 - atr_tight_pct / 100)

    # Position sizing
    price_gbp    = d["price_gbp"]
    max_risk_gbp = EXAMPLE_ACCOUNT_GBP * ACCOUNT_RISK_PCT / 100
    stop_dist_gbp= price_gbp * rec_stop_pct / 100
    max_shares   = int(max_risk_gbp / stop_dist_gbp) if stop_dist_gbp > 0 else 0
    position_gbp = max_shares * price_gbp

    stop_data = {
        "entry_est":        entry_est,
        "atr_stop_price":   atr_stop_price,
        "atr_stop_pct":     atr_stop_pct,
        "atr_tight_price":  atr_tight_price,
        "atr_tight_pct":    atr_tight_pct,
        "swing_low_price":  swing_low_price,
        "swing_low_pct":    swing_low_pct,
        "rec_stop_price":   rec_stop_price,
        "rec_stop_pct":     rec_stop_pct,
        "rec_stop_method":  rec_stop_method,
        "max_shares":       max_shares,
        "position_gbp":     position_gbp,
    }

    # ── RNS feed status badge (v4 NEW) ────────────────────────────────────────
    if not rns_feed_ok:
        reasons.append(
            "⚠ RNS FEED OFFLINE: Confidence labels are technical-only. "
            "Check londonstockexchange.com manually before acting."
        )

    # ── T1. Trend direction gate (BUG6 fix retained) ─────────────────────────
    if not d["above_ema20"] and not d["above_ema50"]:
        ts -= 2
        reasons.append(
            "⛔ DOWNTREND: Close below EMA20 and EMA50 — "
            "avoid unless strong catalyst overrides trend"
        )
    elif not d["above_ema20"]:
        ts -= 1
        reasons.append("⚠ Below EMA20 — short-term weakness. Needs volume/RNS confirmation")

    if d["in_discount"] and not d["above_ema50"]:
        ts -= 1
        reasons.append(
            f"⚠ Bottom third of 52W range ({d['pos52']:.0f}%) + below EMA50 "
            "— falling knife risk"
        )

    # ── T2. RNS Catalyst (0–4 pts) ────────────────────────────────────────────
    has_positive_rns = False
    if rns:
        if rns.get("negative"):
            ts -= 3
            reasons.append(f"⛔ NEGATIVE RNS: {rns.get('label','')}")
            reasons.append(f"   \"{rns.get('headline','')[:90]}\"")
        else:
            has_positive_rns = True
            ts += rns.get("score", 0)
            reasons.append(
                f"{rns['emoji']} RNS TODAY: {rns['label']} "
                f"(expected: {rns['expected_move']})"
            )
            reasons.append(f"   \"{rns.get('headline','')[:90]}\"")
            nums = re.findall(r"\d+", rns.get("expected_move", ""))
            if len(nums) >= 2:
                predicted_move_midpoint = (int(nums[0]) + int(nums[1])) / 2.0

    # ── T3. Bollinger Band Squeeze (0–2 pts) ──────────────────────────────────
    bb_signal = False
    if d["bb_squeeze_strong"]:
        ts += 2; bb_signal = True
        reasons.append("🔥 STRONG BB Squeeze — volatility at historical low. Explosive move imminent")
    elif d["bb_squeeze"]:
        ts += 1; bb_signal = True
        reasons.append(f"📐 BB Squeeze — bottom {BB_SQUEEZE_RANK}th percentile volatility")

    # ── T4. Volume — v4: direction-aware (0–2 pts) ────────────────────────────
    vol_signal = False
    if d["vol_distribution"]:
        ts -= 1
        reasons.append(
            f"⚠ DISTRIBUTION SIGNAL: volume rising {d['vol_ratio']:.1f}× avg "
            "on FALLING price — smart money selling into strength"
        )
    elif d["vol_accumulation"] and d["vol_above_avg"]:
        ts += 2; vol_signal = True
        reasons.append(
            f"🏦 Volume accumulation: {VOL_BUILDUP_DAYS}d rising vol + rising price × "
            f"{d['vol_ratio']:.1f}× avg — genuine institutional buying"
        )
    elif d["vol_accumulation"]:
        ts += 1; vol_signal = True
        reasons.append(
            f"📊 Volume accumulation: {VOL_BUILDUP_DAYS}d rising vol with rising price"
        )
    elif d["vol_above_avg"]:
        ts += 1; vol_signal = True
        reasons.append(f"📊 Volume elevated: {d['vol_ratio']:.1f}× 20-day average")

    # ── T5. Technical position (0–2 pts, AIM-aware) ───────────────────────────
    pos_signal = False
    if d["near_52w_high"] and not d["distribution_risk"]:
        ts += 2; pos_signal = True
        reasons.append(
            f"📈 Near 52W high ({d['pos52']:.0f}%, {d['dist_hi_pct']:.1f}% from top) "
            "— approaching breakout"
        )
    elif d["at_52w_high"] and d["distribution_risk"]:
        reasons.append(
            f"⚠ AT 52W HIGH ({d['pos52']:.0f}%) — AIM distribution zone: "
            "confirm no insider selling before entry"
        )
    elif d["ema_aligned"]:
        ts += 1; pos_signal = True
        reasons.append("✅ EMA aligned: Close > EMA9 > EMA20 > EMA50 — clean uptrend")
    elif d["ema_uptrend"]:
        ts += 1; pos_signal = True
        reasons.append("✅ EMA20 > EMA50 — medium-term bullish structure")

    # ── T6. Compression patterns (0–2 pts) ────────────────────────────────────
    if d["inside_day"] and d["range_compression"]:
        ts += 2
        reasons.append("🗜️  Inside day + range compression — price coiled pre-breakout")
    elif d["inside_day"]:
        ts += 1
        reasons.append("🗜️  Inside day — yesterday's range inside prior session")
    elif d["range_compression"]:
        ts += 1
        reasons.append("🗜️  Range compression — recent ranges 30%+ tighter than 20-day avg")

    if d["strong_close"]:
        reasons.append(
            f"✅ Strong close: top {int((1-d['close_position'])*100)}% of session range"
        )

    # ── T7. RSI filter (0 / −1) ───────────────────────────────────────────────
    rsi = d["rsi14"]
    if rsi > 75:
        ts -= 1
        reasons.append(f"⚠ RSI {rsi:.0f} — overbought, pullback risk before continuation")
    elif rsi < 30:
        reasons.append(f"📉 RSI {rsi:.0f} — oversold, potential bounce but needs catalyst")
    else:
        reasons.append(f"RSI {rsi:.0f} — healthy momentum range")

    # ── T8. ATR capacity (0–1 pt) ─────────────────────────────────────────────
    if d["atr_pct"] >= 6.0:
        ts += 1
        reasons.append(f"✅ ATR {d['atr_pct']:.1f}% — high volatility, 10–20%+ moves possible")
    elif d["atr_pct"] >= MIN_ATR_PCT:
        reasons.append(f"ATR {d['atr_pct']:.1f}% — moderate volatility, 5–10% moves achievable")

    # ── T9. Market cap (0–1 pt) ───────────────────────────────────────────────
    mc = d["mktcap_gbp"]
    if mc is not None and mc < 30e6:
        ts += 1
        reasons.append(f"✅ Micro-cap £{mc/1e6:.1f}m — explosive on volume")
    elif mc is not None and mc < 100e6:
        reasons.append(f"Small-cap £{mc/1e6:.0f}m — manageable for sharp moves")

    # ── T10. Liquidity note ────────────────────────────────────────────────────
    adgbp = d["avg_daily_gbp"]
    if adgbp >= 200_000:
        reasons.append(f"✅ Liquid: £{adgbp/1e3:.0f}k/day")
    elif adgbp >= 50_000:
        reasons.append(f"⚠ Moderate liquidity: £{adgbp/1e3:.0f}k/day — use limit orders")
    else:
        reasons.append(f"🚫 ILLIQUID: £{adgbp/1e3:.0f}k/day — spread may eat any gain")

    if spread >= 2.5:
        reasons.append(
            f"⚠ Wide spread ~{spread:.1f}%: need {spread*2:.1f}%+ move just to break even"
        )

    # ── Minimum signal gate (BUG11 fix retained) ──────────────────────────────
    has_quality = has_positive_rns or bb_signal or vol_signal or pos_signal
    ts_capped   = max(-8, min(ts, 16))
    if not has_quality:
        ts_capped = min(ts_capped, MIN_COMBINED_SCORE - 1)
        reasons.append(
            "⚠ No primary signal — score capped. "
            "Not recommended without RNS / squeeze / accumulation"
        )

    # ── Fundamental score (from data) ─────────────────────────────────────────
    fs = d["fund_data"]["fund_score"]  # 0–6

    # Add fundamental flags to reasons with separator
    reasons.append("── FUNDAMENTAL HEALTH ──")
    for ff in d["fund_data"]["fund_flags"]:
        reasons.append(ff)

    combined = ts_capped + fs

    # ── Confidence + predicted move ───────────────────────────────────────────
    if rns and not rns.get("negative"):
        predicted_move = rns.get("expected_move", "5–15%")
        if   combined >= 14: confidence = "VERY HIGH"
        elif combined >= 10: confidence = "HIGH"
        elif combined >= 7:  confidence = "MEDIUM"
        else:                confidence = "LOW"
    else:
        if   combined >= 12: confidence, predicted_move = "HIGH (Technical)",   "5–15%"
        elif combined >= 8:  confidence, predicted_move = "MEDIUM (Technical)", "3–10%"
        elif combined >= 5:  confidence, predicted_move = "LOW (Technical)",    "2–5%"
        else:                confidence, predicted_move = "SPECULATIVE",        "unknown"

    # ── R/R ratio ─────────────────────────────────────────────────────────────
    rr_ratio = None
    if predicted_move_midpoint and rec_stop_pct > 0:
        rr_ratio = round(predicted_move_midpoint / rec_stop_pct, 1)

    return ts_capped, fs, combined, reasons, confidence, predicted_move, rr_ratio, stop_data


def _fmt_p(p: float, ccy: str) -> str:
    if ccy == "GBp": return f"{p:.2f}p"
    return f"£{p:.4f}" if p < 1 else f"£{p:.2f}"


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4: Build HTML Report
# ─────────────────────────────────────────────────────────────────────────────
def build_html(results: list[dict], rns_map: dict, rns_feed_ok: bool,
               scan_time: str, date_str: str) -> str:

    def sc_col(s, mx=16):
        p = max(0, s) / mx
        if p >= 0.70: return "#00e676"
        if p >= 0.50: return "#f5a623"
        if p >= 0.30: return "#29b6f6"
        return "#546e7a"

    def conf_col(c):
        if "VERY HIGH" in c: return "#00e676"
        if "HIGH"      in c: return "#f5a623"
        if "MEDIUM"    in c: return "#29b6f6"
        return "#546e7a"

    def rr_col(rr):
        if rr is None: return "#546e7a"
        if rr >= 3:    return "#00e676"
        if rr >= 2:    return "#f5a623"
        return "#ff3d57"

    def grade_col(g):
        return {"A":"#00e676","B":"#69f0ae","C":"#f5a623","D":"#ff7043","F":"#ff3d57"}.get(g,"#546e7a")

    def fmt_p(p, ccy): return _fmt_p(p, ccy)

    def fmt_mc(mc):
        if mc is None: return "N/A"
        if mc >= 1e9:  return f"£{mc/1e9:.1f}B"
        if mc >= 1e6:  return f"£{mc/1e6:.0f}M"
        return f"£{mc/1e3:.0f}K"

    def fmt_gbp(v):
        if v >= 1e6:  return f"£{v/1e6:.1f}M"
        if v >= 1000: return f"£{v/1e3:.0f}K"
        return f"£{v:.0f}"

    total    = len(results)
    rns_cnt  = sum(1 for r in results if r.get("has_rns") and not (rns_map.get(r["data"]["symbol"]) or {}).get("negative"))
    top_cnt  = sum(1 for r in results if r["combined"] >= 10)
    good_rr  = sum(1 for r in results if (r.get("rr_ratio") or 0) >= 2)
    a_grade  = sum(1 for r in results if r["data"]["fund_data"]["fund_grade"] in ("A","B"))
    avg_comb = sum(r["combined"] for r in results) / total if total else 0

    now_l = datetime.now(LONDON_TZ)
    hm    = now_l.hour * 60 + now_l.minute
    if   hm < 480: sl, scc = "PRE-MARKET — OPTIMAL TIME", "#00e676"
    elif hm < 510: sl, scc = "AUCTION / OPEN",            "#f5a623"
    elif hm < 810: sl, scc = "SESSION OPEN",              "#29b6f6"
    elif hm < 930: sl, scc = "US OVERLAP",                "#f5a623"
    else:          sl, scc = "MARKET CLOSED",             "#546e7a"

    rns_status = ("✓ RNS FEED LIVE" if rns_feed_ok else "⚠ RNS FEED OFFLINE")
    rns_status_col = "#00e676" if rns_feed_ok else "#ff3d57"

    rows = ""
    for r in results:
        d         = r["data"]
        ts        = r["tech_score"]
        fs        = r["fund_score"]
        comb      = r["combined"]
        reasons   = r["reasons"]
        conf      = r["confidence"]
        pred      = r["predicted_move"]
        rr        = r.get("rr_ratio")
        has_rns   = r.get("has_rns", False)
        rns_info  = rns_map.get(d["symbol"])
        sd        = r["stop_data"]
        fund      = d["fund_data"]
        sym       = d["symbol"].replace(".L", "")

        t_col  = sc_col(ts, 16)
        f_col  = grade_col(fund["fund_grade"])
        c_col  = sc_col(comb, 22)
        cf_col = conf_col(conf)
        rr_c   = rr_col(rr)
        v_col  = "#00e676" if d["vol_ratio"] >= 3 else "#f5a623" if d["vol_ratio"] >= 1.5 else "#546e7a"
        p52_col= "#00e676" if d["pos52"] >= 90 else "#f5a623" if d["pos52"] >= 55 else "#546e7a" if d["pos52"] >= 35 else "#ff3d57"
        sp_col = "#ff3d57" if d["est_spread_pct"] >= 4 else "#f5a623" if d["est_spread_pct"] >= 2 else "#00e676"
        lq_col = "#00e676" if d["avg_daily_gbp"] >= 200_000 else "#f5a623" if d["avg_daily_gbp"] >= 50_000 else "#ff3d57"
        rsi_col= "#ff3d57" if d["rsi14"] > 70 else "#f5a623" if d["rsi14"] < 35 else "#29b6f6"
        tr_col = "#00e676" if d["ema_aligned"] else "#f5a623" if d["ema_uptrend"] else "#ff3d57"

        if d["ema_aligned"]:   tr_lbl = "ALIGNED ↑"
        elif d["ema_uptrend"]: tr_lbl = "UPTREND"
        elif d["above_ema20"]: tr_lbl = "MIXED"
        else:                   tr_lbl = "DOWNTREND"

        vol_lbl = ("ACCUM ✅" if d["vol_accumulation"] else
                   "DISTRIB ⚠" if d["vol_distribution"] else
                   f"{d['vol_ratio']:.1f}×")
        vol_lbl_col = "#00e676" if d["vol_accumulation"] else "#ff3d57" if d["vol_distribution"] else v_col

        dr_col  = {"LOW":"#00e676","MEDIUM":"#f5a623","HIGH":"#ff3d57","CRITICAL":"#ff1744"}.get(fund["dilution_risk"],"#546e7a")

        # Badges
        badges = ""
        if has_rns and rns_info and not rns_info.get("negative"):
            badges += f'<div class="badge rns-b">{rns_info["emoji"]} RNS: {rns_info["label"]}</div>'
        elif has_rns and rns_info and rns_info.get("negative"):
            badges += '<div class="badge neg-b">⛔ NEGATIVE RNS</div>'
        if not rns_feed_ok:
            badges += '<div class="badge offline-b">⚠ RNS OFFLINE</div>'
        if d["bb_squeeze_strong"]: badges += '<div class="badge sqz-b">🔥 STRONG SQUEEZE</div>'
        elif d["bb_squeeze"]:      badges += '<div class="badge sqz-d">📐 BB SQUEEZE</div>'
        if fund["hard_block"]:     badges += '<div class="badge block-b">🚫 FUND BLOCK</div>'
        elif fund["fund_grade"] in ("A","B"): badges += f'<div class="badge grade-b" style="color:{f_col};border-color:{f_col}40">★ GRADE {fund["fund_grade"]}</div>'
        if d["vol_distribution"]:  badges += '<div class="badge dist-v-b">📉 DISTRIBUTION</div>'
        if d["avg_daily_gbp"] < 50_000: badges += '<div class="badge illiq-b">⚠ LOW LIQUIDITY</div>'
        if not d["above_ema20"]:   badges += '<div class="badge dt-b">📉 DOWNTREND</div>'

        # Reasons split into tech and fund sections
        reasons_html = ""
        in_fund = False
        for rr2 in reasons:
            if "FUNDAMENTAL HEALTH" in rr2:
                reasons_html += '<div class="reason-sep">── FUNDAMENTAL HEALTH ──</div>'
                in_fund = True
                continue
            cls = "reason fund-reason" if in_fund else "reason"
            reasons_html += f'<div class="{cls}">{rr2}</div>'

        rr_disp = f"{rr:.1f}:1" if rr is not None else "N/A"
        pos_sz  = f"{sd['max_shares']:,} shares ≈ {fmt_gbp(sd['position_gbp'])}" if sd["max_shares"] > 0 else "N/A"

        tv_url   = f"https://www.tradingview.com/chart/?symbol=LSE%3A{sym}"
        rns_url  = f"https://www.londonstockexchange.com/news?tab=news-explorer&search={sym}"
        ch_url   = f"https://find-and-update.company-information.service.gov.uk/search?q={sym}"
        adv_url  = f"https://www.advfn.com/stock-market/LSE/{sym}/share-price"

        rows += f"""
<div class="card {'rns-card' if has_rns and not (rns_info or {}).get('negative') else ''}
                  {'neg-card' if has_rns and (rns_info or {}).get('negative') else ''}
                  {'block-card' if fund['hard_block'] else ''}
                  {'top-card' if comb >= 10 else ''}"
     data-comb="{comb}" data-tech="{ts}" data-fund="{fs}"
     data-rr="{rr or 0}" data-rns="{'1' if has_rns and not (rns_info or {}).get('negative') else '0'}"
     data-squeeze="{'1' if d['bb_squeeze'] else '0'}"
     data-liquid="{'1' if d['avg_daily_gbp'] >= 50_000 else '0'}"
     data-volbuild="{'1' if d['vol_accumulation'] else '0'}"
     data-grade="{fund['fund_grade']}"
     data-block="{'1' if fund['hard_block'] else '0'}">

  <div class="card-badges">{badges}</div>

  <div class="card-top">
    <div class="card-left">
      <div class="sym">{sym} <span class="exch">{'AIM' if d['is_aim'] else 'LSE'}</span></div>
      <div class="company">{d['name']}</div>
      <div class="sector-l">{d['sector']}{'  ·  '+d['industry'] if d['industry'] else ''}</div>
      <div class="price-l">{fmt_p(d['close'], d['currency'])}
        <span class="chg {'up' if d['pct_change']>=0 else 'dn'}">{d['pct_change']:+.1f}% prev</span>
      </div>
      <div class="meta-l">{fmt_mc(d['mktcap_gbp'])} &nbsp;|&nbsp; {fmt_gbp(d['avg_daily_gbp'])}/day liq</div>
    </div>

    <div class="score-block">
      <div class="score-row">
        <div class="score-item">
          <div class="si-val" style="color:{t_col}">{ts}</div>
          <div class="si-lbl">TECH<br>/16</div>
        </div>
        <div class="score-plus">+</div>
        <div class="score-item">
          <div class="si-val" style="color:{f_col}">{fs}</div>
          <div class="si-lbl">FUND<br>/6</div>
        </div>
        <div class="score-plus">=</div>
        <div class="score-item main-score">
          <div class="si-val" style="color:{c_col}">{comb}</div>
          <div class="si-lbl">TOTAL<br>/22</div>
        </div>
      </div>
      <div class="fund-grade" style="color:{f_col}">
        Fund Grade: <strong>{fund['fund_grade']}</strong>
        &nbsp;|&nbsp; Dilution: <span style="color:{dr_col}">{fund['dilution_risk']}</span>
      </div>
    </div>

    <div class="card-mid">
      <div class="conf-l" style="color:{cf_col}">{conf}</div>
      <div class="pred-lbl">Predicted:</div>
      <div class="pred-val" style="color:{cf_col}">{pred}</div>
      <div class="rr-b">
        <span class="rr-lbl">R/R</span>
        <span class="rr-val" style="color:{rr_c}">{rr_disp}</span>
      </div>
    </div>
  </div>

  <!-- STOP LOSS — calculated from entry estimate, not yesterday's close -->
  <div class="stop-box">
    <div class="stop-title">
      🛑 STOP LOSS
      <span class="entry-note">Entry est: {fmt_p(sd['entry_est'], d['currency'])} — stops from entry, not close</span>
    </div>
    <div class="stop-grid">
      <div class="stop-item recommended">
        <div class="sl-lbl">RECOMMENDED ({sd['rec_stop_method']})</div>
        <div class="sl-price">{fmt_p(sd['rec_stop_price'], d['currency'])}</div>
        <div class="sl-pct">−{sd['rec_stop_pct']:.1f}% from entry</div>
      </div>
      <div class="stop-item">
        <div class="sl-lbl">TIGHT (ATR×1.0)</div>
        <div class="sl-price">{fmt_p(sd['atr_tight_price'], d['currency'])}</div>
        <div class="sl-pct">−{sd['atr_tight_pct']:.1f}%</div>
      </div>
      <div class="stop-item">
        <div class="sl-lbl">SWING LOW ({SWING_LOW_LOOKBACK}d)</div>
        <div class="sl-price">{fmt_p(sd['swing_low_price'], d['currency'])}</div>
        <div class="sl-pct">−{sd['swing_low_pct']:.1f}%</div>
      </div>
      <div class="stop-item">
        <div class="sl-lbl">WIDE (ATR×{ATR_STOP_MULT})</div>
        <div class="sl-price">{fmt_p(sd['atr_stop_price'], d['currency'])}</div>
        <div class="sl-pct">−{sd['atr_stop_pct']:.1f}%</div>
      </div>
    </div>
    <div class="pos-row">
      <span>£{EXAMPLE_ACCOUNT_GBP:,} acct @ {ACCOUNT_RISK_PCT}% risk →</span>
      <span class="ps-val">{pos_sz}</span>
      <span class="ps-sp">Spread ~<span style="color:{sp_col}">{d['est_spread_pct']:.1f}%</span></span>
    </div>
    {"<div class='fund-block-warn'>🚫 FUNDAMENTAL BLOCK: Cash runway critical — treat as speculative only</div>" if fund['hard_block'] else ""}
  </div>

  <div class="metrics-row">
    <div class="metric"><span class="ml">VOL</span><span class="mv" style="color:{vol_lbl_col}">{vol_lbl}</span></div>
    <div class="metric"><span class="ml">ATR%</span><span class="mv">{d['atr_pct']:.1f}%</span></div>
    <div class="metric"><span class="ml">RSI</span><span class="mv" style="color:{rsi_col}">{d['rsi14']:.0f}</span></div>
    <div class="metric"><span class="ml">52W POS</span><span class="mv" style="color:{p52_col}">{d['pos52']:.0f}%</span></div>
    <div class="metric"><span class="ml">BB SQZ</span><span class="mv" style="color:{'#00e676' if d['bb_squeeze'] else '#546e7a'}">{'STRONG' if d['bb_squeeze_strong'] else 'YES' if d['bb_squeeze'] else 'NO'}</span></div>
    <div class="metric"><span class="ml">TREND</span><span class="mv" style="color:{tr_col}">{tr_lbl}</span></div>
    <div class="metric"><span class="ml">CASH RWY</span><span class="mv" style="color:{'#00e676' if (fund['cash_runway_qtrs'] or 0)>=4 else '#f5a623' if (fund['cash_runway_qtrs'] or 0)>=2 else '#ff3d57'}">{'∞' if (fund['cash_runway_qtrs'] or 0)>=99 else f"{fund['cash_runway_qtrs']:.1f}Q" if fund['cash_runway_qtrs'] else 'N/A'}</span></div>
    <div class="metric"><span class="ml">DILUTION</span><span class="mv" style="color:{dr_col}">{fund['dilution_risk']}</span></div>
  </div>

  <div class="reasons">{reasons_html}</div>

  <div class="checklist">
    <div class="cl-title">📋 PRE-TRADE CHECKLIST</div>
    <div class="cl-items">
      <span>1. Read full RNS on LSE</span>
      <span>2. Check L2 at 07:55</span>
      <span>3. Confirm volume first 3 mins</span>
      <span>4. Check Companies House for CLNs/warrants</span>
      <span>5. Set stop BEFORE entering</span>
      <span>6. Check for recent placings / director sales</span>
    </div>
  </div>

  <div class="links">
    <a href="{tv_url}"  target="_blank" class="lb chart-lb">📈 Chart</a>
    <a href="{rns_url}" target="_blank" class="lb rns-lb">📰 RNS</a>
    <a href="{ch_url}"  target="_blank" class="lb ch-lb">🏛 Co. House</a>
    <a href="{adv_url}" target="_blank" class="lb adv-lb">📊 ADVFN</a>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LSE Pre-Market v4 — {date_str}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600&family=Barlow+Condensed:wght@600;700&display=swap');
:root{{--bg:#080c10;--bg2:#0d1318;--bg3:#111920;--border:#1c2e3a;
  --amber:#f5a623;--green:#00e676;--red:#ff3d57;--blue:#29b6f6;
  --purple:#ce93d8;--dim:#4a6478;--text:#c8d8e4;
  --mono:'Share Tech Mono',monospace;--sans:'Barlow',sans-serif;--cond:'Barlow Condensed',sans-serif;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);
  background-image:radial-gradient(ellipse at 15% 0%,rgba(0,230,118,.04) 0%,transparent 50%),
  radial-gradient(ellipse at 85% 100%,rgba(41,182,246,.03) 0%,transparent 50%);}}
.header{{padding:14px 28px;border-bottom:1px solid var(--border);background:rgba(13,19,24,.98);
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}}
.logo{{font-family:var(--mono);font-size:18px;color:var(--green);letter-spacing:2px}}
.logo sub{{font-size:10px;color:var(--amber);letter-spacing:1px}}
.v4{{font-family:var(--cond);font-size:10px;color:var(--blue);border:1px solid var(--blue);
  padding:2px 6px;border-radius:2px;margin-left:8px;vertical-align:middle}}
.hdr-r{{text-align:right;font-family:var(--mono);font-size:11px;color:var(--dim);line-height:1.9}}
.how-bar{{background:rgba(0,230,118,.04);border-bottom:1px solid rgba(0,230,118,.1);
  padding:10px 28px;font-size:11px;color:var(--dim);font-family:var(--mono);
  line-height:2;display:flex;gap:24px;flex-wrap:wrap}}
.hn{{color:var(--green);font-weight:bold}}
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
.grid{{padding:18px 28px;display:grid;grid-template-columns:repeat(auto-fill,minmax(500px,1fr));gap:18px}}
.card{{background:var(--bg2);border:1px solid var(--border);border-radius:4px;
  overflow:hidden;transition:border-color .2s,transform .15s}}
.card:hover{{border-color:rgba(41,182,246,.35);transform:translateY(-1px)}}
.top-card{{border-color:rgba(245,166,35,.3)}}
.rns-card{{border-color:rgba(0,230,118,.35)}}
.neg-card{{border-color:rgba(255,61,87,.25);opacity:.65}}
.block-card{{border-color:rgba(255,61,87,.4);background:rgba(255,61,87,.03)}}
.card-badges{{display:flex;gap:5px;padding:8px 14px 0;flex-wrap:wrap}}
.badge{{font-family:var(--cond);font-size:10px;font-weight:700;letter-spacing:1px;
  padding:2px 8px;border-radius:2px;border:1px solid}}
.rns-b{{background:rgba(0,230,118,.12);border-color:rgba(0,230,118,.3);color:var(--green)}}
.neg-b{{background:rgba(255,61,87,.1);border-color:rgba(255,61,87,.3);color:var(--red)}}
.offline-b{{background:rgba(255,61,87,.08);border-color:rgba(255,61,87,.2);color:#ff7094}}
.sqz-b{{background:rgba(245,166,35,.1);border-color:rgba(245,166,35,.3);color:var(--amber)}}
.sqz-d{{background:rgba(41,182,246,.07);border-color:rgba(41,182,246,.25);color:var(--blue)}}
.block-b{{background:rgba(255,61,87,.12);border-color:rgba(255,61,87,.4);color:var(--red)}}
.grade-b{{background:rgba(0,230,118,.08)}}
.dist-v-b{{background:rgba(255,61,87,.08);border-color:rgba(255,61,87,.2);color:#ff7094}}
.illiq-b{{background:rgba(255,61,87,.08);border-color:rgba(255,61,87,.2);color:var(--red)}}
.dt-b{{background:rgba(255,61,87,.06);border-color:rgba(255,61,87,.15);color:#ff7094}}
.card-top{{display:flex;padding:12px 14px 10px;gap:10px;align-items:flex-start;flex-wrap:wrap}}
.card-left{{flex:1;min-width:150px}}
.sym{{font-family:var(--mono);font-size:20px;color:var(--amber);letter-spacing:1.5px;
  display:flex;align-items:center;gap:8px}}
.exch{{font-family:var(--cond);font-size:9px;color:var(--dim);border:1px solid var(--border);
  padding:1px 5px;border-radius:2px}}
.company{{font-size:12px;color:var(--dim);margin-top:3px}}
.sector-l{{font-size:10px;color:rgba(74,100,120,.6);margin-top:2px}}
.price-l{{font-family:var(--mono);font-size:13px;color:var(--text);margin-top:6px}}
.chg{{font-size:11px;margin-left:6px}}
.up{{color:var(--green)}}.dn{{color:var(--red)}}
.meta-l{{font-size:10px;color:var(--dim);margin-top:4px;font-family:var(--mono)}}
.score-block{{background:var(--bg3);border:1px solid var(--border);border-radius:3px;
  padding:8px 12px;min-width:220px}}
.score-row{{display:flex;align-items:center;gap:6px;justify-content:center}}
.score-item{{text-align:center;padding:0 4px}}
.score-item.main-score .si-val{{font-size:28px!important}}
.si-val{{font-family:var(--mono);font-size:20px;font-weight:bold;line-height:1}}
.si-lbl{{font-family:var(--cond);font-size:9px;color:var(--dim);letter-spacing:1px;margin-top:2px}}
.score-plus{{font-family:var(--mono);font-size:16px;color:var(--dim);padding:0 2px}}
.fund-grade{{font-family:var(--cond);font-size:10px;color:var(--dim);
  margin-top:7px;padding-top:5px;border-top:1px solid var(--border);
  text-align:center;letter-spacing:.5px}}
.card-mid{{text-align:center;min-width:100px}}
.conf-l{{font-family:var(--cond);font-size:12px;font-weight:700;letter-spacing:1px}}
.pred-lbl{{font-size:9px;color:var(--dim);font-family:var(--cond);letter-spacing:1px;
  margin-top:6px;text-transform:uppercase}}
.pred-val{{font-family:var(--mono);font-size:16px;font-weight:bold;margin-top:2px}}
.rr-b{{margin-top:8px}}
.rr-lbl{{font-family:var(--cond);font-size:9px;color:var(--dim);letter-spacing:1px;display:block}}
.rr-val{{font-family:var(--mono);font-size:18px;font-weight:bold}}
.stop-box{{margin:0 14px;padding:10px 12px 8px;
  background:rgba(255,61,87,.04);border:1px solid rgba(255,61,87,.2);border-radius:3px}}
.stop-title{{font-family:var(--cond);font-size:12px;font-weight:700;letter-spacing:1px;
  color:#ff7094;margin-bottom:6px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.entry-note{{font-size:10px;color:var(--dim);font-weight:400;letter-spacing:0;
  font-family:var(--mono)}}
.stop-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-bottom:7px}}
.stop-item{{background:var(--bg3);border:1px solid var(--border);border-radius:3px;
  padding:6px 8px;text-align:center}}
.stop-item.recommended{{background:rgba(255,61,87,.08);border-color:rgba(255,61,87,.35)}}
.sl-lbl{{font-family:var(--cond);font-size:8px;color:var(--dim);letter-spacing:.5px;
  display:block;margin-bottom:3px}}
.sl-price{{font-family:var(--mono);font-size:12px;color:#ff7094;font-weight:bold}}
.stop-item.recommended .sl-price{{font-size:14px;color:var(--red)}}
.sl-pct{{font-family:var(--mono);font-size:9px;color:var(--red);margin-top:2px}}
.pos-row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  padding-top:6px;border-top:1px solid rgba(255,61,87,.1);
  font-family:var(--mono);font-size:10px;color:var(--dim)}}
.ps-val{{color:var(--text);font-weight:bold}}
.ps-sp{{margin-left:auto}}
.fund-block-warn{{margin-top:6px;padding:5px 8px;
  background:rgba(255,61,87,.1);border:1px solid rgba(255,61,87,.3);
  border-radius:2px;font-family:var(--mono);font-size:10px;color:var(--red)}}
.metrics-row{{display:flex;flex-wrap:wrap;border-top:1px solid var(--border);
  border-bottom:1px solid var(--border);background:var(--bg3);margin-top:10px}}
.metric{{flex:1;min-width:60px;padding:6px 4px;text-align:center;
  border-right:1px solid var(--border)}}
.metric:last-child{{border-right:none}}
.ml{{display:block;font-family:var(--cond);font-size:8px;color:var(--dim);
  letter-spacing:1px;text-transform:uppercase}}
.mv{{display:block;font-family:var(--mono);font-size:11px;color:var(--text);margin-top:2px}}
.reasons{{padding:10px 14px}}
.reason{{font-size:11px;color:var(--dim);padding:2px 0;line-height:1.6}}
.fund-reason{{font-size:11px;color:#7aa8c0;padding:2px 0;line-height:1.6}}
.reason-sep{{font-family:var(--cond);font-size:10px;color:#2a4a5e;letter-spacing:1px;
  padding:4px 0 2px;margin-top:4px;border-top:1px solid #1c2e3a}}
.checklist{{margin:6px 14px 8px;padding:8px 12px;
  background:rgba(41,182,246,.04);border:1px solid rgba(41,182,246,.15);border-radius:3px}}
.cl-title{{font-family:var(--cond);font-size:11px;color:var(--blue);font-weight:700;
  letter-spacing:1px;margin-bottom:5px}}
.cl-items{{display:flex;flex-wrap:wrap;gap:8px;font-size:10px;color:var(--dim);
  font-family:var(--mono)}}
.links{{padding:8px 14px 12px;display:flex;gap:6px;flex-wrap:wrap;
  border-top:1px solid rgba(28,46,58,.4)}}
.lb{{font-family:var(--cond);font-size:11px;font-weight:700;letter-spacing:1px;
  padding:4px 10px;border-radius:2px;text-decoration:none;border:1px solid;transition:all .15s}}
.chart-lb{{color:var(--blue);border-color:rgba(41,182,246,.3);background:rgba(41,182,246,.07)}}
.chart-lb:hover{{background:rgba(41,182,246,.15)}}
.rns-lb{{color:var(--amber);border-color:rgba(245,166,35,.3);background:rgba(245,166,35,.07)}}
.rns-lb:hover{{background:rgba(245,166,35,.15)}}
.ch-lb{{color:var(--purple);border-color:rgba(206,147,216,.3);background:rgba(206,147,216,.07)}}
.ch-lb:hover{{background:rgba(206,147,216,.15)}}
.adv-lb{{color:var(--green);border-color:rgba(0,230,118,.3);background:rgba(0,230,118,.07)}}
.adv-lb:hover{{background:rgba(0,230,118,.15)}}
.empty{{text-align:center;padding:60px 20px;color:var(--dim);font-family:var(--mono);
  font-size:13px;line-height:2}}
.footer{{margin-top:40px;padding:18px 28px;border-top:1px solid var(--border);
  font-size:11px;color:var(--dim);font-family:var(--mono);line-height:2.2;background:var(--bg2)}}
.hidden{{display:none!important}}
::-webkit-scrollbar{{width:4px}}::-webkit-scrollbar-thumb{{background:var(--border)}}
@media(max-width:540px){{.grid{{padding:10px;grid-template-columns:1fr}}
  .stop-grid{{grid-template-columns:repeat(2,1fr)}}
  .score-block{{min-width:100%;margin-top:8px}}}}
</style>
</head>
<body>
<div class="header">
  <div class="logo">LSE·PREDICT <span class="v4">v4.0</span><br>
    <sub>TECHNICAL + FUNDAMENTAL SCANNER — STOP LOSS ENGINE</sub>
  </div>
  <div class="hdr-r">
    <div>{date_str} &nbsp;|&nbsp; {scan_time} London</div>
    <div style="color:{rns_status_col};font-weight:bold">{rns_status}</div>
    <div style="color:{scc};font-weight:bold">● {sl}</div>
  </div>
</div>

<div class="how-bar">
  <div><span class="hn">①</span> RNS catalyst (RSS→JSON)</div>
  <div><span class="hn">②</span> BB squeeze: volatility coiled</div>
  <div><span class="hn">③</span> Volume: direction-aware accumulation</div>
  <div><span class="hn">④</span> Cash runway: £ quarters left</div>
  <div><span class="hn">⑤</span> Dilution risk: D/E + cashflow</div>
  <div><span class="hn">⑥</span> Stops from entry price (not close)</div>
  <div><span class="hn">⑦</span> Score = Tech/16 + Fund/6 = /22</div>
</div>

<div class="stats-bar">
  <div class="stat"><span class="stat-v">{total}</span><span class="stat-l">Candidates</span></div>
  <div class="stat"><span class="stat-v" style="color:var(--green)">{rns_cnt}</span><span class="stat-l">Positive RNS</span></div>
  <div class="stat"><span class="stat-v" style="color:var(--amber)">{top_cnt}</span><span class="stat-l">Score ≥ 10/22</span></div>
  <div class="stat"><span class="stat-v" style="color:var(--green)">{a_grade}</span><span class="stat-l">Grade A/B</span></div>
  <div class="stat"><span class="stat-v" style="color:var(--green)">{good_rr}</span><span class="stat-l">R/R ≥ 2:1</span></div>
  <div class="stat"><span class="stat-v">{avg_comb:.1f}</span><span class="stat-l">Avg Score</span></div>
</div>

<div class="filter-bar">
  <span class="fl">FILTER ›</span>
  <button class="fb active" onclick="fc('all',this)">ALL ({total})</button>
  <button class="fb" onclick="fc('rns',this)">HAS RNS</button>
  <button class="fb" onclick="fc('top',this)">SCORE ≥ 10</button>
  <button class="fb" onclick="fc('goodrr',this)">R/R ≥ 2:1</button>
  <button class="fb" onclick="fc('squeeze',this)">BB SQUEEZE</button>
  <button class="fb" onclick="fc('ab',this)">GRADE A/B</button>
  <button class="fb" onclick="fc('liquid',this)">LIQUID</button>
  <button class="fb" onclick="fc('volbuild',this)">VOL ACCUM</button>
  <button class="fb" onclick="fc('noblock',this)">NO FUND BLOCK</button>
</div>

<div class="grid" id="grid">
{rows or '<div class="empty">No candidates found.<br>Try running 06:30–07:55 London time.<br>Lower MIN_COMBINED_SCORE in CONFIG.</div>'}
</div>

<div class="footer">
  ⚠  DISCLAIMERS (v4.0 — Technical + Fundamental):<br>
  · Fundamental data (cash, debt, cashflow) from Yahoo Finance — typically 1–2 quarters stale.<br>
  · Always verify financial ratios on Companies House and latest interim/annual report.<br>
  · "Cash runway" assumes constant burn rate — actual spending is lumpy and may be faster.<br>
  · CLN / warrant detection is indirect (debt/equity proxy) — check full filing for actual instruments.<br>
  · Stop prices are estimated from expected entry (gap-adjusted) — NOT yesterday's close.<br>
  · AIM stocks gap at open — stop orders may execute significantly below your level.<br>
  · Grade A/B = better fundamental health; it does NOT mean the trade is safe or profitable.<br>
  · NOT FINANCIAL ADVICE. AIM stocks carry extreme risk including 100% loss of capital.
</div>

<script>
function fc(type,btn){{
  document.querySelectorAll('.fb').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(card=>{{
    const comb  =parseInt(card.dataset.comb||0);
    const rr    =parseFloat(card.dataset.rr||0);
    const rns   =card.dataset.rns==='1';
    const sqz   =card.dataset.squeeze==='1';
    const liq   =card.dataset.liquid==='1';
    const vb    =card.dataset.volbuild==='1';
    const grade =card.dataset.grade||'F';
    const block =card.dataset.block==='1';
    let show=true;
    if(type==='rns')    show=rns;
    if(type==='top')    show=comb>=10;
    if(type==='goodrr') show=rr>=2;
    if(type==='squeeze')show=sqz;
    if(type==='ab')     show=grade==='A'||grade==='B';
    if(type==='liquid') show=liq;
    if(type==='volbuild')show=vb;
    if(type==='noblock')show=!block;
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
    now_l     = datetime.now(LONDON_TZ)
    date_str  = now_l.strftime("%A %d %B %Y")
    scan_time = now_l.strftime("%H:%M:%S")
    file_date = now_l.strftime("%Y-%m-%d")

    print("═" * 68)
    print("  LSE PRE-MARKET SCANNER v4.0 — Technical + Fundamental")
    print(f"  {date_str}  |  {scan_time} London")
    print("  Score = Tech(0–16) + Fundamental Health(0–6) = 0–22")
    print("═" * 68)

    # 1. RNS
    print("\n[1/4] Fetching RNS announcements...")
    rns_map, rns_feed_ok = fetch_rns_today()
    rns_symbols = set(rns_map.keys())
    print(f"  ✓ {len(rns_map)} RNS tickers  |  Feed: {'LIVE' if rns_feed_ok else 'OFFLINE'}")

    # 2. Build symbol list
    print("\n[2/4] Building symbol list...")
    all_syms = list(dict.fromkeys(list(rns_symbols) + WATCHLIST_YAHOO))
    print(f"  ✓ {len(all_syms)} symbols to scan")

    # 3. Download (1-year history + fundamentals)
    print(f"\n[3/4] Downloading 1-year history + fundamentals ({MAX_WORKERS} workers)...")
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
    print(f"\n  ✓ Valid: {len(all_data)} symbols")

    # 4. Score all tickers
    print("\n[4/4] Scoring (Technical + Fundamental + Entry-adjusted stops)...")
    results   = []
    n_blocked = 0
    n_illiq   = 0

    for d in all_data:
        sym = d["symbol"]

        # Liquidity gate
        if d["avg_daily_gbp"] < MIN_AVG_DAILY_GBP and sym not in rns_symbols:
            n_illiq += 1
            continue

        # ATR gate
        if d["atr_pct"] < MIN_ATR_PCT and sym not in rns_symbols:
            continue

        # Market cap gate
        mc = d["mktcap_gbp"]
        if mc is not None and mc > MAX_MKTCAP_GBP and sym not in rns_symbols:
            continue

        rns_info = rns_map.get(sym)
        rns_tier = (rns_info.get("tier", 0) if rns_info and not rns_info.get("negative") else 0)

        # Fundamental hard block — only bypass for genuine Tier-1 RNS
        if d["fund_hard_block"] and rns_tier < 1:
            n_blocked += 1
            continue

        ts, fs, comb, reasons, conf, pred, rr, sd = score_predictive(
            d, rns_info, rns_feed_ok
        )

        if comb < MIN_COMBINED_SCORE and sym not in rns_symbols:
            continue

        results.append({
            "data":           d,
            "tech_score":     ts,
            "fund_score":     fs,
            "combined":       comb,
            "reasons":        reasons,
            "confidence":     conf,
            "predicted_move": pred,
            "rr_ratio":       rr,
            "stop_data":      sd,
            "has_rns":        sym in rns_symbols,
        })

    results.sort(key=lambda x: (-x["combined"], -(x.get("rr_ratio") or 0), -x["data"]["atr_pct"]))
    print(f"  ✓ {len(results)} candidates passed all filters")
    print(f"  ✓ {n_blocked} hard-blocked (critical fundamentals, no Tier-1 RNS)")
    print(f"  ✓ {n_illiq} filtered (illiquid < £{MIN_AVG_DAILY_GBP/1e3:.0f}k/day)")

    if results:
        print("\n  ┌── TOP CANDIDATES ────────────────────────────────────────────────┐")
        for r in results[:12]:
            d   = r["data"]
            mc  = f"£{d['mktcap_gbp']/1e6:.0f}M" if d["mktcap_gbp"] else "N/A  "
            rr  = f"R/R {r['rr_ratio']:.1f}:1" if r["rr_ratio"] else "R/R N/A  "
            sl  = f"SL −{r['stop_data']['rec_stop_pct']:.1f}%"
            fl  = " ◀RNS" if r["has_rns"] else ""
            trn = "↑" if d["ema_aligned"] else "→" if d["ema_uptrend"] else "↓"
            grd = d["fund_data"]["fund_grade"]
            dlr = d["fund_data"]["dilution_risk"][0]  # first letter
            print(f"  │ {d['symbol'].replace('.L',''):<8} "
                  f"T{r['tech_score']:>2}+F{r['fund_score']}={r['combined']:>2}/22  "
                  f"ATR {d['atr_pct']:.1f}%  {sl:<9} {rr:<11} "
                  f"Grd:{grd} Dil:{dlr} {trn}{fl}")
        print("  └──────────────────────────────────────────────────────────────────┘")

    # 5. HTML
    print("\nGenerating HTML report...")
    html    = build_html(results, rns_map, rns_feed_ok, scan_time, date_str)
    outfile = f"lse_predict_{file_date}.html"
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Saved: {outfile}")

    try:
        webbrowser.open(f"file://{os.path.abspath(outfile)}")
        print("  ✓ Opened in browser")
    except Exception:
        print(f"  ⚠ Open manually: {os.path.abspath(outfile)}")

    print("\n" + "═" * 68)
    if results:
        top = results[0]
        d   = top["data"]
        rr  = f"R/R {top['rr_ratio']:.1f}:1" if top["rr_ratio"] else ""
        print(f"  Best: {d['symbol'].replace('.L','')} | "
              f"T{top['tech_score']}+F{top['fund_score']}={top['combined']}/22 | "
              f"{top['confidence']} | SL −{top['stop_data']['rec_stop_pct']:.1f}% | {rr}")
        cr = d['fund_data']['cash_runway_qtrs']
        cr_str = "∞" if (cr or 0) >= 99 else f"{cr:.1f}Q" if cr else "N/A"
        print(f"  Fund Grade: {d['fund_data']['fund_grade']} | "
              f"Dilution: {d['fund_data']['dilution_risk']} | "
              f"Cash runway: {cr_str}")
        if top["has_rns"]:
            ri = rns_map.get(d["symbol"])
            if ri: print(f"  RNS: {ri['label']} — {ri['headline'][:65]}")
    else:
        print("  No candidates. Lower MIN_COMBINED_SCORE or check RNS feed.")
    print("═" * 68 + "\n")


if __name__ == "__main__":
    main()
