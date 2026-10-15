import re
from curl_cffi import requests
s = requests.Session(impersonate="chrome")
def show(url, pats, ctx=300, enc=None):
    print("=" * 30, url)
    try:
        r = s.get(url, timeout=25)
        t = r.content.decode(enc) if enc else r.text
        print("status", r.status_code, "len", len(t))
        for p in pats:
            for m in list(re.finditer(p, t))[:3]:
                a = max(0, m.start() - ctx); print("--", p, ":", re.sub(r"\s+", " ", t[a:m.end() + ctx]))
    except Exception as e:
        print("ERR", e)
show("https://kabutan.jp/stock/?code=1570", ["信用倍率", "信用買残"], 600)
show("https://kabutan.jp/stock/kabuka?code=1570&ashi=shin", ["信用倍率", "売り残"], 1500)
show("https://finance.yahoo.co.jp/quote/1570.T", ["信用倍率", "信用買残"], 400)
show("https://minkabu.jp/stock/1570", ["信用倍率"], 400)
show("https://www.jpx.co.jp/markets/statistics-equities/margin/02.html", [r'href="[^"]+\.(?:csv|xls|xlsx|pdf)"'], 80)
show("https://www.jpx.co.jp/markets/statistics-equities/margin/07.html", [r'href="[^"]+\.(?:csv|xls|xlsx|pdf)"', "<h1"], 80)
show("https://www.jpx.co.jp/markets/statistics-equities/margin/01.html", [r'href="[^"]+\.(?:csv|xls|xlsx|pdf)"', "<h1"], 80)
show("https://indexes.nikkei.co.jp/nkave/archives/data?list=pbr", [r"\d{4}\.\d{2}\.\d{2}"], 200)
import yfinance as yf
df = yf.Ticker("^VIX", session=s).history(period="6mo")
print(df.tail(3))
