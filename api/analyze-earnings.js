// On-demand earnings analyzer — given a ticker, fetch Yahoo Finance quote
// summary and ask Claude to produce a Fact/Guidance/Speculation analysis
// per the earnings-analyzer skill convention.
//
// POST /api/analyze-earnings  { "ticker": "AAPL" }  or  { "ticker": "7203.T" }

export const config = { runtime: 'edge' };

const CLAUDE_MODEL = 'claude-haiku-4-5-20251001';
const CLAUDE_MAX_TOKENS = 1400;

// ---------------------------------------------------------------------------
// Auth check — verify the same session cookie our middleware sets.
// We can't rely on middleware here because /api/* is excluded from it
// (so /api/login can run unauthenticated). This endpoint must be gated.
// ---------------------------------------------------------------------------

async function hmacB64(message, secret) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const sigBuf = await crypto.subtle.sign('HMAC', key, enc.encode(message));
  const bytes = new Uint8Array(sigBuf);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64UrlDecode(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  return atob(s);
}

async function verifyAuth(request) {
  const cookieHeader = request.headers.get('cookie') || '';
  const m = cookieHeader.match(/(?:^|;\s*)auth-session=([^;]+)/);
  if (!m) return false;
  const token = m[1];
  const [payload, sig] = token.split('.');
  if (!payload || !sig) return false;
  const expected = await hmacB64(payload, process.env.AUTH_SECRET || '');
  if (expected !== sig) return false;
  try {
    const data = JSON.parse(b64UrlDecode(payload));
    return data.exp && Date.now() < data.exp;
  } catch (e) {
    return false;
  }
}

async function fetchYahooSummary(symbol) {
  const modules = [
    'price',
    'summaryDetail',
    'defaultKeyStatistics',
    'financialData',
    'earnings',
    'earningsHistory',
    'earningsTrend',
    'assetProfile',
  ].join(',');
  const url =
    `https://query1.finance.yahoo.com/v10/finance/quoteSummary/${encodeURIComponent(symbol)}` +
    `?modules=${modules}&corsDomain=finance.yahoo.com`;
  const resp = await fetch(url, {
    headers: {
      'User-Agent':
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36',
      Accept: 'application/json',
    },
  });
  if (!resp.ok) {
    throw new Error(`Yahoo Finance HTTP ${resp.status}`);
  }
  const json = await resp.json();
  const result = json?.quoteSummary?.result?.[0];
  if (!result) throw new Error('Ticker not found or no data');
  return result;
}

function pick(obj, path, fallback = null) {
  const parts = path.split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur == null) return fallback;
    cur = cur[p];
  }
  return cur ?? fallback;
}

function fmtNum(v, digits = 2) {
  if (v == null || Number.isNaN(v)) return '-';
  return Number(v).toLocaleString('ja-JP', { maximumFractionDigits: digits });
}

function fmtPct(v, digits = 2) {
  if (v == null || Number.isNaN(v)) return '-';
  return `${Number(v).toFixed(digits)}%`;
}

function fmtLargeYen(v) {
  if (v == null) return '-';
  if (Math.abs(v) >= 1e12) return `${(v / 1e12).toFixed(2)}兆`;
  if (Math.abs(v) >= 1e8) return `${(v / 1e8).toFixed(1)}億`;
  if (Math.abs(v) >= 1e4) return `${(v / 1e4).toFixed(0)}万`;
  return v.toString();
}

function buildPrompt(symbol, data) {
  const longName = pick(data, 'price.longName') || pick(data, 'price.shortName') || symbol;
  const currency = pick(data, 'price.currencySymbol') || '$';
  const sector = pick(data, 'assetProfile.sector') || '-';
  const industry = pick(data, 'assetProfile.industry') || '-';
  const businessSummary = (pick(data, 'assetProfile.longBusinessSummary') || '').slice(0, 500);

  // Earnings quarterly chart
  const earningsChart = pick(data, 'earnings.earningsChart.quarterly') || [];
  const quartersEps = earningsChart
    .map((q) => `${q.date}: 実績 ${pick(q, 'actual.raw') ?? '-'} / 予想 ${pick(q, 'estimate.raw') ?? '-'}`)
    .join(' | ');

  // Most recent quarter
  const lastQ = earningsChart[earningsChart.length - 1] || {};
  const epsActual = pick(lastQ, 'actual.raw');
  const epsEstimate = pick(lastQ, 'estimate.raw');
  let surprisePct = null;
  if (epsActual != null && epsEstimate != null && epsEstimate !== 0) {
    surprisePct = ((epsActual - epsEstimate) / Math.abs(epsEstimate)) * 100;
  }

  // Financials quarterly chart (revenue)
  const finChart = pick(data, 'earnings.financialsChart.quarterly') || [];
  const quartersRev = finChart
    .map((q) => `${q.date}: 売上 ${fmtLargeYen(pick(q, 'revenue.raw'))} / 利益 ${fmtLargeYen(pick(q, 'earnings.raw'))}`)
    .join(' | ');

  // YoY: latest quarter vs same-name quarter previous year (Q1 vs Q1 etc.)
  // Yahoo's chart gives last 4Q labeled by quarter. Compute YoY differently from
  // the Python path: use yearly chart if available.
  const yearlyFin = pick(data, 'earnings.financialsChart.yearly') || [];
  const yearlyStr = yearlyFin
    .map((y) => `${y.date}: 売上 ${fmtLargeYen(pick(y, 'revenue.raw'))} / 利益 ${fmtLargeYen(pick(y, 'earnings.raw'))}`)
    .join(' | ');

  // Forward / key stats
  const forwardEps = pick(data, 'defaultKeyStatistics.forwardEps.raw');
  const forwardPe = pick(data, 'defaultKeyStatistics.forwardPE.raw');
  const trailingPe = pick(data, 'summaryDetail.trailingPE.raw');
  const dividendYield = pick(data, 'summaryDetail.dividendYield.raw');
  const revenueGrowth = pick(data, 'financialData.revenueGrowth.raw');
  const earningsGrowth = pick(data, 'financialData.earningsGrowth.raw');
  const recoMean = pick(data, 'financialData.recommendationMean.raw');
  const recoKey = pick(data, 'financialData.recommendationKey');
  const targetMean = pick(data, 'financialData.targetMeanPrice.raw');
  const currentPrice = pick(data, 'financialData.currentPrice.raw') ?? pick(data, 'price.regularMarketPrice.raw');

  // Earnings trend (analyst growth estimates)
  const trendList = pick(data, 'earningsTrend.trend') || [];
  const fyTrend = trendList.find((t) => t.period === '+1y') || {};
  const fyEpsEstimate = pick(fyTrend, 'earningsEstimate.avg.raw');
  const fyEpsGrowth = pick(fyTrend, 'growth.raw');

  return `あなたは経験豊富な決算アナリストです。下記の決算データを分析し、**earnings-analyzer スキルの規約に厳密に従って** 日本語で出力してください。

【スキル規約 — 厳守】

出力は3セクション構造:

### 事実 (Fact)
- 検証可能な数値のみ
- 必ず出典(yfinance / Yahoo Finance)を明記
- 主観的解釈は一切含めない

### ガイダンス (Guidance)
- 経営陣・アナリストコンセンサスが示した会社見通し
- 数値があれば必ず記載
- データに無い場合は「明示的ガイダンスなし(取得元: Yahoo Finance)」と書く

### 推測 (Speculation)
- 分析者(あなた)の解釈
- **必ず冒頭に「以下は推測です」と明記**
- 「〜と考えられる」「〜の可能性がある」などの推測表現を用いる

【禁止事項】
- 未確認の数値を事実として記載しない
- 「買い」「売り」の投資判断を断定しない
- データ源を曖昧にしない

【出力ボリューム】
- 全体で 300〜500字程度
- 各セクション短く要点のみ

---銘柄---
${longName} (${symbol})
セクター: ${sector} / 業種: ${industry}
現在値: ${currency}${fmtNum(currentPrice)}

【EPS — 直近4四半期(Yahoo Finance earnings)】
${quartersEps || '(データなし)'}
直近Q: 実績 ${fmtNum(epsActual)} vs 予想 ${fmtNum(epsEstimate)} (サプライズ ${surprisePct != null ? fmtPct(surprisePct) : '-'})

【四半期業績(Yahoo Finance financialsChart)】
${quartersRev || '(データなし)'}

【年次業績(Yahoo Finance financialsChart.yearly)】
${yearlyStr || '(データなし)'}

【ガイダンス・コンセンサス(Yahoo Finance defaultKeyStatistics / financialData / earningsTrend)】
予想EPS(forward): ${fmtNum(forwardEps)}
予想PER(forward): ${fmtNum(forwardPe)}
実績PER(trailing): ${fmtNum(trailingPe)}
売上成長率(直近、会社/アナリスト): ${revenueGrowth != null ? fmtPct(revenueGrowth * 100) : '-'}
利益成長率(直近): ${earningsGrowth != null ? fmtPct(earningsGrowth * 100) : '-'}
来期EPS予想(+1y, アナリスト平均): ${fmtNum(fyEpsEstimate)}  (成長率 ${fyEpsGrowth != null ? fmtPct(fyEpsGrowth * 100) : '-'})
配当利回り: ${dividendYield != null ? fmtPct(dividendYield * 100) : '-'}
アナリスト推奨平均: ${fmtNum(recoMean, 1)} (${recoKey || '-'})
アナリスト目標株価平均: ${currency}${fmtNum(targetMean)}

【事業概要(参考、最大500字)】
${businessSummary || '(なし)'}
`;
}

async function callClaude(apiKey, prompt) {
  const resp = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: CLAUDE_MODEL,
      max_tokens: CLAUDE_MAX_TOKENS,
      messages: [{ role: 'user', content: prompt }],
    }),
  });
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(`Claude API HTTP ${resp.status}: ${txt.slice(0, 200)}`);
  }
  const json = await resp.json();
  return json?.content?.[0]?.text || '';
}

function extractFactsForCard(data, symbol) {
  // Minimal numeric facts the frontend card displays alongside the AI text
  const lastQ = (pick(data, 'earnings.earningsChart.quarterly') || []).slice(-1)[0] || {};
  return {
    longName: pick(data, 'price.longName') || pick(data, 'price.shortName') || symbol,
    sector: pick(data, 'assetProfile.sector') || null,
    industry: pick(data, 'assetProfile.industry') || null,
    current_price: pick(data, 'financialData.currentPrice.raw') ?? pick(data, 'price.regularMarketPrice.raw'),
    currency_symbol: pick(data, 'price.currencySymbol') || '$',
    eps_actual: pick(lastQ, 'actual.raw'),
    eps_estimate: pick(lastQ, 'estimate.raw'),
    eps_last_date: lastQ.date || null,
    forward_pe: pick(data, 'defaultKeyStatistics.forwardPE.raw'),
    trailing_pe: pick(data, 'summaryDetail.trailingPE.raw'),
    target_mean: pick(data, 'financialData.targetMeanPrice.raw'),
    recommendation: pick(data, 'financialData.recommendationKey'),
    revenue_growth_pct: pick(data, 'financialData.revenueGrowth.raw') != null
      ? pick(data, 'financialData.revenueGrowth.raw') * 100
      : null,
    earnings_growth_pct: pick(data, 'financialData.earningsGrowth.raw') != null
      ? pick(data, 'financialData.earningsGrowth.raw') * 100
      : null,
  };
}

export default async function handler(request) {
  if (request.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Method not allowed' }), {
      status: 405,
      headers: { 'content-type': 'application/json' },
    });
  }

  // Require valid auth-session cookie (same cookie middleware issues)
  if (!(await verifyAuth(request))) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    });
  }

  let ticker;
  try {
    const body = await request.json();
    ticker = (body.ticker || '').trim();
  } catch (e) {
    return new Response(JSON.stringify({ error: 'Invalid JSON body' }), {
      status: 400,
      headers: { 'content-type': 'application/json' },
    });
  }

  if (!ticker) {
    return new Response(JSON.stringify({ error: 'Missing ticker' }), {
      status: 400,
      headers: { 'content-type': 'application/json' },
    });
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return new Response(JSON.stringify({ error: 'ANTHROPIC_API_KEY not configured' }), {
      status: 500,
      headers: { 'content-type': 'application/json' },
    });
  }

  try {
    const data = await fetchYahooSummary(ticker);
    const facts = extractFactsForCard(data, ticker);
    const prompt = buildPrompt(ticker, data);
    const analysis = await callClaude(apiKey, prompt);
    return new Response(JSON.stringify({
      ticker,
      facts,
      analysis,
      model: CLAUDE_MODEL,
      generated_at: new Date().toISOString(),
    }), {
      status: 200,
      headers: {
        'content-type': 'application/json',
        // 1-hour client-side cache
        'cache-control': 'private, max-age=3600',
      },
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: e.message || 'Unknown error' }), {
      status: 500,
      headers: { 'content-type': 'application/json' },
    });
  }
}
