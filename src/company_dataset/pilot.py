"""Compatibility entry point: a pilot run is an enrich run over a sample. Use company_dataset.enrich."""
from company_dataset.enrich import main, verify_homepage  # noqa: F401
from company_dataset.ingest import interleave, stratified_sample  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
