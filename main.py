import os
import time
import requests
import calendar
import pandas as pd
from datetime import datetime
import pytz

TW_TZ = pytz.timezone('Asia/Taipei')

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATA_DIR = "data/history"
SIGNALS_FILE = os.path.join(DATA_DIR, "signals_history.csv")

# 觀察名單 (格式: tse_代號.tw 或 otc_代號.tw)
WATCHLIST = [
    "tse_1597.tw", "tse_2330.tw", "tse_2603.tw", "tse_2317.tw",
    "tse_3231.tw", "tse_2382.tw", "tse_2609.tw", "tse_2454.tw",
    "otc_8069.tw", "otc_3293.tw", "otc_6488.tw", "otc_3529.tw"
]

def send_telegram_message(message: str):
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

def fetch_mis_quotes(stock_list):
    stocks_query = "|".join(stock_list)
    timestamp = int(time.time() * 1000)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={stocks_query}&json=1&delay=0&_={timestamp}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            return res.json().get("msgArray", [])
    except Exception as e:
        print(f"[Error] 取得盤中報價失敗: {e}")
    return []

def evaluate_manual_signal(stock_data):
    try:
        code = stock_data.get("c")
        name = stock_data.get("n")
        
        latest_price_str = stock_data.get("z", "-")
        if latest_price_str == "-" or float(latest_price_str) <= 0:
            return None
        current_price = float(latest_price_str)
        
        high_p = float(stock_data.get("h", "0"))
        low_p = float(stock_data.get("l", "0"))
        open_p = float(stock_data.get("o", "0"))
        yesterday_close = float(stock_data.get("y", "0"))
        total_vol = int(stock_data.get("v", "0"))

        if yesterday_close <= 0 or current_price < 20.0 or total_vol < 1500:
            return None

        pct_change = ((current_price - yesterday_close) / yesterday_close) * 100

        is_breakout = (current_price >= high_p * 0.995) and (2.5 <= pct_change <= 7.5)

        if is_breakout:
            stop_loss = round(max(low_p, current_price * 0.978), 2)
            target_price = round(current_price + (current_price - stop_loss) * 2, 2)
            
            return {
                "date": datetime.now(TW_TZ).strftime("%Y-%m-%d"),
                "time": datetime.now(TW_TZ).strftime("%H:%M:%S"),
                "code": code,
                "name": name,
                "price": current_price,
                "pct_change": round(pct_change, 2),
                "volume": total_vol,
                "stop_loss": stop_loss,
                "target_price": target_price,
                "link": f"https://tw.stock.yahoo.com/quote/{code}"
            }
    except Exception as e:
        return None
    return None

def record_and_alert(signals):
    if not signals:
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.isfile(SIGNALS_FILE)

    df_new = pd.DataFrame(signals)
    df_new.to_csv(SIGNALS_FILE, mode='a', header=not file_exists, index=False, encoding="utf-8-sig")

    for s in signals:
        msg = (
            f"⚡ *【手動進場訊號觸發】* ⚡\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"標的：*{s['code']} {s['name']}*\n"
            f"現價：`{s['price']}` 元 (漲幅 +{s['pct_change']}%)\n"
            f"成交量：`{s['volume']:,}` 張\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *建議掛單買進*：`{s['price']}` ～ `{round(s['price'] * 1.005, 2)}`\n"
            f"🛑 *嚴守停損點*：`{s['stop_loss']}` 元\n"
            f"🏆 *預估停利點*：`{s['target_price']}` 元\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔗 [點擊開啟 Yahoo 看盤確認]({s['link']})"
        )
        send_telegram_message(msg)

def check_monthly_summary():
    today_dt = datetime.now(TW_TZ)
    _, last_day = calendar.monthrange(today_dt.year, today_dt.month)
    
    if today_dt.day == last_day and os.path.exists(SIGNALS_FILE):
        df = pd.read_csv(SIGNALS_FILE)
        current_month = today_dt.strftime("%Y-%m")
        month_df = df[df['date'].str.startswith(current_month)]
        
        if not month_df.empty:
            summary_msg = (
                f"📊 *【{current_month} 月度訊號彙整與檢討】*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"• 本月累計提醒次數：`{len(month_df)}` 次\n"
                f"• 出現次數最多標的：`{month_df['code'].mode()[0]}`\n"
                f"• 觸發時平均漲幅：`+{round(month_df['pct_change'].mean(), 2)}%`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"請檢視 `data/history` 下的 CSV 紀錄進行個人手動下單勝率覆盤。"
            )
            send_telegram_message(summary_msg)

def main():
    quotes = fetch_mis_quotes(WATCHLIST)
    active_signals = []
    for q in quotes:
        res = evaluate_manual_signal(q)
        if res:
            active_signals.append(res)
            
    record_and_alert(active_signals)
    check_monthly_summary()

if __name__ == "__main__":
    main()
