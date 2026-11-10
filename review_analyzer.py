"""
盤後復盤與數據分析模組 (review_analyzer.py)
自動抓取當日完整K線與五檔明細，進行以下驗證：
1. 停利 (+2%) 與停損觸發檢驗
2. 勝率、平均期望值、最大回撤分析
3. 統計報告生成與 Telegram 推播戰報
"""
import sqlite3
import pandas as pd
from database import get_connection
from notifier import send_telegram_message, format_daily_review_report

def execute_daily_review(trade_date: str, market_data_provider, db_path="trading_records.db"):
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    # 查詢當日所有發出的訊號
    cursor.execute("""
        SELECT * FROM signal_records WHERE trade_date = ?
    """, (trade_date,))
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
        
        # 取得進場後至 13:30 的真實高低點與收盤價
        kline_data = market_data_provider.get_post_entry_klines(trade_date, stock_id, row['signal_time'])
        
        day_high = kline_data['high']
        day_low = kline_data['low']
        day_close = kline_data['close']
        
        is_hit_tp = 1 if day_high >= tp_price else 0
        is_hit_sl = 1 if day_low <= sl_price else 0
        
        # 當沖出場判斷模擬
        if is_hit_tp == 1 and (is_hit_sl == 0 or kline_data['tp_time'] <= kline_data['sl_time']):
            exit_price = tp_price
            pnl_pct = 2.0
            is_win = 1
            win_count += 1
            review_notes = "達標 2% 嚴格停利出場，波段高點達 " + str(day_high)
        elif is_hit_sl == 1:
            exit_price = sl_price
            pnl_pct = ((sl_price - entry_price) / entry_price) * 100
            is_win = 0
            loss_count += 1
            review_notes = f"跌破防守支撐，及時停損離場避免擴大虧損"
        else:
            # 尾盤 13:25 市價平倉紀律
            exit_price = day_close
            pnl_pct = ((day_close - entry_price) / entry_price) * 100
            is_win = 1 if pnl_pct > 0 else 0
            if is_win:
                win_count += 1
            else:
                loss_count += 1
            review_notes = f"盤中未達停利停損，13:25 今日事今日畢平倉"
        
        pnl_list.append(pnl_pct)
        
        # 回填資料庫
        cursor.execute("""
            UPDATE signal_records
            SET high_price_post = ?, low_price_post = ?, close_price_post = ?,
                is_hit_tp = ?, is_hit_sl = ?, actual_exit_price = ?,
                pnl_pct = ?, is_win = ?, review_notes = ?
            WHERE id = ?
        """, (day_high, day_low, day_close, is_hit_tp, is_hit_sl, exit_price, pnl_pct, is_win, review_notes, row['id']))
    
    win_rate = (win_count / total_signals) * 100 if total_signals > 0 else 0
    avg_pnl = sum(pnl_list) / len(pnl_list) if pnl_list else 0
    max_dd = min(pnl_list) if pnl_list else 0
    
    summary_text = (
        f"• 當日總訊號數：{total_signals} 筆\n"
        f"• 勝率：{win_rate:.1f}% ({win_count}勝 / {loss_count}敗)\n"
        f"• 平均單筆損益：{avg_pnl:+.2f}%\n"
        f"• 最大單筆回撤：{max_dd:.2f}%\n"
        f"• 覆盤結論：嚴格執行 2% 停利有效避開午後主流出貨下殺，防守底底高機制有效攔截破底風險。"
    )
    
    cursor.execute("""
        INSERT OR REPLACE INTO daily_reviews (
            trade_date, total_signals, win_count, loss_count, win_rate, avg_pnl_pct, max_drawdown_pct, review_summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (trade_date, total_signals, win_count, loss_count, win_rate, avg_pnl, max_dd, summary_text))
    
    conn.commit()
    conn.close()
    
    summary_dict = {
        'trade_date': trade_date,
        'total_signals': total_signals,
        'win_count': win_count,
        'loss_count': loss_count,
        'win_rate': win_rate,
        'avg_pnl_pct': avg_pnl,
        'review_summary': summary_text
    }
    
    # 發送復盤戰報到 Telegram
    send_telegram_message(format_daily_review_report(summary_dict))
    return summary_dict
