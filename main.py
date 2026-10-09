import os
import time
import requests
import calendar
import pandas as pd
from datetime import datetime, time as dtime
import pytz

TW_TZ = pytz.timezone('Asia/Taipei')

# 環境變數
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATA_DIR = "data/history"
SIGNALS_FILE = os.path.join(DATA_DIR, "signals_history.csv")

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

# ==================== 籌碼風控與股票池過濾 ====================
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

def get_top_market_value_stocks(excluded_set, top_n=35):
    """抓取全市場成交金額排名前 N 大的主流權值/動能標的"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    mis_codes = []
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            stocks = res.json()
            candidates = []
            for s in stocks:
                code = s.get("Code", "").strip()
                if len(code) != 4 or not code.isdigit() or code in excluded_set:
                    continue
                try:
                    trade_val = float(s.get("TradeValue", "0").replace(",", ""))
                    close_p = float(s.get("ClosingPrice", "0").replace(",", ""))
                    if close_p >= 25.0:
                        candidates.append({"code": code, "val": trade_val})
                except ValueError:
                    continue
            candidates.sort(key=lambda x: x["val"], reverse=True)
            mis_codes = [f"tse_{item['code']}.tw" for item in candidates[:top_n]]
    except Exception as e:
        print(f"[Error] 取得動態觀察名單失敗: {e}")

    if not mis_codes:
        mis_codes = ["tse_2330.tw", "tse_2317.tw", "tse_2454.tw", "tse_3231.tw", "tse_2382.tw"]
    return mis_codes

def fetch_mis_quotes(stock_list):
    """取得證交所 MIS 即時盤況"""
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

# ==================== 策略一：開盤 15 分快沖邏輯 ====================
def evaluate_strategy_opening_rush(stock_data):
    """
    開盤 15 分快沖條件：
    - 開盤跳空開高 (1.5% ~ 5.0%)
    - 價格站穩開盤價之上 (紅K強勢)
    - 距最高價 0.5% 內
    - 外盤買盤佔比 >= 58%
    """
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

        open_gap_pct = ((open_p - yesterday_close) / yesterday_close) * 100
        current_pct = ((current_price - yesterday_close) / yesterday_close) * 100

        ask_vols = [int(v) for v in stock_data.get("f", "").split("_") if v.isdigit()]
        bid_vols = [int(v) for v in stock_data.get("g", "").split("_") if v.isdigit()]
        total_depth = sum(ask_vols) + sum(bid_vols)
        buy_ratio = (sum(bid_vols) / total_depth) if total_depth > 0 else 0.5

        is_match = (
            (1.5 <= open_gap_pct <= 5.0) and
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
                "extra_info": f"開盤價 {open_p} 元｜外盤比 {round(buy_ratio*100, 1)}%",
                "link": f"https://tw.stock.yahoo.com/quote/{code}"
            }
    except Exception:
        return None
    return None

# ==================== 策略二：09:35 後主流壓縮突破邏輯 ====================
def evaluate_strategy_trend_breakout(stock_data):
    """
    盤中主流突破條件：
    - 完成 9:30 前沉澱驗證
    - 價格貼近當日最高價 0.5% 內
    - 漲幅介於 2.5% ~ 7.5%
    - 買盤力道充足 (>= 55%)
    """
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

        ask_vols = [int(v) for v in stock_data.get("f", "").split("_") if v.isdigit()]
        bid_vols = [int(v) for v in stock_data.get("g", "").split("_") if v.isdigit()]
        total_depth = sum(ask_vols) + sum(bid_vols)
        buy_ratio = (sum(bid_vols) / total_depth) if total_depth > 0 else 0.5

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
                "extra_info": f"大單外盤比 {round(buy_ratio*100, 1)}%",
                "link": f"https://tw.stock.yahoo.com/quote/{code}"
            }
    except Exception:
        return None
    return None

# ==================== 推播發送模組 ====================
def broadcast_signal(s):
    """根據策略發送專屬 Telegram 訊息"""
    if s["strategy"] == "開盤快沖":
        msg = (
            f"⚡ *【開盤 15 分快沖訊號】* ⚡\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"標的：*{s['code']} {s['name']}*\n"
            f"現價：`{s['price']}` 元 (漲幅 +{s['pct_change']}%)\n"
            f"狀態：`{s['extra_info']}`\n"
            f"成交量：`{s['volume']:,}` 張\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *建議買進*：`{s['price']}` ～ `{round(s['price'] * 1.004, 2)}`\n"
            f"🛑 *嚴格停損*：`{s['stop_loss']}` 元 (破開盤即撤)\n"
            f"🏆 *短沖停利*：`{s['target_price']}` 元\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔗 [點擊開啟 Yahoo 看盤確認]({s['link']})"
        )
    else:
        wave_str = f"（第 {s['wave']} 波發動）" if s.get('wave', 1) > 1 else ""
        msg = (
            f"🔥 *【主流大單帶量突破{wave_str}】* 🔥\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"標的：*{s['code']} {s['name']}*（主流成交值榜）\n"
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
        msg = f"📋 *【每日當沖交易復盤報告】* ({today_str})\n今日全市場均未觸發符合條件之標的，保持空手紀律。"
        send_telegram_message(msg)
        return

    rush_df = today_df[today_df['strategy'] == "開盤快沖"]
    trend_df = today_df[today_df['strategy'] == "主流突破"]

    msg = (
        f"📋 *【每日當沖交易復盤報告】* ({today_str})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"今日共觸發 `{len(today_df)}` 檔交易訊號：\n\n"
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
        f"💡 _提示：所有詳細五檔量能與價格已寫入資料庫，供覆盤手動交易勝率。_"
    )
    send_telegram_message(msg)

def generate_monthly_review():
    """月底最後一天自動彙整整月歷史數據推播"""
    today_dt = datetime.now(TW_TZ)
    _, last_day = calendar.monthrange(today_dt.year, today_dt.month)
    
    # 判斷是否為月底當天收盤後
    if today_dt.day == last_day and os.path.exists(SIGNALS_FILE):
        df = pd.read_csv(SIGNALS_FILE)
        current_month = today_dt.strftime("%Y-%m")
        month_df = df[df['date'].str.startswith(current_month)]
        
        if month_df.empty:
            return

        total_alerts = len(month_df)
        rush_count = len(month_df[month_df['strategy'] == "開盤快沖"])
        trend_count = len(month_df[month_df['strategy'] == "主流突破"])
        top_symbol = month_df['code'].mode()[0] if not month_df.empty else "N/A"
        avg_pct = round(month_df['pct_change'].mean(), 2)

        summary_msg = (
            f"📊 *【{current_month} 月度當沖策略統計總覽】*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• 本月觸發訊號總計：`{total_alerts}` 次\n"
            f"  └ ⚡ 開盤快沖：`{rush_count}` 次\n"
            f"  └ 🔥 主流突破：`{trend_count}` 次\n"
            f"• 訊號觸發平均漲幅：`+{avg_pct}%`\n"
            f"• 最常發動標的代號：`{top_symbol}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"詳細數據已歸檔於 `data/history/signals_history.csv`。"
        )
        send_telegram_message(summary_msg)

# ==================== 主程式排程派發 ====================
def main():
    now_tw = datetime.now(TW_TZ)
    current_t = now_tw.time()
    today_str = now_tw.strftime("%Y-%m-%d")

    # 週末不執行
    if now_tw.weekday() >= 5:
        return

    # 收盤時段 (13:40 以後)：執行當日復盤與月底統計
    if current_t >= dtime(13, 35):
        print("[Info] 執行收盤復盤與月結結算...")
        generate_daily_review()
        generate_monthly_review()
        return

    # 讀取今日歷史推播狀態 (支援 45 分鐘冷卻與二次突破)
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

    # 籌碼過濾與主流股票清單取得
    risk_stocks = fetch_risk_stocks_filter()
    targets = get_top_market_value_stocks(risk_stocks, top_n=35)
    quotes = fetch_mis_quotes(targets)

    signals_to_save = []

    # 時段一：09:03 ~ 09:18（專做開盤 15 分快沖）
    if dtime(9, 3) <= current_t <= dtime(9, 18):
        for q in quotes:
            res = evaluate_strategy_opening_rush(q)
            if res:
                code_str = str(res['code'])
                if code_str not in recent_alerts:  # 開盤快沖當天只推一次
                    signals_to_save.append(res)
                    broadcast_signal(res)
                    recent_alerts[code_str] = {"time": now_tw, "price": res['price'], "count": 1}

    # 時段二：09:35 ~ 13:15（專做主流壓縮突破）
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
                    # 需冷卻 45 分鐘以上且突破前次價格
                    if elapsed_min < 45 or res['price'] <= last_info['price']:
                        allow_alert = False

                if allow_alert:
                    res['wave'] = round_num
                    signals_to_save.append(res)
                    broadcast_signal(res)
                    recent_alerts[code_str] = {"time": now_tw, "price": res['price'], "count": round_num}

    # 保存當批訊號至資料庫
    if signals_to_save:
        os.makedirs(DATA_DIR, exist_ok=True)
        file_exists = os.path.isfile(SIGNALS_FILE)
        df_new = pd.DataFrame(signals_to_save)
        df_new.to_csv(SIGNALS_FILE, mode='a', header=not file_exists, index=False, encoding="utf-8-sig")

if __name__ == "__main__":
    main()
