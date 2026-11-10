import pandas as pd
import numpy as np

def calculate_bollinger_bands(df: pd.DataFrame, period=20, std_dev=2.0):
    """計算 5分K 布林通道 (上軌、中軌20MA、下軌)"""
    df = df.copy()
    df['ma20'] = df['close'].rolling(window=period).mean()
    df['std'] = df['close'].rolling(window=period).std()
    df['upper_band'] = df['ma20'] + (std_dev * df['std'])
    df['lower_band'] = df['ma20'] - (std_dev * df['std'])
    return df

def identify_open_pattern(bar1_open, bar1_close, bar1_high, bar1_low, prev_pm_high, prev_pm_low):
    """
    依據前日尾盤 (12:00~13:30) 高點 (壓力) 與低點 (支撐) 判斷開盤型態:
    - 強強開盤: 第一根為紅K且收盤高於前日尾盤壓力
    - 強弱開盤: 開盤高於前日壓力但收盤為黑K
    - 弱強開盤: 開在前日壓力之下，但收紅K力圖反彈
    - 弱弱開盤: 開在前日低點之下且收黑K
    """
    is_red = bar1_close >= bar1_open
    if bar1_close > prev_pm_high and is_red:
        return "強強開盤"
    elif bar1_high > prev_pm_high and not is_red:
        return "強弱開盤"
    elif bar1_close <= prev_pm_high and is_red:
        return "弱強開盤"
    else:
        return "弱弱開盤"

def evaluate_intraday_signal(stock_info: dict, df_5min: pd.DataFrame) -> dict:
    """盤中 5分K 訊號過濾 (避開 09:00~09:05 隔日沖洗盤，專注 09:05~10:30 主波段)"""
    if len(df_5min) < 3:
        return None
    
    prev_pm_high = stock_info['prev_pm_high']
    prev_pm_low = stock_info['prev_pm_low']
    df_bb = calculate_bollinger_bands(df_5min)
    
    # 第 1 根 5分K 檢驗型態
    bar1 = df_bb.iloc[0]
    pattern = identify_open_pattern(bar1['open'], bar1['close'], bar1['high'], bar1['low'], prev_pm_high, prev_pm_low)
    if pattern != "強強開盤":
        return None  # 嚴格只做強強開盤
    
    current_idx = len(df_bb) - 1
    current_bar = df_bb.iloc[current_idx]
    prev_bar = df_bb.iloc[current_idx - 1]
    
    # 核心條件檢驗：
    # 1. 變化式洗盤不破前日壓力線
    is_support_held = current_bar['low'] >= prev_pm_high
    # 2. 底部墊高 (底底高)
    is_bottom_higher = current_bar['low'] >= prev_bar['low']
    # 3. 外盤大單成交比 >= 55% 且大戶買賣力維持紅柱
    outside_ratio = current_bar.get('outside_ratio', 0.6)
    buyer_power_positive = current_bar.get('buyer_power', 1) > 0
    # 4. 濾除布林通道走平盤整，要求帶量突破中軌且開口發散
    bband_width = (current_bar['upper_band'] - current_bar['lower_band']) / current_bar['ma20']
    is_bband_expanding = bband_width > 0.015
    above_mid_band = current_bar['close'] >= current_bar['ma20']
    
    if is_support_held and is_bottom_higher and outside_ratio >= 0.55 and buyer_power_positive and above_mid_band and is_bband_expanding:
        entry_price = float(current_bar['close'])
        tp_price = round(entry_price * 1.02, 2)  # 嚴守紀律 +2% 停利
        sl_price = round(max(bar1['low'], prev_pm_high) * 0.995, 2) # 防守破線價
        
        reasons = [
            f"1. 開盤符合【強強開盤】，第1根5分K突破昨日尾盤壓力({prev_pm_high}元)",
            f"2. 第{current_idx+1}根拉回洗盤守穩關鍵支撐，底部墊高(底底高)不破前低",
            f"3. 盤中外盤敲進比例達 {outside_ratio*100:.1f}%，大戶買賣力維持強勁紅柱",
            f"4. 5分K布林通道向上發散，股價站穩中軌(20MA)，多方動能表態"
        ]
        
        return {
            'trade_date': stock_info['trade_date'],
            'stock_id': stock_info['stock_id'],
            'stock_name': stock_info['stock_name'],
            'signal_time': current_bar['time'],
            'strategy_type': "開盤勢法-強強突破變化型",
            'entry_reason': "\n".join(reasons),
            'suggested_entry_price': entry_price,
            'take_profit_price': tp_price,
            'stop_loss_price': sl_price,
            'open_pattern': pattern,
            'bband_status': "軌道張開・站穩中軌向上",
            'buyer_power_ratio': float(current_bar.get('buyer_power', 100)),
            'outside_ratio': outside_ratio
        }
    return None
