# Production schedule (dual bots)

| UTC hours | Bot | Strategy |
|-----------|-----|----------|
| **00:00–06:59** (Asia) | `deploy_asarc` | ASARC absolute-best · thr 0.55 · SL/TP 3.0×/2.0× ATR · rich · h0–6 |
| **07:00–20:59** (London / Overlap / NY) | `deploy_v16` | V16 ML · thr 0.64 · SL15/TP10 (0.10 pip) |

- ASARC skips non-Asia (`session==asia` and hour in 0–6).
- V16 skips Asia (`ACTIVE_SESSIONS = london/newyork/overlap`).
- Both run via launchd `KeepAlive` so they survive Cursor/terminal exit.
- Do **not** run V17 at the same time as V16 on western hours.

```bash
cd deploy_asarc && ./run.sh live 0.01   # Asia
cd deploy_v16   && ./run.sh live 0.01   # West
```
