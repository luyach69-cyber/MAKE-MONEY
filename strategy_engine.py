"""
strategy_engine.py - 量化當沖策略與風控引擎
"""
from typing import Optional, Dict, Any
import pandas as pd
import numpy as np

def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """計算 5 分 K 布林通道 (上軌、中軌 20MA、下軌)"""
    df = df.copy()
    df['ma20'] = df['close'].rolling(window=period).mean()
    df['std'] = df['close'].rolling(window=period).std()
    df['upper_band'] = df['ma20'] + (std_dev * df['std'])
    df['lower_band'] = df['ma20'] - (std_dev * df['std'])
    return df

def identify_open_pattern(bar1_open: float, bar1_close: float, bar1_high: float, prev_pm_high: float) -> str:
    """判斷開盤勢法基本型態"""
    is_red = bar1_close >= bar1_open
    if bar1_close > prev_pm_high and is_red:
        return "強強開盤"
    elif bar1_high > prev_pm_high and not is_red:
        return "強弱開盤"
    elif bar1_close <= prev_pm_high and is_red:
        return "弱強開盤"
    else:
        return "弱弱開盤"

def evaluate_intraday_signal(stock_info: Dict[str, Any], df_5min: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """
    評估是否符合開盤勢法「強強開盤突破變化型」進場條件
    """
    if df_5min is None or len(df_5min) < 3:
        return None

    current_idx = len(df_5min) - 1
    current_bar = df_5min.iloc[current_idx]
    prev_bar = df_5min.iloc[current_idx - 1]
    current_time = str(current_bar.get('time', ''))

    # 1. 時間濾網：開盤勢法僅適用於早盤 09:05 ~ 10:30 主力動能表態期
    if current_time and (current_time < "09:05" or current_time > "10:30"):
        return None

    prev_pm_high = float(stock_info['prev_pm_high'])
    prev_pm_low = float(stock_info['prev_pm_low'])

    # 2. 技術指標計算
    df_bb = calculate_bollinger_bands(df_5min)
    curr_bb = df_bb.iloc[current_idx]
    bar1 = df_bb.iloc[0]

    # 3. 開盤型態檢驗
    pattern = identify_open_pattern(bar1['open'], bar1['close'], bar1['high'], prev_pm_high)
    if pattern != "強強開盤":
        return None

    # 4. 形態與籌碼過濾
    # (A) 洗盤拉回守穩昨日尾盤壓力 (壓力轉支撐)
    is_support_held = curr_bb['low'] >= prev_pm_high
    # (B) 底部墊高 (底底高)，不跌破前一根 5分K 與早盤第一根低點
    is_bottom_higher = (curr_bb['low'] >= prev_bar['low']) and (curr_bb['low'] >= bar1['low'])
    # (C) 外盤比率與大戶買賣力紅柱
    outside_ratio = float(curr_bb.get('outside_ratio', 0.6))
    buyer_power_positive = float(curr_bb.get('buyer_power', 1)) > 0
    # (D) 布林通道處於張開向上發散狀態 (排除壓縮走平盤整)
    bband_width = (curr_bb['upper_band'] - curr_bb['lower_band']) / (curr_bb['ma20'] + 1e-6)
    is_bband_expanding = bband_width > 0.015
    above_mid_band = curr_bb['close'] >= curr_bb['ma20']

    # 5. 進場訊號與風控計算
    if (is_support_held and is_bottom_higher and outside_ratio >= 0.55 
            and buyer_power_positive and above_mid_band and is_bband_expanding):
        
        entry_price = float(curr_bb['close'])
        tp_price = round(entry_price * 1.02, 2)  # 嚴守紀律：+2.0% 停利

        # 風控修正：停損點取進場棒線低點與防守支撐，且強制約束必須低於進場成本
        raw_sl = min(float(curr_bb['low']), prev_pm_high) * 0.995
        sl_price = round(min(raw_sl, entry_price * 0.985), 2)

        reasons = [
            f"1. 開盤符合【強強開盤】，第1根5分K突破昨日尾盤壓力({prev_pm_high:.1f}元)",
            f"2. 第{current_idx+1}根拉回洗盤守穩關鍵支撐，低點墊高(底底高)不破前低",
            f"3. 盤中外盤敲進比例達 {outside_ratio*100:.1f}%，大戶買賣力維持強勁紅柱",
            f"4. 5分K布林通道向上發散，股價站穩中軌(20MA)，多方動能表態"
        ]

        return {
            'trade_date': stock_info['trade_date'],
            'stock_id': stock_info['stock_id'],
            'stock_name': stock_info['stock_name'],
            'signal_time': current_time,
            'strategy_type': "開盤勢法-強強突破變化型",
            'entry_reason': "\n".join(reasons),
            'suggested_entry_price': entry_price,
            'take_profit_price': tp_price,
            'stop_loss_price': sl_price,
            'open_pattern': pattern,
            'bband_status': "軌道張開・站穩中軌向上",
            'buyer_power_ratio': float(curr_bb.get('buyer_power', 100)),
            'outside_ratio': outside_ratio
        }

    return None
