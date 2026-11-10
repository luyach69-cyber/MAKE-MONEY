def execute_daily_review(trade_date: str, market_data_provider, db_path="trading_records.db"):
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM signal_records WHERE trade_date = ?", (trade_date,))
    rows = cursor.fetchall()
    
    total_signals = len(rows)
    win_count, loss_count, pnl_list = 0, 0, []
    
    for row in rows:
        # 取得進場後至 13:30 的完整高低點與收盤價
        kline = market_data_provider.get_post_entry_klines(trade_date, row['stock_id'], row['signal_time'])
        is_hit_tp = 1 if kline['high'] >= row['take_profit_price'] else 0
        is_hit_sl = 1 if kline['low'] <= row['stop_loss_price'] else 0
        
        # 當沖模擬出場判定
        if is_hit_tp == 1 and (is_hit_sl == 0 or kline['tp_time'] <= kline['sl_time']):
            exit_price = row['take_profit_price']
            pnl_pct = 2.0
            is_win = 1
            win_count += 1
            notes = "達標 2% 嚴格停利出場"
        elif is_hit_sl == 1:
            exit_price = row['stop_loss_price']
            pnl_pct = ((exit_price - row['suggested_entry_price']) / row['suggested_entry_price']) * 100
            is_win = 0
            loss_count += 1
            notes = "跌破防守支撐，及時停損離場"
        else:
            # 13:25 平倉紀律
            exit_price = kline['close']
            pnl_pct = ((exit_price - row['suggested_entry_price']) / row['suggested_entry_price']) * 100
            is_win = 1 if pnl_pct > 0 else 0
            if is_win: win_count += 1
            else: loss_count += 1
            notes = "13:25 今日事今日畢平倉"
            
        pnl_list.append(pnl_pct)
        # 更新至 SQLite
        cursor.execute("""
            UPDATE signal_records
            SET high_price_post = ?, low_price_post = ?, close_price_post = ?,
                is_hit_tp = ?, is_hit_sl = ?, actual_exit_price = ?,
                pnl_pct = ?, is_win = ?, review_notes = ?
            WHERE id = ?
        """, (kline['high'], kline['low'], kline['close'], is_hit_tp, is_hit_sl, exit_price, pnl_pct, is_win, notes, row['id']))
    
    win_rate = (win_count / total_signals) * 100 if total_signals > 0 else 0
    avg_pnl = sum(pnl_list) / len(pnl_list) if pnl_list else 0
    
    # 寫入每日統計總表並推播 Telegram 戰報
    cursor.execute("""
        INSERT OR REPLACE INTO daily_reviews (trade_date, total_signals, win_count, loss_count, win_rate, avg_pnl_pct)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (trade_date, total_signals, win_count, loss_count, win_rate, avg_pnl))
    conn.commit()
    conn.close()
