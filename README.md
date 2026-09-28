# BalanceBridge

A desktop toolkit for simulating and coordinating spot-account rebalancing strategies.

## Features

- Market depth and spread checks
- Configurable risk limits
- Simulator mode for local testing
- Two-account execution workflow

## Run locally

```bash
python -m venv .venv
pip install -r requirements.txt
copy settings.example.json settings.json
python main.py
```

The example configuration starts in simulator mode. Credential files are excluded from version control.
