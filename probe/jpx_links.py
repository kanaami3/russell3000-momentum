import re
from curl_cffi import requests
s = requests.Session(impersonate="chrome")
for u in ["https://www.jpx.co.jp/markets/statistics-equities/investor-type/index.html",
          "https://www.jpx.co.jp/markets/statistics-equities/margin/04.html"]:
    r = s.get(u, timeout=25)
    print("==", u, r.status_code, len(r.text))
    for m in re.findall(r'href="([^"]+\.(?:xls|xlsx|csv|pdf|zip))"', r.text)[:40]:
        print(m)
    for m in re.findall(r'href="([^"]*(?:investor-type|margin)[^"]*\.html)"', r.text)[:30]:
        print("PAGE", m)
