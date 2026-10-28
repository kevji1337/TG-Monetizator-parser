"""Static presentation styles; native inputs keep keyboard and focus behavior."""
import streamlit as st

STYLES = '''<style>
:root {
  --mon-accent: #55cbbb;
  --mon-border: rgba(148, 163, 184, .20);
}
[data-testid="stAppViewContainer"] { background: #10141d; }
[data-testid="stSidebar"] { background: #171e2b; border-right: 1px solid var(--mon-border); }
[data-testid="stSidebar"] .stMainBlockContainer { padding-top: 1.5rem; }
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: .7rem; }
[data-testid="stAppViewContainer"] :is(input, textarea) { caret-color: var(--mon-accent); }
[data-testid="stAppViewContainer"] :is([data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="select"] > div) {
  border-radius: 10px; border-color: var(--mon-border); background-color: #202a3a;
}
[data-testid="stAppViewContainer"] :is([data-baseweb="input"], [data-baseweb="select"] > div):focus-within {
  border-color: var(--mon-accent); box-shadow: 0 0 0 2px rgba(85, 203, 187, .16);
}
[data-testid="stAppViewContainer"] textarea { border-radius: 10px; }
[data-testid="stAppViewContainer"] :is(input, textarea):disabled { opacity: .65; cursor: not-allowed; }
[data-testid="stAppViewContainer"] button { border-radius: 10px; transition: border-color .15s, background-color .15s; }
[data-testid="stAppViewContainer"] button:focus-visible { outline: 2px solid var(--mon-accent); outline-offset: 3px; }
[data-testid="stAppViewContainer"] button:disabled { opacity: .55; cursor: not-allowed; }
[data-testid="stCheckbox"] label { gap: .45rem; }
[data-testid="stCheckbox"] label > span { border-radius: 6px; }
[data-testid="stSlider"] [role="slider"] { border-radius: 50%; box-shadow: 0 0 0 3px rgba(85, 203, 187, .12); }
[data-testid="stExpander"] { border-radius: 12px; border-color: var(--mon-border); overflow: hidden; }
[data-testid="stExpander"] summary { padding-block: .75rem; }
[data-testid="stExpander"] summary:hover { background: rgba(85, 203, 187, .05); }
[data-testid="stMetric"] {
  padding: .85rem 1rem; border: 1px solid var(--mon-border); border-radius: 14px;
  background: linear-gradient(135deg, #1a2534, #171e2b);
}
[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
[data-testid="stProgress"] [role="progressbar"] { border-radius: 999px; overflow: hidden; }
[data-testid="stProgress"] [role="progressbar"] > div { border-radius: 999px; background: #263448; }
[data-testid="stProgress"] [role="progressbar"] > div > div {
  border-radius: 999px; background: linear-gradient(90deg, #279aa9, #55cbbb);
}
[data-testid="stDataFrame"] { border: 1px solid var(--mon-border); border-radius: 12px; overflow: hidden; }
[data-testid="stAppViewContainer"] * { scrollbar-width: thin; scrollbar-color: #557386 #1b2534; }
[data-testid="stAppViewContainer"] ::-webkit-scrollbar { width: 10px; height: 10px; }
[data-testid="stAppViewContainer"] ::-webkit-scrollbar-track { background: #1b2534; border-radius: 999px; }
[data-testid="stAppViewContainer"] ::-webkit-scrollbar-thumb {
  background: #557386; border: 2px solid #1b2534; border-radius: 999px;
}
[data-testid="stAppViewContainer"] ::-webkit-scrollbar-thumb:hover { background: #70a294; }
@media (prefers-reduced-motion: reduce) {
  [data-testid="stAppViewContainer"] button { transition: none; }
}
</style>'''


def apply_style():
    st.html(STYLES)
