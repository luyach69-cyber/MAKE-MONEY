name: run-monitor

on:
  workflow_dispatch:
  schedule:
    # 1. 盤中主動監控：台灣時間 09:05 ~ 10:25 每 10 分鐘掃描一次
    - cron: '5,15,25,35,45,55 1 * * 1-5'
    - cron: '5,15,25 2 * * 1-5'

    # 2. 盤後自動復盤：台灣時間 13:35 (UTC 05:35)
    - cron: '35 5 * * 1-5'

    # 3. 夜間先行推播：台灣時間週日至週四 20:00 (UTC 12:00) 產出明日清單
    - cron: '0 12 * * 0-4'

jobs:
  run-monitor:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install Dependencies
        run: |
          python -m pip install --upgrade pip
          pip install pandas numpy requests yfinance

      - name: Run Stock Breakout Engine
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          # 判斷如果是 UTC 12:00 (台灣時間 20:00)，就跑夜間清單推播
          CURRENT_HOUR=$(date -u +%H)
          if [ "$CURRENT_HOUR" -eq 12 ]; then
            python main.py --night
          else
            python main.py
          fi

      - name: Commit and Push Data
        run: |
          git config --global user.name "github-actions[bot]"
          git config --global user.email "github-actions[bot]@users.noreply.github.com"
          git add trading_records.db || true
          git commit -m "Auto update trading records database [skip ci]" || true
          git push || true
