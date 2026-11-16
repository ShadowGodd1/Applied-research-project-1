# Nairobi rental market: asking rents, drivers and value

This repository studies what Nairobi landlords ask for residential rent, using
15,750 listings collected from two Kenyan portals. After cleaning, 11,645
monthly listings remain across 73 neighbourhoods. The work follows CRISP-DM:
profile the raw data, clean it, explore it, model it, then publish the results
as a rent guide and an interactive dashboard.

**Everything runs from one notebook.** `main.ipynb` contains the whole
pipeline inline, from the raw export to the figures, model metrics, rent guide
and the private data bundle used by the deployed dashboard. There are no
helper scripts to import.

## Results at a glance

| Measure | Value |
|---|---:|
| Listings analysed | 11,645 |
| Median asking rent | KSh 170,000 per month |
| Middle 50% of listings | KSh 100,000 to 350,000 |
| Neighbourhoods | 73 (31 with at least 30 listings) |
| Highest median | Thigiri, KSh 530,000 |
| Linear baseline | 31.1% MAPE, R² 0.602 |
| Boosted blend | 22.3% MAPE, R² 0.771 |
| Market segments | 2, stable across seeds |

The boosted blend is four small histogram gradient-boosted models. It uses the
same leakage-controlled split as the baseline plus cheap card-text signals,
with no grid search and no larger algorithm.

## Repository contents

```
main.ipynb               complete pipeline, executed end to end
dashboard/app.py         Streamlit dashboard
.streamlit/config.toml   dashboard theme
crawler/                 original collection code (reference only)
requirements.txt         dashboard deployment dependencies
requirements-notebook.txt  pinned analysis stack for the notebook
```

Not in the repository, by design:

- `data/`, which holds the raw export and the cleaned table.
- `reports/`, which holds generated tables, figures and documents.

Both are rebuilt by the notebook. The deployed dashboard reads one private zip
instead, as described below.

## Reproduce the analysis

Requirements: Python 3.12 and about eight minutes for a full run.

1. Install the pinned analysis stack:

   ```
   pip install -r requirements-notebook.txt
   ```

   The deployed dashboard uses the slimmer `requirements.txt`, so the notebook
   and the cloud app can pin different pandas versions.

2. Put the raw export at `data/raw/rentals.csv`. The optional SQLite cache at
   `data/raw/rentals.db` adds the database reconciliation and the Property24
   parser check.

3. Open `main.ipynb` and run every cell, top to bottom.

The notebook writes:

- `data/clean/listings_clean.csv`, the cleaning logs and the neighbourhood map;
- `reports/tables/`, `reports/figures/`, `reports/data_profile.md`,
  `reports/model_report.md` and `reports/rent_guide/`;
- `data/deploy/dashboard_bundle.zip`, the private file the deployed dashboard
  reads.

The closing audit cell checks the row counts, the price and size bounds, the
duplicate rule, the model comparison and the artefact counts, and fails loudly
if any of them no longer hold.

## Dashboard

The dashboard shows aggregate views only: median rent and the middle 50% by
neighbourhood, property type, bedroom count and size, plus segments, value
measures and model quality. No listing, URL or contact detail is displayed, and
every group below 30 listings is hidden.

Run it locally, after the notebook:

```
python -m streamlit run dashboard/app.py
```

Then open <http://localhost:8501>.

### Live app

Live dashboard: <https://nairobi-rent-lens.streamlit.app>

### Deploy it

The app is ready for Streamlit Community Cloud:

1. Push this repository to GitHub.
2. At <https://share.streamlit.io>, create an app from the repository, branch
   `main`, main file `dashboard/app.py`.
3. In the app settings, set **Python version** to **3.12**. Streamlit Cloud
   defaults to 3.14, where Altair 5.5 fails. `runtime.txt` is ignored by Cloud,
   so this has to be set in the dashboard.
4. In the app settings, open **Secrets** and add one entry:

   ```
   DATA_URL = "https://your-private-host/dashboard_bundle.zip"
   ```

5. Deploy. The app downloads the bundle once, caches it, and otherwise behaves
   exactly like the local version.

`DATA_URL` must be a direct download link, because the app reads the bytes
itself. Dropbox links need `?dl=1`, and S3 or Cloudflare R2 presigned links work
as they are. Do not commit the bundle: it contains the cleaned table, and the
repository stays free of listing data. If the secret is missing, the app falls
back to local files and then shows an explicit data-missing state.

## Data and privacy

- The raw export and SQLite cache are read-only and never modified.
- Raw and cleaned data are excluded from git.
- The dashboard never reads `url`, `title`, `raw_text` or location text, and
  the deployment bundle contains only the 22 aggregate columns the dashboard
  needs, plus five model tables.
- The notebook displays counts, medians, quartiles and model metrics only.

## Method summary

Cleaning applies nine ordered rules: scope, duplicate URLs and portal IDs,
re-posted cards, periods and currency, cross-source duplicates, plausibility
including the Property24 `Erf Size` artefact, neighbourhood normalisation,
flags, and feature construction. Every rule is logged at row level.

Modelling compares an interpretable linear baseline with a boosted blend, both
on the same held-out split with imputation fitted inside each training fold.
Two value measures use five-fold out-of-fold predictions only, and shilling
errors include Duan's smearing correction.

## Crawler

`crawler/` holds the original collection code and the parser coverage check. It
is included for provenance and is not needed to reproduce the analysis.

## Visual identity

One palette throughout: burnt orange `#CE5A31` as the single accent, near-black
`#1A1A1A` text, grey `#666666` hints, light grey `#CCCCCC` rules and a warm
`#F4EDE9` callout fill. Documents and figures use a serif stack; the dashboard
uses a UI sans-serif stack for screen readability.
