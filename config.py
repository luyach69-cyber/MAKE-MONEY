"""
config.py - 系統配置與參數常數
"""
import os

# --- Telegram Bot 配置 ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- 資料庫配置 ---
DB_PATH = "trading_records.db"

# --- 交易與策略核心參數 ---
PROFIT_TARGET_RATIO = 0.02   # 嚴格 +2% 停利目標
STOP_LOSS_RATIO = 0.015      # 基準防守停損比例 (1.5%)
MAX_SLIPPAGE = 0.002         # 滑價與手續費保護緩衝

# --- 交易時段規範 (開盤勢法黃金時段) ---
SESSION_START = "09:05"      # 避開 09:00~09:05 隔日沖洗盤
SESSION_END = "10:30"        # 開盤勢法主力主波段截止時段
MARKET_CLOSE_BUFFER = "13:25" # 當日事當日畢，尾盤平倉底線
