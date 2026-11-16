"""
Nairobi rental market dashboard.

Run locally:

    python -m streamlit run dashboard/app.py

Deployed, the app reads one private zip from the `DATA_URL` secret (built by
the last cell of `main.ipynb`), so no listing data sits in the repository.

Design follows a product-dashboard blueprint: page header with scope, a
single dominant KPI, charts before tables, one card family, restrained accent
use, explicit empty/error states and bounded chart heights. Visual identity:
burnt orange accent, near-black text, grey hints, light rules, warm callout
fill, a UI sans-serif body with bold labels and italic hints.

Privacy rules enforced in this file:
  * only aggregate columns are loaded - url, title, raw_text and location
    text are never read into memory for display purposes;
  * no individual listing, contact detail or URL is ever shown;
  * every group below MIN_N listings is hidden (default 30);
  * the raw database and raw export are never opened.
"""

from __future__ import annotations

import html
import io
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
CLEAN = ROOT / "data" / "clean" / "listings_clean.csv"
TABLES = ROOT / "reports" / "tables"
MIN_N = 30

# --------------------------------------------------------------------------
# Visual identity
# --------------------------------------------------------------------------
PRIMARY = "#CE5A31"
PRIMARY_DARK = "#A34420"
TEXT = "#1A1A1A"
MUTED = "#666666"
FAINT = "#8C8C8C"
RULE = "#CCCCCC"
CALLOUT = "#F4EDE9"
UI_FONT = ('Inter, "Segoe UI Variable", "Segoe UI", system-ui, '
           '-apple-system, "Helvetica Neue", Arial, sans-serif')

BED_ORDER = ["0 (studio/bedsitter)", "1", "2", "3", "4", "5+"]
PRICE_ORDER = ["<25k", "25-50k", "50-100k", "100-200k", "200-500k", "500k+"]
SIZE_ORDER = ["<50", "50-100", "100-200", "200-400", "400+"]
AFFORDABLE = ["<25k", "25-50k"]

KIND_LABELS = {"apartment": "Apartment", "house": "House",
               "townhouse": "Townhouse", "bedsitter": "Bedsitter"}

SAFE_COLS = [
    "source", "price_kes", "bedrooms", "bathrooms", "parking", "size_sqm",
    "kind", "kind_group", "neighborhood", "bedroom_band", "price_band",
    "size_band", "ptype", "flag_price_luxury", "flag_short_stay",
    "flag_size_missing", "flag_parking_missing", "is_satellite_town",
    "is_furnished", "has_dsq", "has_pool", "has_gym",
]

st.set_page_config(page_title="Nairobi rent dashboard", layout="wide",
                   initial_sidebar_state="expanded")

# --------------------------------------------------------------------------
# Data source: local files, or the private bundle on a deployed app
# --------------------------------------------------------------------------

def _secret_data_url() -> str:
    """Read DATA_URL from Streamlit secrets; empty when not configured."""
    try:
        return str(st.secrets.get("DATA_URL", "") or "").strip()
    except Exception:
        return ""


@st.cache_data(show_spinner=False)
def download_bundle(url: str) -> Path:
    """Download the private dashboard zip and unpack it to a temp folder."""
    with urllib.request.urlopen(url, timeout=120) as response:
        payload = response.read()
    folder = Path(tempfile.mkdtemp(prefix="nairobi_dashboard_"))
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        bundle.extractall(folder)
    return folder


DATA_URL = _secret_data_url()
DOWNLOAD_ERROR = ""
try:
    REMOTE = download_bundle(DATA_URL) if DATA_URL else None
except Exception as exc:              # keep the app usable, show the reason
    REMOTE = None
    DOWNLOAD_ERROR = str(exc)

LISTINGS = (REMOTE / "listings_clean.csv") if REMOTE else CLEAN
TABLE_DIR = (REMOTE / "tables") if REMOTE else TABLES

# --------------------------------------------------------------------------
# Design system
# --------------------------------------------------------------------------
st.markdown(
    f"""
    <style>
    :root {{
        --brand: {PRIMARY};
        --brand-dark: {PRIMARY_DARK};
        --ink: {TEXT};
        --muted: {MUTED};
        --faint: {FAINT};
        --rule: {RULE};
        --callout: {CALLOUT};
        --ui-font: {UI_FONT};
    }}

    /* canvas and type */
    html, body, .stApp, [data-testid="stAppViewContainer"] {{
        font-family: var(--ui-font);
        color: var(--ink);
        background: #FFFFFF;
    }}
    /* enforce the UI font on real text, including widget internals,
       while leaving icon glyphs and code on their own fonts */
    .stApp :not(code):not(pre):not(kbd):not(samp):not(textarea)
        :not([data-testid="stIconMaterial"]):not([class*="material-symbols"]),
    .stApp label, .stApp input, .stApp textarea, .stApp button,
    .stApp [role="tab"], .stApp [role="option"], .stApp [role="button"],
    .stApp [data-baseweb="select"] *, .stApp [data-baseweb="menu"] *,
    .stApp [data-baseweb="popover"] *, .stApp [data-baseweb="tag"] *,
    .stApp [data-testid="stMarkdownContainer"] *,
    .stApp [data-testid="stCaptionContainer"] *,
    .stApp [data-testid="stWidgetLabel"] *,
    .stApp [data-testid="stMetricLabel"],
    .stApp [data-testid="stMetricValue"] {{
        font-family: var(--ui-font) !important;
    }}
    [data-testid="stIconMaterial"], [class*="material-symbols"] {{
        font-family: "Material Symbols Rounded", "Material Icons" !important;
    }}
    code, pre, kbd, samp {{
        font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    }}
    [data-testid="stAppViewContainer"] > .main .block-container {{
        padding-top: 2.25rem;
        padding-bottom: 3rem;
        max-width: 1180px;
    }}
    h1, h2, h3, h4 {{ font-family: var(--ui-font); color: var(--ink); }}
    h1 {{ font-size: 2.25rem; font-weight: 700; line-height: 1.1;
          letter-spacing: -0.01em; margin: 0 0 .35rem 0; }}
    h2 {{ font-size: 1.5rem; font-weight: 600; line-height: 1.2;
          margin: 1.6rem 0 .5rem 0; }}
    h3 {{ font-size: 1.125rem; font-weight: 600; line-height: 1.25; }}
    p, li {{ line-height: 1.55; }}

    /* product chrome: no Streamlit header, footer or menu */
    [data-testid="stHeader"], [data-testid="stToolbar"], footer,
    #MainMenu {{ display: none !important; }}

    /* page header block */
    .eyebrow {{
        font-size: .75rem; font-weight: 700; letter-spacing: .1em;
        text-transform: uppercase; color: var(--muted); margin-bottom: .5rem;
    }}
    .lede {{ font-size: 1rem; color: var(--muted); max-width: 68ch;
             margin: 0 0 1rem 0; }}
    .scope-row {{ display: flex; flex-wrap: wrap; gap: .5rem;
                  margin: .25rem 0 0 0; }}
    .chip {{
        border: 1px solid var(--rule); border-radius: 999px;
        padding: .3rem .7rem; font-size: .8rem; color: var(--muted);
        background: #FFFFFF;
    }}
    .chip b {{ color: var(--ink); font-weight: 700; }}

    /* KPI band: one dominant number, the rest quiet */
    .kpi-hero {{
        border-top: 3px solid var(--brand); padding-top: .85rem;
    }}
    .kpi-hero .value {{
        font-size: 2.75rem; font-weight: 700; line-height: 1;
        color: var(--brand); font-variant-numeric: tabular-nums;
    }}
    .kpi-hero .label {{
        font-size: .75rem; font-weight: 700; letter-spacing: .08em;
        text-transform: uppercase; color: var(--muted); margin-bottom: .4rem;
    }}
    .kpi-hero .sub {{ font-size: .85rem; color: var(--muted);
                      margin-top: .35rem; }}
    .kpi {{
        border-left: 1px solid var(--rule); padding: .1rem 0 .1rem 1rem;
    }}
    .kpi .value {{
        font-size: 1.6rem; font-weight: 600; line-height: 1.1;
        color: var(--ink); font-variant-numeric: tabular-nums;
    }}
    .kpi .label {{
        font-size: .75rem; font-weight: 700; letter-spacing: .08em;
        text-transform: uppercase; color: var(--muted); margin-bottom: .35rem;
    }}
    .kpi .sub {{ font-size: .8rem; color: var(--muted); }}

    /* cards: one family */
    div[data-testid="stVerticalBlockBorderWrapper"] {{
        border: 1px solid var(--rule) !important;
        border-radius: .625rem !important;
        background: #FFFFFF;
        padding: .2rem .4rem;
    }}
    div[data-testid="stVerticalBlockBorderWrapper"]:hover {{
        border-color: #B8B8B8 !important;
    }}

    /* callouts and hints */
    .callout {{
        background: var(--callout); border-left: 4px solid var(--brand);
        padding: .85rem 1rem; margin: .6rem 0 1rem 0; font-size: .95rem;
        color: var(--ink);
    }}
    .callout b {{ font-weight: 700; }}
    .hint {{ color: var(--muted); font-style: italic; font-size: .85rem; }}
    .rule {{ border-top: 1px solid var(--rule); margin: 1.4rem 0 1rem 0; }}

    /* tabs */
    .stTabs [data-baseweb="tab-list"] {{
        gap: 1.4rem; border-bottom: 1px solid var(--rule);
    }}
    .stTabs [data-baseweb="tab"] {{
        font-size: .95rem; font-weight: 600; color: var(--muted);
        padding: .35rem 0 .55rem 0; background: transparent;
    }}
    .stTabs [aria-selected="true"] {{
        color: var(--ink); border-bottom: 2px solid var(--brand);
    }}
    .stTabs [data-baseweb="tab-highlight"],
    .stTabs [data-baseweb="tab-border"] {{ display: none; }}

    /* sidebar */
    section[data-testid="stSidebar"] {{
        border-right: 1px solid var(--rule); background: #FFFFFF;
    }}
    section[data-testid="stSidebar"] .block-container {{ padding-top: 1.6rem; }}
    section[data-testid="stSidebar"] h2 {{
        font-size: .75rem; text-transform: uppercase; letter-spacing: .1em;
        color: var(--muted); font-weight: 700; margin-bottom: .4rem;
    }}
    section[data-testid="stSidebar"] label {{
        font-size: .85rem; font-weight: 600; color: var(--ink);
    }}

    /* controls */
    .stButton > button {{
        font-family: var(--ui-font); font-weight: 600; font-size: .9rem;
        border: 1px solid var(--rule); border-radius: .5rem;
        color: var(--ink); background: #FFFFFF; padding: .45rem .9rem;
    }}
    .stButton > button:hover {{
        border-color: var(--brand); color: var(--brand);
    }}
    div[data-baseweb="select"] > div, .stMultiSelect [data-baseweb="input"] {{
        border-color: var(--rule); border-radius: .5rem;
    }}
    :focus-visible {{ outline: 2px solid var(--brand); outline-offset: 2px; }}

    /* tables: rendered as HTML so the UI font applies everywhere */
    .table-wrap {{ margin: .4rem 0 .2rem 0; overflow-x: auto; }}
    .table-caption {{
        font-size: .75rem; font-weight: 700; letter-spacing: .08em;
        text-transform: uppercase; color: var(--muted); margin-bottom: .45rem;
    }}
    table.data-table {{
        width: 100%; border-collapse: collapse; font-size: .875rem;
    }}
    table.data-table thead th {{
        font-size: .7rem; font-weight: 700; letter-spacing: .08em;
        text-transform: uppercase; color: var(--muted); text-align: left;
        padding: .5rem .6rem; border-bottom: 1px solid var(--rule);
        white-space: nowrap;
    }}
    table.data-table tbody td {{
        padding: .5rem .6rem; color: var(--ink);
        border-bottom: 1px solid #EDEDED;
        font-variant-numeric: tabular-nums;
    }}
    table.data-table tbody tr:last-child td {{ border-bottom: none; }}
    table.data-table tbody tr:hover td {{ background: #FBF8F6; }}
    table.data-table .num {{ text-align: right; }}

    /* Streamlit's canvas grid, if ever used, should still look neutral */
    div[data-testid="stDataFrame"] {{
        border: 1px solid var(--rule); border-radius: .5rem;
    }}
    [data-testid="stMetricLabel"] {{ color: var(--muted); font-weight: 700; }}
    </style>
    """,
    unsafe_allow_html=True,
)


def callout(body: str) -> None:
    st.markdown(f'<div class="callout">{body}</div>', unsafe_allow_html=True)


def hint(body: str) -> None:
    st.markdown(f'<div class="hint">{body}</div>', unsafe_allow_html=True)


def place(v: str) -> str:
    return " ".join(w.capitalize() for w in str(v).split())


def ksh(v: float) -> str:
    return f"{v:,.0f}"


def _fmt_cell(value) -> str:
    """Format a table cell: thousands separators, no trailing .0."""
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value).is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return html.escape(str(value))


def table(frame: pd.DataFrame, caption: str | None = None,
          numeric: list[str] | None = None) -> None:
    """Render an HTML table so the UI font applies to every cell.

    Streamlit's st.dataframe paints text on a canvas with its own font, which
    cannot be styled; these aggregate tables are small, so HTML is both
    lighter and fully on-brand.
    """
    if frame.empty:
        st.info("No rows to show for this scope.")
        return
    numeric = set(numeric or [c for c in frame.columns
                              if pd.api.types.is_numeric_dtype(frame[c])])
    head = "".join(
        f'<th class="{"num" if c in numeric else ""}">'
        f'{html.escape(str(c))}</th>'
        for c in frame.columns)
    body = []
    for _, row in frame.iterrows():
        cells = "".join(
            f'<td class="{"num" if c in numeric else ""}">'
            f'{_fmt_cell(row[c])}</td>'
            for c in frame.columns)
        body.append(f"<tr>{cells}</tr>")
    cap = f'<div class="table-caption">{html.escape(caption)}</div>' \
        if caption else ""
    st.markdown(
        f'<div class="table-wrap">{cap}<table class="data-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody>"
        f"</table></div>",
        unsafe_allow_html=True)


def style_chart(chart: alt.Chart, height: int | None = None) -> alt.Chart:
    """Project fonts, quiet grid and bounded height for an Altair chart."""
    if height is not None:
        chart = chart.properties(height=height)
    return (chart
            .configure_axis(gridColor=RULE, domainColor=RULE, tickColor=RULE,
                            labelColor=TEXT, titleColor=TEXT,
                            labelFont=UI_FONT, titleFont=UI_FONT,
                            labelFontSize=11, titleFontSize=12,
                            titleFontWeight="normal")
            .configure_view(strokeWidth=0)
            .configure_title(font=UI_FONT, color=TEXT, fontSize=14,
                             fontWeight="bold", anchor="start")
            .configure_legend(labelFont=UI_FONT, titleFont=UI_FONT,
                              labelColor=TEXT, titleColor=MUTED,
                              symbolSize=140))


# --------------------------------------------------------------------------
# Data loading with explicit error state
# --------------------------------------------------------------------------

@st.cache_data
def load_listings() -> pd.DataFrame:
    return pd.read_csv(LISTINGS, usecols=lambda c: c in SAFE_COLS,
                       low_memory=False)


@st.cache_data
def load_table(name: str) -> pd.DataFrame:
    return pd.read_csv(TABLE_DIR / f"{name}.csv")


def missing_data_state() -> None:
    st.markdown('<div class="eyebrow">Nairobi rental market</div>',
                unsafe_allow_html=True)
    st.title("Dashboard data is missing")
    if DOWNLOAD_ERROR:
        st.error("The private data bundle could not be downloaded.")
        st.code(DOWNLOAD_ERROR)
    else:
        st.error("The cleaned dataset or the model tables were not found.")
    st.markdown(
        "Locally, run `main.ipynb` top to bottom and reload this page. On a "
        "deployed app, set the `DATA_URL` secret to a direct download link "
        "for `data/deploy/dashboard_bundle.zip`.")
    st.stop()


if not LISTINGS.exists() or not TABLE_DIR.exists():
    missing_data_state()

df = load_listings()
if df.empty:
    missing_data_state()


# --------------------------------------------------------------------------
# Aggregate helpers - groups below MIN_N never leave this function
# --------------------------------------------------------------------------

def aggregate(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=by + ["n", "median", "p25", "p75"])
    g = (df.groupby(by, dropna=False)["price_kes"]
           .agg(n="size", median="median",
                p25=lambda s: s.quantile(0.25),
                p75=lambda s: s.quantile(0.75))
           .reset_index())
    return g[g["n"] >= MIN_N].sort_values("median", ascending=False)


def suppressed_note(df: pd.DataFrame, by: list[str]) -> None:
    total = df.groupby(by, dropna=False).ngroup().nunique()
    shown = len(aggregate(df, by))
    hint(f"{shown} groups shown, {max(total - shown, 0)} hidden below the "
         f"minimum of {MIN_N} listings.")


def iqr_chart(data: pd.DataFrame, y: str, label_col: str | None = None,
              title: str = "", height: int = 380) -> alt.Chart:
    src = data.copy()
    if label_col:
        src["_label"] = src[label_col].map(place)
        y_field = "_label"
    else:
        y_field = y
    bars = alt.Chart(src).mark_bar(color=PRIMARY, size=16).encode(
        y=alt.Y(f"{y_field}:N", sort="-x", title=""),
        x=alt.X("median:Q", title="Monthly asking rent (KSh)"),
        tooltip=[alt.Tooltip(f"{y_field}:N", title="group"),
                 alt.Tooltip("n", title="listings", format=","),
                 alt.Tooltip("median", title="median", format=",.0f"),
                 alt.Tooltip("p25", title="p25", format=",.0f"),
                 alt.Tooltip("p75", title="p75", format=",.0f")],
    )
    err = alt.Chart(src).mark_errorbar(color=MUTED, thickness=1.4).encode(
        y=alt.Y(f"{y_field}:N", sort="-x"),
        x=alt.X("p25:Q"),
        x2=alt.X2("p75:Q"),
    )
    return style_chart((bars + err).properties(title=title), height=height)


# --------------------------------------------------------------------------
# Filter state and sidebar
# --------------------------------------------------------------------------
DEFAULTS = {
    "f_kinds": sorted(df["kind_group"].dropna().unique()),
    "f_beds": [b for b in BED_ORDER if b in set(df["bedroom_band"])],
    "f_price": PRICE_ORDER,
    "f_source": sorted(df["source"].unique()),
    "f_nb": [],
    "f_satellite": True,
}
for key, value in DEFAULTS.items():
    st.session_state.setdefault(key, value)


def reset_filters() -> None:
    """Widget callback: runs before the next script run, so keys are free.

    Assigning to a session-state key after its widget has been created raises
    StreamlitAPIException, so the reset must happen in a callback rather than
    in the button's own run.
    """
    for key, value in DEFAULTS.items():
        st.session_state[key] = value


with st.sidebar:
    st.markdown("## Scope")
    st.multiselect("Property type", DEFAULTS["f_kinds"], key="f_kinds",
                   format_func=lambda k: KIND_LABELS.get(k, place(k)))
    st.multiselect("Bedrooms", DEFAULTS["f_beds"], key="f_beds")
    st.multiselect("Rent band (KSh/month)", PRICE_ORDER, key="f_price")
    st.multiselect("Source", DEFAULTS["f_source"], key="f_source")
    st.multiselect("Neighbourhood", sorted(df["neighborhood"].unique()),
                   key="f_nb", format_func=place)
    st.checkbox("Include satellite towns", key="f_satellite")

    st.markdown('<div class="rule"></div>', unsafe_allow_html=True)
    st.button("Reset filters", on_click=reset_filters,
              use_container_width=True)

    hint(f"Groups below {MIN_N} listings are hidden everywhere, including "
         f"charts and tables.")

view = df[
    df["kind_group"].isin(st.session_state["f_kinds"])
    & df["bedroom_band"].isin(st.session_state["f_beds"])
    & df["price_band"].isin(st.session_state["f_price"])
    & df["source"].isin(st.session_state["f_source"])
]
if not st.session_state["f_satellite"]:
    view = view[~view["is_satellite_town"].astype(bool)]
if st.session_state["f_nb"]:
    view = view[view["neighborhood"].isin(st.session_state["f_nb"])]

# --------------------------------------------------------------------------
# Page header
# --------------------------------------------------------------------------
st.markdown('<div class="eyebrow">Nairobi rental market, October 2026</div>',
            unsafe_allow_html=True)
st.title("Asking rents across Nairobi")
st.markdown(
    '<p class="lede">What apartments and houses are advertised for, by '
    'neighbourhood, property type and size. Aggregates only - no listings, '
    'contacts or URLs - and every group below 30 listings is hidden.</p>',
    unsafe_allow_html=True)

st.markdown(
    f'<div class="scope-row">'
    f'<span class="chip"><b>{len(view):,}</b> listings in scope</span>'
    f'<span class="chip"><b>{view["neighborhood"].nunique()}</b> areas</span>'
    f'<span class="chip"><b>{len(df):,}</b> in the full cleaned sample</span>'
    f'</div>',
    unsafe_allow_html=True)

if len(view) < MIN_N:
    st.warning(f"Only {len(view)} listings match this scope. Narrowing "
               f"further would hide every group, so nothing is shown.")
    st.button("Reset filters", on_click=reset_filters)
    st.stop()

st.markdown('<div class="rule"></div>', unsafe_allow_html=True)

# --------------------------------------------------------------------------
# KPI band: median rent is the one dominant signal
# --------------------------------------------------------------------------
q25, q50, q75 = (view["price_kes"].quantile([0.25, 0.5, 0.75])
                 .round(-3).astype(int))
share_affordable = 100 * view["price_band"].isin(AFFORDABLE).mean()

k1, k2, k3, k4 = st.columns([1.5, 1, 1, 1], gap="large")
with k1:
    st.markdown(
        f'<div class="kpi-hero"><div class="label">Median asking rent</div>'
        f'<div class="value">KSh {ksh(q50)}</div>'
        f'<div class="sub">per month, middle 50% KSh {ksh(q25)} to '
        f'{ksh(q75)}</div></div>', unsafe_allow_html=True)
with k2:
    st.markdown(
        f'<div class="kpi"><div class="label">Listings</div>'
        f'<div class="value">{len(view):,}</div>'
        f'<div class="sub">in the current scope</div></div>',
        unsafe_allow_html=True)
with k3:
    st.markdown(
        f'<div class="kpi"><div class="label">Under KSh 50k</div>'
        f'<div class="value">{share_affordable:.0f}%</div>'
        f'<div class="sub">of listings in scope</div></div>',
        unsafe_allow_html=True)
with k4:
    st.markdown(
        f'<div class="kpi"><div class="label">Areas</div>'
        f'<div class="value">{view["neighborhood"].nunique()}</div>'
        f'<div class="sub">with listings in scope</div></div>',
        unsafe_allow_html=True)

# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["Overview", "Neighbourhoods", "Market mix",
     "Segments & value", "Model quality"])

with tab1:
    affordable = view["price_band"].isin(AFFORDABLE).sum()
    callout(
        f"<b>{share_affordable:.0f}% of listings in scope ask less than "
        f"KSh 50,000</b> ({affordable:,} listings). The middle of the market "
        f"sits at KSh {ksh(q50)}, and half of all listings fall between "
        f"KSh {ksh(q25)} and KSh {ksh(q75)}.")

    left, right = st.columns([1.7, 1], gap="large")
    with left:
        with st.container(border=True):
            band_counts = (view["price_band"].value_counts()
                           .reindex(PRICE_ORDER).fillna(0).astype(int)
                           .reset_index())
            band_counts.columns = ["band", "n"]
            hist = (alt.Chart(band_counts).mark_bar(color=PRIMARY, size=38)
                    .encode(x=alt.X("band:N", title="Monthly asking rent band",
                                    sort=PRICE_ORDER),
                            y=alt.Y("n:Q", title="Listings"),
                            tooltip=[alt.Tooltip("band:N", title="band"),
                                     alt.Tooltip("n:Q", title="listings",
                                                 format=",")])
                    .properties(title="Listings by rent band"))
            st.altair_chart(style_chart(hist, height=300),
                            use_container_width=True)
    with right:
        with st.container(border=True):
            st.markdown("**By source**")
            src = aggregate(view, ["source"])
            table(src[["source", "n", "median"]].rename(
                columns={"source": "source", "n": "listings",
                         "median": "median (KSh)"}),
                caption="Median rent by source")
            hint("Two portals, different market mix.")

with tab2:
    nb = aggregate(view, ["neighborhood"])
    if len(nb):
        pricey = nb.sort_values("median", ascending=False).iloc[0]
        cheap = nb.sort_values("median").iloc[0]
        ratio = pricey["median"] / cheap["median"] if cheap["median"] else 0
        callout(
            f"<b>{place(pricey['neighborhood'])}</b> has the highest median "
            f"asking rent at KSh {ksh(pricey['median'])}; "
            f"<b>{place(cheap['neighborhood'])}</b> the lowest at "
            f"KSh {ksh(cheap['median'])} - a {ratio:.0f}x spread across the "
            f"areas shown.")

        suppressed_note(view, ["neighborhood"])
        with st.container(border=True):
            st.altair_chart(
                iqr_chart(nb.head(15), y="neighborhood",
                          label_col="neighborhood",
                          title="Median rent and interquartile range, "
                                "15 highest medians",
                          height=430),
                use_container_width=True)
        hint("Bar is the median; whisker is the middle 50% of listings.")

        with st.container(border=True):
            show = nb.copy()
            show["neighbourhood"] = show["neighborhood"].map(place)
            show = show[["neighbourhood", "n", "p25", "median", "p75"]]
            show.columns = ["neighbourhood", "listings", "p25 (KSh)",
                            "median (KSh)", "p75 (KSh)"]
            table(show.sort_values("median (KSh)", ascending=False),
                  caption="All reported neighbourhoods")

        cells = (view.groupby(["neighborhood", "bedroom_band"])["price_kes"]
                 .agg(n="size", median="median").reset_index())
        cells = cells[cells["n"] >= MIN_N]
        if len(cells):
            with st.container(border=True):
                cells["neighbourhood"] = cells["neighborhood"].map(place)
                pivot = (cells.pivot(index="neighbourhood",
                                     columns="bedroom_band",
                                     values="median")
                         .dropna(how="all")
                         .reindex(columns=[b for b in BED_ORDER
                                           if b in set(cells["bedroom_band"])]))
                pivot_display = (pivot / 1000).round(0).reset_index()
                pivot_display.columns = ["neighbourhood"] + [
                    str(c) for c in pivot.columns]
                table(pivot_display,
                      caption="Median rent by neighbourhood and bedrooms "
                              "(KSh thousands)")
                hint(f"Cells with fewer than {MIN_N} listings are left blank.")
    else:
        st.info("No neighbourhood has enough listings in this scope.")

with tab3:
    type_tab = aggregate(view, ["kind_group"])
    size_tab = aggregate(view, ["size_band"])
    bed_tab = aggregate(view, ["bedroom_band"])

    if len(type_tab):
        top_type = type_tab.sort_values("median", ascending=False).iloc[0]
        callout(
            f"<b>{KIND_LABELS.get(top_type['kind_group'], place(top_type['kind_group']))}"
            f"</b> is the most expensive category in scope, with a median of "
            f"KSh {ksh(top_type['median'])}.")

    c1, c2 = st.columns(2, gap="large")
    with c1:
        with st.container(border=True):
            if len(type_tab):
                st.altair_chart(
                    iqr_chart(type_tab, y="kind_group", label_col="kind_group",
                              title="By property type", height=260),
                    use_container_width=True)
            else:
                st.info("Not enough listings per type in this scope.")
    with c2:
        with st.container(border=True):
            if len(size_tab):
                st.altair_chart(
                    iqr_chart(size_tab, y="size_band",
                              title="By floor size (m²)", height=260),
                    use_container_width=True)
                hint("Sizes outside 12-250 m² per bedroom are excluded.")
            else:
                st.info("No size band has enough listings in this scope.")

    with st.container(border=True):
        if len(bed_tab):
            st.altair_chart(
                iqr_chart(bed_tab, y="bedroom_band",
                          title="By bedroom count", height=280),
                use_container_width=True)
        else:
            st.info("No bedroom band has enough listings in this scope.")

    with st.container(border=True):
        st.markdown("**Median rent by bedroom count**")
        bed_show = bed_tab.copy()
        if len(bed_show):
            bed_show = bed_show[["bedroom_band", "n", "p25", "median", "p75"]]
            bed_show.columns = ["bedrooms", "listings", "p25 (KSh)",
                                "median (KSh)", "p75 (KSh)"]
            table(bed_show, caption="Median rent by bedroom count")
        else:
            st.info("No bedroom band has enough listings in this scope.")

with tab4:
    clusters = load_table("model_cluster_profiles").sort_values("median_rent")
    if len(clusters) == 2:
        names = {int(clusters.iloc[0]["cluster"]):
                 "Mid-market apartments",
                 int(clusters.iloc[1]["cluster"]):
                 "Larger family homes"}
    else:
        names = {}
    clusters["segment"] = clusters["cluster"].map(
        lambda c: names.get(int(c), f"Segment {int(c)}"))

    callout(
        "<b>The market splits in two.</b> A large mid-market segment of "
        "smaller apartments and a smaller segment of large family homes, "
        "stable across random seeds, so the split is a property of the data "
        "rather than of the algorithm.")

    c1, c2 = st.columns(2, gap="large")
    for col, (_, row) in zip((c1, c2), clusters.iterrows()):
        with col:
            with st.container(border=True):
                st.markdown(f"**{row['segment']}**")
                st.markdown(
                    f'<div class="kpi-hero"><div class="label">Median rent'
                    f'</div><div class="value">KSh {ksh(row["median_rent"])}'
                    f'</div><div class="sub">{int(row["n"]):,} listings, '
                    f'{row["median_bedrooms"]:.0f} bedrooms median</div></div>',
                    unsafe_allow_html=True)
                hint(f"Largest areas: {place(row['top_neighborhoods'])}")

    value = load_table("model_value_by_neighborhood")
    rep = value[value["reported"]].sort_values("median_structural_value")
    if len(rep):
        best = rep.iloc[0]
        callout(
            f"<b>Value is not the same as cheap.</b> The score compares each "
            f"listing with what the same kind of dwelling costs city-wide. "
            f"<b>{place(best['neighborhood'])}</b> gives the most dwelling per "
            f"shilling ({best['median_structural_value']:+.0%} vs the norm).")
        with st.container(border=True):
            src = rep.copy()
            src["neighbourhood"] = src["neighborhood"].map(place)
            chart_data = src.head(15)
            bars = (alt.Chart(chart_data).mark_bar(size=16)
                    .encode(
                        y=alt.Y("neighbourhood:N", sort="ascending", title=""),
                        x=alt.X("median_structural_value:Q",
                                title="median value vs structure "
                                      "(actual / expected - 1)",
                                axis=alt.Axis(format="%")),
                        color=alt.condition(
                            alt.datum.median_structural_value < 0,
                            alt.value(PRIMARY), alt.value(MUTED)),
                        tooltip=["neighbourhood",
                                 alt.Tooltip("n", format=","),
                                 alt.Tooltip("median_structural_value",
                                             format="+.1%",
                                             title="value vs structure"),
                                 alt.Tooltip("pct_structurally_cheap",
                                             format=".0f",
                                             title="% >15% below norm")])
                    .properties(title="Best value: 15 areas with the most "
                                      "negative structural value"))
            st.altair_chart(style_chart(bars, height=420),
                            use_container_width=True)
        hint("Negative means more dwelling per shilling than the city norm "
             "for the same structure; grey means a premium.")

with tab5:
    metrics = load_table("model_metrics")
    hold = metrics[metrics["evaluation"] == "holdout_20pct"].set_index("model")
    if {"linear_baseline", "gradient_boosting"} <= set(hold.index):
        lin = hold.loc["linear_baseline", "mape_pct"]
        gbm = hold.loc["gradient_boosting", "mape_pct"]
        callout(
            f"<b>The boosted blend beats the linear baseline by "
            f"{lin - gbm:.1f} MAPE points</b> ({lin:.1f}% to {gbm:.1f}%). "
            f"It uses the same leakage-controlled split plus cheap card-text "
            f"signals - amenity flags, 20 keywords, the street name and the "
            f"detailed property type - from four small trees, not a larger "
            f"algorithm.")

    with st.container(border=True):
        st.markdown("**Hold-out performance**")
        metrics_show = (hold.reset_index()[["model", "n", "rmse_ksh",
                                            "mae_ksh", "mape_pct", "r2_ksh"]]
                        .rename(columns={"model": "model", "n": "listings",
                                         "rmse_ksh": "RMSE (KSh)",
                                         "mae_ksh": "MAE (KSh)",
                                         "mape_pct": "MAPE (%)",
                                         "r2_ksh": "R²"}))
        table(metrics_show.round({"RMSE (KSh)": 0, "MAE (KSh)": 0,
                                  "MAPE (%)": 1, "R²": 3}),
              caption="Hold-out performance")
        hint("Shilling errors include Duan's smearing correction, so they are "
             "not biased downward by the log-rent model.")

    drivers = load_table("model_shap_importance").head(10)
    with st.container(border=True):
        drv = (alt.Chart(drivers).mark_bar(color=PRIMARY, size=16)
               .encode(x=alt.X("mean_abs_shap:Q",
                               title="mean |SHAP| (effect on log rent)"),
                       y=alt.Y("label:N", sort="-x", title=""),
                       tooltip=["label", alt.Tooltip("share_pct",
                                                     title="share (%)",
                                                     format=".1f")])
               .properties(title="What drives rent most"))
        st.altair_chart(style_chart(drv, height=300),
                        use_container_width=True)

    band_err = load_table("model_error_by_price_band")
    with st.container(border=True):
        st.markdown("**Error by rent band**")
        long = band_err.melt(id_vars=["group", "n"],
                             value_vars=["linear_baseline_mape_pct",
                                         "gradient_boosting_mape_pct"],
                             var_name="model", value_name="mape")
        long["model"] = long["model"].map({
            "linear_baseline_mape_pct": "Linear baseline",
            "gradient_boosting_mape_pct": "Gradient boosting"})
        order = [b for b in PRICE_ORDER if b in set(long["group"])]
        chart = (alt.Chart(long).mark_bar(size=16)
                 .encode(x=alt.X("group:N", title="Rent band", sort=order),
                         y=alt.Y("mape:Q", title="MAPE (% of rent)"),
                         xOffset=alt.XOffset("model:N", sort=[
                             "Linear baseline", "Gradient boosting"]),
                         color=alt.Color("model:N", title="",
                                         scale=alt.Scale(
                                             domain=["Linear baseline",
                                                     "Gradient boosting"],
                                             range=[MUTED, PRIMARY]),
                                         legend=alt.Legend(orient="top")),
                         tooltip=["group", "model",
                                  alt.Tooltip("mape", format=".1f")])
                 .properties(title="Percentage error is largest in the "
                                   "cheapest band"))
        st.altair_chart(style_chart(chart, height=300),
                        use_container_width=True)
        hint("Out-of-fold errors on the training split; the model is not "
             "reliable below KSh 50,000 per month.")

st.markdown('<div class="rule"></div>', unsafe_allow_html=True)
hint("Asking rents from portal listings, not signed leases. Reproduce with "
     "main.ipynb.")
