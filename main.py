# -*- coding: utf-8 -*-
"""
台股當沖訊號機器人 v3 (高勝率強化版)

核心優化亮點:
  1. VWAP (量加權均價線) 與正乖離過熱濾網 (排除 Bias > 2.2% 追高陷阱)
  2. 加權指數 (tse_t00.tw) 多空共振濾網 (大盤轉弱/開高走低時提高進場門檻或暫停做多)
  3. 避開盤中垃圾洗盤時段 (10:30 ~ 11:20 流動性真空期禁止開新倉)
  4. 五檔下虛上實真突破驗證與大單抽單異常檢查
  5. 支援保本防守 (Trailing Stop) 與嚴格時間停損 (Time Stop)
  6. 完整實盤結果追蹤、R倍數損益結算與勝率統計覆盤

用法:
  python daytrade_bot.py                 單次執行 (Cron / GitHub Actions)
  python daytrade_bot.py --loop          常駐輪詢 (推薦, 本機/VPS, 間隔 15 秒)
  python daytrade_bot.py --report        輸出累計勝率/期望值報告 (--days 30, --send 同步推播)
  python daytrade_bot.py --finalize      手動結算今日或指定日期 (--finalize 2026-10-21)
  python daytrade_bot.py --build-watchlist 立即建立明日作戰清單
  python daytrade_bot.py --check-api     檢查資料來源與欄位是否正常
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
from datetime import datetime, date, time as dtime, timedelta

import pandas as pd
import pytz
import requests

TW_TZ = pytz.timezone("Asia/Taipei")

# ==================== 設定 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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

# 選股條件
MIN_PRICE = 25.0
MIN_LOTS = 2000
MIN_AMPLITUDE_PCT = 3.0          # 前一日振幅門檻
WATCHLIST_SIZE = 30
DAYTRADE_RATIO_MAX = 45.0        # 當沖比 >= 此值視為過熱

# 停損與乖離限制
MAX_VWAP_BIAS = 2.2              # 正乖離大於 2.2% 視為過熱力竭, 嚴禁追高
MIN_RISK_RUSH = 0.008
MIN_RISK_TREND = 0.010

UA = {"User-Agent": "Mozilla/5.0 (compatible; daytrade-bot/3.0)"}
TWSE_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_NOTICE = "https://openapi.twse.com.tw/v1/announcement/notice"
TWSE_DAYTRADE_LIST = "https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U"
TWSE_DAYTRADE_STATS = "https://www.twse.com.tw/rwd/zh/dayTrading/TWTB4U"
TPEX_DAY_ALL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

INDEX_SYMBOL = "tse_t00.tw"
FALLBACK_WATCH = ["tse_2330.tw", "tse_2317.tw", "tse_2454.tw", "tse_3231.tw", "tse_2382.tw"]

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


# ==================== 盤後資料與籌碼過濾 ====================
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
        alert_once("day_all_fail", "STOCK_DAY_ALL 取得失敗或無資料, 無法建立作戰清單")
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
    watch = cands[:WATCHLIST_SIZE]
    if not watch:
        alert_once("empty_watch", "篩選後作戰清單為空, 請檢查篩選條件與資料來源")
        return False

    save_json(WATCHLIST_FILE, {
        "data_date": data_date.isoformat(),
        "built_at": now_tw().isoformat(),
        "items": watch
    })
    if announce:
        msg = (f"🎯 <b>【明日當沖精選作戰清單】</b>(資料日 {data_date})\n━━━━━━━━━━━━━━━━━━\n"
               f"成交值排序, 已排除注意股、當沖過熱、ETF, 且振幅 ≥ {MIN_AMPLITUDE_PCT}%:\n\n")
        for i, it in enumerate(watch[:10], 1):
            mk = "櫃" if it["market"] == "otc" else "市"
            msg += (f"{i}. <b>{esc(it['code'])} {esc(it['name'])}</b>({mk})｜收 <code>{it['close']}</code> "
                    f"({it['pct_change']:+}%)｜量 <code>{it['vol']:,}</code> 張\n")
        if len(watch) > 10:
            msg += f"<i>...另有 {len(watch) - 10} 檔已列入明日監控池</i>\n"
        msg += ("━━━━━━━━━━━━━━━━━━\n📌 <b>明日作戰準則</b>:\n"
                "• 09:03～09:18 僅針對「跳空開高＋不破開盤」執行快沖\n"
                "• 09:35～10:30 主流回檔有撐、VWAP支撐突破進場\n"
                "• 10:30～11:20 洗盤垃圾時間禁止開新倉")
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
    alert_once("no_watchlist", "找不到作戰清單, 改用備用名單")
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

    # 計算 VWAP
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

    # 多頭力竭: 賣一異常重且買單比例低下
    is_exhausted = (first_ask > avg_rest_ask * 3) and (share < 0.52)

    structure = "中性"
    if first_bid > avg_rest_bid * 3 and share > 0.65 and q["last"] and q["high"] and q["last"] < q["high"] * 0.99:
        structure = "誘多疑慮(下實上虛)"
    elif first_ask >= avg_rest_ask * 1.5 and share >= 0.55:
        structure = "健康吃單(下虛上實)"

    return share, is_exhausted, structure


def get_market_index_status():
    """加權指數多空監控 (tse_t00.tw)"""
    try:
        raw = fetch_mis_quotes([INDEX_SYMBOL])
        if raw:
            q = parse_quote(raw[0])
            if q and q["last"] and q["prev"]:
                pct = (q["last"] - q["prev"]) / q["prev"] * 100
                is_weak = (pct < -0.3) or (q["open"] and q["last"] < q["open"])
                return {"valid": True, "pct": round(pct, 2), "weak": is_weak, "last": q["last"]}
    except Exception as e:
        log(f"[Warn] 讀取加權指數失敗: {e}")
    return {"valid": False, "pct": 0.0, "weak": False, "last": 0.0}


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
    p, o, h, l, y = q["last"], q["open"], q["high"], q["low"], q["prev"]
    if not p or p <= 0 or not o or not h or not y or y <= 0 or o <= 0:
        return None

    # 大盤跌幅超過 -0.6% 時暫停開盤快沖
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
    p, h, l, y = q["last"], q["high"], q["low"], q["prev"]
    if not p or p < MIN_PRICE or not h or not l or not y or y <= 0:
        return None

    # 大盤偏弱時嚴禁做多突破
    if market_status.get("weak"):
        return None

    pct = (p - y) / y * 100
    share, exhausted, structure = analyze_order_book(q)

    if exhausted or structure == "誘多疑慮(下實上虛)":
        return None

    if not (p >= h * 0.995 and 2.5 <= pct <= 7.5 and share >= 0.55):
        return None

    ref_vwap, bias = calculate_vwap_bias(p, q["open"], h, l, q["vwap"])

    # 正乖離過大 (> 2.2%) 判定為短線竭盡誘多, 嚴禁追高
    if bias > MAX_VWAP_BIAS:
        log(f"[Skip] {q['code']} 創高但 VWAP 正乖離過大 ({bias}% > {MAX_VWAP_BIAS}%), 放棄進場")
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
        "extra_info": f"五檔買比 {share * 100:.1f}% ｜ VWAP乖離 {bias:+}% ｜ 回踩有撐",
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


# ==================== 結果追蹤與保本停損 ====================
def position_from_signal(d):
    f = lambda k: to_float(d.get(k))
    return {
        "signal_id": d["signal_id"], "date": d["date"], "time": d["time"], "strategy": d["strategy"],
        "market": d.get("market") or "tse", "code": str(d["code"]).strip(), "name": d.get("name", ""),
        "entry": f("price"), "stop": f("stop_loss"), "target": f("target_price"),
        "original_stop": f("stop_loss"),
        "day_high_at_signal": f("day_high_at_signal"), "day_low_at_signal": f("day_low_at_signal"),
        "touched_target": False, "touched_stop": False, "first_touch": None, "trailed_to_be": False,
        "resolved": False
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


def update_positions(parsed, state):
    qmap = {q["code"]: q for q in parsed}
    for p in state.values():
        if p.get("resolved"):
            continue
        q = qmap.get(p["code"])
        if not q:
            continue

        price = q["last"] if q["last"] is not None else q["bid1"]
        if price is None:
            continue

        # 保本停損機制: 獲利達到 +1.0% 時將停損移動至成本價
        if not p.get("trailed_to_be") and price >= p["entry"] * 1.010:
            p["stop"] = p["entry"]
            p["trailed_to_be"] = True

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

        new = {k for k, b, a in (("target", before[0], p["touched_target"]), ("stop", before[1], p["touched_stop"]))
               if a and not b}
        if p.get("first_touch") is None and len(new) == 1 and not any(before):
            p["first_touch"] = new.pop()
    return state


def compute_trade_pnl(entry, exit_price, stop):
    e_eff, x_eff = entry * (1 + SLIPPAGE), exit_price * (1 - SLIPPAGE)
    gross = (x_eff - e_eff) / e_eff * 100
    cost = (2 * FEE_RATE * FEE_DISCOUNT + DAYTRADE_TAX) * 100
    net = gross - cost
    risk = (entry - stop) / entry * 100
    return round(gross, 3), round(cost, 3), round(net, 3), round(risk, 3), (round(net / risk, 2) if risk > 0 else None)


def resolve_outcome(p, close):
    tt, ts, first = p["touched_target"], p["touched_stop"], p.get("first_touch")
    legacy = p.get("day_high_at_signal") is None
    note = ""
    if tt and ts:
        if first == "target":
            outcome, exit_p = "WIN", p["target"]
        elif first == "stop":
            outcome, exit_p = "LOSS", p["stop"]
        else:
            outcome, exit_p = "AMBIGUOUS", p["stop"]
            note = "停損與停利皆被觸及且無法判定先後, 保守以停損計"
    elif tt:
        outcome, exit_p = "WIN", p["target"]
    elif ts:
        outcome, exit_p = "LOSS", p["stop"]
        if p.get("trailed_to_be"):
            note = "觸發保本出場"
    elif legacy:
        outcome, exit_p, note = "NO_DATA", None, "舊版訊號缺少進場當下極值, 不計入統計"
    else:
        outcome, exit_p = "TIME_EXIT", close
        note = "未觸及停損/停利, 以收盤價出場"

    row = {
        "signal_id": p["signal_id"], "date": p["date"], "time": p["time"], "strategy": p["strategy"],
        "market": p["market"], "code": p["code"], "name": p["name"], "entry": p["entry"], "stop": p["stop"],
        "target": p["target"], "outcome": outcome, "touched_target": tt, "touched_stop": ts,
        "first_touch": first or "", "close_price": close, "note": note
    }
    if exit_p is not None:
        g, c, n, r, rm = compute_trade_pnl(p["entry"], exit_p, p["original_stop"] or p["stop"])
        row.update({"exit_price": exit_p, "gross_pct": g, "cost_pct": c, "net_pct": n, "risk_pct": r, "r_multiple": rm})
    return row


def finalize_results(day):
    state = ensure_positions_state(day, load_json(STATE_FILE, {}))
    pending = {k: p for k, p in state.items() if p["date"] == day and not p.get("resolved")}
    if pending:
        ex = sorted({f"{p['market']}_{p['code']}.tw" for p in pending.values()})
        parsed = [q for q in (parse_quote(x) for x in fetch_mis_quotes(ex)) if q]
        update_positions(parsed, state)
        qmap = {q["code"]: q for q in parsed}
        rows = []
        for p in pending.values():
            q = qmap.get(p["code"])
            if not q or q["last"] is None:
                log(f"[Warn] {p['code']} 無收盤價, 稍後重試")
                continue
            rows.append(resolve_outcome(p, q["last"]))
            p["resolved"] = True
        append_rows(RESULTS_FILE, RESULT_COLUMNS, rows)
    cutoff = (now_tw().date() - timedelta(days=3)).isoformat()
    state = {k: v for k, v in state.items() if not (v.get("resolved") and v["date"] < cutoff)}
    save_json(STATE_FILE, state)


# ==================== 勝率統計 ====================
def wilson_ci(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return centre - half, centre + half


def load_results_df(days=0, day=None):
    if not os.path.exists(RESULTS_FILE):
        return pd.DataFrame()
    df = pd.read_csv(RESULTS_FILE, encoding="utf-8-sig", dtype={"code": str})
    df = df[df["outcome"].isin(["WIN", "LOSS", "AMBIGUOUS", "TIME_EXIT"])].copy()
    df["net_pct"] = pd.to_numeric(df["net_pct"], errors="coerce")
    df = df.dropna(subset=["net_pct"])
    if day:
        df = df[df["date"] == day]
    elif days > 0:
        df = df[df["date"] >= (now_tw().date() - timedelta(days=days)).isoformat()]
    return df.sort_values(["date", "time"]).reset_index(drop=True)


def compute_stats(df):
    n = len(df)
    if n == 0:
        return None
    net = df["net_pct"]
    wins, losses = net[net > 0], net[net <= 0]
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = -losses.mean() if len(losses) else 0.0
    loss_sum = -losses.sum()
    lo, hi = wilson_ci(len(wins), n)
    std = net.std(ddof=1) if n > 1 else 0.0
    equity = net.cumsum()
    streak = cur = 0
    for v in net:
        cur = cur + 1 if v <= 0 else 0
        streak = max(streak, cur)
    return {
        "n": n, "win_rate": len(wins) / n, "ci": (lo, hi), "avg_win": avg_win, "avg_loss": avg_loss,
        "expectancy": net.mean(), "profit_factor": (wins.sum() / loss_sum) if loss_sum > 0 else float("inf"),
        "breakeven_wr": (avg_loss / (avg_win + avg_loss)) if (avg_win + avg_loss) > 0 else None,
        "t_stat": (net.mean() / (std / math.sqrt(n))) if std > 0 else 0.0, "total": net.sum(),
        "max_dd": (equity.cummax() - equity).max(), "max_loss_streak": streak,
        "outcomes": df["outcome"].value_counts().to_dict()
    }


def format_stats(s, title):
    if not s:
        return [title, "  尚無已結算的訊號"]
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    be = "-" if s["breakeven_wr"] is None else f"{s['breakeven_wr'] * 100:.1f}%"
    lines = [
        title,
        f"  樣本數: {s['n']}   結果分布: {s['outcomes']}",
        f"  勝率(淨損益>0): {s['win_rate'] * 100:.1f}%   95%信賴區間: {s['ci'][0] * 100:.0f}%~{s['ci'][1] * 100:.0f}%",
        f"  平均獲利 {s['avg_win']:+.2f}%  平均虧損 -{s['avg_loss']:.2f}%  損益兩平勝率 {be}",
        f"  期望值(每筆淨%): {s['expectancy']:+.3f}%   獲利因子: {pf}   t值: {s['t_stat']:.2f}",
        f"  累計淨損益: {s['total']:+.2f}%   最大回落: {s['max_dd']:.2f}%   最長連虧: {s['max_loss_streak']}"
    ]
    if s["n"] < 100:
        lines.append(f"  ⚠ 樣本僅 {s['n']} 筆(<100), 以上數字不具統計意義, 請勿據此調參或加碼")
    elif abs(s["t_stat"]) < 2:
        lines.append("  ⚠ |t| < 2: 期望值與 0 沒有顯著差異")
    return lines


def build_report(days=0):
    df = load_results_df(days=days)
    label = f"近 {days} 天" if days else "全部"
    lines = format_stats(compute_stats(df), f"【累計績效 - {label}】")
    if len(df):
        for strat, g in df.groupby("strategy"):
            lines += [""] + format_stats(compute_stats(g), f"【{strat}】")
    total = len(read_signals())
    lines += ["", f"signals_history.csv 共 {total} 筆訊號, 其中 {len(df)} 筆已納入結果統計"]
    lines.append(f"成本假設: 手續費 {FEE_RATE * 100:.4f}%×{FEE_DISCOUNT} 雙邊 + 證交稅 {DAYTRADE_TAX * 100:.2f}% "
                 f"+ 單邊滑價 {SLIPPAGE * 100:.2f}%")
    return lines


def finalize_and_review(today):
    sigs = [d for d in read_signals() if d.get("date") == today]
    probe = [q for q in (parse_quote(x) for x in fetch_mis_quotes(["tse_2330.tw"])) if q]
    if not probe or probe[0]["date"] != today.replace("-", ""):
        log("[Info] 查無今日行情 (休市日或 MIS 暫時無回應)")
        return not sigs
    finalize_results(today)
    df_today = load_results_df(day=today)
    lines = [f"今日訊號 {len(sigs)} 檔, 已結算 {len(df_today)} 檔"]
    if len(df_today):
        for _, r in df_today.iterrows():
            lines.append(f"{r['outcome']:<9} {r['code']} {r['name']}  進{r['entry']}→出{r['exit_price']}  "
                         f"淨{r['net_pct']:+.2f}%")
        lines += [""] + format_stats(compute_stats(df_today), "【今日】")
    elif not sigs:
        lines.append("今日作戰清單未出現符合高勝率力道標準的訊號, 空手觀望保持紀律。")
    lines += [""] + format_stats(compute_stats(load_results_df()), "【累計】")
    send_telegram_message(f"📋 <b>每日當沖復盤</b> {esc(today)}\n<pre>{esc(chr(10).join(lines))}</pre>")
    return True


# ==================== 推播發送 ====================
def broadcast_signal(s):
    head = (f"標的: <b>{esc(s['code'])} {esc(s['name'])}</b> (作戰清單)\n"
            f"現價: <code>{s['price']}</code> 元 (+{s['pct_change']}%)\n"
            f"狀態: <code>{esc(s['extra_info'])}</code>\n成交量: <code>{s['volume']:,}</code> 張\n"
            "━━━━━━━━━━━━━━━━━━\n")
    link = f'🔗 <a href="{esc(s["link"])}">開啟 Yahoo 看盤確認</a>'
    if s["strategy"] == "開盤快沖":
        msg = ("⚡ <b>【開盤 15 分快沖訊號】</b> ⚡\n━━━━━━━━━━━━━━━━━━\n" + head +
               f"🎯 <b>建議買進</b>: <code>{s['price']}</code> ～ <code>{round(s['price'] * 1.004, 2)}</code>\n"
               f"🛑 <b>嚴格停損</b>: <code>{s['stop_loss']}</code> 元 (跌破開盤即撤)\n"
               f"🏆 <b>短沖停利</b>: <code>{s['target_price']}</code> 元\n"
               "⏱ <b>時間停損</b>: 進場 8 分鐘內未脫離成本區建議平倉\n"
               "🛡 <b>保本防守</b>: 獲利達 +1.0% 時請將停損推至成本價\n"
               "━━━━━━━━━━━━━━━━━━\n" + link)
    else:
        wave = int(to_float(s.get("wave"), 1) or 1)
        wave_str = f" (第 {wave} 波發動)" if wave > 1 else ""
        msg = (f"🔥 <b>【主流大單帶量突破{wave_str}】</b> 🔥\n━━━━━━━━━━━━━━━━━━\n" + head +
               f"🎯 <b>建議掛單</b>: <code>{s['price']}</code> ～ <code>{round(s['price'] * 1.005, 2)}</code>\n"
               f"🛑 <b>嚴守停損</b>: <code>{s['stop_loss']}</code> 元\n"
               f"🏆 <b>波段停利</b>: <code>{s['target_price']}</code> 元 (風報比 1:2)\n"
               "⏱ <b>時間風控</b>: 10 分鐘內未走出利潤建議平倉\n"
               "🛡 <b>保本防守</b>: 獲利達 +1.0% 時請將停損推至成本價\n"
               "━━━━━━━━━━━━━━━━━━\n" + link)
    send_telegram_message(msg)


# ==================== 主流程控制 ====================
def run_intraday(now, today):
    t = now.time()
    if t < dtime(9, 0) or t > dtime(13, 30):
        return

    # 關鍵時段過濾: 10:30 ~ 11:20 垃圾洗盤時段, 禁止開新倉
    is_lunch_slump = (dtime(10, 30) <= t < dtime(11, 20))

    watch_ex = load_watchlist()
    state = ensure_positions_state(today, load_json(STATE_FILE, {}))
    open_ex = {f"{p['market']}_{p['code']}.tw" for p in state.values() if p["date"] == today and not p.get("resolved")}

    fetch_list = sorted(set(watch_ex) | open_ex | {INDEX_SYMBOL})
    parsed = [q for q in (parse_quote(x) for x in fetch_mis_quotes(fetch_list)) if q]
    if not parsed:
        log("[Warn] MIS 沒有回傳任何行情")
        return
    if not any(q["date"] == today.replace("-", "") for q in parsed):
        log("[Info] 非交易日或尚未開盤")
        return

    update_positions(parsed, state)

    idx_q = next((q for q in parsed if q["code"] == "t00"), None)
    market_status = {"valid": False, "pct": 0.0, "weak": False}
    if idx_q and idx_q["last"] and idx_q["prev"]:
        idx_pct = (idx_q["last"] - idx_q["prev"]) / idx_q["prev"] * 100
        market_status = {
            "valid": True, "pct": round(idx_pct, 2),
            "weak": (idx_pct < -0.3) or (idx_q["open"] and idx_q["last"] < idx_q["open"])
        }

    watch_codes = {e.split("_", 1)[1].split(".")[0] for e in watch_ex}
    recent = build_recent_alerts(today)
    new_signals = []

    # 時段 A: 開盤快沖 (09:03 ~ 09:18)
    if dtime(9, 3) <= t <= dtime(9, 18):
        for q in parsed:
            if q["code"] not in watch_codes or q["code"] in recent:
                continue
            s = evaluate_opening_rush(q, market_status)
            if s:
                new_signals.append(s)
                recent[q["code"]] = {"time": now, "price": s["price"], "count": 1}

    # 時段 B: 主流壓縮突破 (09:35 ~ 13:15, 排除 10:30 ~ 11:20 垃圾時間)
    elif dtime(9, 35) <= t <= dtime(13, 15):
        if is_lunch_slump:
            log("[Info] 目前處於 10:30~11:20 洗盤泥沼時段, 暫停篩選新進場訊號")
        else:
            for q in parsed:
                if q["code"] not in watch_codes:
                    continue
                s = evaluate_trend_breakout(q, market_status)
                if not s:
                    continue
                last = recent.get(q["code"])
                if last and ((now - last["time"]).total_seconds() / 60 < 45 or s["price"] <= last["price"]):
                    continue
                s["wave"] = (last["count"] + 1) if last else 1
                new_signals.append(s)
                recent[q["code"]] = {"time": now, "price": s["price"], "count": s["wave"]}

    for s in new_signals:
        state[s["signal_id"]] = position_from_signal(s)
    save_json(STATE_FILE, state)
    append_rows(SIGNALS_FILE, SIGNAL_COLUMNS, new_signals)
    for s in new_signals:
        broadcast_signal(s)


def run_cycle():
    now = now_tw()
    today, t = now.strftime("%Y-%m-%d"), now.time()
    if now.weekday() >= 5:
        return
    if t >= dtime(13, 35):
        if not flag_done("review", today) and finalize_and_review(today):
            mark_done("review", today)
        if t >= dtime(15, 30) and not flag_done("watchlist", today):
            if generate_watchlist(require_date=now.date()) is True:
                mark_done("watchlist", today)
        return
    if dtime(8, 20) <= t < dtime(8, 55):
        if not flag_done("catchup", today) and generate_watchlist(only_if_newer=True) in (True, "skip"):
            mark_done("catchup", today)
        return
    run_intraday(now, today)


def run_loop(interval):
    ensure_signals_schema()
    log(f"[Info] 高勝率增強常駐模式啟動, 盤中輪詢間隔 {interval} 秒")
    while True:
        try:
            run_cycle()
        except Exception as e:
            log(f"[Error] 本輪執行失敗: {e}\n{traceback.format_exc()}")
            alert_once("cycle_error", f"主迴圈發生例外: {e}")
        t = now_tw().time()
        time.sleep(interval if dtime(8, 58) <= t <= dtime(13, 31) else 60)


def check_api():
    def show(name, data):
        if isinstance(data, list) and data:
            print(f"[OK]   {name}: {len(data)} 筆\n       欄位 = {list(data[0].keys())}")
        else:
            print(f"[FAIL] {name}: 無資料")
    show("TWSE STOCK_DAY_ALL", http_get_json(TWSE_DAY_ALL, retries=0, timeout=20))
    show("TWSE notice(注意股)", http_get_json(TWSE_NOTICE, retries=0))
    show("TWSE TWTB4U(可當沖清單)", http_get_json(TWSE_DAYTRADE_LIST, retries=0))
    show("TPEx 日成交", http_get_json(TPEX_DAY_ALL, retries=0, timeout=20))
    rows, d = fetch_twse_daily()
    print(f"上市解析 {len(rows)} 檔, 資料日 {d}")
    rows, d = fetch_tpex_daily()
    print(f"上櫃解析 {len(rows)} 檔, 資料日 {d}")
    base = d or now_tw().date()
    print(f"當沖股數解析 {len(fetch_twse_daytrade_shares(base))} 檔")
    idx_raw = fetch_mis_quotes([INDEX_SYMBOL])
    if idx_raw:
        q = parse_quote(idx_raw[0])
        print(f"加權指數測試: {q['name']} ({q['code']}) 現價: {q['last']}")


def main():
    ap = argparse.ArgumentParser(description="台股當沖訊號機器人 v3 (高勝率強化版)")
    ap.add_argument("--loop", action="store_true", help="常駐輪詢模式")
    ap.add_argument("--interval", type=int, default=15, help="常駐模式盤中輪詢秒數")
    ap.add_argument("--report", action="store_true", help="輸出累計績效報告")
    ap.add_argument("--days", type=int, default=0, help="報告只看最近 N 天")
    ap.add_argument("--send", action="store_true", help="報告同步推送 Telegram")
    ap.add_argument("--finalize", nargs="?", const="today", help="手動結算 (可指定 YYYY-MM-DD)")
    ap.add_argument("--build-watchlist", action="store_true", help="立即建立作戰清單")
    ap.add_argument("--check-api", action="store_true", help="檢查資料來源欄位")
    a = ap.parse_args()

    ensure_signals_schema()
    if a.check_api:
        check_api()
    elif a.report:
        lines = build_report(a.days)
        print("\n".join(lines))
        if a.send:
            send_telegram_message(f"📊 <b>績效報告</b>\n<pre>{esc(chr(10).join(lines))}</pre>")
    elif a.finalize:
        day = now_tw().strftime("%Y-%m-%d") if a.finalize == "today" else a.finalize
        finalize_results(day)
        print("\n".join(format_stats(compute_stats(load_results_df(day=day)), f"【{day} 結算】")))
    elif a.build_watchlist:
        print("結果:", generate_watchlist())
    elif a.loop:
        run_loop(a.interval)
    else:
        run_cycle()


if __name__ == "__main__":
    main()
