# ラッセル3000 モメンタムランキング

米国上場時価総額上位3000銘柄(ラッセル3000相当)の日次モメンタムランキングサイト。

## 構成

- **バッチ (Python)**: `batch/`
  - `fetch_universe.py` — NASDAQ Screener API から上位3000銘柄を取得 → `data/universe.json`
  - `fetch_prices.py` — yfinance で各銘柄の過去14ヶ月日次終値を取得 → `data/prices.csv`
  - `calc_momentum.py` — 1週/1ヶ月/3ヶ月リターン・12-1モメンタムを計算 → `web/data/momentum.json`
  - `generate_summary.py` — Claude API で日本語市場サマリーを生成 → `web/data/momentum.json` に追記
- **フロント (静的HTML)**: `web/`
  - `index.html` — Tailwind CDN + Alpine.js でランキング表示
  - `data/momentum.json` — バッチが日次更新
- **自動化**: `.github/workflows/daily.yml` で日次 cron 実行 → GitHub にデータ commit → Vercel が自動デプロイ
- **ホスティング**: Vercel(`vercel.json` で `web/` を配信)

## ローカル実行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python batch/fetch_universe.py
python batch/fetch_prices.py        # ~30分
python batch/calc_momentum.py
ANTHROPIC_API_KEY=sk-... python batch/generate_summary.py  # 任意

# ローカルプレビュー
cd web && python3 -m http.server 8000
# http://localhost:8000 を開く
```

## デプロイ

1. GitHubリポジトリに push
2. Vercelで Import → ルートディレクトリは `web` (vercel.json で自動設定済み)
3. GitHub の Settings > Secrets で `ANTHROPIC_API_KEY` を設定(市場サマリー有効化)
4. 以後、平日 22:30 UTC (7:30 JST) に自動更新

## 注意

- 投資助言ではありません。教育・参考目的です。
- yfinance は非公式 API なので、データ取得失敗時はリトライまたは別ソースに切り替え。
