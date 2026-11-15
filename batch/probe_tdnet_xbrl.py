"""TDnet の XBRL(決算短信・配当予想の修正)の形を確かめる使い捨てスクリプト。"""
import io, re, zipfile, requests
UA = {"User-Agent": "Mozilla/5.0"}
BASE = "https://www.release.tdnet.info/inbs/"
seen = {"tanshin": 0, "rvdf": 0, "other": 0}
for ymd in ["20261113", "20261112"]:
    for page in range(1, 6):
        r = requests.get(f"{BASE}I_list_{page:03d}_{ymd}.html", headers=UA, timeout=20)
        if r.status_code != 200:
            break
        r.encoding = r.apparent_encoding or "utf-8"
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S)
        if page == 1 and ymd == "20261113":
            print("RAW ROW SAMPLE:", trs[3][:1500] if len(trs) > 3 else trs)
        for tr in trs:
            title = re.sub(r"<[^>]+>|\s+", "", (re.search(r'kjTitle[^>]*>(.*?)</td>', tr, re.S) or [None, ""])[1] if re.search(r'kjTitle', tr) else "")
            z = re.search(r'href="([^"]+\.zip)"', tr)
            if not z:
                continue
            kind = "tanshin" if "決算短信" in title else ("rvdf" if "配当予想" in title else "other")
            if seen[kind] >= 3:
                continue
            seen[kind] += 1
            print("\n=====", kind, ymd, title, z.group(1))
            zr = requests.get(BASE + z.group(1), headers=UA, timeout=30)
            zf = zipfile.ZipFile(io.BytesIO(zr.content))
            for n in zf.namelist():
                print("  file:", n)
            for n in zf.namelist():
                if not n.endswith((".htm", ".html", ".xbrl")):
                    continue
                t = zf.read(n).decode("utf-8", "ignore")
                facts = re.findall(r'<ix:nonFraction[^>]*name="([^"]*Dividend[^"]*)"[^>]*contextRef="([^"]+)"[^>]*>(.*?)</ix:nonFraction>', t, re.S)
                facts += [(a, b, c) for b, a, c in re.findall(r'<ix:nonFraction[^>]*contextRef="([^"]+)"[^>]*name="([^"]*Dividend[^"]*)"[^>]*>(.*?)</ix:nonFraction>', t, re.S)]
                if facts:
                    print("  --", n)
                    for f in facts[:60]:
                        print("    ", f[0], "|", f[1], "|", re.sub(r"<[^>]+>", "", f[2]))
        if len(trs) < 100:
            break
    if all(v >= 3 for v in seen.values()):
        break
