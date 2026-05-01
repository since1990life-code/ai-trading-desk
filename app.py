from flask import Flask, request, render_template, redirect, url_for, jsonify
from datetime import datetime, timezone
import csv, io, json, os, urllib.request, urllib.error, urllib.parse

app = Flask(__name__)

PRESETS_DIR = "strategy_presets"
os.makedirs(PRESETS_DIR, exist_ok=True)

LOGS_FILE = "trade_logs.json"

DEFAULT_SETTINGS = {
    "strategy_name":     "Breakout Hold",
    "ready_threshold":   80,
    "watch_threshold":   60,
    "trend_pts":         30,
    "loc_pts":           25,
    "break_pts":         20,
    "hold_pts":          15,
    "vol_pts":           10,
    "volume_multiplier": 1.5,
    "ma_distance_limit": 20.0,
    "wick_threshold":    0.3,
    "min_rr":            2.0,
}

market_data       = {"gold": None, "btc": None}
strategy_settings = dict(DEFAULT_SETTINGS)
copilot_data      = {}


# ── SMA ───────────────────────────────────────────────────────────────────────

def compute_sma(values, period):
    sma = []
    for i, v in enumerate(values):
        if i + 1 < period:
            sma.append(None)
        else:
            sma.append(round(sum(values[i + 1 - period : i + 1]) / period, 4))
    return sma


# ── SESSION DETECTION ─────────────────────────────────────────────────────────

_SESSIONS = [
    (0,  8,  "Asia",     "アジア時間",         "asia"),
    (8,  13, "London",   "ロンドン時間",       "london"),
    (13, 21, "New York", "ニューヨーク時間",   "newyork"),
]

def get_session():
    hour = datetime.now(timezone.utc).hour
    for start, end, name, label, key in _SESSIONS:
        if start <= hour < end:
            return {"name": name, "label": label, "key": key}
    return {"name": "Off", "label": "オフ時間", "key": "off"}


# ── FLAG DETECTORS ────────────────────────────────────────────────────────────

def flag_trend(recent_closes, recent_smas):
    """UP / DOWN / RANGE / NO_TRADE based on close vs SMA over last 5 bars."""
    pairs = [(c, s) for c, s in zip(recent_closes, recent_smas) if s is not None]
    if len(pairs) < 3:
        return "NO_TRADE"
    last5 = pairs[-5:] if len(pairs) >= 5 else pairs
    above = sum(1 for c, s in last5 if c > s)
    total = len(last5)
    if above / total >= 0.8:
        return "UP"
    if above / total <= 0.2:
        return "DOWN"
    return "RANGE"


def flag_location(last_close, recent_highs, recent_lows, ma_distance_limit):
    """Returns (flag, swing_high, swing_low): SUPPORT/RESISTANCE/RANGE_MIDDLE/NONE."""
    if len(recent_highs) < 2 or len(recent_lows) < 2:
        return "NONE", None, None
    swing_high  = max(recent_highs[:-1])
    swing_low   = min(recent_lows[:-1])
    price_range = swing_high - swing_low
    if price_range <= 0:
        return "NONE", swing_high, swing_low
    zone_pct = min(ma_distance_limit / 100, 0.40)  # ma_distance_limit is % of range (0–40)
    bottom   = swing_low  + price_range * zone_pct
    top      = swing_high - price_range * zone_pct
    if last_close <= bottom:
        return "SUPPORT", swing_high, swing_low
    if last_close >= top:
        return "RESISTANCE", swing_high, swing_low
    return "RANGE_MIDDLE", swing_high, swing_low


def flag_break(last_close, last_low, swing_low, recent_closes, recent_lows):
    """FAKE_BREAK / VALID_BREAK / NO_BREAK — wick below swing_low, closed above."""
    if swing_low is None:
        return "NO_BREAK"
    if last_low < swing_low and last_close >= swing_low:
        return "FAKE_BREAK"
    for c, l in zip((recent_closes or [])[-5:], (recent_lows or [])[-5:]):
        if l < swing_low and c >= swing_low:
            return "FAKE_BREAK"
    if last_close < swing_low:
        return "VALID_BREAK"
    for c in (recent_closes or [])[-5:]:
        if c < swing_low:
            return "VALID_BREAK"
    return "NO_BREAK"


def flag_hold(last_close, swing_low, break_f):
    """HELD / FAILED / UNKNOWN — did price hold above support after fake break."""
    if break_f == "FAKE_BREAK":
        return "HELD" if last_close >= (swing_low or 0) else "FAILED"
    if break_f == "VALID_BREAK":
        return "FAILED"
    return "UNKNOWN"


def flag_reaction(last_high, last_low, last_open, last_close, recent_highs, wick_threshold):
    """HIGHER_HIGH / WICK_REJECTION / NO_REACTION."""
    if recent_highs and len(recent_highs) >= 2:
        prev_high = max(recent_highs[:-1])
        if last_high > prev_high:
            return "HIGHER_HIGH"
    if last_open is not None and last_close is not None:
        total_range = last_high - last_low
        if total_range > 0:
            lower_wick = min(last_open, last_close) - last_low
            if lower_wick / total_range >= wick_threshold:
                return "WICK_REJECTION"
    return "NO_REACTION"


def flag_volume(last_volume, avg_volume):
    """OK / LOW / N/A."""
    if last_volume is None or avg_volume is None or avg_volume == 0:
        return "N/A"
    return "OK" if last_volume >= avg_volume * 0.8 else "LOW"


# ── SCORE FUNCTIONS (flag-based) ──────────────────────────────────────────────

def score_trend_flag(trend_f, max_pts):
    if trend_f == "UP":
        return max_pts, "UP"
    if trend_f == "RANGE":
        return max_pts // 2, "RANGE"
    if trend_f == "DOWN":
        return 0, "DOWN"
    return 0, "NO_TRADE"


def score_location_flag(loc_f, max_pts):
    if loc_f == "SUPPORT":
        return max_pts, "SUPPORT"
    if loc_f == "RESISTANCE":
        return max_pts // 2, "RESIST"
    return 0, loc_f  # RANGE_MIDDLE or NONE → 0 pts


def score_break_flag(break_f, max_pts):
    if break_f == "FAKE_BREAK":
        return max_pts, "FAKE BRK"
    if break_f == "NO_BREAK":
        return max_pts // 2, "NO BRK"
    return 0, "VALID BRK"  # structure broke through → 0 pts


def score_hold_flag(hold_f, max_pts):
    if hold_f == "HELD":
        return max_pts, "HELD"
    if hold_f == "UNKNOWN":
        return max_pts // 2, "UNKNOWN"
    return 0, "FAILED"


def score_volume_flag(vol_f, max_pts):
    if vol_f == "OK":
        return max_pts, "OK"
    if vol_f == "N/A":
        return max_pts // 2, "N/A"
    return 0, "LOW"


# ── SESSION / TIMING (informational only, excluded from total) ────────────────

def score_session(session_name):
    if session_name in ("London", "New York"):
        return 15, session_name.upper()
    if session_name == "Asia":
        return 5, "ASIA"
    return 0, "OFF"


def score_timing(recent_highs, last_close, last_sma, ma_distance_limit):
    if not recent_highs or len(recent_highs) < 2:
        return 0, "N/A"
    swing_high = max(recent_highs[:-1])
    broke    = last_close > swing_high
    near_sma = abs(last_close - last_sma) / last_sma * 100 <= 2.0  # fixed 2% near-SMA
    if broke and near_sma:
        return 10, "CONFIRMED"
    if broke:
        return 8, "BREAK"
    return 0, "NONE"


# ── HELPERS ───────────────────────────────────────────────────────────────────

def get_setup_direction(trend_f, loc_f):
    if trend_f == "UP" or loc_f == "SUPPORT":
        return "LONG"
    if trend_f == "DOWN" or loc_f == "RESISTANCE":
        return "SHORT"
    return "NONE"


def count_positive_signals(trend_f, loc_f, break_f, hold_f, react_f, vol_f):
    return sum([
        trend_f in ("UP", "RANGE"),
        loc_f == "SUPPORT",
        break_f == "FAKE_BREAK",
        hold_f == "HELD",
        react_f in ("HIGHER_HIGH", "WICK_REJECTION"),
        vol_f == "OK",
    ])


def check_hard_ng(loc_f, vol_f, break_f, hold_f, positive_count):
    if loc_f == "RANGE_MIDDLE":
        return True, "Location: RANGE_MIDDLE"
    if vol_f == "LOW":
        return True, "Volume: LOW"
    if break_f == "NO_BREAK" and hold_f == "UNKNOWN":
        return True, "No break + no hold confirmation"
    if positive_count < 2:
        return True, f"Only {positive_count} positive signal(s)"
    return False, ""


# ── ENTRY SCORE ───────────────────────────────────────────────────────────────

def compute_entry_score(mkt, settings, session_name=""):
    recent_closes = mkt.get("recent_closes", [])
    recent_smas   = mkt.get("recent_smas",   [])
    recent_highs  = mkt.get("recent_highs",  [])
    recent_lows   = mkt.get("recent_lows",   [])
    last_close    = mkt["last_close"]
    last_low      = mkt.get("last_low",  last_close)
    last_high     = mkt.get("last_high", last_close)
    last_open     = mkt.get("last_open")

    trend_f              = flag_trend(recent_closes, recent_smas)
    loc_f, sh, sl        = flag_location(last_close, recent_highs, recent_lows,
                                         settings["ma_distance_limit"])
    break_f              = flag_break(last_close, last_low, sl, recent_closes, recent_lows)
    hold_f               = flag_hold(last_close, sl, break_f)
    react_f              = flag_reaction(last_high, last_low, last_open, last_close,
                                         recent_highs, settings["wick_threshold"])
    vol_f                = flag_volume(mkt.get("last_volume"), mkt.get("avg_volume"))

    t_pts, t_lbl = score_trend_flag(trend_f,  settings["trend_pts"])
    l_pts, l_lbl = score_location_flag(loc_f, settings["loc_pts"])
    b_pts, b_lbl = score_break_flag(break_f,  settings["break_pts"])
    h_pts, h_lbl = score_hold_flag(hold_f,    settings["hold_pts"])
    v_pts, v_lbl = score_volume_flag(vol_f,   settings["vol_pts"])

    s_pts,  s_lbl  = score_session(session_name)
    tm_pts, tm_lbl = score_timing(recent_highs, last_close, mkt["last_sma"],
                                  settings["ma_distance_limit"])

    positive_count       = count_positive_signals(trend_f, loc_f, break_f, hold_f, react_f, vol_f)
    has_hard_ng, ng_rsn  = check_hard_ng(loc_f, vol_f, break_f, hold_f, positive_count)
    direction            = get_setup_direction(trend_f, loc_f)

    return {
        "total":       t_pts + l_pts + b_pts + h_pts + v_pts,
        "trend_pts":   t_pts, "trend_lbl":  t_lbl,
        "loc_pts":     l_pts, "loc_lbl":    l_lbl,
        "break_pts":   b_pts, "break_lbl":  b_lbl,
        "hold_pts":    h_pts, "hold_lbl":   h_lbl,
        "vol_pts":     v_pts, "vol_lbl":    v_lbl,
        "sess_pts":    s_pts, "sess_lbl":   s_lbl,
        "time_pts":   tm_pts, "time_lbl":  tm_lbl,
        "trend_flag":  trend_f,
        "loc_flag":    loc_f,
        "break_flag":  break_f,
        "hold_flag":   hold_f,
        "react_flag":  react_f,
        "vol_flag":    vol_f,
        "swing_high":  sh,
        "swing_low":   sl,
        "direction":   direction,
        "has_hard_ng": has_hard_ng,
        "ng_reason":   ng_rsn,
    }


def get_score_status(score, has_hard_ng=False):
    if has_hard_ng:
        return "SKIP"
    ready = strategy_settings.get("ready_threshold", 80)
    watch = strategy_settings.get("watch_threshold", 60)
    if score >= ready:
        return "READY"
    if score >= watch:
        return "WATCH"
    return "WAIT"


def get_controller_decision(gold_status, btc_status):
    statuses = [s for s in [gold_status, btc_status] if s is not None]
    if not statuses:
        return "NO DATA"
    order = {"READY": 4, "WATCH": 3, "WAIT": 2, "SKIP": 1}
    return max(statuses, key=lambda s: order.get(s, 0))


# ── RISK ──────────────────────────────────────────────────────────────────────

def compute_risk(balance, max_risk, lot_size):
    if balance <= 0:
        return "WARNING", "口座残高は0より大きくしてください。", 0.0
    risk_pct = (max_risk / balance) * 100
    if risk_pct <= 2.0:
        return "OK", f"1トレードのリスクは残高の {risk_pct:.2f}% です。2%ルール内。", risk_pct
    return (
        "WARNING",
        f"1トレードのリスクは残高の {risk_pct:.2f}% です。2%ルールを超過 — Max Risk を下げることを検討してください。",
        risk_pct,
    )


# ── CSV PARSING ───────────────────────────────────────────────────────────────

def _safe_float(value, fallback):
    try:
        return float(value) if value else fallback
    except (ValueError, TypeError):
        return fallback


def parse_csv(file_bytes, period):
    text   = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        raise ValueError("CSV appears to be empty.")

    headers = [h.strip().lower() for h in reader.fieldnames]
    reader.fieldnames = headers

    close_key = next((h for h in headers if "close" in h), None)
    date_key  = next((h for h in headers if "date"  in h or "time" in h), None)
    open_key  = next((h for h in headers if "open"  in h), None)
    high_key  = next((h for h in headers if "high"  in h), None)
    low_key   = next((h for h in headers if "low"   in h), None)
    vol_key   = next((h for h in headers if "vol"   in h), None)

    if close_key is None:
        raise ValueError("No 'Close' column found in CSV.")

    has_ohlc   = all(k is not None for k in [open_key, high_key, low_key])
    has_volume = vol_key is not None

    dates, closes, opens, highs, lows, volumes = [], [], [], [], [], []

    for raw in reader:
        row = {k.strip().lower(): v.strip() for k, v in raw.items()}
        try:
            close_val = float(row[close_key])
        except (ValueError, KeyError):
            continue

        closes.append(close_val)
        dates.append(row[date_key] if date_key and date_key in row else str(len(dates) + 1))

        if has_ohlc:
            opens.append(_safe_float(row.get(open_key), close_val))
            highs.append(_safe_float(row.get(high_key), close_val))
            lows.append(_safe_float(row.get(low_key),   close_val))

        if has_volume:
            volumes.append(_safe_float(row.get(vol_key), 0.0))

    if len(closes) < period:
        raise ValueError(
            f"Need at least {period} rows for SMA({period}). Found {len(closes)}."
        )

    smas     = compute_sma(closes, period)
    last_sma = smas[-1]
    trend    = "Uptrend" if closes[-1] > last_sma else "Downtrend"

    rows = [
        {"date": d, "close": c, "sma": s if s is not None else "—"}
        for d, c, s in zip(dates, closes, smas)
    ]

    valid_vols = [v for v in volumes if v > 0]
    avg_volume = round(sum(valid_vols) / len(valid_vols), 2) if valid_vols else None

    return {
        "period":        period,
        "last_close":    closes[-1],
        "last_sma":      last_sma,
        "last_open":     opens[-1]    if has_ohlc   else None,
        "last_high":     highs[-1]    if has_ohlc   else None,
        "last_low":      lows[-1]     if has_ohlc   else None,
        "last_volume":   volumes[-1]  if has_volume else None,
        "avg_volume":    avg_volume,
        "recent_highs":  highs[-10:]  if has_ohlc   else [],
        "recent_lows":   lows[-10:]   if has_ohlc   else [],
        "recent_closes": closes[-10:],
        "recent_smas":   smas[-10:],
        "trend":         trend,
        "rows":          rows[-20:],
        "loaded_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ── LIVE PRICE ────────────────────────────────────────────────────────────────

def fetch_btc_price():
    """Returns (price_float, status_str).
    Tries CoinGecko first, falls back to Coinbase.
    Status is 'OK', 'CG:<error>', or 'CB:<error>' for diagnosis.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TradingDesk/1.0)"}

    # ── 1. CoinGecko ─────────────────────────────────────────────────────────
    cg_url = ("https://api.coingecko.com/api/v3/simple/price"
              "?ids=bitcoin&vs_currencies=usd")
    try:
        req  = urllib.request.Request(cg_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return round(float(data["bitcoin"]["usd"]), 2), "OK"
    except urllib.error.HTTPError as e:
        cg_err = f"CG:HTTP{e.code}"
    except urllib.error.URLError as e:
        cg_err = f"CG:{str(e.reason)[:30]}"
    except Exception as e:
        cg_err = f"CG:{type(e).__name__}"

    # ── 2. Coinbase fallback ──────────────────────────────────────────────────
    cb_url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    try:
        req  = urllib.request.Request(cb_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return round(float(data["data"]["amount"]), 2), "OK"
    except urllib.error.HTTPError as e:
        cb_err = f"CB:HTTP{e.code}"
    except urllib.error.URLError as e:
        cb_err = f"CB:{str(e.reason)[:30]}"
    except Exception as e:
        cb_err = f"CB:{type(e).__name__}"

    return None, f"{cg_err} {cb_err}"


def fetch_gold_price():
    """Returns (price_float, status_str). status is 'OK', 'NO_API_KEY', or an error msg."""
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    if not api_key:
        return None, "NO_API_KEY"
    try:
        params = urllib.parse.urlencode({"symbol": "XAU/USD", "apikey": api_key})
        url = "https://api.twelvedata.com/price?" + params
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        if "price" in data:
            return round(float(data["price"]), 2), "OK"
        msg = str(data.get("message", "API error"))
        return None, msg[:60]
    except Exception:
        return None, "ERROR"


# ── PRESET HELPERS ────────────────────────────────────────────────────────────

def list_presets():
    if not os.path.isdir(PRESETS_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PRESETS_DIR) if f.endswith(".json"))


def save_preset(name, settings):
    safe = "".join(c for c in name if c.isalnum() or c in "_-")
    if not safe:
        raise ValueError("Invalid preset name. Use letters, numbers, _ or -.")
    path = os.path.join(PRESETS_DIR, safe + ".json")
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    return safe


def load_preset_file(name):
    safe = "".join(c for c in name if c.isalnum() or c in "_-")
    path = os.path.join(PRESETS_DIR, safe + ".json")
    if not os.path.exists(path):
        raise ValueError(f"Preset '{safe}' not found.")
    with open(path) as f:
        return json.load(f)


# ── TRADE LOG ─────────────────────────────────────────────────────────────────

def compute_rr(entry, sl, tp):
    if entry is None or sl is None or tp is None:
        return None
    try:
        risk   = float(entry) - float(sl)
        reward = float(tp)    - float(entry)
        if risk <= 0 or reward <= 0:
            return None
        return round(reward / risk, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def load_logs():
    if not os.path.exists(LOGS_FILE):
        return []
    try:
        with open(LOGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def append_log(record):
    logs = load_logs()
    logs.append(record)
    with open(LOGS_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)


# ── COPILOT HELPERS ──────────────────────────────────────────────────────────

def normalize_copilot_data(data):
    def _trend(v):
        s = str(v).strip().lower()
        return +1 if s in ("buy", "bullish") else (-1 if s in ("sell", "bearish") else 0)

    def _bias(v):
        s = str(v).strip().lower()
        return +1 if s == "bullish" else (-1 if s == "bearish" else 0)

    def _pd(v):
        s = str(v).strip().lower()
        return +1 if s == "discount" else (-1 if s == "premium" else 0)

    def _momentum(v):
        s = str(v).strip().lower()
        return +1 if s == "bullish" else (-1 if s == "bearish" else 0)

    def _volume(v):
        s = str(v).strip().lower()
        return +1 if s == "high" else (-1 if s == "low" else 0)

    return {
        "trend_5m":         _trend(data.get("trend_5m",         "Neutral")),
        "trend_15m":        _trend(data.get("trend_15m",        "Neutral")),
        "trend_1h":         _trend(data.get("trend_1h",         "Neutral")),
        "bias":             _bias(data.get("bias",              "Neutral")),
        "premium_discount": _pd(data.get("premium_discount",    "Fair")),
        "momentum":         _momentum(data.get("momentum",      "Neutral")),
        "volume":           _volume(data.get("volume",          "Normal")),
    }


def compute_copilot_score(norm):
    trend_pts = sum(10 for k in ("trend_5m", "trend_15m", "trend_1h") if norm[k] == 1)
    bias_pts  = 20 if norm["bias"] == 1             else (10 if norm["bias"] == 0             else 0)
    pd_pts    = 15 if norm["premium_discount"] == 1 else (7  if norm["premium_discount"] == 0 else 0)
    mom_pts   = 20 if norm["momentum"] == 1         else (10 if norm["momentum"] == 0         else 0)
    vol_pts   = 15 if norm["volume"] == 1           else (7  if norm["volume"] == 0           else 0)
    return trend_pts + bias_pts + pd_pts + mom_pts + vol_pts


def get_copilot_status(score):
    if score >= 70:
        return "CONFIRM"
    if score >= 40:
        return "MIXED"
    return "CAUTION"


_CTRL_NO_COP_MSG = {
    "READY":   "CSV setup confirmed. No Copilot data loaded.",
    "WATCH":   "CSV partial setup. No Copilot data loaded.",
    "WAIT":    "No CSV setup. No Copilot data loaded.",
    "SKIP":    "Hard NG on CSV. No trade regardless of Copilot.",
    "NO DATA": "No market data loaded.",
}

def compute_combined_controller(csv_decision, cop_status):
    """Returns (status, action_message) fusing CSV decision with Copilot status."""
    if cop_status is None:
        return csv_decision, _CTRL_NO_COP_MSG.get(csv_decision, "—")

    if cop_status == "CONFIRM":
        if csv_decision in ("READY", "WATCH"):
            return "READY", "Both signals align. Monitor for entry trigger."
        else:
            return "WAIT_PULLBACK", "Copilot confirms direction. Wait for CSV setup to develop."
    elif cop_status == "MIXED":
        if csv_decision in ("READY", "WATCH"):
            return "CAUTION", "CSV setup detected but Copilot signals mixed. Reduce size or skip."
        else:
            return "NO_TRADE", "No aligned signal. Remain flat."
    else:  # cop_status == "CAUTION"
        return "NO_TRADE", "Copilot caution. No trade until conditions improve."


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    tab = request.args.get("tab", "monitor")

    session_info = get_session()
    now_utc      = datetime.now(timezone.utc).strftime("%H:%M")

    gold = market_data["gold"]
    btc  = market_data["btc"]

    gold_bd = compute_entry_score(gold, strategy_settings, session_info["name"]) if gold else None
    btc_bd  = compute_entry_score(btc,  strategy_settings, session_info["name"]) if btc  else None

    gold_score  = gold_bd["total"] if gold_bd else None
    btc_score   = btc_bd["total"]  if btc_bd  else None
    gold_status = get_score_status(gold_score, gold_bd["has_hard_ng"]) if gold_bd else None
    btc_status  = get_score_status(btc_score,  btc_bd["has_hard_ng"])  if btc_bd  else None
    decision    = get_controller_decision(gold_status, btc_status)

    csv_best_score = max((s for s in [gold_score, btc_score] if s is not None), default=None)

    cop_norm       = normalize_copilot_data(copilot_data) if copilot_data else None
    cop_score      = compute_copilot_score(cop_norm)       if cop_norm   else None
    cop_status     = get_copilot_status(cop_score)         if cop_score is not None else None
    cop_updated_at = copilot_data.get("updated_at")        if copilot_data else None
    cop_freshness  = None
    if cop_updated_at:
        try:
            age_sec = (datetime.now() - datetime.strptime(cop_updated_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            age_min = age_sec / 60
            if age_min < 5:
                cop_freshness = {"label": "LIVE",    "cls": "fresh-live"}
            elif age_min < 15:
                cop_freshness = {"label": "DELAYED", "cls": "fresh-delayed"}
            else:
                cop_freshness = {"label": "STALE",   "cls": "fresh-stale"}
        except ValueError:
            pass

    combined_status, combined_action = compute_combined_controller(decision, cop_status)

    # Max possible score from the five configurable components
    score_max = (strategy_settings["trend_pts"] + strategy_settings["loc_pts"] +
                 strategy_settings["break_pts"] + strategy_settings["hold_pts"] +
                 strategy_settings["vol_pts"])
    if score_max < 1:
        score_max = 1  # guard against zero division

    def _bar(score):
        return min(round(score * 100 / score_max), 100) if score is not None else 0

    gold_bar = _bar(gold_score)
    btc_bar  = _bar(btc_score)

    try:
        balance  = float(request.args.get("balance",  50000))
        max_risk = float(request.args.get("max_risk", 500))
        lot_size = float(request.args.get("lot_size", 0.01))
    except ValueError:
        balance, max_risk, lot_size = 50000.0, 500.0, 0.01

    risk_status, risk_message, risk_pct = compute_risk(balance, max_risk, lot_size)

    logs = list(reversed(load_logs()))

    return render_template(
        "index.html",
        gold=gold,             btc=btc,
        gold_bd=gold_bd,       btc_bd=btc_bd,
        gold_score=gold_score, btc_score=btc_score,
        gold_status=gold_status, btc_status=btc_status,
        gold_bar=gold_bar,     btc_bar=btc_bar,
        score_max=score_max,
        decision=decision,
        session_info=session_info, now_utc=now_utc,
        balance=balance, max_risk=max_risk, lot_size=lot_size,
        risk_status=risk_status, risk_message=risk_message,
        risk_pct=round(risk_pct, 2),
        settings=strategy_settings,
        presets=list_presets(),
        logs=logs,
        active_tab=tab,
        error=request.args.get("error", ""),
        success=request.args.get("success", ""),
        copilot_raw=copilot_data,
        copilot_norm=cop_norm,
        copilot_score=cop_score,
        copilot_status=cop_status,
        copilot_updated_at=cop_updated_at,
        copilot_freshness=cop_freshness,
        csv_best_score=csv_best_score,
        combined_status=combined_status,
        combined_action=combined_action,
    )


@app.route("/upload", methods=["POST"])
def upload():
    def err(msg):
        return redirect(url_for("index", tab="monitor", error=msg))

    file   = request.files.get("file")
    market = request.form.get("market", "gold")

    if not file or file.filename == "":
        return err("No file selected.")

    try:
        period = int(request.form.get("period", 5))
        if period < 2:
            return err("SMA period must be at least 2.")
        result = parse_csv(file.read(), period)
    except ValueError as e:
        return err(str(e))
    except Exception as e:
        return err(f"Could not read file: {e}")

    market_data[market] = result
    return redirect(url_for("index", tab=market))


@app.route("/risk", methods=["POST"])
def risk():
    try:
        balance  = float(request.form.get("balance",  50000))
        max_risk = float(request.form.get("max_risk", 500))
        lot_size = float(request.form.get("lot_size", 0.01))
    except ValueError:
        balance, max_risk, lot_size = 50000.0, 500.0, 0.01
    return redirect(url_for(
        "index", tab="risk",
        balance=balance, max_risk=max_risk, lot_size=lot_size,
    ))


@app.route("/log/save", methods=["POST"])
def log_save():
    market = request.form.get("market", "gold").lower()

    def _price(key):
        try:
            v = float(request.form.get(key) or 0)
            return v if v > 0 else None
        except ValueError:
            return None

    entry = _price("entry_price")
    sl    = _price("stop_loss")
    tp    = _price("take_profit")

    record = {
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market":      {"gold": "GOLD", "btc": "BTC"}.get(market, market.upper()),
        "score":       int(request.form.get("score",  0) or 0),
        "status":      request.form.get("status",    "WAIT"),
        "session":     request.form.get("session",   "—"),
        "timing":      request.form.get("timing",    "—"),
        "tf_daily":    request.form.get("tf_daily",  "—"),
        "tf_h4":       request.form.get("tf_h4",     "—"),
        "tf_h1":       request.form.get("tf_h1",     "—"),
        "tf_m15":      request.form.get("tf_m15",    "—"),
        "entry_price": entry,
        "stop_loss":   sl,
        "take_profit": tp,
        "rr":          compute_rr(entry, sl, tp),
        "action":      request.form.get("action",    "Skipped"),
        "result":      request.form.get("result",    "Pending"),
        "trend_pts":   int(request.form.get("trend_pts",  0) or 0),
        "trend_lbl":   request.form.get("trend_lbl",  "—"),
        "loc_pts":     int(request.form.get("loc_pts",    0) or 0),
        "loc_lbl":     request.form.get("loc_lbl",    "—"),
        "break_pts":   int(request.form.get("break_pts",  0) or 0),
        "break_lbl":   request.form.get("break_lbl",  "—"),
        "hold_pts":    int(request.form.get("hold_pts",   0) or 0),
        "hold_lbl":    request.form.get("hold_lbl",   "—"),
        "vol_pts":     int(request.form.get("vol_pts",    0) or 0),
        "vol_lbl":     request.form.get("vol_lbl",    "—"),
        "trend_flag":  request.form.get("trend_flag", "—"),
        "loc_flag":    request.form.get("loc_flag",   "—"),
        "break_flag":  request.form.get("break_flag", "—"),
        "hold_flag":   request.form.get("hold_flag",  "—"),
        "react_flag":  request.form.get("react_flag", "—"),
        "vol_flag":    request.form.get("vol_flag",   "—"),
        "direction":   request.form.get("direction",  "—"),
    }

    append_log(record)
    return redirect(url_for("index", tab=market, success="トレードログを保存しました。"))


@app.route("/strategy/save", methods=["POST"])
def strategy_save():
    global strategy_settings

    def _int(key, default):
        try:
            return max(0, int(request.form.get(key, default) or default))
        except (ValueError, TypeError):
            return default

    def _float(key, default):
        try:
            return float(request.form.get(key, default) or default)
        except (ValueError, TypeError):
            return default

    new = {
        "strategy_name":     request.form.get("strategy_name", "").strip() or "Default",
        "ready_threshold":   _int("ready_threshold",   80),
        "watch_threshold":   _int("watch_threshold",   60),
        "trend_pts":         _int("trend_pts",         30),
        "loc_pts":           _int("loc_pts",           25),
        "break_pts":         _int("break_pts",         20),
        "hold_pts":          _int("hold_pts",          15),
        "vol_pts":           _int("vol_pts",           10),
        "volume_multiplier": _float("volume_multiplier", 1.5),
        "ma_distance_limit": _float("ma_distance_limit", 20.0),
        "wick_threshold":    _float("wick_threshold",    0.3),
        "min_rr":            _float("min_rr",            2.0),
    }

    strategy_settings = new
    return redirect(url_for("index", tab="strategy",
                            success=f"戦略 '{new['strategy_name']}' を適用しました。"))


@app.route("/strategy/load", methods=["POST"])
def strategy_load():
    global strategy_settings
    name = request.form.get("preset_name", "").strip()
    if not name:
        return redirect(url_for("index", tab="strategy", error="No preset selected."))
    try:
        loaded = load_preset_file(name)
        strategy_settings = {**DEFAULT_SETTINGS, **loaded}
        return redirect(url_for("index", tab="strategy",
                                success=f"プリセット '{name}' を読み込みました。"))
    except ValueError as e:
        return redirect(url_for("index", tab="strategy", error=str(e)))


@app.route("/strategy/save_file", methods=["POST"])
def strategy_save_file():
    """Apply settings from the form AND save them to strategy_presets/{name}.json."""
    global strategy_settings

    def _int(key, default):
        try:
            return max(0, int(request.form.get(key, default) or default))
        except (ValueError, TypeError):
            return default

    def _float(key, default):
        try:
            return float(request.form.get(key, default) or default)
        except (ValueError, TypeError):
            return default

    new = {
        "strategy_name":     request.form.get("strategy_name", "").strip() or "Default",
        "ready_threshold":   _int("ready_threshold",   80),
        "watch_threshold":   _int("watch_threshold",   60),
        "trend_pts":         _int("trend_pts",         30),
        "loc_pts":           _int("loc_pts",           25),
        "break_pts":         _int("break_pts",         20),
        "hold_pts":          _int("hold_pts",          15),
        "vol_pts":           _int("vol_pts",           10),
        "volume_multiplier": _float("volume_multiplier", 1.5),
        "ma_distance_limit": _float("ma_distance_limit", 20.0),
        "wick_threshold":    _float("wick_threshold",    0.3),
        "min_rr":            _float("min_rr",            2.0),
    }

    strategy_settings = new

    try:
        saved = save_preset(new["strategy_name"], new)
        return redirect(url_for("index", tab="strategy",
                                success=f"戦略 '{saved}' をファイルに保存しました。"))
    except ValueError as e:
        return redirect(url_for("index", tab="strategy", error=str(e)))


@app.route("/strategy/load_file", methods=["POST"])
def strategy_load_file():
    """Load a saved strategy JSON, apply it, and redirect so the form re-renders."""
    global strategy_settings
    name = request.form.get("preset_name", "").strip()
    if not name:
        return redirect(url_for("index", tab="strategy", error="No strategy selected."))
    try:
        loaded = load_preset_file(name)
        strategy_settings = {**DEFAULT_SETTINGS, **loaded}
        return redirect(url_for("index", tab="strategy",
                                success=f"戦略 '{name}' を読み込んで適用しました。"))
    except ValueError as e:
        return redirect(url_for("index", tab="strategy", error=str(e)))


@app.route("/api/prices")
def api_prices():
    now                     = datetime.now().strftime("%H:%M:%S")
    btc_price,  btc_status  = fetch_btc_price()
    gold_price, gold_status = fetch_gold_price()
    return jsonify({
        "btc": {
            "price":      btc_price,
            "status":     btc_status,
            "fetched_at": now,
        },
        "gold": {
            "price":      gold_price,
            "status":     gold_status,
            "fetched_at": now,
        },
        "fetched_at": now,
    })


@app.route("/api/price")
def api_price():
    """Legacy single-asset endpoint kept for backward compatibility."""
    price, _ = fetch_btc_price()
    now      = datetime.now().strftime("%H:%M:%S")
    if price is None:
        return jsonify({"price": None, "error": "API unavailable", "fetched_at": now}), 503
    return jsonify({"price": price, "fetched_at": now})


@app.route("/copilot/import", methods=["POST"])
def copilot_import():
    global copilot_data

    raw = (request.form.get("copilot_json") or "").strip()
    if not raw:
        return redirect(url_for("index", tab="monitor",
                                error="Copilot: JSONテキストが空です。"))
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return redirect(url_for("index", tab="monitor",
                                error=f"Copilot: JSON解析エラー — {exc}"))

    if not isinstance(data, dict):
        return redirect(url_for("index", tab="monitor",
                                error="Copilot: JSONはオブジェクト({...})形式が必要です。"))

    required = {"trend_5m", "trend_15m", "trend_1h", "bias"}
    missing  = required - set(data.keys())
    if missing:
        return redirect(url_for("index", tab="monitor",
                                error=f"Copilot: 必須フィールドが不足: {', '.join(sorted(missing))}"))

    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    copilot_data = data
    return redirect(url_for("index", tab="monitor",
                            success="Copilotデータを読み込みました。"))


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
