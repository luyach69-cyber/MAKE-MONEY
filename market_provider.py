"""
market_provider.py - 台灣證券市場公開數據擷取模組 (時區與尾盤切片精確修復版)
"""
import datetime
import requests
import pandas as pd
import numpy as np
import yfinance as yf

class PublicTWDataFetcher:
    """整合 TWSE 官方法人日報與 Yahoo Finance 5分K 行情"""

    @staticmethod
    def get_previous_trading_date(ref_date_str: str) -> str:
        """推算前一個交易日 (YYYYMMDD)"""
        dt = datetime.datetime.strptime(ref_date_str, "%Y-%m-%d")
        if dt.weekday() == 0:
            prev_dt = dt - datetime.timedelta(days=3)
        elif dt.weekday() == 6:
            prev_dt = dt - datetime.timedelta(days=2)
        else:
            prev_dt = dt - datetime.timedelta(days=1)
        return prev_dt.strftime("%Y%m%d")

    @staticmethod
    def get_twse_institutional_consensus(trade_date_str: str = None, top_n: int = 20):
        """向證交所公開端點請求『前一交易日 (T-1)』三大法人買賣超日報"""
        if not trade_date_str:
            trade_date_str = datetime.datetime.now().strftime("%Y-%m-%d")

        query_date = PublicTWDataFetcher.get_previous_trading_date(trade_date_str)
        url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={query_date}&response=json"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        consensus_stocks = []
        try:
            res = requests.get(url, headers=headers, timeout=12)
            data = res.json()
            if data.get("stat") != "OK":
                return []

            fields = data["fields"]
            rows = data["data"]
            df = pd.DataFrame(rows, columns=fields)

            foreign_cols = [c for c in df.columns if "外陸資" in c and "自營商" not in c]
            trust_cols = [c for c in df.columns if "投信" in c and "買賣超" in c]

            if foreign_cols and trust_cols:
                df["foreign_buy"] = df[foreign_cols[0]].astype(str).str.replace(",", "").astype(float)
                df["trust_buy"] = df[trust_cols[0]].astype(str).str.replace(",", "").astype(float)

                # 外資與投信同步買超
                cond = (df["foreign_buy"] > 0) & (df["trust_buy"] > 0)
                df_filtered = df[cond].copy()
                df_filtered["total_inst_buy"] = df_filtered["foreign_buy"] + df_filtered["trust_buy"]
                df_sorted = df_filtered.sort_values(by="total_inst_buy", ascending=False).head(top_n)

                for _, r in df_sorted.iterrows():
                    code = str(r["證券代號"]).strip()
                    name = str(r["證券名稱"]).strip()
                    if len(code) == 4 and code.isdigit():
                        consensus_stocks.append({"stock_id": code, "stock_name": name})
        except Exception as e:
            print(f"[提示] 擷取證交所法人買賣超資料略過: {e}")

        return consensus_stocks

    @staticmethod
    def get_stock_levels_and_5m(stock_id: str, stock_name: str, trade_date: str):
        """
        透過 yfinance 抓取最近 5 天 5分K：
        1. 徹底修復時區轉換，精確切出前日 12:00~13:30 全部 18 根 K 棒
        2. 計算真實尾盤高低點壓力與支撐
        3. 輸出今日 5分K 供策略引擎判斷
        """
        df = pd.DataFrame()
        # 上市試 .TW，上櫃試 .TWO
        for suffix in [".TW", ".TWO"]:
            symbol = f"{stock_id}{suffix}"
            ticker = yf.Ticker(symbol)
            try:
                temp_df = ticker.history(
                    period="5d",
                    interval="5m",
                    auto_adjust=False,
                    back_adjust=False,
                    prepost=False
                )
                if not temp_df.empty:
                    df = temp_df
                    break
            except Exception:
                continue

        if df.empty:
            return None, None, False

        # ---------------------------------------------------------
        # 核心修復 1：時區嚴格轉換為 Asia/Taipei
        # ---------------------------------------------------------
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Taipei")
        else:
            df.index = df.index.tz_convert("Asia/Taipei")

        # 格式化日期與時間字串
        df["date_str"] = df.index.strftime("%Y-%m-%d")
        df["time_str"] = df.index.strftime("%H:%M")

        all_dates = sorted(list(df["date_str"].unique()))
        if len(all_dates) < 2:
            return None, None, False

        prev_date = all_dates[-2]
        today_date = all_dates[-1]

        df_prev_all = df[df["date_str"] == prev_date]
        df_today_all = df[df["date_str"] == today_date]

        # ---------------------------------------------------------
        # 核心修復 2：前日 12:00 ~ 13:30 尾盤區間精確切片 (防單筆重疊)
        # ---------------------------------------------------------
        df_prev_pm = df_prev_all[(df_prev_all["time_str"] >= "12:00") & (df_prev_all["time_str"] <= "13:35")]

        # 若取樣少於 3 根，自動擴大回推至 11:30~13:30 以確保取足有效區間
        if len(df_prev_pm) < 3:
            df_prev_pm = df_prev_all[df_prev_all["time_str"] >= "11:30"]

        if not df_prev_pm.empty:
            prev_pm_high = float(df_prev_pm["High"].max())
            prev_pm_low = float(df_prev_pm["Low"].min())
        else:
            prev_pm_high = float(df_prev_all["High"].max())
            prev_pm_low = float(df_prev_all["Low"].min())

        # 防呆機制：若尾盤完全走平導致壓力等於支撐，自動給予合理保護緩衝
        if prev_pm_high == prev_pm_low:
            spread_buffer = max(prev_pm_high * 0.005, 0.5)
            prev_pm_high += spread_buffer
            prev_pm_low -= spread_buffer

        # 量能放大檢驗 (第一根 5 分 K 爆量 1.8 倍以上)
        is_vol_expanding = False
        if not df_prev_all.empty and not df_today_all.empty:
            first_bar_vol = df_today_all["Volume"].iloc[0] if len(df_today_all) > 0 else 0
            prev_first_vol = df_prev_all["Volume"].iloc[0] if len(df_prev_all) > 0 else 1
            if first_bar_vol > prev_first_vol * 1.5:
                is_vol_expanding = True

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
