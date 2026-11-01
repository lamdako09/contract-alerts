"""Fast watcher: listed companies announcing government contract wins in SEC 8-K filings.

Every run reads SEC EDGAR's live feed of the latest 8-K filings, opens each new one that carries
a press release or agreement (items 1.01, 7.01, 8.01), looks for sentences where the company says it
was awarded / won a contract from a U.S. government customer with a dollar amount, and alerts when
that amount per year is at least `threshold_pct_of_revenue` of the company's yearly revenue.

usage:
  python announcement_alerts.py                      normal run (sends alerts)
  python announcement_alerts.py --dry-run            print findings, send nothing, remember nothing
  python announcement_alerts.py --check CIK ACCESSION [--send]   analyze one filing (testing)
"""
import csv, html, json, os, re, sys, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # also works under python -I
import contract_alerts as ca

STATE = os.path.join(ca.DATA, "announce_state.json")
ALERTS_CSV = os.path.join(ca.DATA, "announcement_alerts.csv")
FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb="
        "&owner=include&start=0&count=100&output=atom")
WANTED_ITEMS = ("1.01", "7.01", "8.01")

GOV = re.compile(
    r"\b(U\.?S\.? (Army|Navy|Air Force|Space Force|Marine Corps|Coast Guard|Government|Department of [A-Z][a-z]+(?: [A-Z][a-z]+)*)"
    r"|United States (Army|Navy|Air Force|Space Force|Marine Corps|Government)"
    r"|Department of (Defense|War|Energy|Homeland Security|Veterans Affairs|Health and Human Services|State|Justice|"
    r"Transportation|Agriculture|the Interior|the Treasury|Commerce|the Army|the Navy|the Air Force)"
    r"|Army|Navy|Air Force|Space Force|Marine Corps|NASA|DARPA|DoD|DoW|BARDA|DTRA|Missile Defense Agency|"
    r"Defense Logistics Agency|Defense Health Agency|Defense Information Systems Agency|DISA|Space Systems Command|"
    r"General Services Administration|GSA|FAA|FEMA|NIH|CDC|Veterans Affairs|Pentagon|federal government|"
    r"Naval [A-Z][a-z]+ (Systems )?Command|Army Contracting Command|Air Force Research Laboratory|AFRL)\b")
WON = re.compile(r"\b(awarded|award of|won|wins|received|receives|selected|secured|secures|was granted|has been granted|"
                 r"definitized|exercised an option)\b", re.I)
CONTRACT = re.compile(r"\b(contract|task order|delivery order|order|agreement|IDIQ|BPA|OTA|other transaction)\b", re.I)
SHARED = re.compile(r"\b(multiple[- ]award|multi[- ]award|shared ceiling|one of \w+ (companies|awardees|vendors))\b", re.I)
AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|million|thousand|bn|mm|[MBK])?\b", re.I)
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
YEARS = re.compile(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)[- ](year|yr)\b"
                   r"|\bover (\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten) years\b", re.I)
UNIT = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
# Words that sit right next to the dollar figure of the award itself ("Awarded $36 Million ...").
ANCHOR = re.compile(r"\b(awarded|award of|won|wins|receives?|received|announces?|announced|secured|secures|selected|"
                    r"valued at|worth|totaling|total value of)\b", re.I)
NOT_THIS_AWARD = re.compile(r"(ceiling|in addition to|previously announced|announced (on )?[A-Z][a-z]+ \d)[^$]{0,40}$", re.I)


def sec_get(cfg, url):
    time.sleep(0.15)  # stay well under SEC's 10 requests/second
    return ca.http(url, headers={"User-Agent": cfg["sec_user_agent"]}, timeout=60)


def to_text(raw):
    s = raw.decode("utf-8", "ignore")
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_amount(m):
    v = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit:
        return v * UNIT[unit]
    return v if v >= 100_000 else None  # "$5" or "$1.25 per share" is not a contract value


def find_awards(text):
    """Sentences announcing a government contract win, with the largest dollar amount in each."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z\"“])", text):
        if len(sent) > 1200 or not (WON.search(sent) and CONTRACT.search(sent) and GOV.search(sent)):
            continue
        if SHARED.search(sent) or re.search(r"\b(revenue|backlog|bookings|book-to-bill)\b", sent, re.I):
            continue  # shared ceilings and quarterly totals are not one contract's money
        amount = pick_amount(sent)
        if not amount:
            continue
        y = YEARS.search(sent)
        years = None
        if y:
            n = (y.group(1) or y.group(3)).lower()
            years = WORDNUM.get(n) or int(n)
        out.append({"sentence": sent.strip(), "amount": amount, "years": years,
                    "customer": GOV.search(sent).group(0)})
    return out


def pick_amount(sent):
    """The dollar figure closest to an award word, skipping ceilings and earlier contracts if possible."""
    anchors = [m.start() for m in ANCHOR.finditer(sent)]
    cands = []
    for m in AMOUNT.finditer(sent):
        v = parse_amount(m)
        if not v:
            continue
        skip = bool(NOT_THIS_AWARD.search(sent[max(0, m.start() - 80):m.start()]))
        dist = min((abs(m.start() - a) for a in anchors), default=10**6)
        cands.append((skip, dist, v))
    if not cands:
        return None
    return min(cands)[2]


def company_revenue(cfg, cik):
    """Yearly revenue from the company's own XBRL facts; used when the yearly SEC frame has none (new listings)."""
    facts = json.loads(sec_get(cfg, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))
    gaap = facts.get("facts", {}).get("us-gaap", {})
    best = None
    for concept in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"):
        for e in gaap.get(concept, {}).get("units", {}).get("USD", []):
            if not e.get("start"):
                continue
            days = (datetime.fromisoformat(e["end"]) - datetime.fromisoformat(e["start"])).days
            if 80 <= days <= 100:
                yearly = e["val"] * 4
            elif 350 <= days <= 380:
                yearly = e["val"]
            else:
                continue
            if not best or e["end"] > best[1]:
                best = (yearly, e["end"])
    return best


def parse_feed(raw):
    s = raw.decode("latin-1")
    for e in re.findall(r"(?s)<entry>(.*?)</entry>", s):
        title = re.search(r"<title>(.*?)</title>", e).group(1)
        m = re.search(r"\((\d{10})\)", title)
        acc = re.search(r"accession-number=([\d-]+)", e)
        if not (m and acc):
            continue
        items = re.findall(r"Item (\d\.\d\d)", html.unescape(e))
        yield {"cik": int(m.group(1)), "accession": acc.group(1), "items": items,
               "company": html.unescape(title.split(" - ", 1)[-1].split(" (")[0]),
               "updated": re.search(r"<updated>(.*?)</updated>", e).group(1)}


def filing_docs(cfg, cik, accession):
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    idx = json.loads(sec_get(cfg, f"{base}/index.json"))
    names = [i["name"] for i in idx["directory"]["item"] if i["name"].lower().endswith((".htm", ".html", ".txt"))
             and not i["name"].lower().endswith("-index.htm") and "index-headers" not in i["name"].lower()]
    return base, names[:6]


def analyze(cfg, filing, cik_info, revenue):
    base, names = filing_docs(cfg, filing["cik"], filing["accession"])
    awards = []
    for n in names:
        awards += find_awards(to_text(sec_get(cfg, f"{base}/{n}")))
    if not awards:
        return None
    best = max(awards, key=lambda a: a["amount"])
    ticker, name, exch = cik_info.get(filing["cik"], (None, filing["company"], None))
    rev = revenue.get(str(filing["cik"]))
    if not rev:
        try:
            rev = company_revenue(cfg, filing["cik"])
        except Exception:
            rev = None
    per_year = best["amount"] / max(best["years"] or 1, 1)
    return {
        **best, "ticker": ticker, "company": name, "exchange": exch, "per_year": per_year,
        "revenue": rev[0] if rev else None, "impact": per_year / rev[0] if rev and rev[0] > 0 else None,
        "filed": filing["updated"], "accession": filing["accession"],
        "link": f"{base}/{filing['accession']}-index.htm",
    }


def render(a):
    title = f"{a['ticker']}: announced {ca.money(a['amount'])} {a['customer']} contract = {a['impact']:.1%} of yearly revenue"
    yrs = f"over {a['years']} years (~{ca.money(a['per_year'])}/yr)" if a["years"] else "(length not stated; counted as 1 year)"
    body = (
        f"{a['company']} ({a['exchange']}: {a['ticker']}) filed this with the SEC at {a['filed']}\n"
        f"Value: {ca.money(a['amount'])} {yrs}\n"
        f"Company yearly revenue: {ca.money(a['revenue'])}\n\n"
        f"\"{a['sentence'][:600]}\"\n\n"
        f"Stock: https://finance.yahoo.com/quote/{a['ticker']}\n"
        f"Filing: {a['link']}\n\n"
        f"Read the filing before acting: amounts can be ceilings or include options."
    )
    return title, body


def main():
    os.makedirs(ca.DATA, exist_ok=True)
    cfg = ca.load_config()
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    dry = "--dry-run" in sys.argv
    ca.refresh_sec(cfg, state)
    _, by_ticker, revenue = ca.load_sec()
    cik_info = {}
    for t, (cik, name, exch) in by_ticker.items():
        cik_info.setdefault(cik, (t, name, exch))
    threshold = cfg["threshold_pct_of_revenue"] / 100

    if "--check" in sys.argv:
        i = sys.argv.index("--check")
        f = {"cik": int(sys.argv[i + 1]), "accession": sys.argv[i + 2], "company": "?", "updated": "?"}
        a = analyze(cfg, f, cik_info, revenue)
        if not a:
            print("no government contract award found")
            return
        print(json.dumps({k: v for k, v in a.items() if k != "sentence"}, indent=1))
        print("sentence:", a["sentence"][:500])
        if "--send" in sys.argv and a["ticker"] and a["impact"] is not None:
            ca.notify(cfg, *render(a), a["link"], a["ticker"])
            print("sent")
        return

    seen = set(state.get("seen", []))
    new = [f for f in parse_feed(sec_get(cfg, FEED))
           if f["accession"] not in seen and f["cik"] in cik_info and any(i in WANTED_ITEMS for i in f["items"])]
    found, failed = 0, set()
    for f in new:
        try:
            a = analyze(cfg, f, cik_info, revenue)
        except Exception as e:
            ca.log(f"[8-K] {f['accession']} will retry next run: {e}")
            failed.add(f["accession"])
            continue
        seen.add(f["accession"])
        if not a:
            continue
        found += 1
        imp = f"{a['impact']:.1%}" if a["impact"] is not None else "n/a"
        ca.log(f"[8-K] {a['ticker']} {ca.money(a['amount'])} {a['customer']} -> {imp} of revenue")
        if a["impact"] is not None and a["impact"] >= threshold:
            title, body = render(a)
            if dry:
                ca.log(f"(dry run) would alert: {title}")
                continue
            ca.notify(cfg, title, body, a["link"], a["ticker"])
            with open(ALERTS_CSV, "a", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerow([datetime.now().isoformat(timespec="seconds"), a["ticker"], title, a["link"]])
            ca.log(f"ALERT {title}")
    if not dry:
        # remember every checked filing so it is never re-read; failed ones are retried next run
        state["seen"] = sorted(seen | {f["accession"] for f in new} - failed)[-5000:]
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    print(f"{len(new)} new relevant 8-Ks checked, {found} with government contract wins")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        ca.log(f"[8-K] ERROR {type(e).__name__}: {e}")
        raise
