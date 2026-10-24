# -*- coding: utf-8 -*-
"""
台股當沖雙引擎旗艦版 (Turnaround & Exhaustion 轉折精準版)

核心模組:
  1. 盤前/盤後高周轉率與大震幅選股 (動態保底名單，杜絕 0 檔作戰清單)
  2. 09:03 ~ 09:18 開盤快沖引擎 (跳空、紅K站穩開盤價、外盤吃單)
  3. 09:35 ~ 13:15 主流壓縮突破 (VWAP 均價線支撐、排除正乖離 > 2.2% 追高陷阱)
  4. 避開 10:30 ~ 11:20 垃圾洗盤泥沼時段 (禁止開新倉)
  5. 主力熄燈轉折預警 (Exhaustion Alert: 賣一巨單壓頂 + 買單比率潰散 + 衝高拉回)
  6. 移動保本防守 (Trailing Stop) 與 8~10 分鐘時間停損 (Time Stop)
  7. 加權指數 (tse_t00.tw) 共振防護
  8. 內建輕量 HTTP Keep-Alive 伺服器 (防止 Render 等雲端平台休眠)
"""
import os
import sys
import csv
import json
import math
import time
import html
import shutil
import argparse
import traceback
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date, time as dtime, timedelta

import pandas as pd
import pytz
import requests

TW_TZ = pytz.timezone("Asia/Taipei")

# ==================== 環境變數與路徑設定 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PORT = int(os.getenv("PORT", "10000"))

DATA_DIR = os.getenv("DATA_DIR", "data/history")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals_history.csv")
RESULTS_FILE = os.path.join(DATA_DIR, "signals_results.csv")
STATE_FILE = os.path.join(DATA_DIR, "open_positions.json")
WATCHLIST_FILE = os.path.join(DATA_DIR, "tomorrow_watchlist.json")
FLAGS_FILE = os.path.join(DATA_DIR, "daily_flags.json")

# 交易成本設定
FEE_RATE = 0.001425
FEE_DISCOUNT = float(os.getenv("FEE_DISCOUNT", "0.6"))
DAYTRADE_TAX = float(os.getenv("DAYTRADE_TAX", "0.0015"))
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.0005"))

# 選股與風控條件
MIN_PRICE = 25.0
MIN_LOTS = 2000
MIN_AMPLITUDE_PCT = 3.5          # 震幅門檻 (會動才有空間)
WATCHLIST_SIZE = 25
DAYTRADE_RATIO_MAX = 50.0        # 當沖比上限門檻

# 乖離率與停損參數
MAX_VWAP_BIAS = 2.2              # 正乖離大於 2.2% 視為過熱力竭, 嚴禁追高
MIN_RISK_RUSH = 0.008
MIN_RISK_TREND = 0.010

UA = {"User-Agent": "Mozilla/5.0 (compatible; daytrade-bot/5.0)"}
TWSE_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_NOTICE = "https://openapi.twse.com.tw/v1/announcement/notice"
TWSE_DAYTRADE_LIST = "https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U"
TWSE_DAYTRADE_STATS = "https://www.twse.com.tw/rwd/zh/dayTrading/TWTB4U"
TPEX_DAY_ALL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

INDEX_SYMBOL = "tse_t00.tw"      # 加權指數代碼
FALLBACK_WATCH = [
    "tse_2330.tw", "tse_2317.tw", "tse_2454.tw", "tse_3231.tw", "tse_2382.tw",
    "tse_2603.tw", "tse_3037.tw", "tse_3653.tw", "tse_2609.tw", "otc_8069.tw"
]

SIGNAL_COLUMNS = [
    "signal_id", "strategy", "date", "time", "market", "code", "name", "price", "pct_change",
    "vwap", "vwap_bias", "volume", "stop_loss", "target_price", "wave", "day_high_at_signal",
    "day_low_at_signal", "extra_info", "link",
]
RESULT_COLUMNS = [
    "signal_id", "date", "time", "strategy", "market", "code", "name", "entry", "stop", "target",
    "outcome", "exit_price", "gross_pct", "cost_pct", "net_pct", "risk_pct", "r_multiple",
    "touched_target", "touched_stop", "first_touch", "close_price", "note",
]


# ==================== 輕量 Keep-Alive 伺服器 (防止雲端休眠) ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"Daytrade Bot Engine Live: {now_tw():%Y-%m-%d %H:%M:%S}\n".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # 靜音 HTTP 日誌


def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        log(f"[Info] 雲端 Keep-Alive HTTP 伺服器已在連接埠 {PORT} 啟動")
    except Exception as e:
        log(f"[Warn] HTTP 伺服器啟動失敗: {e}")


# ==================== 基礎工具 ====================
def now_tw():
    return datetime.now(TW_TZ)


def log(msg):
    print(f"[{now_tw():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def esc(x):
    return html.escape(str(x), quote=True)


def to_float(v, default=None):
    try:
        s = str(v).replace(",", "").strip()
        if s in ("", "-", "--"):
            return default
        return float(s)
    except (ValueError, TypeError):
        return default


def pick(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def roc_to_date(s):
    s = str(s or "").strip()
    try:
        if "/" in s:
            y, m, d = s.split("/")
            y = int(y)
            return date(y + 1911 if y < 1911 else y, int(m), int(d))
        if s.isdigit() and len(s) == 7:
            return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]))
        if s.isdigit() and len(s) == 8:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        pass
    return None


def tick_size(price):
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def round_tick(price, direction="nearest"):
    tk = tick_size(price)
    n = price / tk
    if direction == "up":
        n = math.ceil(n - 1e-9)
    elif direction == "down":
        n = math.floor(n + 1e-9)
    else:
        n = round(n)
    return round(n * tk, 2)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log(f"[Error] 讀取 {path} 失敗: {e}")
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def flag_done(name, day):
    return bool(load_json(FLAGS_FILE, {}).get(day, {}).get(name))


def mark_done(name, day):
    flags = load_json(FLAGS_FILE, {})
    flags.setdefault(day, {})[name] = True
    cutoff = (now_tw().date() - timedelta(days=14)).isoformat()
    save_json(FLAGS_FILE, {k: v for k, v in flags.items() if k >= cutoff})


# ==================== Telegram 推播 ====================
def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("[Warn] 未設定 Telegram 參數, 訊息僅輸出至終端機:\n" + message)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks, cur = [], ""
    for line in message.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    ok = True
    for c in chunks:
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": c,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not r.ok:
                log(f"[Error] Telegram 回應 {r.status_code}: {r.text[:200]}")
                ok = False
        except Exception as e:
            log(f"[Error] Telegram 推播失敗: {e}")
            ok = False
    return ok


def alert_once(key, text):
    log(f"[Alert] {text}")
    day = now_tw().strftime("%Y-%m-%d")
    if not flag_done("alert:" + key, day):
        send_telegram_message(f"⚠️ <b>系統警示</b>\n{esc(text)}")
        mark_done("alert:" + key, day)


# ==================== HTTP ====================
def http_get_json(url, retries=2, timeout=12, **kw):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout, headers=UA, **kw)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(1.5 * (i + 1))
    log(f"[Error] GET 失敗 {url}: {last}")
    return None


# ==================== 盤後資料與高周轉/動能選股 ====================
def fetch_twse_daily():
    data = http_get_json(TWSE_DAY_ALL, timeout=20)
    if not isinstance(data, list) or not data:
        return [], None
    rows, dates = [], set()
    for s in data:
        code = str(s.get("Code", "")).strip()
        chg_raw = str(s.get("Change", "0")).strip()
        if "X" in chg_raw.upper():
            continue
        o, h, l, c = (to_float(s.get(k)) for k in ("OpeningPrice", "HighestPrice", "LowestPrice", "ClosingPrice"))
        vol, val = to_float(s.get("TradeVolume")), to_float(s.get("TradeValue"))
        if None in (o, h, l, c, vol, val) or vol <= 0:
            continue
        d = roc_to_date(s.get("Date"))
        if d:
            dates.add(d)
        rows.append({
            "market": "tse", "code": code, "name": str(s.get("Name", "")).strip(), "open": o,
            "high": h, "low": l, "close": c, "change": to_float(chg_raw, 0.0),
            "vol_shares": vol, "lots": vol / 1000.0, "value": val
        })
    return rows, (max(dates) if dates else None)


def fetch_tpex_daily():
    data = http_get_json(TPEX_DAY_ALL, timeout=20)
    if not isinstance(data, list) or not data:
        return [], None
    rows, dates = [], set()
    for s in data:
        code = str(pick(s, "SecuritiesCompanyCode", "Code") or "").strip()
        name = str(pick(s, "CompanyName", "Name") or "").strip()
        o, h, l, c = (to_float(pick(s, *k)) for k in (("Open", "OpeningPrice"), ("High", "HighestPrice"),
                                                       ("Low", "LowestPrice"), ("Close", "ClosingPrice")))
        chg = to_float(pick(s, "Change"), 0.0)
        vol = to_float(pick(s, "TradingShares", "TradeVolume"))
        val = to_float(pick(s, "TransactionAmount", "TradeValue"))
        if not code or None in (o, h, l, c, vol, val) or vol <= 0:
            continue
        d = roc_to_date(pick(s, "Date"))
        if d:
            dates.add(d)
        rows.append({
            "market": "otc", "code": code, "name": name, "open": o, "high": h, "low": l, "close": c,
            "change": chg, "vol_shares": vol, "lots": vol / 1000.0, "value": val
        })
    return rows, (max(dates) if dates else None)


def fetch_twse_daytrade_shares(data_date):
    if not data_date:
        return {}
    data = http_get_json(
        TWSE_DAYTRADE_STATS,
        params={"date": data_date.strftime("%Y%m%d"), "selectType": "All", "response": "json"}
    )
    out = {}
    for t in (data or {}).get("tables") or []:
        fields = t.get("fields") or []
        if "證券代號" not in fields:
            continue
        idx = next((i for i, f in enumerate(fields) if "當日沖銷交易成交股數" in f), None)
        if idx is None:
            continue
        ic = fields.index("證券代號")
        for row in t.get("data") or []:
            sh = to_float(row[idx])
            if sh is not None:
                out[str(row[ic]).strip()] = sh
    return out


def fetch_risk_filters(data_date):
    excluded = {}
    notice = http_get_json(TWSE_NOTICE)
    if isinstance(notice, list):
        for it in notice:
            c = str(it.get("Code", "")).strip()
            if c:
                excluded[c] = "注意股"
    else:
        alert_once("notice_fail", "注意股 API 取得失敗, 本次未過濾注意股")

    allow = None
    lst = http_get_json(TWSE_DAYTRADE_LIST)
    if isinstance(lst, list) and len(lst) > 300:
        allow = {str(it.get("Code", "")).strip() for it in lst}
    else:
        alert_once("daytrade_list_fail", "可當沖清單取得失敗, 本次未過濾不可當沖標的")

    dt_shares = fetch_twse_daytrade_shares(data_date)
    if not dt_shares:
        alert_once("daytrade_ratio_fail", "當沖比資料取得失敗, 本次未過濾當沖過熱標的")
    return excluded, allow, dt_shares


def generate_watchlist(require_date=None, only_if_newer=False, announce=True):
    rows, data_date = fetch_twse_daily()
    if not rows or not data_date:
        alert_once("day_all_fail", "STOCK_DAY_ALL 取得失敗或無資料, 啟動備用名單")
        return False
    if require_date and data_date != require_date:
        log(f"[Info] 上市日成交資料日期為 {data_date}, 尚未更新到 {require_date}, 稍後重試")
        return False
    if only_if_newer:
        old = load_json(WATCHLIST_FILE, None)
        old_date = old.get("data_date") if isinstance(old, dict) else None
        if old_date and old_date >= data_date.isoformat():
            return "skip"

    otc, otc_date = fetch_tpex_daily()
    if otc and otc_date == data_date:
        rows += otc
    else:
        alert_once("tpex_fail", f"上櫃資料不可用或日期不符(上市 {data_date} / 上櫃 {otc_date}), 本次清單僅含上市股")

    excluded, allow, dt_shares = fetch_risk_filters(data_date)
    cands = []
    fallback_ranks = []

    for r in rows:
        code = r["code"]
        if not (len(code) == 4 and code.isdigit()) or code.startswith("0") or r["name"].endswith("-DR"):
            continue
        if code in excluded:
            continue
        if r["market"] == "tse" and allow is not None and code not in allow:
            continue
        if r["close"] < MIN_PRICE or r["lots"] < MIN_LOTS:
            continue
        ref = r["close"] - r["change"]
        if ref <= 0:
            continue
        amp = (r["high"] - r["low"]) / ref * 100

        # 保底池: 按成交金額排序備選
        fallback_ranks.append({
            "market": r["market"], "code": code, "name": r["name"], "close": r["close"],
            "pct_change": round(r["change"] / ref * 100, 2), "amplitude": round(amp, 2),
            "daytrade_ratio": None, "val": r["value"], "vol": int(r["lots"])
        })

        if amp < MIN_AMPLITUDE_PCT:
            continue
        ratio = None
        if r["market"] == "tse" and dt_shares.get(code):
            ratio = dt_shares[code] / r["vol_shares"] * 100
            if ratio >= DAYTRADE_RATIO_MAX:
                continue

        cands.append({
            "market": r["market"], "code": code, "name": r["name"], "close": r["close"],
            "pct_change": round(r["change"] / ref * 100, 2), "amplitude": round(amp, 2),
            "daytrade_ratio": None if ratio is None else round(ratio, 1),
            "val": r["value"], "vol": int(r["lots"])
        })

    cands.sort(key=lambda x: x["val"], reverse=True)
    fallback_ranks.sort(key=lambda x: x["val"], reverse=True)

    # 動態保底邏輯: 若嚴選條件產出小於 5 檔，自動以市場最大成交金額主流標的補足
    if len(cands) < 5:
        log(f"[Warn] 嚴格篩選僅 {len(cands)} 檔，啟動成交值前 25 大自動保底機制")
        watch = fallback_ranks[:WATCHLIST_SIZE]
    else:
        watch = cands[:WATCHLIST_SIZE]

    if not watch:
        alert_once("empty_watch", "篩選後作戰清單仍為空, 請檢查資料庫")
        return False

    save_json(WATCHLIST_FILE, {
        "data_date": data_date.isoformat(),
        "built_at": now_tw().isoformat(),
        "items": watch
    })
    if announce:
        msg = (f"🎯 <b>【明日當沖精選作戰清單】</b>(資料日 {data_date})\n━━━━━━━━━━━━━━━━━━\n"
               f"高周轉與大震幅嚴選, 已排除注意股與當沖過熱 (≥ {MIN_AMPLITUDE_PCT}%):\n\n")
        for i, it in enumerate(watch[:10], 1):
            mk = "櫃" if it["market"] == "otc" else "市"
            dt_str = f"｜沖 {it['daytrade_ratio']}%" if it.get("daytrade_ratio") else ""
            msg += (f"{i}. <b>{esc(it['code'])} {esc(it['name'])}</b>({mk})｜收 <code>{it['close']}</code> "
                    f"({it['pct_change']:+}%)｜量 <code>{it['vol']:,}</code> 張{dt_str}\n")
        if len(watch) > 10:
            msg += f"<i>...另有 {len(watch) - 10} 檔已列入明日監控池</i>\n"
        msg += ("━━━━━━━━━━━━━━━━━━\n📌 <b>作戰紀律準則</b>:\n"
                "• 09:03～09:18 僅針對「跳空開高＋紅K不破開盤」執行快沖\n"
                "• 09:35～10:30 主流回踩VWAP有撐、過前高突破進場\n"
                "• 10:30～11:20 洗盤泥沼時段禁止開新倉\n"
                "• 盤中遇主力熄燈(賣單壓頂)果斷獲利出場")
        send_telegram_message(msg)
    return True


def load_watchlist():
    data = load_json(WATCHLIST_FILE, None)
    items, dd = None, None
    if isinstance(data, dict):
        items, dd = data.get("items"), data.get("data_date")
    elif isinstance(data, list):
        items = data
    if items:
        if dd and (now_tw().date() - date.fromisoformat(dd)).days > 5:
            alert_once("stale_watch", f"作戰清單已過期(資料日 {dd}), 請檢查盤後建檔流程")
        return [f"{it.get('market', 'tse')}_{it['code']}.tw" for it in items]
    alert_once("no_watchlist", "找不到作戰清單, 啟用保底主流核心名單")
    return list(FALLBACK_WATCH)


# ==================== 即時行情 (MIS) ====================
_session = None


def mis_session():
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(UA)
        try:
            s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=8)
        except Exception:
            pass
        _session = s
    return _session


def fetch_mis_quotes(ex_ch_list, chunk=40):
    global _session
    out = []
    items = list(ex_ch_list)
    for i in range(0, len(items), chunk):
        part = items[i:i + chunk]
        url = (f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={'|'.join(part)}"
               f"&json=1&delay=0&_={int(time.time() * 1000)}")
        try:
            r = mis_session().get(url, timeout=8)
            r.raise_for_status()
            out.extend(r.json().get("msgArray", []))
        except Exception as e:
            log(f"[Error] MIS 取價失敗: {e}")
            _session = None
        if i + chunk < len(items):
            time.sleep(0.3)
    return out


def _split_nums(s):
    return [x for x in str(s or "").split("_") if x]


def parse_quote(q):
    code = q.get("c")
    if not code:
        return None
    last_p = to_float(q.get("z"))
    trade_vol_shares = to_float(q.get("v"), 0) or 0
    trade_val_amount = to_float(q.get("tv_amt", q.get("a_amt", 0)))

    vwap = None
    if trade_val_amount and trade_vol_shares and trade_vol_shares > 0:
        vwap = round(trade_val_amount / trade_vol_shares, 2)

    return {
        "code": code, "name": q.get("n", ""), "ex": q.get("ex", "tse"),
        "last": last_p,
        "bid1": to_float((_split_nums(q.get("b")) or [None])[0]),
        "ask1": to_float((_split_nums(q.get("a")) or [None])[0]),
        "open": to_float(q.get("o")), "high": to_float(q.get("h")), "low": to_float(q.get("l")),
        "prev": to_float(q.get("y")), "vol": int(trade_vol_shares),
        "trade_val": trade_val_amount,
        "vwap": vwap,
        "date": q.get("d"),
        "ask_vols": [int(x) for x in _split_nums(q.get("f")) if x.isdigit()],
        "bid_vols": [int(x) for x in _split_nums(q.get("g")) if x.isdigit()],
    }


def analyze_order_book(q):
    """五檔買賣結構、力竭檢查與下虛上實辨識"""
    asks, bids = q["ask_vols"], q["bid_vols"]
    total = sum(asks) + sum(bids)
    if total == 0:
        return 0.5, False, "中性"

    share = sum(bids) / total
    first_ask = asks[0] if asks else 0
    first_bid = bids[0] if bids else 0
    rest_asks = asks[1:] if len(asks) > 1 else [first_ask]
    rest_bids = bids[1:] if len(bids) > 1 else [first_bid]
    avg_rest_ask = sum(rest_asks) / len(rest_asks) if rest_asks else first_ask
    avg_rest_bid = sum(rest_bids) / len(rest_bids) if rest_bids else first_bid

    # 多頭力竭: 賣一異常龐大 (壓頂出貨) 且買盤比率萎縮
    is_exhausted = (first_ask > avg_rest_ask * 3.2) and (share < 0.50)

    structure = "中性"
    if first_bid > avg_rest_bid * 3 and share > 0.65 and q["last"] and q["high"] and q["last"] < q["high"] * 0.99:
        structure = "誘多疑慮(下實上虛)"
    elif first_ask >= avg_rest_ask * 1.5 and share >= 0.55:
        structure = "健康吃單(下虛上實)"

    return share, is_exhausted, structure


# ==================== 策略評估 ====================
def _stamp():
    n = now_tw()
    return n.strftime("%Y-%m-%d"), n.strftime("%H:%M:%S")


def make_signal_id(d, t, code, strategy):
    return f"{d}_{t}_{code}_{strategy}"


def calculate_vwap_bias(p, o, h, l, vwap):
    ref_vwap = vwap if vwap and vwap > 0 else round((h + l + p) / 3, 2)
    bias = ((p - ref_vwap) / ref_vwap) * 100
    return ref_vwap, round(bias, 2)


def evaluate_opening_rush(q, market_status):
    """09:03 ~ 09:18 開盤快沖"""
    p, o, h, l, y = q["last"], q["open"], q["high"], q["low"], q["prev"]
    if not p or p <= 0 or not o or not h or not y or y <= 0 or o <= 0:
        return None

    if market_status.get("weak") and market_status.get("pct", 0) < -0.6:
        return None

    gap = (o - y) / y * 100
    pct = (p - y) / y * 100
    share, exhausted, structure = analyze_order_book(q)

    if exhausted or structure == "誘多疑慮(下實上虛)":
        return None

    if not (1.5 <= gap <= 5.0 and p >= o and p >= h * 0.995 and 2.0 <= pct <= 6.5
            and share >= 0.58 and q["vol"] >= 800):
        return None

    ref_vwap, bias = calculate_vwap_bias(p, o, h, l, q["vwap"])
    if bias > MAX_VWAP_BIAS + 0.5:
        return None

    stop = round_tick(min(max(o, p * 0.985), p * (1 - MIN_RISK_RUSH)), "down")
    target = round_tick(p + (p - stop) * 1.5, "up")
    d, t = _stamp()

    return {
        "signal_id": make_signal_id(d, t, q["code"], "開盤快沖"),
        "strategy": "開盤快沖", "date": d, "time": t,
        "market": q["ex"], "code": q["code"], "name": q["name"],
        "price": p, "pct_change": round(pct, 2),
        "vwap": ref_vwap, "vwap_bias": bias,
        "volume": q["vol"], "stop_loss": stop, "target_price": target, "wave": "",
        "day_high_at_signal": h, "day_low_at_signal": l,
        "extra_info": f"五檔買比 {share * 100:.1f}% ｜ VWAP乖離 {bias:+}% ｜ {structure}",
        "link": f"https://tw.stock.yahoo.com/quote/{q['code']}"
    }


def evaluate_trend_breakout(q, market_status):
    """09:35 ~ 13:15 壓縮後帶量突破"""
    p, o, h, l, y = q["last"], q["open"], q["high"], q["low"], q["prev"]
    if not p or p < MIN_PRICE or not h or not l or not y or y <= 0:
        return None

    if market_status.get("weak"):
        return None

    pct = (p - y) / y * 100
    share, exhausted, structure = analyze_order_book(q)

    if exhausted or structure == "誘多疑慮(下實上虛)":
        return None

    if not (p >= h * 0.995 and 2.5 <= pct <= 7.5 and share >= 0.55):
        return None

    ref_vwap, bias = calculate_vwap_bias(p, o, h, l, q["vwap"])

    if bias > MAX_VWAP_BIAS:
        log(f"[Skip] {q['code']} 創高但 VWAP 正乖離過大 ({bias}% > {MAX_VWAP_BIAS}%), 放棄進場")
        return None

    intraday_swing = (h - l) / l * 100
    if intraday_swing > 9.0:
        return None

    stop = round_tick(min(max(l, p * 0.978), p * (1 - MIN_RISK_TREND)), "down")
    target = round_tick(p + (p - stop) * 2.0, "up")
    d, t = _stamp()

    return {
        "signal_id": make_signal_id(d, t, q["code"], "主流突破"),
        "strategy": "主流突破", "date": d, "time": t,
        "market": q["ex"], "code": q["code"], "name": q["name"],
        "price": p, "pct_change": round(pct, 2),
        "vwap": ref_vwap, "vwap_bias": bias,
        "volume": q["vol"], "stop_loss": stop, "target_price": target, "wave": 1,
        "day_high_at_signal": h, "day_low_at_signal": l,
        "extra_info": f"五檔買比 {share * 100:.1f}% ｜ VWAP乖離 {bias:+}% ｜ 壓縮後首發",
        "link": f"https://tw.stock.yahoo.com/quote/{q['code']}"
    }


# ==================== 訊號紀錄 (CSV) ====================
def read_signals():
    if not os.path.exists(SIGNALS_FILE):
        return []
    rows = []
    with open(SIGNALS_FILE, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []
        for r in reader:
            if not r:
                continue
            d = dict(zip(header, r))
            rows.append(d)
    return rows


def ensure_signals_schema():
    if not os.path.exists(SIGNALS_FILE):
        return
    with open(SIGNALS_FILE, newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    if header == SIGNAL_COLUMNS:
        return
    rows = read_signals()
    backup = f"{SIGNALS_FILE}.bak_{now_tw():%Y%m%d%H%M%S}"
    shutil.copy(SIGNALS_FILE, backup)
    with open(SIGNALS_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=SIGNAL_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for d in rows:
            d.setdefault("market", "tse")
            d.setdefault("vwap", "")
            d.setdefault("vwap_bias", "")
            d["signal_id"] = d.get("signal_id") or make_signal_id(d.get("date"), d.get("time"),
                                                                  d.get("code"), d.get("strategy"))
            w.writerow(d)
    log(f"[Info] 已將 {SIGNALS_FILE} 轉為固定欄位 ({len(rows)} 筆), 備份於 {backup}")


def append_rows(path, columns, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new_file = not os.path.isfile(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def build_recent_alerts(today):
    alerts = {}
    for d in read_signals():
        if d.get("date") != today:
            continue
        try:
            t = TW_TZ.localize(datetime.strptime(f"{d['date']} {d['time']}", "%Y-%m-%d %H:%M:%S"))
            price = float(d["price"])
        except (KeyError, ValueError):
            continue
        e = alerts.setdefault(str(d["code"]).strip(), {"time": t, "price": price, "count": 0})
        e["count"] += 1
        if t >= e["time"]:
            e["time"], e["price"] = t, price
    return alerts


# ==================== 部位狀態與主力熄燈 (轉折預警) ====================
def position_from_signal(d):
    f = lambda k: to_float(d.get(k))
    return {
        "signal_id": d["signal_id"], "date": d["date"], "time": d["time"], "strategy": d["strategy"],
        "market": d.get("market") or "tse", "code": str(d["code"]).strip(), "name": d.get("name", ""),
        "entry": f("price"), "stop": f("stop_loss"), "target": f("target_price"),
        "original_stop": f("stop_loss"),
        "day_high_at_signal": f("day_high_at_signal"), "day_low_at_signal": f("day_low_at_signal"),
        "touched_target": False, "touched_stop": False, "first_touch": None, "trailed_to_be": False,
        "warned_exhaustion": False, "resolved": False
    }


def ensure_positions_state(day, state):
    done = set(pd.read_csv(RESULTS_FILE, usecols=["signal_id"])["signal_id"]) if os.path.exists(RESULTS_FILE) else set()
    for d in read_signals():
        if d.get("date") != day or not d.get("signal_id"):
            continue
        if d["signal_id"] in state or d["signal_id"] in done:
            continue
        if None in (to_float(d.get("price")), to_float(d.get("stop_loss")), to_float(d.get("target_price"))):
            continue
        state[d["signal_id"]] = position_from_signal(d)
    return state


def update_positions_and_check_exhaustion(parsed, state):
    """
    即時更新持倉狀態，並觸發「主力熄燈 / 動能竭盡」轉折通知
    """
    qmap = {q["code"]: q for q in parsed}
    exhaustion_alerts = []

    for p in state.values():
        if p.get("resolved"):
            continue
        q = qmap.get(p["code"])
        if not q:
            continue

        price = q["last"] if q["last"] is not None else q["bid1"]
        if price is None:
            continue

        # 1. 保本停損推升: 獲利達到 +1.0% 時自動將停損移動至成本價
        if not p.get("trailed_to_be") and price >= p["entry"] * 1.010:
            p["stop"] = p["entry"]
            p["trailed_to_be"] = True

        # 2. 熄燈轉折預警 (動能竭盡偵測):
        share, is_exhausted, _ = analyze_order_book(q)
        high_p = q.get("high") or p["entry"]
        pullback = (high_p - price) / high_p * 100 if high_p > 0 else 0

        if not p.get("warned_exhaustion") and price > p["entry"] and pullback >= 0.8:
            if is_exhausted or share < 0.46:
                p["warned_exhaustion"] = True
                exhaustion_alerts.append({
                    "code": p["code"], "name": p["name"], "entry": p["entry"], "price": price,
                    "gain_pct": round((price - p["entry"]) / p["entry"] * 100, 2),
                    "pullback": round(pullback, 2),
                    "link": f"https://tw.stock.yahoo.com/quote/{p['code']}"
                })

        # 3. 停損與停利觸及判定
        before = (p["touched_target"], p["touched_stop"])
        if price >= p["target"]:
            p["touched_target"] = True
        if price <= p["stop"]:
            p["touched_stop"] = True

        dh, dl = p.get("day_high_at_signal"), p.get("day_low_at_signal")
        if dh is not None and q["high"] is not None and q["high"] > dh and q["high"] >= p["target"]:
            p["touched_target"] = True
        if dl is not None and q["low"] is not None and q["low"] < dl and q["low"] <= p["stop"]:
            p["touched_stop"] = True
