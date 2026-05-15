# agilent-biostack4-standalone

Standalone Python driver for the Agilent BioStack 4 microplate stacker over
RS-232. No Gen5, no Cytation, no vendor software in the loop.

The driver speaks the BioStack 4's binary framed serial protocol directly,
exposes only the two workflows the lab needs (`drop_plate`, `pickup_plate`),
and converts any non-success status payload from the device into a typed
Python exception.

This package is **pre-bench-validation**. Read [PLAN.md](PLAN.md) and
[PHYSICAL_TESTS.md](PHYSICAL_TESTS.md) before running any code path that
actually opens COM8.

## Status

| Layer | State |
| --- | --- |
| Protocol notes (sniffed) | [`PROTOCOL_NOTES.md`](PROTOCOL_NOTES.md) |
| Frame codec | implemented, unit-tested |
| Dry-run transport | implemented |
| Serial transport | implemented (not yet exercised against hardware) |
| High-level workflows (`status`, `home`, `drop_plate`, `pickup_plate`) | implemented as recorded-sequence playback |
| FastAPI service | not yet (follow-up PR; see PLAN.md) |
| Physical validation | pending (see PHYSICAL_TESTS.md) |

## Install (development)

```bash
uv venv
uv pip install -e ".[dev]"
```

## Run the unit tests

The unit tests do not touch hardware and run on macOS / Linux / Windows:

```bash
uv run pytest
```

## Try the driver in dry-run

```python
from agilent_biostack4 import BioStack4
from agilent_biostack4.transport import DryRunTransport

stacker = BioStack4(transport=DryRunTransport())
stacker.connect()
print(stacker.status())
stacker.home()
stacker.drop_plate()
stacker.pickup_plate()
stacker.close()
```

## Run against real hardware

Only after [PHYSICAL_TESTS.md](PHYSICAL_TESTS.md) step 0 passes.

```bash
cp config.example.toml config.toml
# edit config.toml: com_port, dry_run = false
uv run python -c "from agilent_biostack4 import BioStack4; \
    b = BioStack4(); b.connect(); print(b.status()); b.close()"
```

## Layout

```
agilent-biostack4-standalone/
+- PLAN.md                       design contract
+- PHYSICAL_TESTS.md             bench validation sign-off
+- PROTOCOL_NOTES.md             byte-level protocol observations
+- pyproject.toml
+- config.example.toml
+- src/agilent_biostack4/        the driver package
+- tests/                        unit tests (no hardware)
+- tools/analyze_captures.py     reproducible CSV summarizer
+- port-communication-captures/  gitignored: raw sniffer CSVs and workflow notes
```

## License

MIT. See [LICENSE](LICENSE).
