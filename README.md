# ATE Primary Research Tracker

Collects two independent, third-party measurements of the semiconductor test
market. Written for the DTL 2026 Equity Research Challenge (Teradyne), but
nothing in it is specific to that submission — it reads public sources and
writes CSVs.

Deliberately has no knowledge of any valuation model. The point is to measure
first and compare afterwards.

---

## What it measures

**Module A — test intensity (Taiwan).**
Taiwan-listed companies must file monthly revenue with the Market Observation
Post System (MOPS) by the 10th of the following month. The script pulls the
independent test houses (King Yuan 2449, Sigurd 6257, Ardentec 3264) and the
foundry and assembly names that define their end market (TSMC 2330, UMC 2303,
ASE 3711), then derives ratios between the groups.

Monthly frequency, roughly a six-week reporting lag, no vendor in between.

**Module B — competitive share (customs).**
HS 9030.82 is the customs line for "instruments and appliances for measuring or
checking semiconductor wafers or devices" — automated test equipment. US
exports under that code come overwhelmingly from US-headquartered ATE vendors;
Japanese exports come overwhelmingly from Advantest. The ratio over time is a
customs-verified read on relative shipments, and the destination split
separates memory-driven from logic-driven demand.

It also cross-correlates the two modules at lags of ±6 months, which tells you
whether Taiwanese test-house revenue leads or trails US equipment shipments.

---

## Setup

Python 3.8 or newer.

```bash
pip install requests pandas lxml matplotlib
```

`matplotlib` is optional — without it you still get every CSV, just no charts.
`lxml` is not optional; the MOPS pages are HTML tables and pandas needs it.

### API keys

| Source | Key needed? | Where |
|---|---|---|
| MOPS (Taiwan) | No | — |
| US Census | **Yes** | https://api.census.gov/data/key_signup.html |
| UN Comtrade | Strongly recommended | https://comtradedeveloper.un.org |

The Census endpoint rejects unkeyed requests outright with a "Missing Key" HTML
page. Signup is instant, but **you have to click the activation link in the
confirmation email** or the key silently keeps failing the same way.

Comtrade is only used for the Japan side of the share ratio. Without it the
script tries a keyless preview endpoint, and if that fails you still get the
full US series — you just lose the US-versus-Japan comparison, which is the
more interesting output. Register, subscribe to the free tier under Products,
copy the "Primary key" from your profile.

Either key can go in the environment instead of on the command line:

```bash
export CENSUS_KEY=...
export COMTRADE_KEY=...
```

---

## Running it

```bash
python ate_primary_research.py --census-key XXX --comtrade-key YYY
```

Defaults to January 2019 through two months back. Useful flags:

```
--start 2018-01          first month
--end 2026-06            last month
--skip-taiwan            Module B only
--skip-customs           Module A only
--no-charts              CSVs only
--refresh                ignore the cache and refetch everything
```

**First run takes 6–8 minutes**, almost all of it MOPS. Everything is cached to
`./cache`, so reruns are near-instant and adding a key later only refetches
what it needs. Error pages are never cached, so a failed run leaves nothing to
clean up.

If you want it faster, delete the Ardentec (`3264`) entry from
`TAIWAN_COMPANIES` at the top of the file. It's the only TPEx-listed name, so
removing it halves the MOPS sweep from two markets to one and the run drops to
about three minutes.

### Rate limiting

Per-source, set at the top of the file:

```python
MOPS_PAUSE     = 2.0   # a website, not an API — it throttles scrapers
CENSUS_PAUSE   = 0.3   # published API, ~500 calls/day unkeyed
COMTRADE_PAUSE = 1.0   # published API, calls are batched 12 months at a time
```

If MOPS starts returning blanks partway through a run, raise `MOPS_PAUSE` to 4
or 5 and rerun — cached months are kept, so only the misses are refetched.

---

## Output

Everything lands in `./output`.

| File | Contents |
|---|---|
| `taiwan_monthly_revenue.csv` | Tidy long format: month, code, company, group, revenue (NT$ thousands) |
| `test_intensity_proxy.csv` | Group aggregates, ratios, rolling averages, y/y growth |
| `us_ate_exports_detail.csv` | Monthly US exports of HS 9030.82 by destination country |
| `ate_export_totals.csv` | Monthly and TTM totals, y/y, and US share of US+Japan |
| `ate_export_destination_mix.csv` | Destination shares by month |
| `lead_lag_correlations.csv` | Correlation at each lag from −6 to +6 months |
| `*.png` | Chart versions of the above |

A summary block prints to the terminal at the end. That's a convenience view,
not the deliverable — the CSVs are.

---

## Reading the numbers

A few things that are easy to misread.

**`test_per_foundry` is not "test as a share of capex."** It's NT$ of
independent test-house revenue per NT$ of foundry revenue. It measures
outsourced test *services*, not test *equipment* purchases. The two move
differently and are not substitutes for each other. Quote the slope, never the
level.

**The foundry denominator is dominated by TSMC**, whose revenue growth reflects
leading-edge wafer pricing as much as volume. That inflates the denominator in
ways unrelated to test. If the ratio against foundry looks odd, recompute it
against the `osat` column instead — test-house revenue over assembly revenue
strips out wafer ASP and is arguably the better instrument. Both columns are
already in `test_intensity_proxy.csv`, so this is an Excel formula, not a
rerun.

**US customs data is a floor, not a measure.** Vendors with non-US
manufacturing ship from those sites, and those flows never appear in US export
statistics. Use it directionally.

**A large "Other" bucket in the destination mix is expected** — only nine
countries are named in `CENSUS_COUNTRIES` and ATE ships to far more than nine.
If Other exceeds about half, check whether an aggregate row is landing in it:

```python
import pandas as pd
df = pd.read_csv("output/us_ate_exports_detail.csv")
print(df[~df.is_total].groupby(["cty_code", "country"]).value_usd.sum()
        .sort_values(ascending=False).head(20))
```

Anything at the top with an implausible share is an aggregate, not a country.
Add its code to `CENSUS_COUNTRIES` or exclude it.

---

## Known limitations

- MOPS is scraped HTML, not an API. The URL scheme has changed before and will
  change again. The script tries two hosts and reports any market-month that
  came back empty rather than failing silently — watch that count.
- Taiwan revenue is NT$; US and Japan customs values are US$. Nothing is FX
  converted, because every ratio computed here is within a single currency.
- Comtrade revises historical data. Reruns months later may not reproduce
  exactly.
- Census publishes on roughly a five-week lag, so the two most recent months
  will usually come back empty. That's normal, not an error.
- Three test houses is a narrow sample of Taiwanese test capacity, and Taiwan is
  not the whole world. It is a proxy, and it should be labelled as one wherever
  it appears.

---

## Source list

- MOPS monthly revenue filings, Taiwan Stock Exchange — `mopsov.twse.com.tw`
- US Census Bureau, International Trade API, exports/hs timeseries
- UN Comtrade, monthly HS-level trade, reporter 392 (Japan)
