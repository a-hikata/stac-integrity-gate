"""Dev-only benchmark / regression tooling for stac-integrity-gate.

Not part of the installed package: the wheel ships only ``stac_integrity/``
and MANIFEST.in prunes this directory from the sdist. Run from a checkout:

    python -m benchmark.run                 # offline regression suite
    python -m benchmark.run --baseline benchmark/baseline/offline.json
    python -m benchmark.run --live          # opt-in: live public catalogs
"""
