import os
import time
import requests
import calendar
import pandas as pd
from datetime import datetime, time as dtime
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

def fetch_mis_quotes(stock_list):
    """從證交所 MIS 取得即時五檔與量價快照"""
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
    """評估是否符合手動進場的爆量突破條件"""
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

        if yesterday_close <= 0 or current_price < 20.0 or total_vol < 1500:
            return None

        pct_change = ((current_price - yesterday_close) / yesterday_close) * 100
        # 突破條件：距最高價小於 0.5%，且漲幅在 2.5% ~ 7.5% 之間
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
    except Exception:
        return None
    return None

def check_monthly_summary():
    """月底自動彙整並推播當月手動追蹤數據"""
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
    now_tw = datetime.now(TW_TZ)
    current_t = now_tw.time()
    
    # 週末不執行
    if now_tw.weekday() >= 5:
        print("[Info] 週末非交易日。")
        return

    today_str = now_tw.strftime("%Y-%m-%d")

    # 1. 讀取今日最新推播歷史，支援「冷卻時間」與「二次突破」判定
    recent_alerts = {}  # 格式: { "2330": {"time": datetime, "price": float, "count": int} }
    if os.path.exists(SIGNALS_FILE):
        try:
            df_hist = pd.read_csv(SIGNALS_FILE)
            today_records = df_hist[df_hist['date'] == today_str]
            for _, row in today_records.iterrows():
                code = str(row['code'])
                record_dt = datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW_TZ)
                
                # 保留當天該標的最新一次紀錄
                if code not in recent_alerts or record_dt > recent_alerts[code]["time"]:
                    count = recent_alerts.get(code, {}).get("count", 0) + 1
                    recent_alerts[code] = {
                        "time": record_dt,
                        "price": float(row['price']),
                        "count": count
                    }
        except Exception as e:
            print(f"[Warn] 讀取歷史訊號失敗: {e}")

    # 2. 嚴格交易時鐘：只允許 09:10 ~ 13:15 推播買進訊號
    is_trading_hour = (dtime(9, 10) <= current_t <= dtime(13, 15))

    quotes = fetch_mis_quotes(WATCHLIST)
    active_signals = []
    
    for q in quotes:
        res = evaluate_manual_signal(q)
        if res:
            code_str = str(res['code'])
            allow_alert = True
            round_num = 1
            
            # 若今日已經推播過此股票，進行二次發動檢驗
            if code_str in recent_alerts:
                last_info = recent_alerts[code_str]
                round_num = last_info["count"] + 1
                elapsed_minutes = (now_tw - last_info['time']).total_seconds() / 60
                
                # 規則：距離上次推播需相隔 > 45 分鐘，且現價突破前次推播價，才算有效第二波
                if elapsed_minutes < 45 or res['price'] <= last_info['price']:
                    allow_alert = False
            
            if allow_alert:
                res['wave'] = round_num  # 紀錄發動波段數
                active_signals.append(res)
                # 即時更新記憶體狀態，防止同一批次重複加入
                recent_alerts[code_str] = {
                    "time": now_tw,
                    "price": res['price'],
                    "count": round_num
                }

    # 3. 儲存與推播
    if active_signals:
        os.makedirs(DATA_DIR, exist_ok=True)
        file_exists = os.path.isfile(SIGNALS_FILE)
        df_new = pd.DataFrame(active_signals)
        df_new.to_csv(SIGNALS_FILE, mode='a', header=not file_exists, index=False, encoding="utf-8-sig")

        if is_trading_hour:
            for s in active_signals:
                wave_title = f"（第 {s['wave']} 波突破）" if s.get('wave', 1) > 1 else ""
                msg = (
                    f"⚡ *【手動進場訊號觸發{wave_title}】* ⚡\n"
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
        else:
            print(f"[Info] 當前時間 {current_t} 已超過盤中進場時限 (13:15)，數據僅存檔不發推播。")

    # 4. 收盤後月底總結檢查
    if current_t >= dtime(13, 30):
        check_monthly_summary()

if __name__ == "__main__":
    main()
