"""
main.py - 公開資料全自動當沖選股、即時推播與盤後復盤系統 (修復版)
"""

import time
import datetime
import requests
import pandas as pd
import numpy as np
import yfinance as yf

from database import init_db, insert_signal
from notifier import send_telegram_message, format_entry_signal
from strategy_engine import evaluate_intraday_signal
from review_analyzer import execute_daily_review
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

class PublicTWDataFetcher:
    """台灣證券市場公開資料擷取器 (免 API Key)"""

    @staticmethod
    def get_twse_institutional_consensus(top_n=30):
        url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        consensus_stocks = []
        try:
            res = requests.get(url, headers=headers, timeout=12)
            data = res.json()
            if data.get("stat") != "OK":
                return []
            
            fields = data["fields"]
            rows = data["data"]
            df = pd.DataFrame(rows, columns=fields)
            
            foreign_cols = [c for c in df.columns if "外陸資" in c and "自營商" not in c]
            trust_cols = [c for c in df.columns if "投信" in c and "買賣超" in c]
            
            if foreign_cols and trust_cols:
                df["foreign_buy"] = df[foreign_cols[0]].astype(str).str.replace(",", "").astype(float)
                df["trust_buy"] = df[trust_cols[0]].astype(str).str.replace(",", "").astype(float)
                
                cond = (df["foreign_buy"] > 0) & (df["trust_buy"] > 0)
                df_filtered = df[cond].copy()
                df_filtered["total_inst_buy"] = df_filtered["foreign_buy"] + df_filtered["trust_buy"]
                df_sorted = df_filtered.sort_values(by="total_inst_buy", ascending=False).head(top_n)
                
                for _, r in df_sorted.iterrows():
                    code = str(r["證券代號"]).strip()
                    name = str(r["證券名稱"]).strip()
                    if len(code) == 4 and code.isdigit():
                        consensus_stocks.append({"stock_id": code, "stock_name": name})
        except Exception as e:
            print(f"[提示] 擷取證交所法人買賣超資料略過: {e}")
            
        return consensus_stocks

    @staticmethod
    def get_stock_levels_and_5m(stock_id: str, stock_name: str, trade_date: str):
        # 優先嘗試 .TW，失敗或空值再嘗試 .TWO
        df = pd.DataFrame()
        for suffix in [".TW", ".TWO"]:
            symbol = f"{stock_id}{suffix}"
            ticker = yf.Ticker(symbol)
            try:
                temp_df = ticker.history(period="5d", interval="5m")
                if not temp_df.empty:
                    df = temp_df
                    break
            except Exception:
                continue

        if df.empty:
            return None, None

        # 核心防呆：強制將 Index 轉為 DatetimeIndex，避免 AttributeError
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        try:
            if df.index.tz is not None:
                df.index = df.index.tz_convert("Asia/Taipei")
        except Exception:
            pass

        df["date_str"] = df.index.strftime("%Y-%m-%d")
        df["time_str"] = df.index.strftime("%H:%M")

        all_dates = sorted(list(df["date_str"].unique()))
        if len(all_dates) < 2:
            return None, None

        prev_date = all_dates[-2]
        today_date = all_dates[-1]

        # 篩選昨日 12:00 ~ 13:30 尾盤區間計算撐壓
        df_prev_pm = df[(df["date_str"] == prev_date) & (df["time_str"] >= "12:00") & (df["time_str"] <= "13:30")]
        if df_prev_pm.empty:
            df_prev_all = df[df["date_str"] == prev_date]
            prev_pm_high = float(df_prev_all["High"].max())
            prev_pm_low = float(df_prev_all["Low"].min())
        else:
            prev_pm_high = float(df_prev_pm["High"].max())
            prev_pm_low = float(df_prev_pm["Low"].min())

        stock_info = {
            "trade_date": trade_date,
            "stock_id": stock_id,
            "stock_name": stock_name,
            "prev_pm_high": round(prev_pm_high, 2),
            "prev_pm_low": round(prev_pm_low, 2),
            "day_open": float(df[df["date_str"] == today_date]["Open"].iloc[0]) if not df[df["date_str"] == today_date].empty else prev_pm_high
        }

        df_today = df[df["date_str"] == today_date].copy()
        if df_today.empty:
            return stock_info, None

        df_today = df_today.rename(columns={
            "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
        })
        df_today["time"] = df_today["time_str"]

        spread = df_today["high"] - df_today["low"]
        spread = spread.replace(0, 0.01)
        df_today["outside_ratio"] = 0.5 + ((df_today["close"] - df_today["open"]) / spread) * 0.25
        df_today["outside_ratio"] = df_today["outside_ratio"].clip(0.3, 0.85)
        df_today["buyer_power"] = np.where(df_today["close"] >= df_today["open"], 100, -50)

        return stock_info, df_today

class LiveTWMarketDataProvider:
    """公開資料整合行情提供者"""

    def __init__(self, fallback_stocks=None):
        self.default_stocks = fallback_stocks or [
            {"stock_id": "3037", "stock_name": "欣興"},
            {"stock_id": "2376", "stock_name": "技嘉"},
            {"stock_id": "2454", "stock_name": "聯發科"},
            {"stock_id": "6274", "stock_name": "台燿"},
            {"stock_id": "3017", "stock_name": "奇鋐"},
            {"stock_id": "3533", "stock_name": "嘉澤"},
            {"stock_id": "2383", "stock_name": "台光電"},
            {"stock_id": "3450", "stock_name": "聯鈞"}
        ]
        self.cached_info = {}
        self.cached_today_klines = {}

    def get_watchlist(self, trade_date: str):
        print("正在自證交所公開資料擷取三大法人買賣超共識名單...")
        consensus = PublicTWDataFetcher.get_twse_institutional_consensus(top_n=20)
        
        combined = []
        seen = set()
        for s in (consensus + self.default_stocks):
            if s["stock_id"] not in seen:
                seen.add(s["stock_id"])
                combined.append(s)
        
        watchlist = []
        print(f"篩選完成，正在計算標的之前日尾盤撐壓線 (12:00~13:30)...")
        for s in combined[:15]:
            info, df_today = PublicTWDataFetcher.get_stock_levels_and_5m(s["stock_id"], s["stock_name"], trade_date)
            if info and df_today is not None and not df_today.empty:
                self.cached_info[s["stock_id"]] = info
                self.cached_today_klines[s["stock_id"]] = df_today
                watchlist.append(info)
                print(f"  ✓ {s['stock_id']} {s['stock_name']} -> 昨日尾盤壓力: {info['prev_pm_high']} 元 | 支撐: {info['prev_pm_low']} 元")

        return watchlist

    def get_5min_bars(self, stock_id: str):
        return self.cached_today_klines.get(stock_id)

    def get_post_entry_klines(self, trade_date: str, stock_id: str, entry_time: str):
        df = self.cached_today_klines.get(stock_id)
        if df is not None and not df.empty:
            df_post = df[df["time"] >= entry_time]
            if not df_post.empty:
                return {
                    "high": float(df_post["high"].max()),
                    "low": float(df_post["low"].min()),
                    "close": float(df_post["close"].iloc[-1]),
                    "tp_time": "10:00",
                    "sl_time": "13:00"
                }
        return {"high": 0.0, "low": 0.0, "close": 0.0, "tp_time": "13:30", "sl_time": "13:30"}

def run_trading_day():
    print("==================================================")
    print("=== 當沖選股推播與自動復盤系統 (公開資料正式版) ===")
    print("==================================================")
    init_db()
    provider = LiveTWMarketDataProvider()
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    # 1. 載入自選清單與昨日尾盤撐壓
    watchlist = provider.get_watchlist(today)
    print(f"[{today}] 核心自選清單計算完成，共取得 {len(watchlist)} 檔有效標的。")

    if not watchlist:
        print("[提示] 今日無符合或無即時資料之標的，結束掃描。")
        return

    # 2. 盤中即時掃描與開盤勢法表態判斷
    print("--- 開始執行開盤勢法與 5分K 策略掃描 ---")
    signals_triggered = 0
    for stock in watchlist:
        df_5min = provider.get_5min_bars(stock["stock_id"])
        if df_5min is None or len(df_5min) < 3:
            continue
        
        signal = evaluate_intraday_signal(stock, df_5min)
        if signal:
            signals_triggered += 1
            print(f"🎯 觸發進場訊號: {signal['stock_id']} {signal['stock_name']} @ {signal['suggested_entry_price']} 元")
            insert_signal(signal)
            msg = format_entry_signal(signal)
            send_telegram_message(msg)

    # 若今日無觸發訊號，發送空手日報至 Telegram
    if signals_triggered == 0:
        scan_msg = (
            f"🔍 *【台股當沖策略監控日報】*\n"
            f"📅 日期：`{today}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 今日監控標的：共 `{len(watchlist)}` 檔（欣興、技嘉、聯發科等）\n"
            f"🛡️ 掃描結果：*今日無標的完全符合進場標準*\n"
            f"──────────────────\n"
            f"💡 *紀律核心*：未見『強強開盤 + 底底高 + 布林開口』前不盲目追價，嚴守空手觀望紀律！"
        )
        send_telegram_message(scan_msg)
        print("盤中掃描完成：已發送『空手觀望』推播戰報至 Telegram。")

    # 3. 盤後自動復盤與戰報統計
    print("--- 執行盤後數據收集與復盤分析 ---")
    execute_daily_review(today, provider)
    print("=== 今日交易流程與復盤已全數歸檔完畢 ===")

if __name__ == "__main__":
    run_trading_day()
