"""
推播通知模組 (notifier.py)
整合 Telegram Bot API 傳送格式化當沖即時進場訊號與盤後復盤戰報
"""
import requests
import logging
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(f"[模擬推播] 未填寫 Telegram Token，終端機輸出:\n{message}")
        return True
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        logger.error(f"Telegram 推播失敗: {e}")
        return False

def format_entry_signal(sig: dict) -> str:
    msg = (
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
        f"  • 布林軌道：`{sig.get('bband_status', '帶量突破中軌 / 沿上軌發散')}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ *紀律警語*：今日事今日畢，達標自動分批停利；嚴禁當沖變留倉！"
    )
    return msg

def format_daily_review_report(summary: dict) -> str:
    msg = (
        f"📊 *【盤後復盤分析與數據回測戰報】* 📊\n"
        f"📅 *日期*：`{summary['trade_date']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *當日訊號總數*：`{summary['total_signals']}` 檔\n"
        f"🟢 *成功達標停利 (+2%)*：`{summary['win_count']}` 檔\n"
        f"🔴 *觸及停損離場*：`{summary['loss_count']}` 檔\n"
        f"🏆 *當沖策略勝率*：`{summary['win_rate']:.1f}%`\n"
        f"💰 *平均單筆期望報酬*：`{summary['avg_pnl_pct']:+.2f}%`\n"
        f"──────────────────\n"
        f"📝 *今日總結分析*：\n{summary['review_summary']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 *明日重點*：持盈保泰，嚴格遵守底底高與大戶買賣力濾網！"
    )
    return msg
