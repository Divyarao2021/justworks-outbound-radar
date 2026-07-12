# Justworks Outbound Radar (prototype)

Pulls **recently filed SEC Form D filings** (private raises) **live** from EDGAR,
drops investment funds / SPVs to keep real operating companies, enriches them with
**live hiring signals**, and scores each on **Fit x Intent** for a Justworks SDR
team - with a suggested first-touch play and a CSV export.

## Live signals (all public, no API key, no scraping)
1. **Fresh raise** - SEC EDGAR full-text search API (filings searchable ~60s after filing).
2. **Issuer size + round closed** - parsed from the Form D XML we already fetch
   (revenue range = SMB fit; fully-sold round = capital in hand now).
3. **Hiring footprint** - Greenhouse & Lever public job-board APIs:
   - hiring across 2+ US states -> **multi-state payroll** play
   - hiring internationally -> **EOR** play

   *Best-effort:* there's no way to list ATS customers, so the app guesses each
   company's board token from its name. It hits for many startups and misses for
   the rest; a miss just means no hiring signal, not a broken run.

## Why it runs server-side
SEC requires a User-Agent header and a browser-only site would hit CORS. Streamlit
fetches server-side, so both problems disappear.

## Run locally first (2 minutes)
```bash
pip install -r requirements.txt
streamlit run app.py
```
Put your real email in the sidebar (goes into the SEC User-Agent), click **Pull live filings**.

## Deploy free + shareable
1. Public GitHub repo with `app.py` + `requirements.txt`.
2. share.streamlit.io -> New app -> point at the repo + `app.py`.
3. Public `https://<name>.streamlit.app` URL; every git push auto-redeploys.

(Alt host: Hugging Face Spaces -> new Space -> SDK "Streamlit" -> upload both files.)

## How the score works
- **Fit (0-60):** operating-company industry (funds excluded) + raise in the SMB
  sweet spot + small issuer revenue + young company + target geography.
- **Intent (0-40):** recency of raise + round fully closed + multi-state/international hiring.
- **Tier:** A (strike now) / B (nurture) / C (deprioritize). Weighting adjustable live.

## Roadmap (what I'd layer next)
- First-HR-hire and leadership-change signals (job-title + news feeds).
- In production this logic operationalizes in Clay / 6sense feeding Salesforce, not a rebuild.
