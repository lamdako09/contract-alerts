"""Watch USAspending for new federal contracts that are large for a listed company.

Each run:
  1. Asks USAspending for contracts first awarded in the last `lookback_days`
     (at least `min_award_usd`), as a CSV export with parent-company names.
  2. Matches each winner (or its parent) to an NYSE / Nasdaq ticker using the SEC ticker list.
  3. Scores each contract: potential value per year / company's yearly revenue (SEC filings).
  4. Sends a phone push (ntfy) and/or email for every new contract at or above the threshold.

usage:
  python contract_alerts.py              normal run (sends alerts)
  python contract_alerts.py --dry-run    score and print, send nothing, remember nothing
  python contract_alerts.py --test       send one test notification and exit
  --lookback N / --threshold PCT         override config.json for one run
  --every-hours N                        skip unless the last full run was N+ hours ago (for frequent schedulers)
  --when-updated                         skip unless USAspending has published new data since the last run
"""
import csv, io, json, os, re, smtplib, sys, time, urllib.request, zipfile
from datetime import date, datetime, timedelta
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATE = os.path.join(DATA, "state.json")
LOG = os.path.join(DATA, "run.log")
ALERTS_CSV = os.path.join(DATA, "alerts.csv")
API = "https://api.usaspending.gov/api/v2"

SUFFIX = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LLC", "L", "LTD", "LIMITED",
          "PLC", "LP", "HOLDINGS", "HOLDING", "GROUP", "THE", "NV", "SA", "AG", "DE", "NEW", "CL", "A"}
# Normalized winner / parent names that don't normalize onto the SEC registrant name.
ALIASES = {
    "MODERNATX": "MRNA", "RAYTHEON": "RTX", "RAYTHEON TECHNOLOGIES": "RTX", "SPECTRUM INTERNATIONAL": "AMRK",
    "GOOGLE": "GOOGL", "AMAZON WEB SERVICES": "AMZN", "INTERNATIONAL BUSINESS MACHINES": "IBM",
    "HEWLETT PACKARD ENTERPRISE": "HPE", "PFIZER": "PFE", "MERCK SHARP DOHME": "MRK",
}
# Winners checked by hand and found to be unrelated to the listed company their name matches.
FALSE_MATCHES = {
    "API, LLC", "NVE, INC", "THE IBEX GROUP INC", "PERATON INC.", "THE NAKAMOTO GROUP, INC.",
    "SUNDIAL MARINE TUG & BARGE WORKS, INC.", "IPOWER LLC", "SPIRE CORPORATION", "FRONTLINE GROUP LLC",
    "AES GROUP INC", "CONSOLIDATED WATER GROUP, LLC", "ACUITY INTERNATIONAL, LLC",
    "ATLANTIC FABRICATION & BOILER SERVICES, INC.",
}
# Winners whose filed parent is stale; the listed owner today.
REMAP = {"CRANE & CO., INC.": "CXT", "CGI FEDERAL INC.": "GIB", "GEORGIA POWER COMPANY": "SO"}
EXCHANGES = ("Nasdaq", "NYSE", "NYSE American", "CBOE")


def load_config():
    """config.json, plus secrets from secrets.json (on this PC) or environment variables (GitHub Actions)."""
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    sp = os.path.join(HERE, "secrets.json")
    sec = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else {}
    for key, env in (("ntfy_topic", "NTFY_TOPIC"), ("email_address", "EMAIL_ADDRESS"), ("email_app_password", "EMAIL_APP_PASSWORD")):
        if os.environ.get(env):
            sec[key] = os.environ[env]
    cfg["ntfy"]["topic"] = sec.get("ntfy_topic", "")
    cfg["email"]["from"] = cfg["email"]["to"] = sec.get("email_address", "")
    cfg["email"]["app_password"] = sec.get("email_app_password", "")
    return cfg


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http(url, data=None, headers=None, timeout=120):
    h = {"Content-Type": "application/json"} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def norm(s):
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"/[A-Z]{2}/", " ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w not in SUFFIX)


def d(s):
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def num(s):
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


# ---------- SEC reference data (tickers + yearly revenue), refreshed weekly ----------

def refresh_sec(cfg, state):
    paths = [os.path.join(DATA, "tickers.json"), os.path.join(DATA, "revenue.json")]
    week_ago = time.time() - 7 * 86400
    if all(os.path.exists(p) and os.path.getmtime(p) > week_ago for p in paths):
        return
    ua = {"User-Agent": cfg["sec_user_agent"]}
    tickers = http("https://www.sec.gov/files/company_tickers_exchange.json", headers=ua)
    with open(paths[0], "wb") as f:
        f.write(tickers)
    revenue = {}  # cik -> [value, period end]
    y = date.today().year
    for year in (y - 2, y - 1):  # later year overwrites
        for concept in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"):
            try:
                frame = json.loads(http(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/CY{year}.json", headers=ua))
            except Exception as e:
                log(f"SEC frame {concept} CY{year} unavailable: {e}")
                continue
            for e in frame["data"]:
                prev = revenue.get(str(e["cik"]))
                if not prev or e["end"] > prev[1] or (e["end"] == prev[1] and e["val"] > prev[0]):
                    revenue[str(e["cik"])] = [e["val"], e["end"]]
            time.sleep(0.3)
    with open(paths[1], "w", encoding="utf-8") as f:
        json.dump(revenue, f)
    log(f"SEC data refreshed: {len(revenue)} companies with revenue")


def load_sec():
    by_norm, by_ticker = {}, {}
    for cik, name, ticker, exch in json.load(open(os.path.join(DATA, "tickers.json"), encoding="utf-8"))["data"]:
        if not ticker or exch not in EXCHANGES:
            continue
        by_ticker.setdefault(ticker, (cik, name, exch))
        k = norm(name)
        if len(k) >= 3 and k not in by_norm:  # first listed = primary share class
            by_norm[k] = ticker
    revenue = json.load(open(os.path.join(DATA, "revenue.json"), encoding="utf-8"))
    return by_norm, by_ticker, revenue


def lookup(name, by_norm):
    k = norm(name or "")
    if not k:
        return None
    return ALIASES.get(k) or by_norm.get(k)


# ---------- USAspending ----------

def usaspending_last_updated():
    return json.loads(http(f"{API}/awards/last_updated/"))["last_updated"]


def fetch_new_awards(cfg, min_award=None, recipient_types=None):
    end = date.today()
    start = end - timedelta(days=cfg["lookback_days"])
    min_award = min_award or cfg["min_award_usd"]
    body = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat(), "date_type": "new_awards_only"}],
            "award_amounts": [{"lower_bound": min_award}],
        },
        "file_format": "csv",
    }
    if recipient_types:
        body["filters"]["recipient_type_names"] = recipient_types
    job = json.loads(http(f"{API}/download/awards/", data=body))
    for _ in range(120):  # up to 20 minutes
        st = json.loads(http(job["status_url"]))
        if st["status"] == "finished":
            break
        if st["status"] == "failed":
            raise RuntimeError(f"USAspending export failed: {st.get('message')}")
        time.sleep(10)
    else:
        raise RuntimeError("USAspending export timed out")
    z = zipfile.ZipFile(io.BytesIO(http(job["file_url"], timeout=600)))
    name = next(n for n in z.namelist() if n.startswith("Contracts_PrimeAwardSummaries"))
    csv.field_size_limit(10**8)
    rows = list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline="")))
    who = f", recipients: {', '.join(recipient_types)}" if recipient_types else ""
    log(f"USAspending: {len(rows)} contracts first awarded {start} .. {end} at >= ${min_award:,.0f}{who}")
    return rows


def score(rows, by_norm, by_ticker, revenue):
    out = []
    for r in rows:
        if r["recipient_name"] in FALSE_MATCHES:
            continue
        t = REMAP.get(r["recipient_name"]) or lookup(r["recipient_parent_name"], by_norm) or lookup(r["recipient_name"], by_norm)
        if not t or t not in by_ticker:
            continue
        cik, sec_name, exch = by_ticker[t]
        rev = revenue.get(str(cik))
        start, pend = d(r["period_of_performance_start_date"]), d(r["period_of_performance_potential_end_date"])
        potential = num(r["potential_total_value_of_award"]) or num(r["current_total_value_of_award"])
        years = max((pend - start).days / 365.25, 1.0) if start and pend else 1.0
        per_year = potential / years
        out.append({
            "key": r["contract_award_unique_key"], "ticker": t, "company": sec_name, "exchange": exch,
            "winner": r["recipient_name"], "agency": r["awarding_agency_name"], "sub_agency": r["awarding_sub_agency_name"],
            "description": r["prime_award_base_transaction_description"], "awarded": r["award_base_action_date"],
            "obligated": num(r["total_obligated_amount"]), "potential": potential, "per_year": per_year,
            "runs_until": r["period_of_performance_potential_end_date"], "revenue": rev[0] if rev else None,
            "impact": per_year / rev[0] if rev and rev[0] > 0 else None, "link": r["usaspending_permalink"],
            "small_business_set_aside": r["type_of_set_aside"],
        })
    return out


# ---------- notifications ----------

def money(v):
    return f"${v / 1e9:,.2f}B" if v >= 1e9 else f"${v / 1e6:,.1f}M"


def render(a):
    share = f"{a['impact']:.1%} of yearly revenue" if a["impact"] is not None else "revenue unknown"
    title = f"{a['ticker']}: {money(a['potential'])} contract = {share}"
    if a.get("saved_search"):
        title = "[Your saved search] " + title
    body = (
        f"{a['company']} ({a['exchange']}: {a['ticker']})\n"
        f"Winner as filed: {a['winner']}\n"
        f"Agency: {a['sub_agency'] or a['agency']}\n"
        f"Awarded: {a['awarded']}   Runs until: {a['runs_until'] or '?'}\n"
        f"Potential value: {money(a['potential'])}  (~{money(a['per_year'])}/yr)   Obligated so far: {money(a['obligated'])}\n"
        f"Company yearly revenue: {money(a['revenue']) if a['revenue'] else 'unknown'}\n"
        f"What it's for: {(a['description'] or '')[:300]}\n"
        f"Stock: https://finance.yahoo.com/quote/{a['ticker']}\n"
        f"Contract: {a['link']}\n\n"
        f"Check the winner really belongs to {a['ticker']} and whether it was already announced before acting."
    )
    return title, body


def send_ntfy(cfg, title, body, link=None, ticker=None):
    n = cfg["ntfy"]
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Tags": "moneybag", "Priority": "high"}
    if ticker:
        yahoo = f"https://finance.yahoo.com/quote/{ticker}"
        headers["Click"] = yahoo
        headers["Actions"] = f"view, Yahoo Finance, {yahoo}" + (f"; view, Contract, {link}" if link else "")
    elif link:
        headers["Click"] = link
    req = urllib.request.Request(f"{n['server'].rstrip('/')}/{n['topic']}", data=body.encode("utf-8"), headers=headers)
    urllib.request.urlopen(req, timeout=30).read()


def send_email(cfg, title, body):
    e = cfg["email"]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = f"[Contract alert] {title}", e["from"], e["to"]
    msg.set_content(body)
    with smtplib.SMTP_SSL(e["smtp_host"], e["smtp_port"], timeout=30) as s:
        s.login(e["from"], e["app_password"])
        s.send_message(msg)


def notify(cfg, title, body, link=None, ticker=None):
    if cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic"):
        try:
            send_ntfy(cfg, title, body, link, ticker)
        except Exception as e:
            log(f"ntfy failed: {e}")
    if cfg["email"].get("enabled") and cfg["email"].get("app_password"):
        try:
            send_email(cfg, title, body)
        except Exception as e:
            log(f"email failed: {e}")


def append_alerts(alerts):
    new_file = not os.path.exists(ALERTS_CSV)
    with open(ALERTS_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["alerted_at", *alerts[0].keys()])
        if new_file:
            w.writeheader()
        for a in alerts:
            w.writerow({"alerted_at": datetime.now().isoformat(timespec="seconds"), **a})


# ---------- main ----------

def main():
    os.makedirs(DATA, exist_ok=True)
    cfg = load_config()
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    dry = "--dry-run" in sys.argv
    if "--every-hours" in sys.argv and state.get("last_run"):
        hours = float(sys.argv[sys.argv.index("--every-hours") + 1])
        if datetime.now() - datetime.fromisoformat(state["last_run"]) < timedelta(hours=hours):
            print(f"last run {state['last_run']} is under {hours:g}h ago; skipping")
            return
    published = None
    if "--when-updated" in sys.argv:
        published = usaspending_last_updated()
        recent = state.get("last_run") and datetime.now() - datetime.fromisoformat(state["last_run"]) < timedelta(hours=24)
        if published == state.get("usaspending_last_updated") and recent:
            print(f"USAspending data unchanged since {published}; skipping")
            return
    for flag, key, cast in (("--lookback", "lookback_days", int), ("--threshold", "threshold_pct_of_revenue", float)):
        if flag in sys.argv:
            cfg[key] = cast(sys.argv[sys.argv.index(flag) + 1])

    if "--test" in sys.argv:
        where = "GitHub (cloud)" if os.environ.get("GITHUB_ACTIONS") else "this PC"
        sample = {  # a real October award, shown in the exact alert format
            "ticker": "BAER", "company": "Bridger Aerospace Group Holdings, Inc.", "exchange": "Nasdaq",
            "winner": "BRIDGER AEROSPACE GROUP, LLC", "agency": "Department of the Interior",
            "sub_agency": "Departmental Offices", "awarded": "2026-10-13", "runs_until": "2027-10-12",
            "potential": 5_340_000, "per_year": 5_340_000, "obligated": 5_340_000, "revenue": 122_800_000,
            "impact": 0.0435, "description": "TASK ORDER FOR 2027 AIR ATTACK FLIGHT SERVICES WITH CAMERA SYSTEMS",
            "link": "https://www.usaspending.gov/search?hash=ccf9627b340ee34882aff1af876f3a95",
        }
        title, body = render(sample)
        notify(cfg, f"TEST from {where} - {title}", "This is a test. A real alert looks like this:\n\n" + body,
               sample["link"], sample["ticker"])
        log(f"test notification sent from {where}")
        return

    refresh_sec(cfg, state)
    by_norm, by_ticker, revenue = load_sec()
    scored = score(fetch_new_awards(cfg), by_norm, by_ticker, revenue)
    threshold = cfg["threshold_pct_of_revenue"] / 100
    seen = set(state.get("seen", []))
    hits = sorted((a for a in scored if a["impact"] is not None and a["impact"] >= threshold and a["key"] not in seen),
                  key=lambda a: -a["impact"])
    # Your saved USAspending search: every match won by a listed company alerts, whatever its size vs revenue.
    ss = cfg.get("saved_search")
    if ss:
        rows = fetch_new_awards(cfg, ss["min_award_usd"], ss.get("recipient_types"))
        matches = score(rows, by_norm, by_ticker, revenue)
        log(f"saved search: {len(rows)} matches, {len(matches)} won by listed companies")
        hit_keys = {a["key"] for a in hits}
        for a in matches:
            if a["key"] not in seen and a["key"] not in hit_keys:
                hits.append({**a, "saved_search": True})
    log(f"{len(scored)} awards to listed companies; {len(hits)} new at >= {threshold:.0%} of revenue")

    for a in sorted(scored, key=lambda a: -(a["impact"] or 0))[:10]:
        imp = f"{a['impact']:.2%}" if a["impact"] is not None else "  n/a"
        print(f"   {imp:>7}  {a['ticker']:<6} {money(a['potential']):>9}  {a['awarded']}  {(a['description'] or '')[:60]}")

    if dry:
        for a in hits:
            log(f"(dry run) would alert: {render(a)[0]}")
        return
    for a in hits:
        title, body = render(a)
        notify(cfg, title, body, a["link"], a["ticker"])
        log(f"ALERT {title}")
    if hits:
        append_alerts(hits)
    state["seen"] = sorted(seen | {a["key"] for a in hits})
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    if published:
        state["usaspending_last_updated"] = published
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        os.makedirs(DATA, exist_ok=True)
        log(f"ERROR {type(e).__name__}: {e}")
        raise
