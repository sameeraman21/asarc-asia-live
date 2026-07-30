# ASARC Absolute-Best — Asia Live Bot

XAUUSD Asia-session production bot (UTC 00:00–06:59).

## Strategy

- Features: rich pack
- Train: last 12 months rolling
- SL **3.0×ATR** / TP **2.0×ATR**
- Threshold **0.55**
- Hours UTC **0–6**
- Entry: next M1 after M5 close + cost floor $0.30
- Drift ≤ $0.30, one trade, even M5 slots
- Executes via MetaAPI → Exness MT5 (`XAUUSDm`)

## Setup

```bash
cp config/metaapi.json.example config/metaapi.json
# edit config/metaapi.json with your MetaAPI token + account id

chmod +x run.sh install_launchd.sh
./run.sh setup
./run.sh live 0.01    # real orders via macOS launchd KeepAlive
./run.sh status
./run.sh log
./run.sh stop
```

Shadow (paper only):

```bash
./run.sh shadow
```

## Retrain

Models ship in `models/`. Monthly retrain (recommended) from the research repo:

```bash
python3 live/train_asarc_model.py
```

Then copy `live/models_asarc/*` → `models/`.

## Safety

- Default live lot: **0.01**
- Requires `--confirm-live` (wired in `run.sh live`)
- Do **not** commit real `config/metaapi.json`
