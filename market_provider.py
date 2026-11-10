"""
market_provider.py - 台灣證券市場公開數據擷取模組
"""
import datetime
import requests
import pandas as pd
import numpy as np
import yfinance as yf

class PublicTWDataFetcher:
    """整合 TWSE 官方 OpenAPI 與 Yahoo Finance 5分K"""

    @staticmethod
    def get_twse_institutional_consensus(top_n: int = 20):
        """抓取證交所三大法人買賣超日報，篩選外資與投信『同步買超』標的"""
        url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json"
        headers = {"User-Agent": "Mozilla/5.0"}
        consensus_stocks = []
        try:
            res = requests.get(url, headers=headers, timeout=10)
            data = res.json()
            if data.get("stat") != "OK":
                return []
            
            df = pd.DataFrame(data["data"], columns=data["fields"])
            foreign_cols = [c for c in df.columns if "外陸資" in c and "自營商" not in c]
            trust_cols = [c for c in df.columns if "投信" in c and "買賣超" in c]

            if foreign_cols and trust_cols:
                df["foreign_buy"] = df[foreign_cols[0]].astype(str).str.replace(",", "").astype(float)
                df["trust_buy"] = df[trust_cols[0]].astype(str).str.replace(",", "").astype(float)
                
                cond = (df["foreign_buy"] > 0) & (df["trust_buy"] > 0)
                df_filtered = df[cond].copy()
                df_filtered["total_buy"] = df_filtered["foreign_buy"] + df_filtered["trust_buy"]
                df_sorted = df_filtered.sort_values(by="total_buy", ascending=False).head(top_n)

                for _, r in df_sorted.iterrows():
                    code = str(r["證券代號"]).strip()
                    name = str(r["證券名稱"]).strip()
                    if len(code) == 4 and code.isdigit():
                        consensus_stocks.append({"stock_id": code, "stock_name": name})
        except Exception as e:
            print(f"[提示] 擷取證交所法人買賣超略過: {e}")
        return consensus_stocks

    @staticmethod
    def get_stock_levels_and_5m(stock_id: str, stock_name: str, trade_date: str):
        """抓取 5 天 5分K，計算昨日 12:00~13:30 尾盤高低撐壓並整理今日數據"""
        df = pd.DataFrame()
        for suffix in [".TW", ".TWO"]:
            ticker = yf.Ticker(f"{stock_id}{suffix}")
            try:
                temp_df = ticker.history(period="5d", interval="5m")
                if not temp_df.empty:
                    df = temp_df
                    break
            except Exception:
                continue

        if df.empty:
            return None, None, False

        # 確保 DatetimeIndex 格式正確
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        try:
            if df.index.tz is not None:
                df.index = df.index.tz_convert("Asia/Taipei")
        except Exception:
            pass

        df["date_str"] = df.index.strftime("%Y-%m-%d")
        df["time_str"] = df.index.strftime("%H:%M")
        all_dates = sorted(list(df["date_str"].unique()))
        if len(all_dates) < 2:
            return None, None, False

        prev_date = all_dates[-2]
        today_date = all_dates[-1]

        df_prev_all = df[df["date_str"] == prev_date]
        df_today_all = df[df["date_str"] == today_date]

        # 量能放大檢驗
        is_vol_expanding = False
        if not df_prev_all.empty and not df_today_all.empty:
            first_bar_vol = df_today_all["Volume"].iloc[0] if len(df_today_all) > 0 else 0
            prev_avg_vol = df_prev_all["Volume"].mean()
            if first_bar_vol > prev_avg_vol * 1.8:
                is_vol_expanding = True

        # 計算昨日 12:00 ~ 13:30 尾盤撐壓
        df_prev_pm = df[(df["date_str"] == prev_date) & (df["time_str"] >= "12:00") & (df["time_str"] <= "13:30")]
        if df_prev_pm.empty:
            prev_pm_high = float(df_prev_all["High"].max())
            prev_pm_low = float(df_prev_all["Low"].min())
        else:
            prev_pm_high = float(df_prev_pm["High"].max())
            prev_pm_low = float(df_prev_pm["Low"].min())

        stock_info = {
            "trade_date": trade_date,
            "stock_id": stock_id,
            "stock_name": stock_name,
            "prev_pm_high": round(prev_pm_high, 2),
            "prev_pm_low": round(prev_pm_low, 2),
            "day_open": float(df_today_all["Open"].iloc[0]) if not df_today_all.empty else prev_pm_high
        }

        df_today = df_today_all.rename(columns={
            "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
        }).copy()
        df_today["time"] = df_today["time_str"]

        spread = (df_today["high"] - df_today["low"]).replace(0, 0.01)
        df_today["outside_ratio"] = (0.5 + ((df_today["close"] - df_today["open"]) / spread) * 0.25).clip(0.3, 0.85)
        df_today["buyer_power"] = np.where(df_today["close"] >= df_today["open"], 100, -50)

        return stock_info, df_today, is_vol_expanding
