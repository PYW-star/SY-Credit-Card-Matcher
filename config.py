"""
Backend configuration for the Credit Card Claims Matcher.

Secrets resolution order for each value:
  1. st.secrets (Streamlit Community Cloud's Secrets manager, or a local
     .streamlit/secrets.toml if you use one - never commit that file)
  2. environment variable / local key.env file (see key.env.example) -
     convenient for running the app outside Streamlit Cloud

Never hardcode real secrets in this file - it is NOT gitignored (key.env is).
"""
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / "key.env")


def _get_secret(key: str, default: str = "") -> str:
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass  # no secrets.toml present locally - fall through to env/key.env
    return os.environ.get(key, default)


# Your Anthropic API key. Required — used to read statements/receipts and
# categorise transactions.
ANTHROPIC_API_KEY = _get_secret("ANTHROPIC_API_KEY", "")

# Password required to use the app (simple shared-password gate - see app.py).
# Required for any publicly reachable deployment (e.g. Streamlit Community
# Cloud) so a stranger with the URL can't spend your Anthropic credits.
APP_PASSWORD = _get_secret("APP_PASSWORD", "")

# Banks shown in the "Choose bank" dropdown. This is just a label - it's
# passed to Claude as a hint when reading the statement, and used to name the
# output - it doesn't need to match any folder path or file naming elsewhere.
BANKS = [
    "Standard Chartered",
    "Maybank",
    "CIMB",
    "Public Bank",
]