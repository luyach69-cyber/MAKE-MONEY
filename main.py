"""
主程式流程 (main.py)
整合盤前篩選、盤中即時掃描、推播發送與盤後自動復盤
"""
import time
from datetime import datetime
import pandas as pd
from database import init_db, insert_signal
from notifier import send_telegram_message, format_entry_signal
from strategy_engine import evaluate_intraday_signal
from review_analyzer import execute_daily_review

class MockMarketDataProvider:
    """模擬行情資料提供者 (實戰可無縫替換為 Shioaji 或 XQ DDE / WebSocket)"""
    def get_watchlist(self, trade_date: str):
        return [
            {
                'trade_date': trade_date,
                'stock_id': '3037',
                'stock_name': '欣興',
                'prev_pm_high': 178.5,
                'prev_pm_low': 175.0,
                'day_open': 179.0
            },
            {
                'trade_date': trade_date,
                'stock_id': '2376',
                'stock_name': '技嘉',
                'prev_pm_high': 265.0,
                'prev_pm_low': 260.5,
                'day_open': 266.5
            }
        ]
    
    def get_5min_bars(self, stock_id: str):
        # 模擬 5分K 走勢：符合強強開盤、底底高、中軌有撐
        return pd.DataFrame([
            {'time': '09:05', 'open': 178.5, 'high': 181.0, 'low': 178.0, 'close': 180.5, 'volume': 2500, 'outside_ratio': 0.65, 'buyer_power': 350},
            {'time': '09:10', 'open': 180.5, 'high': 181.5, 'low': 179.5, 'close': 180.0, 'volume': 1400, 'outside_ratio': 0.58, 'buyer_power': 120},
            {'time': '09:15', 'open': 180.0, 'high': 182.5, 'low': 180.0, 'close': 182.0, 'volume': 3200, 'outside_ratio': 0.72, 'buyer_power': 480}
        ])
    
    def get_post_entry_klines(self, trade_date: str, stock_id: str, entry_time: str):
        return {
            'high': 186.0,
            'low': 179.0,
            'close': 184.5,
            'tp_time': '09:35',
            'sl_time': '13:00'
        }

def run_trading_day():
    print("=== 當沖選股推播與自動復盤系統啟動 ===")
    init_db()
    provider = MockMarketDataProvider()
    today = datetime.now().strftime("%Y-%m-%d")
    
    # 1. 盤前核心池載入
    watchlist = provider.get_watchlist(today)
    print(f"[{today}] 核心自選觀察名單載入完成，共 {len(watchlist)} 檔標的。")
    
    # 2. 盤中即時掃描 (09:05 ~ 10:30)
    for stock in watchlist:
        df_5min = provider.get_5min_bars(stock['stock_id'])
        signal = evaluate_intraday_signal(stock, df_5min)
        
        if signal:
            print(f"🎯 觸發進場訊號: {signal['stock_id']} {signal['stock_name']} @ {signal['suggested_entry_price']}")
            # 寫入資料庫
            insert_signal(signal)
            # 即時 Telegram 推播
            msg = format_entry_signal(signal)
            send_telegram_message(msg)
            
    # 3. 盤後自動復盤分析 (13:35 執行)
    print("執行盤後數據收集與復盤分析...")
    execute_daily_review(today, provider)
    print("=== 今日交易流程與復盤已全數歸檔完畢 ===")

if __name__ == "__main__":
    run_trading_day()
