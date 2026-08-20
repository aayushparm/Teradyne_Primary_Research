"""
ATE Primary Research Tracker
============================

Two independent, third-party measurements of the semiconductor test market:

  Module A - Test intensity.
      Taiwan-listed companies are required to file monthly revenue with the
      Market Observation Post System (MOPS) by the 10th of the following month.
      Pulling the independent test houses (King Yuan, Sigurd, Ardentec) against
      the foundries (TSMC, UMC) gives a monthly, observable read on how fast
      test value-add is growing relative to front-end output.

  Module B - Competitive share.
      HS 9030.82 covers "instruments and appliances for measuring or checking
      semiconductor wafers or devices" - i.e. automated test equipment. US
      exports under that code are overwhelmingly US-headquartered ATE vendors;
      Japanese exports are overwhelmingly Advantest. The ratio over time is a
      customs-verified read on relative shipment share, and the destination
      split separates memory-driven from logic-driven demand.

Sources are official (TWSE/MOPS, US Census Bureau, UN Comtrade). Nothing here
is derived from company filings, broker notes or any valuation model - the
outputs are raw measurements, to be compared against modelled assumptions
separately.

Usage:
    python ate_primary_research.py
    python ate_primary_research.py --start 2018-01 --end 2026-06
    python ate_primary_research.py --comtrade-key YOUR_KEY --no-charts

Requires: requests, pandas, lxml (for table parsing). matplotlib is optional
and only needed for the PNG exhibits.

First run takes roughly 8-12 minutes because MOPS is rate-limited and we are
polite about it. Everything is cached to ./cache, so re-runs are near-instant.
"""

import argparse
import hashlib
import io
import json
import os
import sys
import time
from datetime import date

import requests

try:
    import pandas as pd
except ImportError:
    sys.exit("pandas is required:  pip install pandas lxml requests")


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

# Taiwan universe. "market" is the MOPS bucket: sii = TWSE main board,
# otc = TPEx. Group tags drive the aggregation later on.
TAIWAN_COMPANIES = {
    "2449": {"name": "King Yuan Electronics", "market": "sii", "group": "test"},
    "6257": {"name": "Sigurd Microelectronics", "market": "sii", "group": "test"},
    "3264": {"name": "Ardentec", "market": "otc", "group": "test"},
    "2330": {"name": "TSMC", "market": "sii", "group": "foundry"},
    "2303": {"name": "UMC", "market": "sii", "group": "foundry"},
    "3711": {"name": "ASE Technology Holding", "market": "sii", "group": "osat"},
}

# MOPS moved hosts a while back and old links still float around, so try both.
MOPS_HOSTS = [
    "https://mopsov.twse.com.tw",
    "https://mops.twse.com.tw",
]
MOPS_PATH = "/nas/t21/{market}/t21sc03_{roc}_{month}_0.html"

# US Census international trade API. No key needed under ~500 calls/day, but
# the endpoint is fussy about how the period is expressed and the accepted
# form has changed over the years - so we probe a few and keep whichever works.
CENSUS_URL = "https://api.census.gov/data/timeseries/intltrade/exports/hs"
HS_CODE = "903082"

CENSUS_VARIANTS = [
    ("YEAR/MONTH",
     "{base}?get=CTY_CODE,CTY_NAME,ALL_VAL_MO"
     "&E_COMMODITY={hs}&COMM_LVL=HS6&YEAR={year}&MONTH={month}"),
    ("time=",
     "{base}?get=CTY_CODE,CTY_NAME,ALL_VAL_MO"
     "&E_COMMODITY={hs}&COMM_LVL=HS6&time={year}-{month}"),
    ("YEAR/MONTH + SUMMARY_LVL",
     "{base}?get=CTY_CODE,CTY_NAME,ALL_VAL_MO"
     "&E_COMMODITY={hs}&COMM_LVL=HS6&SUMMARY_LVL=DET&YEAR={year}&MONTH={month}"),
    ("time= without COMM_LVL",
     "{base}?get=CTY_CODE,CTY_NAME,ALL_VAL_MO,E_COMMODITY"
     "&E_COMMODITY={hs}&time={year}-{month}"),
]

# Destinations we care about. Everything else gets bucketed as "Other".
CENSUS_COUNTRIES = {
    "5830": "Taiwan",
    "5800": "South Korea",
    "5700": "China",
    "5570": "Malaysia",
    "5880": "Japan",
    "5590": "Singapore",
    "5650": "Philippines",
    "4280": "Germany",
    "2010": "Mexico",
}

# UN Comtrade, used for the Japan side of the share calculation.
COMTRADE_AUTH = "https://comtradeapi.un.org/data/v1/get/C/M/HS"
COMTRADE_PREVIEW = "https://comtradeapi.un.org/public/v1/preview/C/M/HS"
JAPAN_REPORTER = "392"

CACHE_DIR = "cache"
OUTPUT_DIR = "output"
REQUEST_PAUSE = 2.0          # seconds between live requests
USER_AGENT = "Mozilla/5.0 (compatible; equity-research-tracker/1.0)"


# ----------------------------------------------------------------------------
# Small utilities
# ----------------------------------------------------------------------------

def month_range(start, end):
    """Yield (year, month) tuples inclusive. Inputs are 'YYYY-MM' strings."""
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def default_end_month(lag_months=2):
    """Most recent month likely to have published data everywhere."""
    y, m = date.today().year, date.today().month
    m -= lag_months
    while m <= 0:
        y, m = y - 1, m + 12
    return "%04d-%02d" % (y, m)


def to_number(text):
    """Parse a MOPS revenue cell. Returns None if it isn't a usable number."""
    if text is None:
        return None
    cleaned = str(text).replace(",", "").replace(" ", "").strip()
    if cleaned in ("", "-", "nan", "None"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def cache_path(url):
    key = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, key + ".txt")


def body_looks_ok(body, expect_json):
    """Reject plain-text or HTML error pages that arrive with a 200 status."""
    if not body or not body.strip():
        return False
    if expect_json:
        return body.lstrip()[:1] in (b"[", b"{")
    return True


def fetch(url, refresh=False, timeout=45, expect_json=False, quiet=False):
    """GET with an on-disk cache. Returns the response body as bytes, or None."""
    path = cache_path(url)
    if os.path.exists(path) and not refresh:
        with open(path, "rb") as fh:
            cached = fh.read()
        if body_looks_ok(cached, expect_json):
            return cached
        os.remove(path)          # an error page cached by an earlier run

    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as exc:
        print("    request failed: %s" % exc)
        return None
    finally:
        time.sleep(REQUEST_PAUSE)

    if resp.status_code != 200:
        if not quiet:
            print("    HTTP %s - %s"
                  % (resp.status_code,
                     resp.content[:200].decode("utf-8", "replace").strip()))
        return None

    if not body_looks_ok(resp.content, expect_json):
        if not quiet:
            print("    server said: %s"
                  % resp.content[:250].decode("utf-8", "replace").strip())
        return None

    with open(path, "wb") as fh:
        fh.write(resp.content)
    return resp.content


# ----------------------------------------------------------------------------
# Module A - Taiwan monthly revenue (MOPS)
# ----------------------------------------------------------------------------

def decode_mops(raw):
    """MOPS pages are historically Big5. Newer ones are sometimes UTF-8."""
    for codec in ("big5", "utf-8", "cp950"):
        try:
            text = raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        if "公司代號" in text or "Company" in text:
            return text
    # Company codes and revenue figures are ASCII either way, so a lossy
    # decode is still usable for our purposes.
    return raw.decode("big5", errors="replace")


def parse_mops_page(html, wanted_codes):
    """Pull (code, name, monthly revenue) for the codes we care about.

    The page is a stack of per-industry tables. Column order is stable:
    0 = company code, 1 = company name, 2 = current month revenue (NT$ '000).
    We match on the code rather than trusting header text, which varies.
    """
    try:
        tables = pd.read_html(io.StringIO(html))
    except ImportError:
        sys.exit("Table parsing needs lxml:  pip install lxml")
    except ValueError:
        return {}          # no tables at all - usually a 'no data' stub page

    found = {}
    for table in tables:
        if table.shape[1] < 3:
            continue
        for row in table.itertuples(index=False, name=None):
            code = str(row[0]).strip()
            if code not in wanted_codes or code in found:
                continue
            revenue = to_number(row[2])
            if revenue and revenue > 0:
                found[code] = (str(row[1]).strip(), revenue)
    return found


def scrape_taiwan(start, end, refresh=False):
    """Walk MOPS month by month and assemble a tidy revenue table."""
    markets = sorted({c["market"] for c in TAIWAN_COMPANIES.values()})
    codes_by_market = {
        mkt: {code for code, meta in TAIWAN_COMPANIES.items() if meta["market"] == mkt}
        for mkt in markets
    }

    records = []
    misses = []

    for year, month in month_range(start, end):
        roc = year - 1911
        for market in markets:
            path = MOPS_PATH.format(market=market, roc=roc, month=month)

            raw = None
            for host in MOPS_HOSTS:
                raw = fetch(host + path, refresh=refresh)
                if raw:
                    break

            if not raw:
                misses.append("%04d-%02d/%s" % (year, month, market))
                continue

            found = parse_mops_page(decode_mops(raw), codes_by_market[market])
            if not found:
                misses.append("%04d-%02d/%s" % (year, month, market))
                continue

            for code, (name, revenue) in found.items():
                records.append({
                    "month": "%04d-%02d" % (year, month),
                    "code": code,
                    "company": TAIWAN_COMPANIES[code]["name"],
                    "group": TAIWAN_COMPANIES[code]["group"],
                    "revenue_ntd_k": revenue,
                    "mops_name": name,
                })

        print("  MOPS %04d-%02d done (%d rows so far)" % (year, month, len(records)))

    if misses:
        print("  note: %d market-months returned nothing (%s%s)"
              % (len(misses), ", ".join(misses[:6]), " ..." if len(misses) > 6 else ""))

    return pd.DataFrame(records)


def build_intensity(taiwan_df):
    """Aggregate by group and derive the test-intensity proxy series."""
    if taiwan_df.empty:
        return pd.DataFrame()

    panel = (taiwan_df
             .groupby(["month", "group"])["revenue_ntd_k"]
             .sum()
             .unstack("group")
             .sort_index())

    for col in ("test", "foundry", "osat"):
        if col not in panel.columns:
            panel[col] = float("nan")

    # Core ratio: independent test revenue per NT$ of foundry output. The
    # absolute level is not comparable to "test as % of capex" - what matters
    # is the slope.
    panel["test_per_foundry"] = panel["test"] / panel["foundry"]
    panel["test_per_frontend_backend"] = panel["test"] / (panel["foundry"] + panel["osat"])

    # Smooth the lumpiness. Taiwan monthlies are noisy around Lunar New Year.
    panel["test_per_foundry_3m"] = panel["test_per_foundry"].rolling(3).mean()
    panel["test_per_foundry_12m"] = panel["test_per_foundry"].rolling(12).mean()

    # Indexed view - usually the cleanest single exhibit.
    for col in ("test", "foundry", "osat"):
        base = panel[col].head(12).mean()
        if base and base == base:
            panel[col + "_index"] = panel[col] / base * 100

    # Year-on-year growth on a 12-month lag (monthly data, so shift 12).
    for col in ("test", "foundry", "osat"):
        panel[col + "_yoy"] = panel[col].pct_change(12)

    panel["growth_spread_pp"] = (panel["test_yoy"] - panel["foundry_yoy"]) * 100

    return panel


# ----------------------------------------------------------------------------
# Module B - customs flows for HS 9030.82
# ----------------------------------------------------------------------------

def census_url(template, year, month, key=None):
    url = template.format(base=CENSUS_URL, hs=HS_CODE,
                          year="%04d" % year, month="%02d" % month)
    if key:
        url += "&key=" + key
    return url


def pick_census_variant(year, month, key=None, refresh=False):
    """Work out which parameter format this endpoint currently accepts."""
    print("  probing Census parameter formats on %04d-%02d..." % (year, month))

    for label, template in CENSUS_VARIANTS:
        raw = fetch(census_url(template, year, month, key),
                    refresh=refresh, expect_json=True, quiet=True)
        if not raw:
            print("    '%s' rejected" % label)
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            print("    '%s' returned unreadable JSON" % label)
            continue
        if len(payload) > 1:
            print("    using '%s' (%d rows on the probe month)"
                  % (label, len(payload) - 1))
            return template
        print("    '%s' returned an empty result" % label)

    # Nothing worked - show the raw server message so it can be diagnosed.
    print("\n  All parameter formats failed. Raw response from the first attempt:")
    fetch(census_url(CENSUS_VARIANTS[0][1], year, month, key),
          refresh=True, expect_json=True)
    print("  If that mentions a key, get a free one at "
          "https://api.census.gov/data/key_signup.html and pass --census-key.\n")
    return None


def scrape_census(start, end, key=None, refresh=False):
    """Monthly US exports of HS 903082, by destination country."""
    records = []
    months = list(month_range(start, end))
    if not months:
        return pd.DataFrame()

    # Probe on a month that is definitely published rather than the newest one.
    probe_year, probe_month = months[0]
    template = pick_census_variant(probe_year, probe_month, key=key, refresh=refresh)
    if template is None:
        return pd.DataFrame()

    for year, month in months:
        stamp = "%04d-%02d" % (year, month)

        raw = fetch(census_url(template, year, month, key),
                    refresh=refresh, expect_json=True, quiet=True)
        if not raw:
            print("  Census %s unavailable (likely not published yet)" % stamp)
            continue

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            print("  Census %s returned unreadable JSON" % stamp)
            continue

        if len(payload) < 2:
            continue

        header = payload[0]
        rows = [dict(zip(header, r)) for r in payload[1:]]

        for row in rows:
            value = to_number(row.get("ALL_VAL_MO"))
            if value is None:
                continue
            code = str(row.get("CTY_CODE", "")).strip()
            records.append({
                "month": stamp,
                "cty_code": code,
                "country": CENSUS_COUNTRIES.get(code, row.get("CTY_NAME", "").title()),
                "value_usd": value,
                "is_total": code == "-",
            })

        print("  Census %s done" % stamp)

    return pd.DataFrame(records)


def comtrade_get(period, reporter, key=None, refresh=False):
    """One Comtrade call. Falls back to the keyless preview endpoint."""
    base = COMTRADE_AUTH if key else COMTRADE_PREVIEW
    url = ("%s?reporterCode=%s&flowCode=X&cmdCode=%s&period=%s"
           "&partnerCode=0&motCode=0&customsCode=C00"
           % (base, reporter, HS_CODE, period))

    if key:
        # Keyed requests bypass the cache helper because of the auth header.
        try:
            resp = requests.get(url,
                                headers={"Ocp-Apim-Subscription-Key": key,
                                         "User-Agent": USER_AGENT},
                                timeout=45)
            time.sleep(REQUEST_PAUSE)
            if resp.status_code != 200:
                return None
            return resp.json()
        except (requests.RequestException, ValueError):
            return None

    raw = fetch(url, refresh=refresh, expect_json=True)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def scrape_japan(start, end, key=None, refresh=False):
    """Monthly Japanese exports of HS 903082 - the Advantest side of the ratio."""
    records = []
    periods = ["%04d%02d" % (y, m) for y, m in month_range(start, end)]

    # Comtrade accepts batched periods; twelve at a time keeps URLs sane.
    for i in range(0, len(periods), 12):
        chunk = ",".join(periods[i:i + 12])
        payload = comtrade_get(chunk, JAPAN_REPORTER, key=key, refresh=refresh)

        if not payload or not payload.get("data"):
            print("    no Comtrade data for %s" % chunk[:6])
            continue

        for row in payload["data"]:
            period = str(row.get("period", ""))
            value = to_number(row.get("primaryValue") or row.get("fobvalue"))
            if len(period) != 6 or value is None:
                continue
            records.append({
                "month": period[:4] + "-" + period[4:],
                "value_usd": value,
            })

        print("  Comtrade batch %s done" % chunk[:6])

    if not records:
        return pd.DataFrame()

    return (pd.DataFrame(records)
            .groupby("month", as_index=False)["value_usd"].sum())


def build_export_view(census_df, japan_df):
    """Totals, destination mix and the US-vs-Japan shipment share."""
    if census_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    # The Census total row is CTY_CODE '-'; use it where present, otherwise
    # sum the country rows.
    totals = (census_df[census_df["is_total"]]
              .groupby("month", as_index=False)["value_usd"].sum()
              .rename(columns={"value_usd": "us_exports_usd"}))
    if totals.empty:
        totals = (census_df[~census_df["is_total"]]
                  .groupby("month", as_index=False)["value_usd"].sum()
                  .rename(columns={"value_usd": "us_exports_usd"}))

    totals = totals.sort_values("month").reset_index(drop=True)
    totals["us_exports_ttm"] = totals["us_exports_usd"].rolling(12).sum()
    totals["us_exports_yoy"] = totals["us_exports_usd"].pct_change(12)

    # Destination mix, majors only.
    dest = census_df[~census_df["is_total"]].copy()
    dest["country"] = dest["country"].where(
        dest["cty_code"].isin(CENSUS_COUNTRIES), "Other")
    mix = (dest.groupby(["month", "country"])["value_usd"].sum()
           .unstack("country").fillna(0).sort_index())
    mix_share = mix.div(mix.sum(axis=1), axis=0)

    if not japan_df.empty:
        totals = totals.merge(
            japan_df.rename(columns={"value_usd": "jp_exports_usd"}),
            on="month", how="left")
        combined = totals["us_exports_usd"] + totals["jp_exports_usd"]
        totals["us_share_of_us_jp"] = totals["us_exports_usd"] / combined
        # Smoothed, because monthly shipment timing is lumpy.
        totals["us_share_3m"] = totals["us_share_of_us_jp"].rolling(3).mean()
        totals["us_share_12m"] = totals["us_share_of_us_jp"].rolling(12).mean()

    return totals, mix_share


# ----------------------------------------------------------------------------
# Cross-module analysis
# ----------------------------------------------------------------------------

def lead_lag(series_a, series_b, max_lag=6):
    """Correlate A against B at shifts of -max_lag..+max_lag months.

    Positive lag means series_a leads series_b.
    """
    rows = []
    joined = pd.concat([series_a.rename("a"), series_b.rename("b")], axis=1).dropna()
    if len(joined) < 18:
        return pd.DataFrame()

    for lag in range(-max_lag, max_lag + 1):
        shifted = joined["a"].shift(lag)
        pair = pd.concat([shifted, joined["b"]], axis=1).dropna()
        if len(pair) < 12:
            continue
        rows.append({
            "lag_months": lag,
            "correlation": pair.iloc[:, 0].corr(pair.iloc[:, 1]),
            "observations": len(pair),
        })

    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Charts (optional)
# ----------------------------------------------------------------------------

def make_charts(intensity, totals, mix_share):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed - skipping charts")
        return

    if not intensity.empty:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(intensity.index, intensity["test_per_foundry_3m"] * 100,
                label="3-month average")
        ax.plot(intensity.index, intensity["test_per_foundry_12m"] * 100,
                label="12-month average", linewidth=2)
        ax.set_title("Taiwan independent test-house revenue per NT$100 of foundry revenue")
        ax.set_ylabel("NT$")
        ax.legend()
        ax.grid(alpha=0.3)
        _thin_xticks(ax, intensity.index)
        fig.tight_layout()
        fig.savefig(os.path.join(OUTPUT_DIR, "test_intensity.png"), dpi=150)
        plt.close(fig)

        idx_cols = [c for c in ("test_index", "foundry_index", "osat_index")
                    if c in intensity.columns]
        if idx_cols:
            fig, ax = plt.subplots(figsize=(10, 5))
            for col in idx_cols:
                ax.plot(intensity.index, intensity[col],
                        label=col.replace("_index", "").title())
            ax.set_title("Taiwan revenue, indexed to first-year average = 100")
            ax.legend()
            ax.grid(alpha=0.3)
            _thin_xticks(ax, intensity.index)
            fig.tight_layout()
            fig.savefig(os.path.join(OUTPUT_DIR, "revenue_indexed.png"), dpi=150)
            plt.close(fig)

    if not totals.empty:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(totals["month"], totals["us_exports_ttm"] / 1e9)
        ax.set_title("US exports of HS 9030.82 (ATE), trailing twelve months")
        ax.set_ylabel("US$bn")
        ax.grid(alpha=0.3)
        _thin_xticks(ax, totals["month"])
        fig.tight_layout()
        fig.savefig(os.path.join(OUTPUT_DIR, "us_ate_exports.png"), dpi=150)
        plt.close(fig)

        if "us_share_12m" in totals.columns and totals["us_share_12m"].notna().any():
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(totals["month"], totals["us_share_12m"] * 100, linewidth=2)
            ax.set_title("US share of US + Japan ATE exports (12-month average)")
            ax.set_ylabel("%")
            ax.grid(alpha=0.3)
            _thin_xticks(ax, totals["month"])
            fig.tight_layout()
            fig.savefig(os.path.join(OUTPUT_DIR, "us_jp_share.png"), dpi=150)
            plt.close(fig)

    if not mix_share.empty:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.stackplot(range(len(mix_share)),
                     *[mix_share[c].values * 100 for c in mix_share.columns],
                     labels=list(mix_share.columns))
        ax.set_title("US ATE exports by destination (share of month)")
        ax.set_ylabel("%")
        ax.set_xlim(0, len(mix_share) - 1)
        ax.legend(loc="upper left", fontsize=8, ncol=3)
        step = max(1, len(mix_share) // 12)
        ax.set_xticks(range(0, len(mix_share), step))
        ax.set_xticklabels(list(mix_share.index)[::step], rotation=45, ha="right")
        fig.tight_layout()
        fig.savefig(os.path.join(OUTPUT_DIR, "export_destinations.png"), dpi=150)
        plt.close(fig)

    print("Charts written to %s/" % OUTPUT_DIR)


def _thin_xticks(ax, labels):
    labels = list(labels)
    step = max(1, len(labels) // 12)
    ax.set_xticks(range(0, len(labels), step))
    ax.set_xticklabels(labels[::step], rotation=45, ha="right")


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def summarise(intensity, totals, mix_share, corr):
    print("\n" + "=" * 74)
    print("SUMMARY")
    print("=" * 74)

    if not intensity.empty:
        latest = intensity.dropna(subset=["test_per_foundry"]).tail(1)
        if not latest.empty:
            month = latest.index[0]
            print("\nTest intensity proxy (Taiwan)")
            print("  Latest month observed          : %s" % month)
            print("  Test revenue per NT$100 foundry: %.2f"
                  % (latest["test_per_foundry"].iloc[0] * 100))

            early = intensity["test_per_foundry_12m"].dropna()
            if len(early) > 1:
                print("  12m average, first observation : %.2f" % (early.iloc[0] * 100))
                print("  12m average, latest            : %.2f" % (early.iloc[-1] * 100))
                change = (early.iloc[-1] / early.iloc[0] - 1) * 100
                print("  Change over the sample         : %+.1f%%" % change)

            if latest["test_yoy"].notna().iloc[0]:
                print("  Test houses, y/y               : %+.1f%%"
                      % (latest["test_yoy"].iloc[0] * 100))
                print("  Foundries, y/y                 : %+.1f%%"
                      % (latest["foundry_yoy"].iloc[0] * 100))
                print("  Spread                         : %+.1f pp"
                      % latest["growth_spread_pp"].iloc[0])

    if not totals.empty:
        latest = totals.dropna(subset=["us_exports_usd"]).tail(1)
        if not latest.empty:
            print("\nATE customs flows (HS 9030.82)")
            print("  Latest month observed          : %s" % latest["month"].iloc[0])
            if latest["us_exports_ttm"].notna().iloc[0]:
                print("  US exports, trailing 12m       : US$%.2fbn"
                      % (latest["us_exports_ttm"].iloc[0] / 1e9))
            if latest["us_exports_yoy"].notna().iloc[0]:
                print("  US exports, y/y                : %+.1f%%"
                      % (latest["us_exports_yoy"].iloc[0] * 100))
            if "us_share_12m" in latest.columns and latest["us_share_12m"].notna().iloc[0]:
                share = totals["us_share_12m"].dropna()
                print("  US share of US+JP, 12m avg     : %.1f%%" % (share.iloc[-1] * 100))
                print("  Same measure, start of sample  : %.1f%%" % (share.iloc[0] * 100))
                print("  Change                         : %+.1f pp"
                      % ((share.iloc[-1] - share.iloc[0]) * 100))
            else:
                print("  Japan side unavailable - rerun with --comtrade-key for share")

    if not mix_share.empty:
        recent = mix_share.tail(12).mean().sort_values(ascending=False)
        print("\nUS export destinations, last 12 months")
        for country, share in recent.head(6).items():
            print("  %-16s %5.1f%%" % (country, share * 100))

    if not corr.empty:
        best = corr.loc[corr["correlation"].abs().idxmax()]
        print("\nLead-lag, Taiwan test-house growth vs US ATE export growth")
        print("  Strongest correlation          : %.2f at %+d months"
              % (best["correlation"], int(best["lag_months"])))
        print("  (positive lag = Taiwan test revenue leads US shipments)")

    print("\nCSV outputs in %s/" % OUTPUT_DIR)
    print("=" * 74 + "\n")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test-intensity and ATE competitive-share tracker")
    parser.add_argument("--start", default="2019-01", help="first month, YYYY-MM")
    parser.add_argument("--end", default=default_end_month(), help="last month, YYYY-MM")
    parser.add_argument("--comtrade-key", default=os.environ.get("COMTRADE_KEY"),
                        help="UN Comtrade subscription key (optional but recommended)")
    parser.add_argument("--census-key", default=os.environ.get("CENSUS_KEY"),
                        help="US Census API key (free, only needed at volume)")
    parser.add_argument("--skip-taiwan", action="store_true")
    parser.add_argument("--skip-customs", action="store_true")
    parser.add_argument("--no-charts", action="store_true")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the local cache and refetch everything")
    args = parser.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Window: %s to %s\n" % (args.start, args.end))

    intensity = pd.DataFrame()
    totals = pd.DataFrame()
    mix_share = pd.DataFrame()
    corr = pd.DataFrame()

    # --- Module A ---------------------------------------------------------
    if not args.skip_taiwan:
        print("[A] Taiwan monthly revenue (MOPS)")
        taiwan = scrape_taiwan(args.start, args.end, refresh=args.refresh)
        if taiwan.empty:
            print("  no data returned - MOPS may be blocking or the URL scheme changed")
        else:
            taiwan.to_csv(os.path.join(OUTPUT_DIR, "taiwan_monthly_revenue.csv"),
                          index=False)
            intensity = build_intensity(taiwan)
            intensity.to_csv(os.path.join(OUTPUT_DIR, "test_intensity_proxy.csv"))
            print("  %d company-months captured" % len(taiwan))

    # --- Module B ---------------------------------------------------------
    if not args.skip_customs:
        print("\n[B] Customs flows, HS %s" % HS_CODE)
        census = scrape_census(args.start, args.end,
                               key=args.census_key, refresh=args.refresh)

        japan = pd.DataFrame()
        if not census.empty:
            japan = scrape_japan(args.start, args.end,
                                 key=args.comtrade_key, refresh=args.refresh)

        if census.empty:
            print("  no Census data returned")
        else:
            census.to_csv(os.path.join(OUTPUT_DIR, "us_ate_exports_detail.csv"),
                          index=False)
            totals, mix_share = build_export_view(census, japan)
            totals.to_csv(os.path.join(OUTPUT_DIR, "ate_export_totals.csv"), index=False)
            mix_share.to_csv(os.path.join(OUTPUT_DIR, "ate_export_destination_mix.csv"))
            print("  %d months of US export data" % len(totals))

    # --- Cross-module -----------------------------------------------------
    if not intensity.empty and not totals.empty:
        us_yoy = totals.set_index("month")["us_exports_yoy"]
        corr = lead_lag(intensity["test_yoy"], us_yoy)
        if not corr.empty:
            corr.to_csv(os.path.join(OUTPUT_DIR, "lead_lag_correlations.csv"),
                        index=False)

    if not args.no_charts:
        make_charts(intensity, totals, mix_share)

    summarise(intensity, totals, mix_share, corr)


if __name__ == "__main__":
    main()
