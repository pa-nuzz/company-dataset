"""The pipeline: ingest -> identity -> enrich -> export, over data/store.db.

    company-dataset ingest --db data/store.db --raw data/raw
    company-dataset enrich --db data/store.db --raw data/raw --run myrun --out data/runs/myrun
    company-dataset repair reresolve --db data/store.db --run myfix --from-runs myrun
    company-dataset export --db data/store.db --runs myrun,myfix --out data/exports/final_YYYY-MM-DD
"""
from __future__ import annotations

import importlib
import sys

STAGES = {"ingest": "company_dataset.ingest", "enrich": "company_dataset.enrich", "repair": "company_dataset.repairs", "export": "company_dataset.export"}


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in STAGES:
        print(__doc__, file=sys.stderr)
        return 2
    return importlib.import_module(STAGES[args[0]]).main(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
