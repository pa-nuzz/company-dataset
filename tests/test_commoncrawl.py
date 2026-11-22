"""Common Crawl harvest from static CDX shards (no index server)."""
import gzip
import json
from pathlib import Path

import pytest

from company_dataset.http import HttpResponse
from company_dataset.sources import commoncrawl


def _cdx_line(surt, url):
    return f'{surt} 20260901000000 {json.dumps({"url": url, "status": "200"})}'


SHARD_BLOCK = gzip.compress("\n".join([
    _cdx_line("co,lever,api)/v0", "https://api.lever.co/v0"),
    _cdx_line("co,lever,jobs)/acme", "https://jobs.lever.co/acme"),
    _cdx_line("co,lever,jobs)/acme/123", "https://jobs.lever.co/acme/123-abc"),
    _cdx_line("co,lever,jobs)/zenith", "https://jobs.lever.co/Zenith"),
]).encode())

CLUSTER = "\n".join([
    "co,lever,api)/a 2026\tcdx-00001.gz\t0\t%d\t1" % len(SHARD_BLOCK),
    "co,lever,jobs)/zz 2026\tcdx-00001.gz\t%d\t10\t2" % len(SHARD_BLOCK),
    "co,lever,www)/ 2026\tcdx-00001.gz\t%d\t10\t3" % (len(SHARD_BLOCK) + 10),
]) + "\n"


class RangeClient:
    """Serves byte ranges of in-memory files like data.commoncrawl.org would."""

    def __init__(self, files, fail=()):
        self.files = files
        self.fail = set(fail)

    def get(self, url, headers=None, binary=False):
        name = url.rsplit("/", 1)[-1]
        if url in self.fail or name not in self.files:
            return HttpResponse(503, "", url, b"")
        data = self.files[name]
        start, end = map(int, headers["Range"].split("=")[1].split("-"))
        return HttpResponse(206, "", url, data[start:end + 1])


def test_find_blocks_includes_preceding_block():
    client = RangeClient({"cluster.idx": CLUSTER.encode()})
    blocks = commoncrawl.find_blocks(client, "CC-MAIN-2026-39", "co,lever,jobs)/", len(CLUSTER))
    assert blocks[0] == ("cdx-00001.gz", 0, len(SHARD_BLOCK))
    assert ("cdx-00001.gz", len(SHARD_BLOCK), 10) in blocks


def test_tokens_in_block_only_takes_prefix_hosts():
    assert commoncrawl.tokens_in_block(SHARD_BLOCK, "co,lever,jobs)/") == {("lever", "acme"), ("lever", "Zenith")}


def test_write_tokens_ignores_old_cache_format(tmp_path):
    cache = {
        "jobs.ashbyhq.com/CC-MAIN-2026-34/a": ["oldtoken"],
        "CC-MAIN-2026-39|co,lever,jobs)/": [["lever", "acme"]],
        "CC-MAIN-2025-13|co,lever,jobs)/": [["lever", "acme"], ["lever", "older"]],
    }
    path = tmp_path / "tokens.csv"
    assert commoncrawl.write_tokens(path, cache) == 2
    records, rejects = commoncrawl.parse(path)
    assert {(r.ats, r.code) for r in records} == {("lever", "acme"), ("lever", "older")}
    assert rejects == []


def test_failed_harvest_raises_and_keeps_existing_file(tmp_path, monkeypatch):
    tokens = tmp_path / "tokens.csv"
    tokens.write_text("ats,code,crawl\nlever,acme,CC-MAIN-2026-39\n")
    monkeypatch.setattr(commoncrawl, "cluster_size", lambda client, crawl: (_ for _ in ()).throw(commoncrawl.HarvestError("504")))
    with pytest.raises(commoncrawl.HarvestError):
        commoncrawl.run_download(tmp_path, client=None)
    assert "acme" in tokens.read_text()


class TestCommonCrawl:
    def test_parse_valid_csv(self, tmp_path: Path):
        path = tmp_path / "tokens.csv"
        path.write_text("ats,code\ngreenhouse,stripe\ngreenhouse,vercel\nashby,0g\n")
        records, rejects = commoncrawl.parse(path)
        assert len(records) == 3
        assert len(rejects) == 0
        assert records[0].ats == "greenhouse"
        assert records[0].code == "stripe"

    def test_parse_rejects_invalid_ats(self, tmp_path: Path):
        path = tmp_path / "tokens.csv"
        path.write_text("ats,code\njazzhr,acme\ngreenhouse,stripe\n")
        records, rejects = commoncrawl.parse(path)
        assert len(records) == 1
        assert len(rejects) == 1
        assert rejects[0].reason == "unsupported_ats"

    def test_parse_rejects_invalid_slug(self, tmp_path: Path):
        path = tmp_path / "tokens.csv"
        path.write_text("ats,code\ngreenhouse,with/slash\nashby,ok\n")
        records, rejects = commoncrawl.parse(path)
        assert len(records) == 1
        assert len(rejects) == 1
        assert rejects[0].reason == "invalid_slug"

    def test_parse_missing_file(self, tmp_path: Path):
        records, rejects = commoncrawl.parse(tmp_path / "nonexistent.csv")
        assert records == []
        assert rejects == []

    def test_meta_license_note_mentions_factual(self):
        assert "factual" in commoncrawl.META.license_note.lower()
