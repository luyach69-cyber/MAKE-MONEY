"""
market_provider.py - 台灣證券市場公開數據擷取模組
過濾規則：
1. 僅排除 ETF (代碼為 00 開頭)。
2. 保留所有 4 碼純數字之普通個股 (包含電子、金融、傳產權值股等)。
3. 精確解析證交所 T-1 外陸資與投信同步買超資料。
4. Asia/Taipei 時區切片前日 12:00~13:30 尾盤高低撐壓。
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
        """推算前一個交易日 (YYYYMMDD 格式供證交所查詢)"""
        dt = datetime.datetime.strptime(ref_date_str, "%Y-%m-%d")
        # 0: 週一 -> 回推至上週五 (3天)；6: 週日 -> 回推至上週五 (2天)；其餘回推 1 天
        if dt.weekday() == 0:
            prev_dt = dt - datetime.timedelta(days=3)
        elif dt.weekday() == 6:
            prev_dt = dt - datetime.timedelta(days=2)
        else:
            prev_dt = dt - datetime.timedelta(days=1)
        return prev_dt.strftime("%Y%m%d")

    @staticmethod
    def get_twse_institutional_consensus(trade_date_str: str = None, top_n: int = 20):
        """
        向證交所公開端點請求『前一交易日 (T-1)』三大法人買賣超日報
        僅排除 00 開頭的 ETF，其餘外資與投信同步大買之普通股全數保留
        """
        if not trade_date_str:
            trade_date_str = datetime.datetime.now().strftime("%Y-%m-%d")

        query_date = PublicTWDataFetcher.get_previous_trading_date(trade_date_str)
        url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={query_date}&selectType=ALL&response=json"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.twse.com.tw/zh/trading/foreign/t86.html"
        }
        consensus_stocks = []
        try:
            print(f"正在向證交所請求前一交易日 ({query_date}) 法人買超日報...")
            res = requests.get(url, headers=headers, timeout=15)
            data = res.json()
            
            if data.get("stat") != "OK":
                print(f"[提示] 證交所回傳非 OK ({data.get('stat')})，使用核心自選名單備援。")
                return []

            fields = data["fields"]
            rows = data["data"]
            df = pd.DataFrame(rows, columns=fields)

            # 定位外資與投信買賣超欄位
            foreign_col = None
            trust_col = None
            for c in df.columns:
                if "外陸資" in c and "買賣超" in c:
                    foreign_col = c
                elif "投信" in c and "買賣超" in c:
                    trust_col = c

            if foreign_col and trust_col:
                df["foreign_buy"] = df[foreign_col].astype(str).str.replace(",", "").astype(float)
                df["trust_buy"] = df[trust_col].astype(str).str.replace(",", "").astype(float)

                # 外資與投信同步買超
                cond = (df["foreign_buy"] > 0) & (df["trust_buy"] > 0)
                df_filtered = df[cond].copy()
                df_filtered["total_inst_buy"] = df_filtered["foreign_buy"] + df_filtered["trust_buy"]
                df_sorted = df_filtered.sort_values(by="total_inst_buy", ascending=False).head(50)

                for _, r in df_sorted.iterrows():
                    code = str(r["證券代號"]).strip()
                    name = str(r["證券名稱"]).strip()

                    # 1. 必須是 4 碼純數字
                    if not (len(code) == 4 and code.isdigit()):
                        continue

                    # 2. 僅排除 00 開頭的 ETF (如 0050, 0052, 0056 等)
                    if code.startswith("00"):
                        continue

                    # 其餘個股 (包含 1303 南亞, 2886 兆豐金, 1802 台玻, 2317 鴻海等) 全數保留
                    consensus_stocks.append({"stock_id": code, "stock_name": name})

                    if len(consensus_stocks) >= top_n:
                        break

                print(f"✓ 成功抓取前一交易日 ({query_date}) 外資投信合買共識股共 {len(consensus_stocks)} 檔！")
            else:
                print(f"[警告] 找不到對應的外資或投信欄位，現有欄位清單為：{fields}")

        except Exception as e:
            print(f"[提示] 擷取證交所法人買賣超資料略過: {e}")

        return consensus_stocks

    @staticmethod
    def get_stock_levels_and_5m(stock_id: str, stock_name: str, trade_date: str):
        """
        透過 yfinance 抓取最近 5 天 5分K：
        1. 關閉除權息調整，取得真實交易價格
        2. 嚴格對齊 Asia/Taipei 時區
        3. 切出前日 12:00~13:30 尾盤高低撐壓
        """
        df = pd.DataFrame()
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

        # 強制轉為 DatetimeIndex 並對齊台灣時區
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Taipei")
        else:
            df.index = df.index.tz_convert("Asia/Taipei")

        df["date_str"] = df.index.strftime("%Y-%m-%d")
        df["time_str"] = df.index.strftime("%H:%M")

        all_dates = sorted(list(df["date_str"].unique()))
        if len(all_dates) < 2:
            return None, None, False

        prev_date = all_dates[-2]
        today_date = all_dates[-1]

        df_prev_all = df[df["date_str"] == prev_date]
        df_today_all = df[df["date_str"] == today_date]

        # 前日 12:00 ~ 13:30 尾盤區間切片
        df_prev_pm = df_prev_all[(df_prev_all["time_str"] >= "12:00") & (df_prev_all["time_str"] <= "13:35")]

        if len(df_prev_pm) < 3:
            df_prev_pm = df_prev_all[df_prev_all["time_str"] >= "11:30"]

        if not df_prev_pm.empty:
            prev_pm_high = float(df_prev_pm["High"].max())
            prev_pm_low = float(df_prev_pm["Low"].min())
        else:
            prev_pm_high = float(df_prev_all["High"].max())
            prev_pm_low = float(df_prev_all["Low"].min())

        # 走平防呆價差
        if prev_pm_high == prev_pm_low:
            spread_buffer = max(prev_pm_high * 0.005, 0.5)
            prev_pm_high += spread_buffer
            prev_pm_low -= spread_buffer

        # 量能放大檢驗
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
