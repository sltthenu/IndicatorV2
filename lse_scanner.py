#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  LSE DAILY 20%+ MOVER SCANNER                                    ║
║  Finds AIM / small-cap candidates every morning                  ║
║  No API key needed — uses Yahoo Finance (free)                   ║
║                                                                  ║
║  INSTALL (run once):                                             ║
║    pip install yfinance requests pandas                          ║
║                                                                  ║
║  RUN DAILY (07:00–08:00 London time):                            ║
║    python lse_scanner.py                                         ║
║                                                                  ║
║  Output: lse_scan_YYYY-MM-DD.html  (opens in your browser)      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import yfinance as yf
import requests
import json
import time
import webbrowser
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo   # Python 3.9+  (or: pip install backports.zoneinfo)
import concurrent.futures
import warnings
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
MAX_WORKERS     = 12      # parallel fetches — keep ≤ 15 to avoid Yahoo rate limit
MIN_SCORE       = 3       # minimum score (out of 10) to show in report
TOP_N           = 50      # scan top-N Yahoo Finance UK gainers
VOL_SPIKE_MULT  = 1.5     # volume must be this × 20-day avg to count as spike
DISP_ATR_MULT   = 1.5     # displacement: body must be > this × ATR
MIN_GAIN_PCT    = 3.0     # only score stocks already up ≥ 3% today
MAX_MKTCAP_GBP  = 500e6   # ignore anything above this market cap (£500m)

LONDON_TZ = ZoneInfo("Europe/London")

# ─────────────────────────────────────────────────────────────────────────────
#  EXTENDED AIM / SMALL-CAP SEED LIST
#  Used as fallback and supplement to Yahoo screener.
#  Add your own tickers at the bottom of this list.
# ─────────────────────────────────────────────────────────────────────────────
AIM_SEED = [
    # Biotech / Pharma
    "AVCT","BVXP","CEL","CLI","CLIN","CRSO","CRW","DCAN","ECHO","EMIS",
    "EVO","FRP","GBG","GCM","GRG","GRMN","GTI","HAT","HBR","HDIV",
    "HGT","HYR","IGP","IKA","IMM","INFA","IOFN","IQE","JOG","KBT",
    "KORE","LBRT","LIO","LWI","MACF","MAIA","MCB","MCLS","MDC","MED",
    "MKA","MMX","MNRZ","MOTS","MRC","MSTR","MTI","MXCT","NANO","NCZ",
    # Mining / Resources
    "AAZ","ABCA","ABM","ACL","ADT","AFG","AFPO","AGOL","AIM","ALBA",
    "AMER","AMI","AMMO","AMS","AMYT","ANX","AOG","APH","AQX","ARBB",
    "ARCM","ARG","ARL","ARM","ARML","ARNO","ARP","ARR","ARS","ART",
    "ASA","ASC","ASL","AST","ATM","ATOS","ATR","ATS","ATT","AUE",
    "AUG","AUGM","AUR","AUS","AUT","AVN","AVP","AVR","AVRO","AVT",
    "BKT","BLV","BMN","BON","BPM","BRCK","BRK","BRM","BRSC","BRT",
    "CAD","CAL","CAP","CAR","CASK","CAT","CBX","CCC","CCR","CCT",
    "CDL","CEIN","CEK","CEM","CEO","CEQ","CER","CERE","CERT","CET",
    "CFX","CGH","CGR","CGS","CGT","CHF","CHL","CHP","CHR","CHS",
    "DEC","DEL","DEM","DEV","DEX","DGO","DGR","DGS","DGT","DHI",
    "DIA","DIB","DIG","DIL","DIM","DIN","DIS","DIT","DIV","DIX",
    "ECR","ECT","EDL","EGO","EKT","ELC","ELG","ELR","EMS","EMX",
    "ENS","ENT","EPO","EPS","EPT","ERG","ERM","ERN","ERO","ERP",
    # Tech / Digital
    "ALFA","ALT","AMER","AMS","ANP","AOF","APC","APO","APP","APPH",
    "BIG","BIOG","BIOP","BIOT","BIT","BKPB","BLG","BLI","BLS","BMT",
    "BOO","BOOM","BRN","BSE","BTG","BUR","BWA","CAB","CAI","CAL",
    "CAMB","CAN","CANO","CAO","CARD","CARE","CARK","CARS","CARV",
    "FEYE","FGEN","FGH","FGI","FGL","FGLJ","FGP","FGR","FGRP","FGS",
    "GAM","GAME","GAN","GAP","GAR","GARN","GAS","GAT","GAW","GAX",
    "HAV","HAWK","HAY","HAYD","HAYS","HAZ","HBT","HCL","HCM","HCS",
    "IFP","IGC","IGI","IGL","IGM","IGN","IGO","IGP","IGR","IHC",
    "KAV","KBIO","KCBG","KCG","KCI","KCLI","KCS","KCT","KED","KEF",
    "LAD","LAM","LAP","LAR","LARK","LAS","LASL","LAT","LAU","LAW",
    "MCK","MCKN","MCL","MCM","MCMJ","MCN","MCP","MCPH","MCR","MCS",
    "NANO","NAP","NAR","NAS","NAST","NAT","NATC","NATH","NATS","NAV",
    "OXB","OXI","OXL","OXM","OXP","OXR","OXS","OXSM","OXT","OXY",
    "PAD","PAF","PAG","PAH","PAI","PAJ","PAK","PAL","PAM","PAN",
    "QRT","QRTL","QRX","QSP","QTX","QXL","QXP","QXR","QXS","QXT",
    "RAD","RAF","RAG","RAI","RAJ","RAK","RAM","RAN","RAO","RAP",
    "SLP","SLPE","SLR","SLS","SLT","SLV","SLW","SLX","SLY","SLZ",
    "TAM","TAN","TAP","TAR","TARG","TAS","TAT","TAV","TAW","TAX",
    "UAI","UAJ","UAK","UAL","UAM","UAN","UAO","UAP","UAR","UAS",
    "VAL","VAM","VAN","VAP","VAR","VARE","VAS","VAT","VAU","VAV",
    "WAL","WAM","WAN","WAP","WAR","WARE","WAS","WAT","WAV","WAX",
    "ZINC","ZIN","ZIO","ZIP","ZIT","ZIV","ZIX","ZJO","ZJT","ZKL",
    # Well-known AIM names worth watching
    "ASOS","BOO","PETS","TED","WHR","GFRD","GTI","HYD","JOG","MACF",
    "MAIR","MCLS","MCX","MED","MLIN","MOON","MTL","MYI","MYRE","MYSL",
    "SDX","SHRE","SOS","STB","STI","TBT","TCG","TFIF","TGRE","THT",
]

# De-duplicate and format as Yahoo Finance symbols (append .L for LSE)
def to_yahoo(ticker: str) -> str:
    t = ticker.strip().upper()
    if not t.endswith(".L"):
        t = t + ".L"
    return t

SEED_YAHOO = list(dict.fromkeys(to_yahoo(t) for t in AIM_SEED))


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1: Fetch Yahoo Finance UK Top Gainers (screener, no API key)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_yahoo_uk_gainers(count: int = TOP_N) -> list[str]:
    """
    Calls Yahoo Finance's public predefined screener for day gainers,
    filtered to GB region. Returns list of Yahoo ticker symbols.
    """
    url = (
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        f"?scrIds=day_gainers&region=GB&lang=en-GB&count={count}&offset=0"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        symbols = [q["symbol"] for q in quotes if "symbol" in q]
        print(f"  ✓ Yahoo screener returned {len(symbols)} UK gainers")
        return symbols
    except Exception as e:
        print(f"  ⚠ Yahoo screener failed ({e}), using seed list only")
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2: Fetch individual ticker data via yfinance
# ─────────────────────────────────────────────────────────────────────────────
def fetch_ticker_data(symbol: str) -> dict | None:
    """
    Downloads price history and info for one ticker.
    Returns a dict with computed metrics, or None if data unavailable.
    """
    try:
        tk = yf.Ticker(symbol)

        # Grab 60 days of daily data for ATR + volume average
        hist = tk.history(period="60d", auto_adjust=True)
        if hist.empty or len(hist) < 10:
            return None

        info = {}
        try:
            info = tk.info or {}
        except Exception:
            pass

        latest     = hist.iloc[-1]
        prev       = hist.iloc[-2] if len(hist) >= 2 else hist.iloc[-1]

        open_p     = float(latest.get("Open",  latest["Close"]))
        high_p     = float(latest.get("High",  latest["Close"]))
        low_p      = float(latest.get("Low",   latest["Close"]))
        close_p    = float(latest["Close"])
        volume     = float(latest.get("Volume", 0))
        prev_close = float(prev["Close"])

        # % change today
        pct_change = (close_p - prev_close) / prev_close * 100 if prev_close else 0

        # 20-day average volume
        vol_series = hist["Volume"].dropna()
        avg_vol_20 = float(vol_series.iloc[-21:-1].mean()) if len(vol_series) >= 21 else float(vol_series.mean())
        vol_ratio  = volume / avg_vol_20 if avg_vol_20 > 0 else 0

        # ATR(14) — simplified True Range average
        tr_list = []
        for i in range(1, min(15, len(hist))):
            h = float(hist.iloc[-i]["High"])
            l = float(hist.iloc[-i]["Low"])
            pc = float(hist.iloc[-(i+1)]["Close"]) if i+1 <= len(hist) else l
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr14 = sum(tr_list) / len(tr_list) if tr_list else 0

        # ATR as % of price — volatility proxy
        atr_pct = atr14 / close_p * 100 if close_p > 0 else 0

        # Displacement: today's body vs ATR
        body       = abs(close_p - open_p)
        displaced  = body > atr14 * DISP_ATR_MULT and close_p > high_p  # big bull candle closing at highs

        # 50-day trend: close > 20-day EMA > 50-day EMA
        closes = hist["Close"].dropna()
        ema20  = float(closes.ewm(span=20).mean().iloc[-1])  if len(closes) >= 20 else None
        ema50  = float(closes.ewm(span=50).mean().iloc[-1])  if len(closes) >= 50 else None
        uptrend = (ema20 is not None and ema50 is not None
                   and close_p > ema20 > ema50)

        # 52-week range position (discount = lower 50%)
        hi52 = float(closes.max())
        lo52 = float(closes.min())
        range52 = hi52 - lo52
        range_pos = (close_p - lo52) / range52 * 100 if range52 > 0 else 50
        in_discount = range_pos < 50

        # VWAP (current day approximation using today's OHLC)
        vwap_approx = (high_p + low_p + close_p) / 3
        above_vwap  = close_p >= vwap_approx

        # Market cap
        mktcap = info.get("marketCap", None)
        if mktcap is None:
            shares = info.get("sharesOutstanding", None)
            mktcap = close_p * shares if shares else None

        # Convert GBX (pence) to GBP if needed
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
            "symbol":       symbol,
            "name":         short_name,
            "currency":     currency,
            "close":        close_p,
            "open":         open_p,
            "high":         high_p,
            "low":          low_p,
            "pct_change":   pct_change,
            "volume":       volume,
            "avg_vol_20":   avg_vol_20,
            "vol_ratio":    vol_ratio,
            "atr14":        atr14,
            "atr_pct":      atr_pct,
            "displaced":    displaced,
            "uptrend":      uptrend,
            "ema20":        ema20,
            "ema50":        ema50,
            "in_discount":  in_discount,
            "range_pos":    range_pos,
            "above_vwap":   above_vwap,
            "mktcap_gbp":   mktcap_gbp,
            "is_aim":       is_aim,
            "sector":       info.get("sector", ""),
            "industry":     info.get("industry", ""),
        }
    except Exception as e:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3: Scoring (10 points max)
# ─────────────────────────────────────────────────────────────────────────────
def score_ticker(d: dict) -> tuple[int, list[str]]:
    """
    Returns (score_out_of_10, list_of_reasons).
    """
    s = 0
    reasons = []

    # 1. Strong intraday gain (2 pts)
    pc = d["pct_change"]
    if pc >= 20:
        s += 2; reasons.append(f"🔥 Up {pc:.1f}% today (20%+ threshold met)")
    elif pc >= 10:
        s += 2; reasons.append(f"📈 Up {pc:.1f}% today (strong)")
    elif pc >= 5:
        s += 1; reasons.append(f"📈 Up {pc:.1f}% today")
    elif pc >= 3:
        s += 0; reasons.append(f"Up {pc:.1f}% — weak, needs catalyst")

    # 2. Institutional volume spike (2 pts)
    vr = d["vol_ratio"]
    if vr >= 4:
        s += 2; reasons.append(f"🔥 Volume {vr:.1f}× average (institutional)")
    elif vr >= VOL_SPIKE_MULT:
        s += 1; reasons.append(f"📊 Volume {vr:.1f}× average (above avg)")

    # 3. Uptrend on daily (1 pt)
    if d["uptrend"]:
        s += 1; reasons.append("✅ Daily uptrend: Close > EMA20 > EMA50")

    # 4. Price above intraday VWAP (1 pt)
    if d["above_vwap"]:
        s += 1; reasons.append("✅ Price above intraday VWAP")

    # 5. Discount zone — in lower 50% of 52-week range (1 pt)
    if d["in_discount"]:
        s += 1; reasons.append(f"✅ In discount zone ({d['range_pos']:.0f}% of 52w range)")

    # 6. High volatility stock — structural ability to move 20% (1 pt)
    if d["atr_pct"] >= 5:
        s += 1; reasons.append(f"✅ ATR% {d['atr_pct']:.1f}% — structurally volatile")
    elif d["atr_pct"] >= 3:
        s += 0; reasons.append(f"ATR% {d['atr_pct']:.1f}% — moderate volatility")

    # 7. Displacement candle (1 pt)
    if d["displaced"]:
        s += 1; reasons.append("✅ Displacement candle — institutional momentum")

    # 8. Small market cap / AIM (1 pt)
    mc = d["mktcap_gbp"]
    if mc is not None and mc < 50e6:
        s += 1; reasons.append(f"✅ Micro-cap £{mc/1e6:.0f}m — high move potential")
    elif mc is not None and mc < MAX_MKTCAP_GBP:
        s += 0; reasons.append(f"Market cap £{mc/1e6:.0f}m — small cap")
    elif mc is None:
        reasons.append("⚠ Market cap unknown — check manually")

    return min(s, 10), reasons


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4: Build the HTML report
# ─────────────────────────────────────────────────────────────────────────────
def build_html(results: list[dict], scan_time: str, date_str: str) -> str:

    def score_colour(s):
        if s >= 8: return "#00e676"
        if s >= 6: return "#f5a623"
        if s >= 4: return "#29b6f6"
        return "#546e7a"

    def pct_colour(p):
        if p >= 20: return "#00e676"
        if p >= 10: return "#f5a623"
        if p >= 5:  return "#29b6f6"
        return "#546e7a"

    def fmt_price(p, ccy):
        if ccy == "GBp":
            return f"{p:.2f}p"
        return f"£{p:.4f}" if p < 1 else f"£{p:.2f}"

    def fmt_cap(mc):
        if mc is None: return "N/A"
        if mc >= 1e9:  return f"£{mc/1e9:.1f}B"
        return f"£{mc/1e6:.0f}M"

    # Sort by score desc, then % change desc
    results.sort(key=lambda x: (-x["score"], -x["data"]["pct_change"]))

    rows_html = ""
    for r in results:
        d = r["data"]
        sc = r["score"]
        reasons = r["reasons"]
        sym_clean = d["symbol"].replace(".L", "")
        sc_col = score_colour(sc)
        pc_col = pct_colour(d["pct_change"])
        vr_col = "#00e676" if d["vol_ratio"] >= 4 else ("#f5a623" if d["vol_ratio"] >= 1.5 else "#546e7a")
        reasons_html = "".join(f'<div class="reason">{rr}</div>' for rr in reasons)
        tv_url  = f"https://www.tradingview.com/chart/?symbol=LSE%3A{sym_clean}"
        rns_url = f"https://www.londonstockexchange.com/news?tab=news-explorer&search={sym_clean}"
        adv_url = f"https://www.advfn.com/stock-market/LSE/{sym_clean}/share-price"
        yhoo_url= f"https://finance.yahoo.com/quote/{d['symbol']}"

        rows_html += f"""
        <div class="card {'top-card' if sc >= 7 else ''}">
          <div class="card-top">
            <div class="card-left">
              <div class="sym">{sym_clean}
                <span class="exchange-badge">{'AIM' if d['is_aim'] else 'LSE'}</span>
              </div>
              <div class="company">{d['name']}</div>
              <div class="meta">{d['sector']} {'· ' + d['industry'] if d['industry'] else ''}</div>
            </div>
            <div class="card-right">
              <div class="pct-change" style="color:{pc_col}">{d['pct_change']:+.1f}%</div>
              <div class="price">{fmt_price(d['close'], d['currency'])}</div>
              <div class="mkcap">{fmt_cap(d['mktcap_gbp'])}</div>
            </div>
            <div class="score-col">
              <div class="score-num" style="color:{sc_col}">{sc}</div>
              <div class="score-label">/ 10</div>
              <div class="score-bar-wrap">
                <div class="score-bar" style="width:{sc*10}%; background:{sc_col}"></div>
              </div>
            </div>
          </div>
          <div class="metrics-row">
            <div class="metric">
              <span class="m-label">VOL RATIO</span>
              <span class="m-val" style="color:{vr_col}">{d['vol_ratio']:.1f}×</span>
            </div>
            <div class="metric">
              <span class="m-label">ATR%</span>
              <span class="m-val">{d['atr_pct']:.1f}%</span>
            </div>
            <div class="metric">
              <span class="m-label">52W POS</span>
              <span class="m-val">{d['range_pos']:.0f}%</span>
            </div>
            <div class="metric">
              <span class="m-label">UPTREND</span>
              <span class="m-val" style="color:{'#00e676' if d['uptrend'] else '#546e7a'}">
                {'YES' if d['uptrend'] else 'NO'}
              </span>
            </div>
            <div class="metric">
              <span class="m-label">ABOVE VWAP</span>
              <span class="m-val" style="color:{'#00e676' if d['above_vwap'] else '#546e7a'}">
                {'YES' if d['above_vwap'] else 'NO'}
              </span>
            </div>
            <div class="metric">
              <span class="m-label">DISPLACED</span>
              <span class="m-val" style="color:{'#f5a623' if d['displaced'] else '#546e7a'}">
                {'YES' if d['displaced'] else 'NO'}
              </span>
            </div>
          </div>
          <div class="reasons">{reasons_html}</div>
          <div class="links">
            <a href="{tv_url}"  target="_blank" class="link-btn chart-btn">📈 Chart</a>
            <a href="{rns_url}" target="_blank" class="link-btn rns-btn">📰 RNS</a>
            <a href="{adv_url}" target="_blank" class="link-btn adv-btn">📊 ADVFN</a>
            <a href="{yhoo_url}" target="_blank" class="link-btn yh-btn">💹 Yahoo</a>
          </div>
        </div>
        """

    top_count   = sum(1 for r in results if r["score"] >= 7)
    total_count = len(results)
    avg_score   = sum(r["score"] for r in results) / total_count if total_count else 0
    best_gain   = max((r["data"]["pct_change"] for r in results), default=0)

    now_london = datetime.now(LONDON_TZ)
    sess_label = "CLOSED"
    sess_col   = "#546e7a"
    hm = now_london.hour * 60 + now_london.minute
    if   480 <= hm < 570:  sess_label, sess_col = "LSE OPEN KZ", "#00e676"
    elif 570 <= hm < 810:  sess_label, sess_col = "MID SESSION",  "#29b6f6"
    elif 810 <= hm < 900:  sess_label, sess_col = "US OVERLAP KZ","#00e676"
    elif 900 <= hm < 930:  sess_label, sess_col = "MID SESSION",  "#29b6f6"
    elif 930 <= hm < 990:  sess_label, sess_col = "LSE CLOSE KZ", "#00e676"
    elif 480 <= hm < 990:  sess_label, sess_col = "MARKET OPEN",  "#29b6f6"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LSE Scanner — {date_str}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600&family=Barlow+Condensed:wght@600;700&display=swap');

  :root {{
    --bg:#080c10; --bg2:#0d1318; --bg3:#111920; --border:#1c2e3a;
    --amber:#f5a623; --green:#00e676; --red:#ff3d57; --blue:#29b6f6;
    --gray:#546e7a; --text:#c8d8e4; --dim:#4a6478;
    --mono:'Share Tech Mono',monospace; --sans:'Barlow',sans-serif;
    --cond:'Barlow Condensed',sans-serif;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text); font-family:var(--sans); min-height:100vh; }}
  body {{ background-image:
    radial-gradient(ellipse at 10% 0%,rgba(245,166,35,.05) 0%,transparent 55%),
    radial-gradient(ellipse at 90% 100%,rgba(0,230,118,.04) 0%,transparent 55%); }}

  .header {{
    padding:16px 28px; border-bottom:1px solid var(--border);
    background:rgba(13,19,24,.98); display:flex; align-items:center;
    justify-content:space-between; position:sticky; top:0; z-index:50;
  }}
  .logo {{ font-family:var(--mono); font-size:20px; color:var(--amber); letter-spacing:2px; }}
  .logo span {{ color:var(--dim); }}
  .header-right {{ text-align:right; font-family:var(--mono); font-size:11px; color:var(--dim); line-height:1.8; }}
  .sess {{ font-size:12px; font-weight:bold; letter-spacing:1px; }}

  .stats-bar {{
    display:flex; gap:0; border-bottom:1px solid var(--border);
    background:var(--bg2);
  }}
  .stat {{
    flex:1; text-align:center; padding:12px 8px;
    border-right:1px solid var(--border);
  }}
  .stat:last-child {{ border-right:none; }}
  .stat-v {{ font-family:var(--mono); font-size:22px; color:var(--amber); display:block; }}
  .stat-l {{ font-family:var(--cond); font-size:10px; color:var(--dim); letter-spacing:1.5px; text-transform:uppercase; }}

  .filter-bar {{
    padding:12px 28px; background:var(--bg2); border-bottom:1px solid var(--border);
    display:flex; gap:10px; align-items:center; flex-wrap:wrap;
  }}
  .filter-label {{ font-family:var(--cond); font-size:11px; color:var(--dim); letter-spacing:1px; text-transform:uppercase; }}
  .filter-btn {{
    font-family:var(--cond); font-size:12px; font-weight:700; letter-spacing:1px;
    padding:5px 14px; border:1px solid var(--border); border-radius:2px;
    background:var(--bg3); color:var(--dim); cursor:pointer; transition:all .15s;
  }}
  .filter-btn:hover, .filter-btn.active {{
    border-color:var(--amber); color:var(--amber); background:rgba(245,166,35,.08);
  }}

  .grid {{ padding:20px 28px; display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr)); gap:14px; }}

  .card {{
    background:var(--bg2); border:1px solid var(--border); border-radius:4px;
    overflow:hidden; transition:border-color .2s, transform .15s;
  }}
  .card:hover {{ border-color:rgba(245,166,35,.35); transform:translateY(-1px); }}
  .top-card {{ border-color:rgba(0,230,118,.25); }}
  .top-card:hover {{ border-color:rgba(0,230,118,.5); }}

  .card-top {{ display:flex; padding:14px; gap:14px; align-items:flex-start; }}
  .card-left {{ flex:1; }}
  .sym {{
    font-family:var(--mono); font-size:20px; color:var(--amber);
    letter-spacing:1.5px; display:flex; align-items:center; gap:8px;
  }}
  .exchange-badge {{
    font-family:var(--cond); font-size:10px; color:var(--dim);
    border:1px solid var(--border); padding:1px 6px; border-radius:2px;
    letter-spacing:1px; vertical-align:middle;
  }}
  .company {{ font-size:12px; color:var(--dim); margin-top:3px; }}
  .meta {{ font-size:10px; color:rgba(74,100,120,.7); margin-top:2px; }}
  .card-right {{ text-align:right; }}
  .pct-change {{ font-family:var(--mono); font-size:22px; font-weight:bold; }}
  .price {{ font-family:var(--mono); font-size:13px; color:var(--text); }}
  .mkcap {{ font-size:11px; color:var(--dim); margin-top:2px; }}
  .score-col {{ text-align:center; min-width:50px; }}
  .score-num {{ font-family:var(--mono); font-size:26px; font-weight:bold; line-height:1; }}
  .score-label {{ font-size:10px; color:var(--dim); font-family:var(--mono); }}
  .score-bar-wrap {{ width:40px; height:3px; background:var(--bg3); border-radius:2px; margin:4px auto 0; overflow:hidden; }}
  .score-bar {{ height:100%; border-radius:2px; transition:width .4s; }}

  .metrics-row {{
    display:flex; gap:0; border-top:1px solid var(--border);
    border-bottom:1px solid var(--border); background:var(--bg3);
  }}
  .metric {{
    flex:1; padding:7px 6px; text-align:center;
    border-right:1px solid var(--border);
  }}
  .metric:last-child {{ border-right:none; }}
  .m-label {{ display:block; font-family:var(--cond); font-size:9px; color:var(--dim); letter-spacing:1px; text-transform:uppercase; }}
  .m-val   {{ display:block; font-family:var(--mono); font-size:13px; color:var(--text); margin-top:2px; }}

  .reasons {{ padding:10px 14px; }}
  .reason {{ font-size:11px; color:var(--dim); padding:2px 0; line-height:1.5; }}

  .links {{
    padding:8px 14px 12px; display:flex; gap:8px; flex-wrap:wrap;
    border-top:1px solid rgba(28,46,58,.5);
  }}
  .link-btn {{
    font-family:var(--cond); font-size:11px; font-weight:700; letter-spacing:1px;
    padding:4px 12px; border-radius:2px; text-decoration:none;
    border:1px solid; transition:all .15s;
  }}
  .chart-btn {{ color:#29b6f6; border-color:rgba(41,182,246,.3); background:rgba(41,182,246,.07); }}
  .chart-btn:hover {{ background:rgba(41,182,246,.15); }}
  .rns-btn   {{ color:#f5a623; border-color:rgba(245,166,35,.3); background:rgba(245,166,35,.07); }}
  .rns-btn:hover {{ background:rgba(245,166,35,.15); }}
  .adv-btn   {{ color:#ce93d8; border-color:rgba(206,147,216,.3); background:rgba(206,147,216,.07); }}
  .adv-btn:hover {{ background:rgba(206,147,216,.15); }}
  .yh-btn    {{ color:#00e676; border-color:rgba(0,230,118,.3); background:rgba(0,230,118,.07); }}
  .yh-btn:hover {{ background:rgba(0,230,118,.15); }}

  .empty {{ text-align:center; padding:60px 20px; color:var(--dim); font-family:var(--mono); font-size:14px; }}
  .footer {{
    margin-top:40px; padding:20px 28px; border-top:1px solid var(--border);
    font-size:11px; color:var(--dim); font-family:var(--mono); line-height:2;
    background:var(--bg2);
  }}
  ::-webkit-scrollbar {{ width:4px; }} ::-webkit-scrollbar-thumb {{ background:var(--border); }}

  .hidden {{ display:none !important; }}
  @media(max-width:500px) {{ .grid {{ padding:12px; grid-template-columns:1fr; }} }}
</style>
</head>
<body>

<div class="header">
  <div class="logo">IF<span>·</span>LSE<span>·</span>SCAN</div>
  <div class="header-right">
    <div>{date_str} &nbsp;|&nbsp; Scanned at {scan_time} London time</div>
    <div class="sess" style="color:{sess_col}">● {sess_label}</div>
  </div>
</div>

<div class="stats-bar">
  <div class="stat">
    <span class="stat-v">{total_count}</span>
    <span class="stat-l">Candidates</span>
  </div>
  <div class="stat">
    <span class="stat-v" style="color:var(--green)">{top_count}</span>
    <span class="stat-l">Score ≥ 7</span>
  </div>
  <div class="stat">
    <span class="stat-v" style="color:var(--amber)">{best_gain:.1f}%</span>
    <span class="stat-l">Best Gain</span>
  </div>
  <div class="stat">
    <span class="stat-v">{avg_score:.1f}</span>
    <span class="stat-l">Avg Score</span>
  </div>
</div>

<div class="filter-bar">
  <span class="filter-label">FILTER:</span>
  <button class="filter-btn active" onclick="filterCards('all', this)">ALL ({total_count})</button>
  <button class="filter-btn" onclick="filterCards('top', this)">SCORE ≥ 7</button>
  <button class="filter-btn" onclick="filterCards('vol', this)">VOL SPIKE 4×+</button>
  <button class="filter-btn" onclick="filterCards('pct20', this)">20%+ TODAY</button>
  <button class="filter-btn" onclick="filterCards('uptrend', this)">UPTREND</button>
</div>

<div class="grid" id="grid">
  {rows_html if rows_html.strip() else '<div class="empty">No candidates found matching criteria.<br>Try running between 08:00–16:30 London time, or lower MIN_SCORE / MIN_GAIN_PCT in config.</div>'}
</div>

<div class="footer">
  ⚠ This scanner uses end-of-day price data from Yahoo Finance and does not reflect real-time intraday moves.
  Run at 08:00–08:30 for best results. Always verify with RNS before trading.
  Stocks listed may include FTSE as well as AIM — check exchange badge on each card.
  This is not financial advice.
</div>

<script>
  function filterCards(type, btn) {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.card').forEach(card => {{
      const score  = parseInt(card.querySelector('.score-num')?.textContent || '0');
      const pct    = parseFloat(card.querySelector('.pct-change')?.textContent || '0');
      const volEl  = card.querySelectorAll('.m-val')[0];
      const volRat = volEl ? parseFloat(volEl.textContent) : 0;
      const upEl   = card.querySelectorAll('.m-val')[3];
      const up     = upEl ? upEl.textContent.trim() === 'YES' : false;
      let show = true;
      if (type === 'top')    show = score >= 7;
      if (type === 'vol')    show = volRat >= 4;
      if (type === 'pct20')  show = pct >= 20;
      if (type === 'uptrend') show = up;
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

    print("═" * 60)
    print("  IF·LSE·SCAN — Daily 20%+ Mover Hunter")
    print(f"  {date_str}  |  {scan_time} London")
    print("═" * 60)

    # ── Step 1: Get candidates
    print("\n[1/4] Fetching Yahoo Finance UK gainers...")
    yahoo_syms = fetch_yahoo_uk_gainers(TOP_N)

    # Merge with seed list, deduplicate
    all_syms = list(dict.fromkeys(yahoo_syms + SEED_YAHOO))
    print(f"  ✓ Total symbols to scan: {len(all_syms)}")

    # ── Step 2: Download data in parallel
    print(f"\n[2/4] Downloading ticker data ({MAX_WORKERS} parallel workers)...")
    all_data = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_map = {ex.submit(fetch_ticker_data, sym): sym for sym in all_syms}
        for future in concurrent.futures.as_completed(future_map):
            done += 1
            result = future.result()
            if result is not None:
                all_data.append(result)
            # Progress
            if done % 20 == 0 or done == len(all_syms):
                pct = done / len(all_syms) * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(f"  [{bar}] {done}/{len(all_syms)}  ({len(all_data)} valid)", end="\r")
    print(f"\n  ✓ Valid data received for {len(all_data)} symbols")

    # ── Step 3: Filter and score
    print("\n[3/4] Scoring candidates...")
    results = []
    for d in all_data:
        if d["pct_change"] < MIN_GAIN_PCT:
            continue
        score, reasons = score_ticker(d)
        if score < MIN_SCORE:
            continue
        results.append({"data": d, "score": score, "reasons": reasons})

    results.sort(key=lambda x: (-x["score"], -x["data"]["pct_change"]))
    print(f"  ✓ {len(results)} candidates scored ≥ {MIN_SCORE}/10 with gain ≥ {MIN_GAIN_PCT}%")

    if results:
        print("\n  TOP CANDIDATES:")
        for r in results[:10]:
            d = r["data"]
            sym = d["symbol"].replace(".L", "")
            print(f"    {sym:<8} {d['pct_change']:>+6.1f}%  vol×{d['vol_ratio']:.1f}  score {r['score']}/10  {d['name'][:30]}")

    # ── Step 4: Generate HTML
    print("\n[4/4] Generating HTML report...")
    html = build_html(results, scan_time, date_str)
    outfile = f"lse_scan_{file_date}.html"
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Saved: {outfile}")

    # Open in browser
    try:
        webbrowser.open(f"file://{os.path.abspath(outfile)}")
        print("  ✓ Opened in browser")
    except Exception:
        print(f"  ⚠ Open manually: {os.path.abspath(outfile)}")

    print("\n" + "═" * 60)
    print(f"  Scan complete. {len(results)} candidates. Best: "
          f"{results[0]['data']['symbol'].replace('.L','')} "
          f"{results[0]['data']['pct_change']:+.1f}% score {results[0]['score']}/10"
          if results else "  No candidates today.")
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
