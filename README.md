# BikePulse

BikePulse is an MVP pipeline for collecting official Velib GBFS data and building a trustworthy history for future stockout prediction.

Current milestone: **M1 - Trustworthy history**.

## Local setup

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -e ".[dev]"
pytest
```

## Commands

```powershell
bikepulse-collect --config config.example.toml
bikepulse-refresh-stations --config config.example.toml
bikepulse-compact-hour --config config.example.toml --hour 2026-08-09T21:00:00Z
bikepulse-quality-report --config config.example.toml --date 2026-08-09
streamlit run src/bikepulse/app/main.py
```

No tests call the live GBFS feed.
