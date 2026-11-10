"""
review_analyzer.py - 盤後數據回補、盈虧統計與復盤引擎
"""
import sqlite3
from database import get_connection
from notifier import send_telegram_message

def execute_daily_review(trade_date: str, market_provider, db_path="trading_records.db"):
    """盤後 13:35 自動檢驗當日訊號真實損益與期望值"""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM signal_records WHERE trade_date = ?", (trade_date,))
    rows = cursor.fetchall()

    if not rows:
        print(f"[{trade_date}] 今日無交易訊號，無需復盤。")
        conn.close()
        return None

    total_signals = len(rows)
    win_count = 0
    loss_count = 0
    pnl_list = []

    for row in rows:
        stock_id = row['stock_id']
        entry_price = row['suggested_entry_price']
        tp_price = row['take_profit_price']
        sl_price = row['stop_loss_price']

        kline = market_provider.get_post_entry_klines(trade_date, stock_id, row['signal_time'])
        is_hit_tp = 1 if kline['high'] >= tp_price else 0
        is_hit_sl = 1 if kline['low'] <= sl_price else 0

        # 當沖出場判定邏輯
        if is_hit_tp == 1 and (is_hit_sl == 0 or kline['tp_time'] <= kline['sl_time']):
            exit_price = tp_price
            pnl_pct = 2.0
            is_win = 1
            win_count += 1
            notes = "達標 2% 嚴格停利出場"
        elif is_hit_sl == 1:
            exit_price = sl_price
            pnl_pct = round(((sl_price - entry_price) / entry_price) * 100, 2)
            is_win = 0
            loss_count += 1
            notes = "跌破防守支撐，及時停損離場"
        else:
            exit_price = kline['close']
            pnl_pct = round(((kline['close'] - entry_price) / entry_price) * 100, 2)
            is_win = 1 if pnl_pct > 0 else 0
            if is_win:
                win_count += 1
            else:
                loss_count += 1
            notes = "13:25 尾盤今日事今日畢平倉"

        pnl_list.append(pnl_pct)

        cursor.execute("""
            UPDATE signal_records
            SET high_price_post = ?, low_price_post = ?, close_price_post = ?,
                is_hit_tp = ?, is_hit_sl = ?, actual_exit_price = ?,
                pnl_pct = ?, is_win = ?, review_notes = ?
            WHERE id = ?
        """, (kline['high'], kline['low'], kline['close'], is_hit_tp, is_hit_sl, exit_price, pnl_pct, is_win, notes, row['id']))

    win_rate = (win_count / total_signals) * 100 if total_signals > 0 else 0
    avg_pnl = sum(pnl_list) / len(pnl_list) if pnl_list else 0
    max_dd = min(pnl_list) if pnl_list else 0

    summary_text = (
        f"• 當日總訊號數：{total_signals} 筆\n"
        f"• 當日勝率：{win_rate:.1f}% ({win_count}勝 / {loss_count}敗)\n"
        f"• 平均單筆損益：{avg_pnl:+.2f}%\n"
        f"• 最大單筆回撤：{max_dd:.2f}%\n"
        f"• 覆盤結論：嚴格執行 2% 停利與底底高停損，避開尾盤急殺風險。"
    )

    cursor.execute("""
        INSERT OR REPLACE INTO daily_reviews (
            trade_date, total_signals, win_count, loss_count, win_rate, avg_pnl_pct, max_drawdown_pct, review_summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (trade_date, total_signals, win_count, loss_count, win_rate, avg_pnl, max_dd, summary_text))
    conn.commit()
    conn.close()

    # 發送復盤總結推播
    report_msg = (
        f"📊 *【盤後復盤分析與數據回測戰報】* 📊\n"
        f"📅 *日期*：`{trade_date}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *當日訊號總數*：`{total_signals}` 檔\n"
        f"🟢 *成功達標停利 (+2%)*：`{win_count}` 檔\n"
        f"🔴 *觸及停損/平倉*：`{loss_count}` 檔\n"
        f"🏆 *策略勝率*：`{win_rate:.1f}%`\n"
        f"💰 *平均單筆期望報酬*：`{avg_pnl:+.2f}%`\n"
        f"──────────────────\n"
        f"📝 *今日總結分析*：\n{summary_text}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_telegram_message(report_msg)
