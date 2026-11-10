"""
資料庫存取層 (database.py)
使用 SQLite 紀錄每日推播訊號、成交價位、盤後收盤、盈虧與策略復盤統計
"""
import sqlite3
from datetime import datetime

def get_connection(db_path="trading_records.db"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path="trading_records.db"):
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    # 建立訊號與交易記錄表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signal_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        stock_id TEXT NOT NULL,
        stock_name TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        strategy_type TEXT NOT NULL,
        entry_reason TEXT NOT NULL,
        suggested_entry_price REAL NOT NULL,
        take_profit_price REAL NOT NULL,
        stop_loss_price REAL NOT NULL,
        open_pattern TEXT,
        bband_status TEXT,
        buyer_power_ratio REAL,
        outside_ratio REAL,
        
        -- 盤後復盤更新欄位
        high_price_post REAL,
        low_price_post REAL,
        close_price_post REAL,
        is_hit_tp INTEGER DEFAULT 0,
        is_hit_sl INTEGER DEFAULT 0,
        actual_exit_price REAL,
        pnl_pct REAL,
        is_win INTEGER,
        review_notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # 建立每日績效統計表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_reviews (
        trade_date TEXT PRIMARY KEY,
        total_signals INTEGER,
        win_count INTEGER,
        loss_count INTEGER,
        win_rate REAL,
        avg_pnl_pct REAL,
        max_drawdown_pct REAL,
        review_summary TEXT
    )
    """)
    conn.commit()
    conn.close()

def insert_signal(signal_data, db_path="trading_records.db"):
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO signal_records (
        trade_date, stock_id, stock_name, signal_time, strategy_type,
        entry_reason, suggested_entry_price, take_profit_price, stop_loss_price,
        open_pattern, bband_status, buyer_power_ratio, outside_ratio
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal_data['trade_date'],
        signal_data['stock_id'],
        signal_data['stock_name'],
        signal_data['signal_time'],
        signal_data['strategy_type'],
        signal_data['entry_reason'],
        signal_data['suggested_entry_price'],
        signal_data['take_profit_price'],
        signal_data['stop_loss_price'],
        signal_data.get('open_pattern', ''),
        signal_data.get('bband_status', ''),
        signal_data.get('buyer_power_ratio', 0.0),
        signal_data.get('outside_ratio', 0.0)
    ))
    conn.commit()
    sig_id = cursor.lastrowid
    conn.close()
    return sig_id
