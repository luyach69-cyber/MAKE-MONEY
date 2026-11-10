"""
系統設定檔 (config.py)
定義交易參數、技術指標門檻、Telegram 推播與 SQLite 資料庫設定
"""
import os

# --- Telegram Bot 設定 ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

# --- 資料庫設定 ---
DB_PATH = "trading_records.db"

# --- 交易與策略核心參數 ---
PROFIT_TARGET_RATIO = 0.02   # 嚴格 +2% 停利
MAX_CAPITAL_PER_TRADE = 100000 # 單筆當沖部位上限 (資金 10% 紀律)

# --- 開盤勢法與技術濾網門檻 ---
VOLUME_MULTIPLIER_THRESHOLD = 2.5  # 成交量放大倍數 (比前日爆量 2.5 倍以上)
TURNOVER_RATE_THRESHOLD = 5.0      # 高週轉率門檻 (日週轉率 >= 5%)
OUTSIDE_TRADE_RATIO_MIN = 0.55     # 外盤成交比例 >= 55% (主動追價強烈)
BBAND_PERIOD = 20                  # 布林通道中軌 (20 MA / 月線)
BBAND_STD = 2.0                    # 布林通道標準差

# --- 交易時段定義 ---
MARKET_OPEN_TIME = "09:00:00"
OBSERVE_START_TIME = "09:05:00"    # 避開 09:00~09:05 第一根洗盤
SCAN_CUTOFF_TIME = "10:30:00"      # 盤中主波段發動黃金監控期
MARKET_CLOSE_TIME = "13:30:00"
