# Contract Alerts

Alerts on your phone (ntfy) and/or email when a stock-listed company wins a U.S. government contract
that is large compared with the company's yearly revenue. Every alert links to the stock on Yahoo Finance.

| Watcher | Source | Speed |
|---|---|---|
| `announcement_alerts.py` | SEC EDGAR live 8-K feed: companies announcing government contract wins | minutes after the company files |
| `contract_alerts.py` | USAspending award data | days (Defense Dept.: 90 days) |

`announcement_alerts.py` reads each new 8-K that carries a press release or agreement (items 1.01 / 7.01 / 8.01),
finds sentences where the company says it was awarded a contract by a U.S. government customer, takes the award's
dollar figure, and compares it to yearly revenue (SEC filings).

`contract_alerts.py` pulls contracts first awarded in the last 7 days ($1M+) from USAspending, matches each winner
(or its parent company) to an NYSE / Nasdaq ticker, and compares potential value per year to yearly revenue.

Each contract / filing alerts once, when it is at or above `threshold_pct_of_revenue` (default 5%).

## Where it runs

**GitHub Actions** (`.github/workflows/alerts.yml`): every 5 minutes (GitHub may start runs late when busy).
The USAspending check runs only once every 6 hours. Secrets go in the repository's
*Settings → Secrets and variables → Actions*:

| Secret | Value |
|---|---|
| `NTFY_TOPIC` | your ntfy topic name (required for phone alerts) |
| `EMAIL_ADDRESS` | Gmail address to send from and to (optional) |
| `EMAIL_APP_PASSWORD` | Gmail app password (optional; email stays off without it) |

**On a PC**: put the same three values in `secrets.json` (`ntfy_topic`, `email_address`, `email_app_password`);
never upload that file.

## Settings (`config.json`)

| Key | Meaning |
|---|---|
| `threshold_pct_of_revenue` | Alert when contract value per year is at least this % of yearly revenue |
| `min_award_usd` | USAspending: ignore contracts smaller than this |
| `lookback_days` | USAspending: how far back each run looks |

## Manual commands

```
python announcement_alerts.py --dry-run                       # check latest filings, send nothing
python announcement_alerts.py --check <CIK> <accession>       # analyze one filing
python contract_alerts.py --dry-run --lookback 31 --threshold 2
python contract_alerts.py --test                              # send a test notification
```

## Limits

- No public source has a contract before it is announced; this catches the announcement itself within minutes.
  Professional traders read the same filings within milliseconds.
- Contracts a company does not file with the SEC (too small to matter to it) only show up via USAspending, days later.
- Amounts can be ceilings or include options. Read the filing before acting. This is data, not investment advice.
