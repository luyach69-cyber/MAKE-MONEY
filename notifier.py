"""
notifier.py - Telegram 推播通知模組
"""
import requests
import logging
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

def send_telegram_message(message: str) -> bool:
    """發送 Markdown 格式訊息至 Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[推播記錄 (無 Token)]:\n{message}")
        return True
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=8)
        return res.status_code == 200
    except Exception as e:
        logger.error(f"Telegram 推播失敗: {e}")
        return False

def format_entry_signal(sig: dict) -> str:
    """格式化進場即時推播訊息"""
    return (
        f"🚨 *【當沖實戰進場訊號表態】* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 *標的*：`{sig['stock_id']} {sig['stock_name']}`\n"
        f"⏰ *觸發時間*：`{sig['signal_time']}`\n"
        f"📊 *策略類型*：`{sig['strategy_type']}`\n"
        f"──────────────────\n"
        f"🎯 *建議進場價*：`{sig['suggested_entry_price']:.2f}` 元\n"
        f"🟢 *目標停利價*：`{sig['take_profit_price']:.2f}` 元 *(+2.0% 嚴守紀律)*\n"
        f"🔴 *防守停損價*：`{sig['stop_loss_price']:.2f}` 元 *(跌破前低/支撐)*\n"
        f"──────────────────\n"
        f"💡 *進場核心原因*：\n{sig['entry_reason']}\n"
        f"📈 *即時盤面數據*：\n"
        f"  • 開盤型態：`{sig.get('open_pattern', '強強開盤')}`\n"
        f"  • 外盤成交比：`{sig.get('outside_ratio', 0)*100:.1f}%` *(大單敲進)*\n"
        f"  • 大戶買賣力：`紅柱放量`\n"
        f"  • 布林軌道：`{sig.get('bband_status', '站穩中軌向上')}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ *紀律警語*：今日事今日畢，達標自動分批停利；嚴禁當沖變留倉！"
    )
