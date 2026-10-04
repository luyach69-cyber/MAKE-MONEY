import os
import json
import csv
import calendar
from datetime import datetime, time as dtime
import pytz
import requests
import pandas as pd

# 設定時區為台灣時間 (UTC+8)
TW_TZ = pytz.timezone('Asia/Taipei')

# 環境變數設定
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATA_DIR = "data/history"
SIGNALS_FILE = os.path.join(DATA_DIR, "signals_history.csv")

def send_telegram_message(message: str):
    """發送 Telegram 訊息通知"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Warn] 未設定 Telegram Token 或 Chat ID，略過推播。")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"[Error] Telegram 推播失敗: {e}")

def get_twse_market_data():
    """取得台股當日最新行情資料 (TWSE OpenAPI)"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"[Error] 取得台股即時資料失敗: {e}")
    return []

def is_last_trading_day_of_month(today_dt: datetime) -> bool:
    """判斷今天是否為當月最後一個工作天 (簡單工作日判斷)"""
    _, last_day = calendar.monthrange(today_dt.year, today_dt.month)
    # 若今天就是當月最後一日
    if today_dt.day == last_day:
        return True
    # 若最後一天是週末，檢查今天是否為週五且剛好是月底前最後一個上班日
    current_day = today_dt.day
    remaining_days = [today_dt.replace(day=d) for d in range(current_day + 1, last_day + 1)]
    has_weekday_left = any(d.weekday() < 5 for d in remaining_days)
    return not has_weekday_left

def scan_breakout_signals(data_list):
    """
    依據壓縮突破策略進行篩選
    條件：
    1. 成交量充足 (> 2,500 張)
    2. 價格 > 20 元
    3. 實質漲幅介於 2% ~ 8%
    4. 突破收在高點附近 (Close ~ High)
    """
    signals = []
    for item in data_list:
        try:
            name = item.get("Name", "").strip()
            code = item.get("Code", "").strip()
            
            # 排除非普通股代碼
            if len(code) != 4 or not code.isdigit():
                continue

            trade_vol = float(item.get("TradeVolume", "0").replace(",", "")) / 1000 # 轉為張數
            open_p = float(item.get("OpeningPrice", "0").replace(",", ""))
            high_p = float(item.get("HighestPrice", "0").replace(",", ""))
            low_p = float(item.get("LowestPrice", "0").replace(",", ""))
            close_p = float(item.get("ClosingPrice", "0").replace(",", ""))
            change = float(item.get("Change", "0").replace(",", ""))

            # 基本過濾門檻
            if trade_vol < 2500 or close_p < 20.0 or open_p <= 0 or low_p <= 0:
                continue

            ref_price = close_p - change
            if ref_price <= 0:
                continue

            pct_change = (change / ref_price) * 100
            
            # 震幅與突破結構檢測
            intraday_range = (high_p - low_p) / low_p
            # 股價收在今日前 95% 高位，漲幅在 2.5% ~ 8.5%，且日內動能顯著
            if 2.5 <= pct_change <= 8.5 and (close_p >= high_p * 0.985) and intraday_range >= 0.03:
                stop_loss = round(low_p if low_p > (close_p * 0.96) else (close_p * 0.975), 2)
                target_price = round(close_p + (close_p - stop_loss) * 1.5, 2)
                
                signals.append({
                    "date": datetime.now(TW_TZ).strftime("%Y-%m-%d"),
                    "code": code,
                    "name": name,
                    "close": close_p,
                    "high": high_p,
                    "volume": int(trade_vol),
                    "pct_change": round(pct_change, 2),
                    "stop_loss": stop_loss,
                    "target_price": target_price
                })
        except (ValueError, TypeError):
            continue
    return signals

def save_and_notify_signals(signals):
    """保存訊號並發送當日 Telegram 通知"""
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.isfile(SIGNALS_FILE)

    today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")

    # 寫入 CSV
    if signals:
        keys = signals[0].keys()
        with open(SIGNALS_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            if not file_exists:
                writer.writeheader()
            writer.writerows(signals)

    # 推播當日訊號
    if not signals:
        send_telegram_message(f"📊 *【盤中/盤後突破訊號通知】*\n日期：`{today_str}`\n今日無符合高勝率壓縮突破條件之標的。")
        return

    msg = f"🚀 *【突破上漲訊號推播】* ({today_str})\n"
    msg += f"共篩選出 `{len(signals)}` 檔潛在帶量突破標的：\n"
    msg += "━━━━━━━━━━━━━━━━━━\n"
    for s in signals[:8]:  # 避免訊息過長，取前 8 檔
        msg += (
            f"📌 *{s['code']} {s['name']}*\n"
            f"• 參考價位：`{s['close']}` 元 (漲幅 +{s['pct_change']}%)\n"
            f"• 當日成交量：`{s['volume']:,}` 張\n"
            f"• 建議停損：`{s['stop_loss']}` 元\n"
            f"• 停利目標：`{s['target_price']}` 元\n"
            f"──────────────────\n"
        )
    if len(signals) > 8:
        msg += f"_...另有 {len(signals) - 8} 檔標的已記錄於歷史庫中。_\n"
    
    send_telegram_message(msg)

def generate_monthly_report():
    """生成並推播當月彙整統計數據"""
    if not os.path.exists(SIGNALS_FILE):
        return

    df = pd.read_csv(SIGNALS_FILE)
    if df.empty:
        return

    today = datetime.now(TW_TZ)
    current_month_prefix = today.strftime("%Y-%m")
    
    # 篩選當月份資料
    month_df = df[df['date'].str.startswith(current_month_prefix)]
    if month_df.empty:
        send_telegram_message(f"📅 *【{current_month_prefix} 月度數據分析報告】*\n本月未有突破訊號觸發。")
        return

    total_signals = len(month_df)
    unique_tickers = month_df['code'].nunique()
    avg_pct = round(month_df['pct_change'].mean(), 2)
    top_vol = month_df.sort_values(by="volume", ascending=False).iloc[0]

    report = (
        f"📈 *【{current_month_prefix} 月度突破訊號總覽】*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"• 本月觸發訊號總數：`{total_signals}` 次\n"
        f"• 涵蓋獨立標的數：`{unique_tickers}` 檔\n"
        f"• 觸發當日平均漲幅：`+{avg_pct}%`\n"
        f"• 本月最大量突破標的：`{top_vol['code']} {top_vol['name']}` ({int(top_vol['volume']):,} 張)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 _提示：完整月度詳細數據已保存在 Git 資料庫 `data/history` 下。_"
    )
    send_telegram_message(report)

def main():
    today_dt = datetime.now(TW_TZ)
    # 週末不執行
    if today_dt.weekday() >= 5:
        print("[Info] 今日為週末非交易日，程式退出。")
        return

    print(f"[Start] 執行盤中/盤後監控: {today_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    data = get_twse_market_data()
    if data:
        signals = scan_breakout_signals(data)
        save_and_notify_signals(signals)
    else:
        print("[Warn] 無法取得行情數據。")

    # 判斷是否為當月最後一個交易日，是則進行月度分析推播
    if is_last_trading_day_of_month(today_dt):
        print("[Info] 偵測到今日為月底結算日，準備推播月度報告...")
        generate_monthly_report()

if __name__ == "__main__":
    main()
