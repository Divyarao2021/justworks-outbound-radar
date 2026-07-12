"""
Justworks Outbound Radar - prototype
------------------------------------
Pulls RECENTLY FILED SEC Form D filings (private capital raises) LIVE from EDGAR,
filters out investment funds / SPVs to keep operating companies, then scores each
on Fit x Intent for a Justworks SDR team and suggests a first-touch play.

Live intent signals (all public, no API key, no scraping):
  1. Fresh Form D raise         -> SEC EDGAR full-text search API
  2. Issuer revenue range + round fully closed -> Form D XML (already fetched)
  3. Live hiring footprint       -> Greenhouse & Lever public job-board APIs
        - hiring across 2+ US states -> multi-state payroll play
        - hiring internationally     -> EOR play

Runs server-side (Streamlit) so we can set the SEC User-Agent header and avoid CORS.
"""

import re
import time
import datetime as dt
import xml.etree.ElementTree as ET

import requests
import pandas as pd
import streamlit as st

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
GH_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{token}?mode=json"
THIS_YEAR = dt.date.today().year

EXCLUDE_INDUSTRIES = {
    "Pooled Investment Fund", "Investing", "Investment Banking",
    "Commercial Banking", "Insurance", "Other Banking and Financial Services",
    "REITS and Finance",
}
STRONG_FIT_INDUSTRIES = {
    "Technology", "Computers", "Telecommunications", "Other Technology",
    "Business Services", "Health Care", "Biotechnology", "Pharmaceuticals",
    "Other Health Care", "Retailing", "Restaurants", "Manufacturing",
    "Tourism and Travel Services", "Other Travel",
}
SMALL_REVENUE = {"No Revenues", "$1 - $1,000,000", "$1,000,000 - $5,000,000"}

US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}
INTL_HINTS = {
    "united kingdom","uk","london","canada","toronto","vancouver","ireland",
    "dublin","germany","berlin","munich","france","paris","spain","madrid",
    "barcelona","netherlands","amsterdam","india","bangalore","bengaluru",
    "singapore","australia","sydney","brazil","sao paulo","mexico","poland",
    "portugal","lisbon","israel","tel aviv","emea","apac","latam","remote - eu",
    "remote - emea","remote - international","philippines","japan","tokyo",
}


# --- EDGAR -------------------------------------------------------------------
def _headers(email):
    return {"User-Agent": f"Justworks Outbound Radar prototype {email}",
            "Accept": "application/json"}


def search_recent_form_d(days_back, email, max_scan=60):
    end = dt.date.today()
    start = end - dt.timedelta(days=days_back)
    hits, frm = [], 0
    while len(hits) < max_scan:
        params = {"forms": "D", "dateRange": "custom", "startdt": start.isoformat(),
                  "enddt": end.isoformat(), "sort": "desc", "from": frm}
        r = requests.get(EFTS_URL, params=params, headers=_headers(email), timeout=20)
        r.raise_for_status()
        page = r.json().get("hits", {}).get("hits", [])
        if not page:
            break
        for h in page:
            src = h.get("_source", {})
            names = src.get("display_names", [""])
            name = names[0].split("(CIK")[0].strip() if names else "Unknown"
            ciks = src.get("ciks", [])
            hits.append({
                "name": name, "cik": ciks[0] if ciks else None,
                "accession": h.get("_id", "").split(":")[0],
                "file_date": src.get("file_date"),
                "states": src.get("biz_states") or src.get("inc_states") or [],
            })
        frm += len(page)
        time.sleep(0.15)
    return hits[:max_scan]


def _text(root, localname):
    for el in root.iter():
        if el.tag.split("}")[-1] == localname and el.text:
            return el.text.strip()
    return None


def _issuer_age(root):
    for el in root.iter():
        if el.tag.split("}")[-1] == "yearOfInc":
            kids = [c.tag.split("}")[-1] for c in el.iter()]
            within = ("withinFiveYears" in kids) or ("yetToBeFormed" in kids)
            year = None
            for c in el.iter():
                if c.tag.split("}")[-1] == "value" and c.text:
                    try:
                        year = int(c.text.strip())
                    except ValueError:
                        pass
            young = within or (year is not None and THIS_YEAR - year <= 5)
            return young, year
    return False, None


def _num(v):
    if v is None or str(v).strip().lower() in {"", "indefinite"}:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def fetch_form_d_details(cik, accession, email):
    url = f"{ARCHIVES}/{int(cik)}/{accession.replace('-', '')}/primary_doc.xml"
    out = {"industry": None, "state": None, "total_offering": None,
           "amount_sold": None, "revenue_range": None, "young": False, "year_inc": None}
    try:
        r = requests.get(url, headers=_headers(email), timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out["industry"] = _text(root, "industryGroupType")
        out["state"] = _text(root, "stateOrCountry")
        out["revenue_range"] = _text(root, "revenueRange")
        out["total_offering"] = _num(_text(root, "totalOfferingAmount"))
        out["amount_sold"] = _num(_text(root, "totalAmountSold"))
        out["young"], out["year_inc"] = _issuer_age(root)
    except Exception as e:
        out["error"] = str(e)
    return out


# --- Live hiring signal (Greenhouse + Lever public APIs) ---------------------
def slug_candidates(name):
    base = name.lower()
    base = re.sub(r"[,\.]", " ", base)
    base = re.sub(r"\b(inc|llc|l\.l\.c|corp|corporation|ltd|co|company|holdings|"
                  r"incorporated|group|the)\b", " ", base)
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", base).split() if w]
    if not words:
        return []
    cands = ["".join(words), "-".join(words), words[0]]
    if len(words) >= 2:
        cands.append("".join(words[:2]))
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:3]


def parse_locations(locs):
    us_states, intl = set(), False
    for loc in locs:
        if not loc:
            continue
        s = loc.strip()
        low = s.lower()
        if any(h in low for h in INTL_HINTS):
            intl = True
        for tok in re.split(r"[,/;|]", s):
            tok = tok.strip().upper()
            if tok in US_STATES:
                us_states.add(tok)
        if "remote" in low and ("us" in low or "united states" in low):
            us_states.add("US-REMOTE")
    return us_states, intl


def _gh_locations(jobs):
    out = []
    for j in jobs:
        loc = (j.get("location") or {}).get("name")
        if loc:
            out.append(loc)
        for off in j.get("offices", []) or []:
            if off.get("name"):
                out.append(off["name"])
    return out


def _lever_locations(posts):
    out = []
    for p in posts:
        cat = p.get("categories") or {}
        if cat.get("location"):
            out.append(cat["location"])
        for l in cat.get("allLocations", []) or []:
            out.append(l)
    return out


def fetch_hiring_signal(name, email):
    result = {"found": False, "source": None, "n_jobs": 0,
              "multistate": False, "international": False, "locations": ""}
    hdr = {"User-Agent": f"Justworks Outbound Radar prototype {email}"}
    for token in slug_candidates(name):
        # Greenhouse
        try:
            r = requests.get(GH_URL.format(token=token), headers=hdr, timeout=8)
            if r.ok:
                jobs = r.json().get("jobs", [])
                if jobs:
                    locs = _gh_locations(jobs)
                    states, intl = parse_locations(locs)
                    result.update(found=True, source=f"greenhouse:{token}",
                                  n_jobs=len(jobs),
                                  multistate=len([s for s in states if s != "US-REMOTE"]) >= 2,
                                  international=intl,
                                  locations="; ".join(sorted(set(locs))[:5]))
                    return result
        except Exception:
            pass
        # Lever
        try:
            r = requests.get(LEVER_URL.format(token=token), headers=hdr, timeout=8)
            if r.ok:
                posts = r.json()
                if isinstance(posts, list) and posts:
                    locs = _lever_locations(posts)
                    states, intl = parse_locations(locs)
                    result.update(found=True, source=f"lever:{token}",
                                  n_jobs=len(posts),
                                  multistate=len([s for s in states if s != "US-REMOTE"]) >= 2,
                                  international=intl,
                                  locations="; ".join(sorted(set(locs))[:5]))
                    return result
        except Exception:
            pass
        time.sleep(0.1)
    return result


# --- Scoring -----------------------------------------------------------------
def score_account(rec, target_states, sweet_low, sweet_high, w_fit, w_intent, hiring=None):
    industry = rec.get("industry") or ""
    offering = rec.get("total_offering")
    sold = rec.get("amount_sold")
    state = rec.get("state") or (rec.get("states")[0] if rec.get("states") else "")
    fit_items, intent_items = [], []  # (label, points)

    # ---- FIT (0-60) ----
    if industry in STRONG_FIT_INDUSTRIES:
        fit_items.append((f"{industry} operating company", 26))
    elif industry and industry not in EXCLUDE_INDUSTRIES:
        fit_items.append((f"{industry} (secondary-fit industry)", 12))
    if offering is not None:
        if sweet_low <= offering <= sweet_high:
            fit_items.append(("Raise in SMB sweet spot", 16))
        elif offering < sweet_low:
            fit_items.append(("Early / small raise", 8))
        else:
            fit_items.append(("Large raise (may be too big)", 4))
    if rec.get("revenue_range") in SMALL_REVENUE:
        fit_items.append((f"Issuer size: {rec['revenue_range']}", 8))
    if rec.get("young"):
        fit_items.append(("Young company (<=5y)", 4))
    if not target_states or state in target_states:
        fit_items.append((f"Geography{(' - ' + state) if state else ''}", 6))
    fit_sum = sum(p for _, p in fit_items)
    fit = min(fit_sum, 60)

    # ---- INTENT (0-40) ----
    fd = rec.get("file_date")
    if fd:
        age = (dt.date.today() - dt.date.fromisoformat(fd)).days
        pts = round(max(8.0, 26.0 - (age / 90.0) * 18.0), 1)
        intent_items.append((f"Raised {age}d ago (recency)", pts))
    if offering and sold and sold >= 0.95 * offering:
        intent_items.append(("Round fully closed (capital in hand)", 6))
    if hiring and hiring.get("found"):
        if hiring.get("multistate"):
            intent_items.append(("Hiring across multiple states", 8))
        if hiring.get("international"):
            intent_items.append(("Hiring internationally", 8))
        if not hiring.get("multistate") and not hiring.get("international") and hiring.get("n_jobs"):
            intent_items.append((f"Actively hiring ({hiring['n_jobs']} roles)", 3))
    intent_sum = sum(p for _, p in intent_items)
    intent = min(intent_sum, 40)

    fit_weighted = round((fit / 60.0) * w_fit, 1)
    intent_weighted = round((intent / 40.0) * w_intent, 1)
    total = round(fit_weighted + intent_weighted, 1)
    tier = "A - strike now" if total >= 70 else "B - nurture" if total >= 45 else "C - deprioritize"
    why = ", ".join(lbl for lbl, _ in (fit_items + intent_items))
    breakdown = {"fit_items": fit_items, "intent_items": intent_items,
                 "fit": fit, "intent": intent, "fit_sum": fit_sum, "intent_sum": intent_sum,
                 "fit_weighted": fit_weighted, "intent_weighted": intent_weighted,
                 "w_fit": w_fit, "w_intent": w_intent, "total": total}
    return {"fit": round(fit, 1), "intent": round(intent, 1), "score": total,
            "tier": tier, "why": why, "play": _play(industry, offering, hiring),
            "breakdown": breakdown}


def _play(industry, offering, hiring):
    if hiring and hiring.get("international"):
        return "Hiring abroad -> lead with EOR: hire full-time employees overseas with no foreign entity."
    if hiring and hiring.get("multistate"):
        return "Hiring across multiple states -> multi-state payroll + tax compliance + workers' comp."
    if industry in {"Technology", "Computers", "Other Technology", "Telecommunications"}:
        return "Just-raised tech co scaling headcount -> benefits-to-win-talent + multi-state payroll."
    if industry in {"Health Care", "Biotechnology", "Pharmaceuticals", "Other Health Care"}:
        return "Funded health/bio co hiring -> compliance + premium benefits for clinical/technical talent."
    if industry in {"Restaurants", "Retailing"}:
        return "Multi-location hiring -> multi-state payroll + workers' comp + benefits admin."
    if offering and offering >= 20_000_000:
        return "Larger raise -> likely hiring internationally; open with the EOR angle."
    return "New raise -> 'congrats on the round; here's how peers your size set up payroll/benefits/compliance.'"


# --- Scorecard panel ---------------------------------------------------------
def render_scorecard(row):
    b = row.get("breakdown") or {}
    st.markdown(f"#### {row.get('name', 'Account')}")
    m = st.columns(4)
    m[0].metric("Score", row.get("score", 0))
    m[1].metric("Tier", str(row.get("tier", "-")).split(" - ")[0])
    m[2].metric("Fit", f"{b.get('fit', 0):.0f} / 60")
    m[3].metric("Intent", f"{b.get('intent', 0):.0f} / 40")

    col_fit, col_int = st.columns(2)
    with col_fit:
        st.markdown(f"**Fit — {b.get('fit', 0):.0f} / 60**")
        st.progress(min(b.get("fit", 0) / 60, 1.0))
        for lbl, pts in b.get("fit_items", []):
            st.write(f"`+{pts}`  {lbl}")
        if not b.get("fit_items"):
            st.caption("No fit points.")
    with col_int:
        st.markdown(f"**Intent — {b.get('intent', 0):.0f} / 40**")
        st.progress(min(b.get("intent", 0) / 40, 1.0))
        for lbl, pts in b.get("intent_items", []):
            st.write(f"`+{pts}`  {lbl}")
        if b.get("intent_sum", 0) > 40:
            st.caption(f"Raw {b['intent_sum']:.0f} capped at 40.")
        if not b.get("intent_items"):
            st.caption("No intent points.")

    st.markdown(
        f"**Weighted total:**  Fit {b.get('fit', 0):.0f}/60 x {b.get('w_fit', 0)}% = "
        f"{b.get('fit_weighted', 0)}  +  Intent {b.get('intent', 0):.0f}/40 x "
        f"{b.get('w_intent', 0)}% = {b.get('intent_weighted', 0)}  =  **{b.get('total', 0)}**")
    st.success(f"Suggested play: {row.get('play', '-')}")


# --- UI ----------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Justworks Outbound Radar", page_icon="R", layout="wide")
    st.title("Justworks Outbound Radar")
    st.caption("Live recently-funded companies from SEC Form D filings, enriched with live hiring "
               "signals, scored on Fit x Intent for SDR prioritization. Prototype - public data only.")

    with st.sidebar:
        st.header("Controls")
        email = st.text_input("Your contact email (SEC User-Agent)", "you@example.com")
        days = st.slider("Look back (days)", 1, 30, 7)
        scan = st.slider("Max filings to scan", 20, 100, 40, step=10)
        detail = st.slider("Fetch Form D details for top N", 10, 60, 25, step=5)
        st.markdown("**Live hiring signal (best-effort)**")
        do_hiring = st.checkbox("Enrich top accounts with live hiring lookup", value=True)
        hire_n = st.slider("Hiring lookup for top N", 0, 25, 12,
                           help="Guesses each company's Greenhouse/Lever board token. Hits for many startups, misses for the rest.")
        states = st.multiselect("Target states (blank = all US)",
                                ["NY", "CA", "TX", "MA", "WA", "IL", "CO", "FL", "GA", "PA", "NJ"])
        st.markdown("**Sweet-spot raise ($)**")
        sweet_low = st.number_input("Min", value=500_000, step=250_000)
        sweet_high = st.number_input("Max", value=20_000_000, step=1_000_000)
        w_fit = st.slider("Fit weight", 0, 100, 55)
        w_intent = 100 - w_fit
        st.caption(f"Intent weight: {w_intent}")
        run = st.button("Pull live filings", type="primary")

    if not run:
        st.info("Set controls and click **Pull live filings**. The app queries SEC EDGAR live, "
                "drops investment funds/SPVs, enriches with live hiring signals, and ranks real "
                "operating companies as Justworks targets.")
        return

    with st.status("Querying SEC EDGAR live...", expanded=False) as status:
        try:
            raw = search_recent_form_d(days, email, max_scan=scan)
        except Exception as e:
            st.error(f"EDGAR search failed: {e}")
            return
        status.update(label=f"Found {len(raw)} filings. Fetching Form D details...")
        rows = []
        for rec in raw[:detail]:
            if not rec.get("cik") or not rec.get("accession"):
                continue
            rec.update(fetch_form_d_details(rec["cik"], rec["accession"], email))
            time.sleep(0.15)
            if (rec.get("industry") or "") in EXCLUDE_INDUSTRIES:
                continue
            rec.update(score_account(rec, states, sweet_low, sweet_high, w_fit, w_intent))
            rows.append(rec)

        rows.sort(key=lambda r: r["score"], reverse=True)

        if do_hiring and hire_n:
            status.update(label=f"Checking live hiring signals for top {hire_n}...")
            for rec in rows[:hire_n]:
                hiring = fetch_hiring_signal(rec["name"], email)
                rec["hiring_found"] = hiring["found"]
                rec["hiring_jobs"] = hiring["n_jobs"]
                rec["multistate"] = hiring["multistate"]
                rec["international"] = hiring["international"]
                rec.update(score_account(rec, states, sweet_low, sweet_high, w_fit, w_intent, hiring))
            rows.sort(key=lambda r: r["score"], reverse=True)
        status.update(label="Done", state="complete")

    if not rows:
        st.warning("No operating companies matched after filtering. Widen the window or clear filters.")
        return

    df = pd.DataFrame(rows)
    df["EDGAR"] = df.apply(
        lambda r: f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={r['cik']}&type=D", axis=1)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Operating companies", len(df))
    c2.metric("A-tier (strike now)", int(df["tier"].str.startswith("A").sum()))
    c3.metric("Hiring multi-state", int(df.get("multistate", pd.Series(dtype=bool)).sum()))
    c4.metric("Hiring international", int(df.get("international", pd.Series(dtype=bool)).sum()))

    cols = ["name", "score", "tier", "industry", "total_offering", "revenue_range", "state",
            "file_date", "hiring_jobs", "multistate", "international", "why", "play", "EDGAR"]
    cols = [c for c in cols if c in df.columns]
    show = df[cols].rename(columns={
        "name": "Company", "score": "Score", "tier": "Tier", "industry": "Industry",
        "total_offering": "Raise ($)", "revenue_range": "Issuer revenue", "state": "State",
        "file_date": "Filed", "hiring_jobs": "Open roles", "multistate": "Multi-state hiring",
        "international": "Intl hiring", "why": "Why", "play": "Suggested play"})
    st.dataframe(show, use_container_width=True, hide_index=True,
                 column_config={"EDGAR": st.column_config.LinkColumn("EDGAR", display_text="filing")})

    st.subheader("Score breakdown")
    st.caption("Pick any account to see exactly how its Fit and Intent points add up.")
    idx = st.selectbox("Inspect an account", range(len(df)),
                       format_func=lambda i: f"{df.iloc[i]['name']}  (score {df.iloc[i]['score']})")
    render_scorecard(df.iloc[idx].to_dict())

    st.subheader("Tier breakdown")
    st.bar_chart(df["tier"].value_counts())
    st.download_button("Download ranked list (CSV)", show.to_csv(index=False).encode("utf-8"),
                       "justworks_outbound_radar.csv", "text/csv")


if __name__ == "__main__":
    main()
