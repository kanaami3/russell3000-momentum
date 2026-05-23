# 米国・日本株 モメンタムランキング

米国ラッセル3000(時価総額上位3000銘柄)と東証プライム全銘柄(約1,600銘柄)の日次モメンタムランキングサイト。

## 構成

- **バッチ (Python)**: `batch/` — 全スクリプトは `us` / `jp` 引数で市場切替
  - `fetch_universe.py` — NASDAQ Screener API から上位3000銘柄を取得 → `data/universe_us.json`
  - `fetch_universe_jp.py` — JPX 東証上場銘柄一覧 Excel から Prime 1,600銘柄 → `data/universe_jp.json`
  - `fetch_prices.py {us|jp}` — yfinance で過去14ヶ月日次終値を取得 → `data/prices_{market}.csv`
  - `calc_momentum.py {us|jp}` — 1週/1ヶ月/3ヶ月リターン・12-1モメンタムを計算 → `web/data/momentum_{market}.json`
  - `generate_summary.py {us|jp}` — Claude API で日本語市場サマリー生成 → 同 JSON に追記
- **フロント (静的HTML)**: `web/`
  - `index.html` — Tailwind CDN + Alpine.js、US/JP タブ切替UI
  - `data/momentum_us.json` / `data/momentum_jp.json` — バッチが日次更新
- **自動化**: GitHub Actions
  - `.github/workflows/daily_us.yml` — 平日 22:30 UTC (7:30 JST) US更新
  - `.github/workflows/daily_jp.yml` — 平日 08:00 UTC (17:00 JST) JP更新
- **ホスティング**: Vercel(`vercel.json` で `web/` を配信)

## ローカル実行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# US
python batch/fetch_universe.py
python batch/fetch_prices.py us       # ~2分
python batch/calc_momentum.py us
ANTHROPIC_API_KEY=sk-... python batch/generate_summary.py us

# JP
python batch/fetch_universe_jp.py
python batch/fetch_prices.py jp       # ~1.5分
python batch/calc_momentum.py jp
ANTHROPIC_API_KEY=sk-... python batch/generate_summary.py jp

# ローカルプレビュー
cd web && python3 -m http.server 8000
# http://localhost:8000 を開く(タブで US/JP 切替)
```

## デプロイ

1. GitHubリポジトリに push
2. Vercel で Import(`vercel.json` で設定済み)
3. GitHub Secrets に `ANTHROPIC_API_KEY` を設定(市場サマリー有効化、任意)
4. 以後、平日朝7:30(US)/夕方17:00(JP)に自動更新

## URL ルーティング

- `https://your-domain.vercel.app/`     → デフォルト US 表示
- `https://your-domain.vercel.app/#us`  → US 強制
- `https://your-domain.vercel.app/#jp`  → JP 強制

選択した市場は localStorage に保存され、次回訪問時も維持されます。

## 注意

- 投資助言ではありません。教育・参考目的です。
- yfinance は非公式 API のため、データ取得失敗時はリトライまたは別ソースに切替が必要。
- JPX Excel は月次更新なので、月初に新規上場銘柄が反映されない可能性あり(翌バッチで補正)。
