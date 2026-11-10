"""
main.py - 交易系統主程式
"""
import datetime
from database import init_db, insert_signal
from notifier import send_telegram_message, format_entry_signal
from strategy_engine import evaluate_intraday_signal
from review_analyzer import execute_daily_review
from market_provider import PublicTWDataFetcher

class MarketPipeline:
    def __init__(self):
        self.default_stocks = [
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
        self.cached_klines = {}

    def get_watchlist(self, trade_date: str):
        print("【第一層：籌碼聚焦】獲取證交所外資＋投信同步買超名單...")
        consensus = PublicTWDataFetcher.get_twse_institutional_consensus(top_n=20)
        combined = []
        seen = set()
        for s in (consensus + self.default_stocks):
            if s["stock_id"] not in seen:
                seen.add(s["stock_id"])
                combined.append(s)

        watchlist = []
        print(f"【第二層：量能與撐壓聚焦】計算候選股撐壓與量能...")
        for s in combined[:15]:
            info, df_today, is_vol_expanding = PublicTWDataFetcher.get_stock_levels_and_5m(s["stock_id"], s["stock_name"], trade_date)
            if info and df_today is not None and not df_today.empty:
                info["is_vol_expanding"] = is_vol_expanding
                self.cached_info[s["stock_id"]] = info
                self.cached_klines[s["stock_id"]] = df_today
                watchlist.append(info)
                tag = "🔥 爆量攻擊" if is_vol_expanding else "⚪ 正常量能"
                print(f"  ✓ {s['stock_id']} {s['stock_name']} [{tag}] -> 昨日尾盤壓力: {info['prev_pm_high']} 元 | 支撐: {info['prev_pm_low']} 元")

        return watchlist

    def get_5min_bars(self, stock_id: str):
        return self.cached_klines.get(stock_id)

    def get_post_entry_klines(self, trade_date: str, stock_id: str, entry_time: str):
        df = self.cached_klines.get(stock_id)
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

def run():
    print("==================================================")
    print("=== 當沖自動化量化選股與復盤系統啟動 ===")
    print("==================================================")
    init_db()
    pipeline = MarketPipeline()
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    # 1. 執行第一層與第二層聚焦 (籌碼 + 量能 + 撐壓)
    watchlist = pipeline.get_watchlist(today)
    if not watchlist:
        print("[提示] 今日無有效標的，退出。")
        return

    # 2. 第三層聚焦：盤中開盤勢法掃描 (限 09:05 ~ 10:30)
    print("\n--- 開始執行 5分K 開盤勢法掃描 ---")
    signals = 0
    for stock in watchlist:
        df_5m = pipeline.get_5min_bars(stock["stock_id"])
        sig = evaluate_intraday_signal(stock, df_5m)
        if sig:
            signals += 1
            print(f"🎯 觸發訊號: {sig['stock_id']} {sig['stock_name']} @ {sig['suggested_entry_price']}")
            insert_signal(sig)
            send_telegram_message(format_entry_signal(sig))

    if signals == 0:
        msg = (
            f"🔍 *【台股當沖策略監控日報】*\n"
            f"📅 日期：`{today}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 監控標的數：共 `{len(watchlist)}` 檔\n"
            f"🛡️ 掃描結果：*今日無標的完全符合進場標準*\n"
            f"──────────────────\n"
            f"💡 *紀律核心*：未見『09:05~10:30 強強開盤 + 底底高 + 布林開口』前不盲目追價，嚴守空手觀望紀律！"
        )
        send_telegram_message(msg)
        print("盤中掃描完成：已發送『空手觀望』推播戰報至 Telegram。")

    # 3. 盤後自動復盤 (收盤 13:35 自動統計)
    print("\n--- 執行盤後數據收集與復盤分析 ---")
    execute_daily_review(today, pipeline)
    print("=== 今日交易流程與復盤全數歸檔完畢 ===")

if __name__ == "__main__":
    run()
