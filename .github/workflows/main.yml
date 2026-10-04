name: Taiwan Stock Breakout Monitor

on:
  schedule:
    # 台灣時區 (UTC+8) 週一至週五 11:30、13:45 執行
    # 對應 UTC 時間為 03:30, 05:45
    - cron: '30 3 * * 1-5'
    - cron: '45 5 * * 1-5'
  workflow_dispatch: # 支援在 GitHub 網頁手動點擊測試

jobs:
  run-monitor:
    runs-on: ubuntu-latest
    permissions:
      contents: write # 賦予推回資料庫的權限

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'
          cache: 'pip'

      - name: Install Dependencies
        run: |
          pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run Stock Breakout Engine
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          python main.py

      - name: Commit and Push Data
        run: |
          git config --local user.email "action@github.com"
          git config --local user.name "GitHub Action Bot"
          git add data/history/*.csv || true
          if git diff --staged --quiet; then
            echo "No data changes to commit."
          else
            git commit -m "Auto-update market signal data [skip ci]"
            git push
          fi
