"""
main.py - 當沖量化選股、即時推播與自動復盤主調度程式
已完整同步 market_provider.py 最新架構：
1. 傳入 trade_date 取得前一交易日 (T-1) 證交所三大法人同步買超共識。
2. 結合 Asia/Taipei 台灣時區精確切片的前日 12:00~13:30 尾盤高低撐壓。
3. 嚴格限時 09:05~10:30 開盤勢法掃描，觸發即發送 Telegram 進場推播。
4. 支援盤中即時監控、收盤自動復盤與夜間 20:00 預選清單推播 (--night)。
"""

import sys
import datetime
import logging

from database import init_db, insert_signal
from notifier import send_telegram_message, format_entry_signal
from strategy_engine import evaluate_intraday_signal
from review_analyzer import execute_daily_review
from market_provider import PublicTWDataFetcher

# 設定基礎日誌輸出格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


class MarketPipeline:
    """行情數據調度與快取管理管線"""

    def __init__(self):
        # 核心備援與常駐觀察動能標的名單
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
        """
        雙軌彙整候選股票名單：
        1. 證交所前一交易日 (T-1) 外資＋投信同步大買前 20 檔標的。
        2. 核心自選池合併去重。
        3. 計算每檔標的昨日 12:00~13:30 尾盤真實撐壓與開盤爆量狀態。
        """
        logger.info("【第一層：籌碼聚焦】向證交所公開資料擷取前一交易日法人買超名單...")
        consensus = PublicTWDataFetcher.get_twse_institutional_consensus(trade_date, top_n=20)

        # 合併法人共識名單與預設核心股，並依代號去重
        combined = []
        seen = set()
        for s in (consensus + self.default_stocks):
            if s["stock_id"] not in seen:
                seen.add(s["stock_id"])
                combined.append(s)

        watchlist = []
        target_candidates = combined[:20]
        logger.info(f"【第二層：量能與撐壓聚焦】計算前 {len(target_candidates)} 檔候選股之尾盤撐壓與爆量狀態...")

        for s in target_candidates:
            info, df_today, is_vol_expanding = PublicTWDataFetcher.get_stock_levels_and_5m(
                s["stock_id"], s["stock_name"], trade_date
            )
            if info and df_today is not None and not df_today.empty:
                info["is_vol_expanding"] = is_vol_expanding
                self.cached_info[s["stock_id"]] = info
                self.cached_klines[s["stock_id"]] = df_today
                watchlist.append(info)

                tag = "🔥 爆量攻擊" if is_vol_expanding else "⚪ 正常量能"
                print(
                    f"  ✓ {s['stock_id']} {s['stock_name']} [{tag}] -> "
                    f"昨日尾盤壓力: {info['prev_pm_high']} 元 | 支撐: {info['prev_pm_low']} 元"
                )

        return watchlist

    def get_5min_bars(self, stock_id: str):
        """取得特定標的今日 5分K 資料"""
        return self.cached_klines.get(stock_id)

    def get_post_entry_klines(self, trade_date: str, stock_id: str, entry_time: str):
        """取得觸發進場時間點後的 K 棒數據供盤後復盤使用"""
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


def run_trading_day():
    """盤中主動監控與盤後自動復盤作業"""
    print("==================================================")
    print("=== 當沖自動化量化選股與復盤系統啟動 ===")
    print("==================================================")
    init_db()
    pipeline = MarketPipeline()
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    # 1. 第一層與第二層聚焦 (籌碼 + 爆量 + 撐壓)
    watchlist = pipeline.get_watchlist(today)
    print(f"\n[{today}] 核心聚焦清單計算完成，共取得 {len(watchlist)} 檔有效監控標的。")

    if not watchlist:
        print("[提示] 今日無有效標的或市場休市，結束掃描。")
        return

    # 2. 第三層聚焦：盤中開盤勢法掃描 (限時 09:05 ~ 10:30)
    print("\n--- 開始執行 5分K 開盤勢法與技術指標掃描 ---")
    signals_triggered = 0
    for stock in watchlist:
        df_5m = pipeline.get_5min_bars(stock["stock_id"])
        if df_5m is None or len(df_5m) < 3:
            continue

        signal = evaluate_intraday_signal(stock, df_5m)
        if signal:
            signals_triggered += 1
            print(
                f"🎯 觸發進場訊號: {signal['stock_id']} {signal['stock_name']} "
                f"@ {signal['suggested_entry_price']} 元"
            )
            insert_signal(signal)
            msg = format_entry_signal(signal)
            send_telegram_message(msg)

    # 若盤中無標的滿足全部進場規則，發送守紀律日報
    if signals_triggered == 0:
        scan_msg = (
            f"🔍 *【台股當沖策略監控日報】*\n"
            f"📅 日期：`{today}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 監控標的數：共 `{len(watchlist)}` 檔 (已結合 T-1 法人共識)\n"
            f"🛡️ 掃描結果：*今日無標的完全符合進場標準*\n"
            f"──────────────────\n"
            f"💡 *紀律核心*：未見『09:05~10:30 強強開盤 + 底底高 + 布林開口』前不盲目追價，嚴守空手觀望紀律！"
        )
        send_telegram_message(scan_msg)
        print("盤中掃描完成：已發送『空手觀望』推播戰報至 Telegram。")

    # 3. 盤後自動復盤分析 (計算勝率、盈虧比與出場原因)
    print("\n--- 執行盤後數據收集與復盤分析 ---")
    execute_daily_review(today, pipeline)
    print("=== 今日交易流程與復盤全數歸檔完畢 ===")


def run_night_watchlist():
    """夜間 20:00 預選清單產生與提前推播"""
    print("==================================================")
    print("=== 執行夜間選股：計算明日當沖自選清單與關鍵撐壓 ===")
    print("==================================================")
    init_db()
    pipeline = MarketPipeline()
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    watchlist = pipeline.get_watchlist(today)
    if not watchlist:
        print("[提示] 今日無符合之名單。")
        return

    stock_lines = []
    for s in watchlist[:15]:
        vol_tag = "🔥" if s.get("is_vol_expanding") else "🔹"
        stock_lines.append(
            f"{vol_tag} *{s['stock_id']} {s['stock_name']}*\n"
            f"   • 明日關鍵壓力：`{s['prev_pm_high']:.1f}` 元\n"
            f"   • 明日防守支撐：`{s['prev_pm_low']:.1f}` 元"
        )

    stocks_detail = "\n".join(stock_lines)
    night_msg = (
        f"📋 *【明日當沖獵殺・核心自選觀察清單】* 📋\n"
        f"📅 統計基準日：`{today}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *法人共識＋動能標的*（共 `{len(watchlist[:15])}` 檔）：\n\n"
        f"{stocks_detail}\n"
        f"──────────────────\n"
        f"💡 *明晨作戰守則*：\n"
        f"1. 09:00 開盤第一根 5 分 K 帶量衝破壓力線 ➜ 鎖定【強強開盤】\n"
        f"2. 洗盤不破前低 (底底高) 且布林開口張開時 ➜ 系統將自動推播進場！\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_telegram_message(night_msg)
    print("夜間自選名單已成功推播至 Telegram！")


if __name__ == "__main__":
    # 支援透過 --night 參數在夜間 20:00 推播次日觀察名單
    if len(sys.argv) > 1 and sys.argv[1] == "--night":
        run_night_watchlist()
    else:
        run_trading_day()
