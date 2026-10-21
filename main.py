import os
import time
import json
import requests
import calendar
import pandas as pd
from datetime import datetime, time as dtime
import pytz

TW_TZ = pytz.timezone('Asia/Taipei')

# 環境變數設定
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATA_DIR = "data/history"
SIGNALS_FILE = os.path.join(DATA_DIR, "signals_history.csv")
WATCHLIST_FILE = os.path.join(DATA_DIR, "tomorrow_watchlist.json")

def send_telegram_message(message: str):
    """發送 Telegram 訊息通知"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Warn] 未設定 Telegram 參數")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[Error] Telegram 推播失敗: {e}")

# ==================== 風險籌碼過濾 ====================
def fetch_risk_stocks_filter():
    """排除注意股票及當沖比 > 45% 的標的"""
    excluded_codes = set()
    # 1. 抓取注意股票
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/announcement/notice", timeout=8)
        if res.status_code == 200:
            for item in res.json():
                c = item.get("Code", "").strip()
                if c:
                    excluded_codes.add(c)
    except Exception:
        pass

    # 2. 抓取當沖比過熱股
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U", timeout=8)
        if res.status_code == 200:
            for item in res.json():
                c = item.get("Code", "").strip()
                ratio_str = item.get("DayTradingRatio", "0").replace("%", "").strip()
                try:
                    if float(ratio_str) >= 45.0:
                        excluded_codes.add(c)
                except ValueError:
                    continue
    except Exception:
        pass

    return excluded_codes

# ==================== 盤後功能：產出明日作戰清單 ====================
def generate_and_broadcast_tomorrow_watchlist():
    """每日盤後 15:30 執行：挑選明日高周轉、大成交值主流作戰清單"""
    excluded_set = fetch_risk_stocks_filter()
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    candidates = []

    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            stocks = res.json()
            for s in stocks:
                code = s.get("Code", "").strip()
                name = s.get("Name", "").strip()
                if len(code) != 4 or not code.isdigit() or code in excluded_set:
                    continue
                try:
                    trade_val = float(s.get("TradeValue", "0").replace(",", ""))
                    close_p = float(s.get("ClosingPrice", "0").replace(",", ""))
                    trade_vol = float(s.get("TradeVolume", "0").replace(",", "")) / 1000 # 張數
                    change = float(s.get("Change", "0").replace(",", ""))
                    ref_p = close_p - change
                    pct_change = (change / ref_p) * 100 if ref_p > 0 else 0

                    # 篩選條件：股價 >= 25元、成交量 >= 2,000張、成交值排在主力資金區
                    if close_p >= 25.0 and trade_vol >= 2000:
                        candidates.append({
                            "code": code,
                            "name": name,
                            "close": close_p,
                            "pct_change": round(pct_change, 2),
                            "val": trade_val,
                            "vol": int(trade_vol)
                        })
                except ValueError:
                    continue
    except Exception as e:
        print(f"[Error] 取得盤後清單失敗: {e}")

    # 依成交值大小排序，挑選前 20 檔作為明日主力作戰標的
    candidates.sort(key=lambda x: x["val"], reverse=True)
    watchlist = candidates[:20]

    if not watchlist:
        return

    # 儲存至本地 JSON
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)

    # 推播明日作戰清單至 Telegram
    today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    msg = (
        f"🎯 *【明日當沖精選作戰清單】* ({today_str} 盤後建檔)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"依「大成交值 ＋ 高流動性」篩選，已排除注意股與當沖過熱標的：\n\n"
    )
    for idx, item in enumerate(watchlist[:10], 1):
        msg += f"{idx}. *{item['code']} {item['name']}*｜收 `{item['close']}` ({item['pct_change']:+}%)｜量 `{item['vol']:,}` 張\n"
    
    if len(watchlist) > 10:
        msg += f"_...另有 {len(watchlist) - 10} 檔已列入明日監控池_\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 *明日作戰準則*：\n"
        f"• 09:00～09:15 僅針對「跳空開高＋紅K不破開盤」執行快沖\n"
        f"• 09:35 後確認早盤回檔有撐、過前高才進場主流突破"
    )
    send_telegram_message(msg)

def load_today_watchlist():
    """讀取昨日盤後建檔的今日作戰標的"""
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [f"tse_{item['code']}.tw" for item in data]
        except Exception:
            pass
    # 若檔案不存在則備用市場主流名單
    return ["tse_2330.tw", "tse_2317.tw", "tse_2454.tw", "tse_3231.tw", "tse_2382.tw"]

# ==================== 即時盤況與買賣力道 ====================
def fetch_mis_quotes(stock_list):
    """取得 MIS 報價"""
    if not stock_list:
        return []
    stocks_query = "|".join(stock_list)
    timestamp = int(time.time() * 1000)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={stocks_query}&json=1&delay=0&_={timestamp}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            return res.json().get("msgArray", [])
    except Exception:
        pass
    return []

def analyze_order_book_momentum(stock_data):
    """計算外盤買單比率與多頭竭盡檢驗"""
    ask_vols = [int(v) for v in stock_data.get("f", "").split("_") if v.isdigit()]
    bid_vols = [int(v) for v in stock_data.get("g", "").split("_") if v.isdigit()]
    total_depth = sum(ask_vols) + sum(bid_vols)
    if total_depth == 0:
        return 0.5, False
    buy_ratio = sum(bid_vols) / total_depth
    first_ask = ask_vols[0] if ask_vols else 0
    avg_ask = (sum(ask_vols[1:]) / len(ask_vols[1:])) if len(ask_vols) > 1 else first_ask
    is_exhaustion = (first_ask > avg_ask * 3) and (buy_ratio < 0.52)
    return buy_ratio, is_exhaustion

# ==================== 策略評估 ====================
def evaluate_strategy_opening_rush(stock_data):
    """開盤 15 分快沖邏輯"""
    try:
        code = stock_data.get("c")
        name = stock_data.get("n")
        latest_price_str = stock_data.get("z", "-")
        if latest_price_str == "-" or float(latest_price_str) <= 0:
            return None
        current_price = float(latest_price_str)

        open_p = float(stock_data.get("o", "0"))
        high_p = float(stock_data.get("h", "0"))
        yesterday_close = float(stock_data.get("y", "0"))
        total_vol = int(stock_data.get("v", "0"))

        if yesterday_close <= 0 or open_p <= 0:
            return None

        open_gap = ((open_p - yesterday_close) / yesterday_close) * 100
        current_pct = ((current_price - yesterday_close) / yesterday_close) * 100
        buy_ratio, is_exhaustion = analyze_order_book_momentum(stock_data)

        if is_exhaustion:
            return None

        is_match = (
            (1.5 <= open_gap <= 5.0) and
            (current_price >= open_p) and
            (current_price >= high_p * 0.995) and
            (2.0 <= current_pct <= 6.5) and
            (buy_ratio >= 0.58) and
            (total_vol >= 800)
        )

        if is_match:
            stop_loss = round(max(open_p, current_price * 0.985), 2)
            target_price = round(current_price + (current_price - stop_loss) * 1.5, 2)
            return {
                "strategy": "開盤快沖",
                "date": datetime.now(TW_TZ).strftime("%Y-%m-%d"),
                "time": datetime.now(TW_TZ).strftime("%H:%M:%S"),
                "code": code,
                "name": name,
                "price": current_price,
                "pct_change": round(current_pct, 2),
                "volume": total_vol,
                "stop_loss": stop_loss,
                "target_price": target_price,
                "extra_info": f"外盤買力 {round(buy_ratio*100, 1)}%（紅K站穩開盤）",
                "link": f"https://tw.stock.yahoo.com/quote/{code}"
            }
    except Exception:
        return None
    return None

def evaluate_strategy_trend_breakout(stock_data):
    """09:35 後主流突破邏輯"""
    try:
        code = stock_data.get("c")
        name = stock_data.get("n")
        latest_price_str = stock_data.get("z", "-")
        if latest_price_str == "-" or float(latest_price_str) <= 0:
            return None
        current_price = float(latest_price_str)

        high_p = float(stock_data.get("h", "0"))
        low_p = float(stock_data.get("l", "0"))
        yesterday_close = float(stock_data.get("y", "0"))
        total_vol = int(stock_data.get("v", "0"))

        if yesterday_close <= 0 or current_price < 25.0:
            return None

        pct_change = ((current_price - yesterday_close) / yesterday_close) * 100
        buy_ratio, is_exhaustion = analyze_order_book_momentum(stock_data)

        if is_exhaustion:
            return None

        is_breakout = (current_price >= high_p * 0.995) and (2.5 <= pct_change <= 7.5) and (buy_ratio >= 0.55)

        if is_breakout:
            stop_loss = round(max(low_p, current_price * 0.978), 2)
            target_price = round(current_price + (current_price - stop_loss) * 2.0, 2)
            return {
                "strategy": "主流突破",
                "date": datetime.now(TW_TZ).strftime("%Y-%m-%d"),
                "time": datetime.now(TW_TZ).strftime("%H:%M:%S"),
                "code": code,
                "name": name,
                "price": current_price,
                "pct_change": round(pct_change, 2),
                "volume": total_vol,
                "stop_loss": stop_loss,
                "target_price": target_price,
                "extra_info": f"大單外盤佔比 {round(buy_ratio*100, 1)}%（突破回檔支撐）",
                "link": f"https://tw.stock.yahoo.com/quote/{code}"
            }
    except Exception:
        return None
    return None

# ==================== 推播發送模組 ====================
def broadcast_signal(s):
    """發送盤中符合進場標準的訊號推播"""
    if s["strategy"] == "開盤快沖":
        msg = (
            f"⚡ *【開盤 15 分快沖訊號】* ⚡\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"標的：*{s['code']} {s['name']}* (作戰清單)\n"
            f"現價：`{s['price']}` 元 (漲幅 +{s['pct_change']}%)\n"
            f"力道：`{s['extra_info']}`\n"
            f"成交量：`{s['volume']:,}` 張\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *建議買進*：`{s['price']}` ～ `{round(s['price'] * 1.004, 2)}`\n"
            f"🛑 *嚴格停損*：`{s['stop_loss']}` 元 (跌破開盤價即撤)\n"
            f"🏆 *短沖停利*：`{s['target_price']}` 元\n"
            f"⏱ *時間停損*：進場 8 分鐘內未拉開獲利建議平倉\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔗 [點擊開啟 Yahoo 看盤確認]({s['link']})"
        )
    else:
        wave_str = f"（第 {s['wave']} 波發動）" if s.get('wave', 1) > 1 else ""
        msg = (
            f"🔥 *【主流大單帶量突破{wave_str}】* 🔥\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"標的：*{s['code']} {s['name']}* (作戰清單)\n"
            f"現價：`{s['price']}` 元 (漲幅 +{s['pct_change']}%)\n"
            f"力道：`{s['extra_info']}`\n"
            f"成交量：`{s['volume']:,}` 張\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *建議掛單*：`{s['price']}` ～ `{round(s['price'] * 1.005, 2)}`\n"
            f"🛑 *嚴守停損*：`{s['stop_loss']}` 元\n"
            f"🏆 *波段停利*：`{s['target_price']}` 元 (風報比 1:2)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔗 [點擊開啟 Yahoo 看盤確認]({s['link']})"
        )
    send_telegram_message(msg)

# ==================== 盤後復盤與月底分析 ====================
def generate_daily_review():
    """每日收盤後自動總結推播"""
    if not os.path.exists(SIGNALS_FILE):
        return
    df = pd.read_csv(SIGNALS_FILE)
    today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    today_df = df[df['date'] == today_str]

    if today_df.empty:
        msg = f"📋 *【每日當沖交易復盤報告】* ({today_str})\n今日作戰清單中均未出現符合標準之進場訊號，嚴守紀律空手觀望。"
        send_telegram_message(msg)
        return

    rush_df = today_df[today_df['strategy'] == "開盤快沖"]
    trend_df = today_df[today_df['strategy'] == "主流突破"]

    msg = (
        f"📋 *【每日當沖交易復盤報告】* ({today_str})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"今日作戰清單共觸發 `{len(today_df)}` 檔交易訊號：\n\n"
        f"⚡ *開盤 15 分快沖 ({len(rush_df)} 檔)*：\n"
    )
    if not rush_df.empty:
        for _, r in rush_df.iterrows():
            msg += f"• `{r['code']} {r['name']}`：進場價 {r['price']} (+{r['pct_change']}%) / 停損 {r['stop_loss']}\n"
    else:
        msg += "• 無觸發標的\n"

    msg += f"\n🔥 *盤中主流突破 ({len(trend_df)} 檔)*：\n"
    if not trend_df.empty:
        for _, r in trend_df.iterrows():
            msg += f"• `{r['code']} {r['name']}`：進場價 {r['price']} (+{r['pct_change']}%) / 目標 {r['target_price']}\n"
    else:
        msg += "• 無觸發標的\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 _提示：所有詳細紀錄已寫入資料庫，供覆盤手動交易期望值。_"
    )
    send_telegram_message(msg)

# ==================== 主流程調度 ====================
def main():
    now_tw = datetime.now(TW_TZ)
    current_t = now_tw.time()
    today_str = now_tw.strftime("%Y-%m-%d")

    if now_tw.weekday() >= 5:
        return

    # 1. 盤後時段一 (13:40)：執行當日復盤
    if dtime(13, 35) <= current_t <= dtime(14, 30):
        print("[Info] 執行收盤復盤報告...")
        generate_daily_review()
        return

    # 2. 盤後時段二 (15:30)：產出並推播明日作戰清單
    if current_t >= dtime(15, 30):
        print("[Info] 產出並推播明日作戰清單...")
        generate_and_broadcast_tomorrow_watchlist()
        return

    # 3. 盤中交易時段：載入作戰清單並執行訊號監控
    targets = load_today_watchlist()
    quotes = fetch_mis_quotes(targets)

    recent_alerts = {}
    if os.path.exists(SIGNALS_FILE):
        try:
            df_hist = pd.read_csv(SIGNALS_FILE)
            today_records = df_hist[df_hist['date'] == today_str]
            for _, row in today_records.iterrows():
                code = str(row['code'])
                record_dt = datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW_TZ)
                if code not in recent_alerts or record_dt > recent_alerts[code]["time"]:
                    recent_alerts[code] = {
                        "time": record_dt,
                        "price": float(row['price']),
                        "count": recent_alerts.get(code, {}).get("count", 0) + 1
                    }
        except Exception:
            pass

    signals_to_save = []

    # 盤中時段 A：09:03 ~ 09:18（開盤 15 分快沖）
    if dtime(9, 3) <= current_t <= dtime(9, 18):
        for q in quotes:
            res = evaluate_strategy_opening_rush(q)
            if res:
                code_str = str(res['code'])
                if code_str not in recent_alerts:
                    signals_to_save.append(res)
                    broadcast_signal(res)
                    recent_alerts[code_str] = {"time": now_tw, "price": res['price'], "count": 1}

    # 盤中時段 B：09:35 ~ 13:15（主流壓縮突破）
    elif dtime(9, 35) <= current_t <= dtime(13, 15):
        for q in quotes:
            res = evaluate_strategy_trend_breakout(q)
            if res:
                code_str = str(res['code'])
                allow_alert = True
                round_num = 1
                if code_str in recent_alerts:
                    last_info = recent_alerts[code_str]
                    round_num = last_info["count"] + 1
                    elapsed_min = (now_tw - last_info['time']).total_seconds() / 60
                    if elapsed_min < 45 or res['price'] <= last_info['price']:
                        allow_alert = False

                if allow_alert:
                    res['wave'] = round_num
                    signals_to_save.append(res)
                    broadcast_signal(res)
                    recent_alerts[code_str] = {"time": now_tw, "price": res['price'], "count": round_num}

    if signals_to_save:
        os.makedirs(DATA_DIR, exist_ok=True)
        file_exists = os.path.isfile(SIGNALS_FILE)
        df_new = pd.DataFrame(signals_to_save)
        df_new.to_csv(SIGNALS_FILE, mode='a', header=not file_exists, index=False, encoding="utf-8-sig")

if __name__ == "__main__":
    main()
