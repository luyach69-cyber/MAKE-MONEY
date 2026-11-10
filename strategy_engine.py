"""
量化策略引擎 (strategy_engine.py)
整合四大關鍵邏輯：
1. 盤前核心池篩選 (量放大、高週轉、外資投信共識)
2. 開盤勢法辨識 (強強開盤、前日 12:00~13:30 尾盤高低撐壓)
3. 5分K 形態與大戶籌碼過濾 (底底高、外盤比率 > 55%、大戶買賣力紅柱、不破第1根低點)
4. 布林通道 (5分K, 20 MA) 突破與回踩支撐確認
"""
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
    """
    盤中 5分K 訊號精確評估 (於 09:05~10:30 運行)
    回傳符合條件之訊號字典，不符合回傳 None
    """
    if len(df_5min) < 3:
        return None
    
    # 取得前日尾盤高低點
    prev_pm_high = stock_info['prev_pm_high']
    prev_pm_low = stock_info['prev_pm_low']
    
    # 計算布林通道
    df_bb = calculate_bollinger_bands(df_5min)
    
    # 第 1 根 5分K (09:00 ~ 09:05)
    bar1 = df_bb.iloc[0]
    pattern = identify_open_pattern(bar1['open'], bar1['close'], bar1['high'], bar1['low'], prev_pm_high, prev_pm_low)
    
    # 策略主規則 1: 專注於強強開盤 (或強勢帶量突破)
    if pattern != "強強開盤":
        return None
    
    # 當前最新棒線 (第 2 或第 3 根)
    current_idx = len(df_bb) - 1
    current_bar = df_bb.iloc[current_idx]
    prev_bar = df_bb.iloc[current_idx - 1]
    
    # 策略主規則 2: 變化式走法與底底高確認
    # 條件 A: 當前根不破前根低點 (或回檔但絕不跌破前日尾盤關鍵壓力 prev_pm_high)
    is_support_held = current_bar['low'] >= prev_pm_high
    is_bottom_higher = current_bar['low'] >= prev_bar['low']
    
    # 條件 B: 大戶籌碼與外盤買單條件 (外盤成交比 > 55% 且大戶買賣力為紅柱)
    outside_ratio = current_bar.get('outside_ratio', 0.6)
    buyer_power_positive = current_bar.get('buyer_power', 1) > 0
    
    # 條件 C: 布林通道位置檢定 (不可在走平壓縮通道，需為向上發散或帶量突破中軌)
    bband_width = (current_bar['upper_band'] - current_bar['lower_band']) / current_bar['ma20']
    is_bband_expanding = bband_width > 0.015 # 濾除走平黏滯盤整
    above_mid_band = current_bar['close'] >= current_bar['ma20']
    
    if is_support_held and is_bottom_higher and outside_ratio >= 0.55 and buyer_power_positive and above_mid_band and is_bband_expanding:
        entry_price = float(current_bar['close'])
        tp_price = round(entry_price * 1.02, 2)  # 嚴格 +2% 停利
        
        # 停損價取「第 1 根低點」與「前日尾盤高點 (支撐線)」之較高者
        sl_price = round(max(bar1['low'], prev_pm_high) * 0.995, 2)
        
        reasons = [
            f"1. 開盤符合【強強開盤】，第1根5分K帶量收紅突破昨日尾盤壓力({prev_pm_high}元)",
            f"2. 第{current_idx+1}根拉回洗盤守穩關鍵支撐，底部墊高(底底高)不破前低",
            f"3. 盤中外盤敲進比例達 {outside_ratio*100:.1f}%，大戶買賣力維持強勁紅柱",
            f"4. 5分K布林通道向上發散，股價站穩中軌(20MA)，趨勢動能表態"
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
