import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
import os
import re
from typing import Dict, Optional
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# 1. 啟用日誌記錄
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================= 機器人 Token 設定 =================
# 自動從環境變數讀取，若本地測試未設環境變數，也可將引號內改為實際 Token
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# ====================================================


@dataclass
class BreakoutSignal:
    symbol: str
    price: float
    high_15m: float
    volume: int
    rvol: float
    vwap: float
    sl: float
    tp: float
    time_str: str


class BreakoutEvaluator:

    def __init__(
        self,
        min_rvol: float = 1.8,
        max_rvol: float = 4.8,
        max_vwap_bias: float = 3.0,
    ):
        self.min_rvol = min_rvol
        self.max_rvol = max_rvol
        self.max_vwap_bias = max_vwap_bias

    def parse_signal_text(self, text: str) -> Optional[BreakoutSignal]:
        """精準解析當沖推播訊息"""
        try:
            symbol_m = re.search(r"([0-9]{4}(?:\.TW)?)", text)
            price_m = re.search(r"現價[：:]\s*([0-9.]+)", text)
            high_15m_m = re.search(r"首15分高[：:]\s*([0-9.]+)", text)
            vol_m = re.search(r"量能[：:]\s*([0-9]+)", text)
            rvol_m = re.search(r"相對量能\s*([0-9.]+)x", text)
            vwap_m = re.search(r"VWAP[：:]\s*([0-9.]+)", text)
            sl_m = re.search(r"停損[：:]\s*([0-9.]+)", text)
            tp_m = re.search(r"停利[：:]\s*([0-9.]+)", text)
            time_m = re.search(r"時間[：:]\s*([0-9]{2}:[0-9]{2}:[0-9]{2})", text)

            if not all(
                [
                    symbol_m,
                    price_m,
                    high_15m_m,
                    vol_m,
                    rvol_m,
                    vwap_m,
                    sl_m,
                    tp_m,
                    time_m,
                ]
            ):
                return None

            return BreakoutSignal(
                symbol=symbol_m.group(1),
                price=float(price_m.group(1)),
                high_15m=float(high_15m_m.group(1)),
                volume=int(vol_m.group(1)),
                rvol=float(rvol_m.group(1)),
                vwap=float(vwap_m.group(1)),
                sl=float(sl_m.group(1)),
                tp=float(tp_m.group(1)),
                time_str=time_m.group(1),
            )
        except Exception as e:
            logger.error(f"解析訊號字串時發生錯誤: {e}")
            return None

    def evaluate(self, sig: BreakoutSignal) -> Dict:
        """多維度量化過濾與動態風控計算"""
        score = 100
        alerts = []

        # 1. VWAP 乖離率檢驗
        vwap_bias = (sig.price - sig.vwap) / sig.vwap * 100
        if sig.price < sig.vwap:
            score -= 60
            alerts.append("❌ 現價跌破 VWAP 成本線，多方動能喪失")
        elif vwap_bias > 3.5:
            score -= 30
            alerts.append(f"⚠️ 現價距 VWAP +{vwap_bias:.2f}%，乖離過大防拉回急殺")
        elif vwap_bias > 2.5:
            score -= 10
            alerts.append(f"ℹ️ 乖離偏大 (+{vwap_bias:.2f}%)，避免市價單追價")

        # 2. 相對量能 (RVOL) 甜點區過濾
        if sig.rvol < self.min_rvol:
            score -= 40
            alerts.append(f"❌ RVOL {sig.rvol}x 動能偏弱，高度疑為假突破")
        elif sig.rvol > 7.0:
            score -= 35
            alerts.append(f"⚠️ RVOL {sig.rvol}x 爆量過熱，嚴防力竭或主力倒貨")
        elif sig.rvol > self.max_rvol:
            score -= 15
            alerts.append(f"ℹ️ RVOL {sig.rvol}x 略顯過熱，建議等待回測確認")

        # 3. 時間窗口效益 (過濾午盤量縮期)
        t = datetime.strptime(sig.time_str, "%H:%M:%S")
        mins = (t.hour - 9) * 60 + t.minute

        if mins < 15:
            score -= 25
            alerts.append("⚠️ 09:15 前開盤首 15 分 K 線尚未走完確認")
        elif 15 <= mins <= 60:
            pass  # 09:15 ~ 10:00 黃金推升時段
        elif 60 < mins <= 150:
            score -= 15
            alerts.append(
                f"⚠️ {sig.time_str} 進入午盤量縮震盪區，動能易受阻中斷"
            )
        else:
            score -= 40
            alerts.append("❌ 11:30 後尾盤當沖結算潮，不建議開新倉")

        # 4. 突破幅度檢驗
        breakout_pct = (sig.price - sig.high_15m) / sig.high_15m * 100
        if breakout_pct < 0:
            score -= 50
            alerts.append("❌ 現價尚未有效突破首 15 分高點")
        elif breakout_pct > 2.5:
            score -= 20
            alerts.append(f"⚠️ 已脫離突破點 +{breakout_pct:.2f}%，追價風報比不佳")

        # 5. 精準防守點計算 (採首15分高點下 2 個 tick 防守)
        tight_sl = round(sig.high_15m * 0.998, 1)
        tight_risk = sig.price - tight_sl
        reward = sig.tp - sig.price
        optimized_rr = (reward / tight_risk) if tight_risk > 0 else 0

        # 原訊號風報比
        orig_risk = sig.price - sig.sl
        orig_rr = (reward / orig_risk) if orig_risk > 0 else 0

        # 結論判定
        score = max(0, min(100, score))
        if score >= 75:
            verdict = "🟢 允許進場 (APPROVED)"
            entry_strategy = "可於現價或回踩 1 tick 內順勢切入。"
        elif score >= 55:
            verdict = "🟡 僅限回測掛單 (PULLBACK ONLY)"
            entry_strategy = f"<b>嚴禁市價追單！</b>僅能於突破點 <b>{sig.high_15m}</b> 附近掛買單承接。"
        else:
            verdict = "🔴 放棄操作 (REJECTED)"
            entry_strategy = "風險過高或高機率為假突破，建議取消觀察。"

        return {
            "symbol": sig.symbol,
            "score": score,
            "verdict": verdict,
            "entry_strategy": entry_strategy,
            "tight_sl": tight_sl,
            "orig_sl": sig.sl,
            "vwap_bias": round(vwap_bias, 2),
            "orig_rr": round(orig_rr, 2),
            "optimized_rr": round(optimized_rr, 2),
            "alerts": alerts,
        }


evaluator = BreakoutEvaluator()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理接收到的所有文字訊息"""
    if not update.message or not update.message.text:
        return

    text = update.message.text

    # 快速關鍵字快篩
    if "當沖進場訊號" not in text and "放量突破" not in text:
        return

    sig = evaluator.parse_signal_text(text)
    if not sig:
        return

    res = evaluator.evaluate(sig)

    alerts_text = (
        "\n".join([f"  • {a}" for a in res["alerts"]])
        if res["alerts"]
        else "  • 各項量化指標表現健康，無異常風險。"
    )

    reply_message = (
        f"🤖 <b>【當沖進場自動檢核報告】</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"標的代號：<code>{res['symbol']}</code>\n"
        f"勝率評分：<b>{res['score']} / 100</b>\n"
        f"決策判定：<b>{res['verdict']}</b>\n"
        f"──────────────────\n"
        f"💡 <b>操作指引：</b>\n"
        f"{res['entry_strategy']}\n\n"
        f"🛡️ <b>防守調整建議：</b>\n"
        f"• 原訊號停損：<s>{res['orig_sl']} 元</s> (風報比 {res['orig_rr']}R - 偏深)\n"
        f"• <b>優化防守點：{res['tight_sl']} 元</b> (頸線防守，風報比提升至 <b>{res['optimized_rr']}R</b>)\n"
        f"• VWAP 乖離率：<b>+{res['vwap_bias']}%</b>\n\n"
        f"⚠️ <b>風險示警濾網：</b>\n"
        f"{alerts_text}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    await update.message.reply_text(
        text=reply_message,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=update.message.message_id,
    )


def main():
    if not BOT_TOKEN:
        print("錯誤：未偵測到 TELEGRAM_BOT_TOKEN，請確認環境變數已設定。")
        return

    print("啟動 Telegram 當沖過濾守門員機器人中...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
