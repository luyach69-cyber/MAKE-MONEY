"""
database.py - SQLite 資料庫操作模組
"""
import sqlite3
from config import DB_PATH

def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path=DB_PATH):
    """初始化交易記錄與復盤資料表"""
    conn = get_connection(db_path)
    cursor = conn.cursor()
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

def insert_signal(sig: dict, db_path=DB_PATH) -> int:
    """寫入即時觸發訊號"""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO signal_records (
        trade_date, stock_id, stock_name, signal_time, strategy_type,
        entry_reason, suggested_entry_price, take_profit_price, stop_loss_price,
        open_pattern, bband_status, buyer_power_ratio, outside_ratio
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sig['trade_date'], sig['stock_id'], sig['stock_name'], sig['signal_time'],
        sig['strategy_type'], sig['entry_reason'], sig['suggested_entry_price'],
        sig['take_profit_price'], sig['stop_loss_price'], sig.get('open_pattern', ''),
        sig.get('bband_status', ''), sig.get('buyer_power_ratio', 0.0), sig.get('outside_ratio', 0.0)
    ))
    conn.commit()
    sig_id = cursor.lastrowid
    conn.close()
    return sig_id
