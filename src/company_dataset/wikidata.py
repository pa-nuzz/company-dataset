"""Wikidata (CC0) lookup of companies by their official website (P856).

Exact-IRI lookups through the public SPARQL endpoint, batched. Only used to corroborate a
website we already verified: LinkedIn company ID (P4264) and country (P17 / ISO P297).
"""
from __future__ import annotations

import json
from urllib.parse import quote

from company_dataset.normalize import host_of

ENDPOINT = "https://query.wikidata.org/sparql?format=json&query="
BATCH = 10


def _variants(host: str) -> list[str]:
    bare = host.removeprefix("www.")
    out = []
    for scheme in ("https", "http"):
        for h in (bare, "www." + bare):
            out += [f"{scheme}://{h}", f"{scheme}://{h}/"]
    return out


def query_for(hosts: list[str]) -> str:
    values = " ".join(f"<{u}>" for h in hosts for u in _variants(h))
    return (
        "SELECT ?u ?item ?itemLabel ?li ?iso WHERE { VALUES ?u { " + values + " } ?item wdt:P856 ?u . "
        "OPTIONAL { ?item wdt:P4264 ?li } OPTIONAL { ?item wdt:P17 ?c . ?c wdt:P297 ?iso } "
        'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }'
    )


def parse(payload: dict) -> dict[str, list[dict]]:
    """host -> [{qid, label, linkedin, country}] (one entry per item)."""
    out: dict[str, dict[str, dict]] = {}
    for b in (payload.get("results") or {}).get("bindings") or []:
        host = host_of(b["u"]["value"]).removeprefix("www.")
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        item = out.setdefault(host, {}).setdefault(qid, {"qid": qid, "label": None, "linkedin": set(), "country": set()})
        item["label"] = (b.get("itemLabel") or {}).get("value") or item["label"]
        if b.get("li"):
            item["linkedin"].add(b["li"]["value"])
        if b.get("iso"):
            item["country"].add(b["iso"]["value"])
    return {h: [{**i, "linkedin": sorted(i["linkedin"]), "country": sorted(i["country"])} for i in items.values()] for h, items in out.items()}


def lookup(client, hosts: list[str]) -> dict[str, list[dict]]:
    hosts = sorted({h.removeprefix("www.") for h in hosts if h})
    result: dict[str, list[dict]] = {}
    for i in range(0, len(hosts), BATCH):
        chunk = hosts[i:i + BATCH]
        resp = client.get(ENDPOINT + quote(query_for(chunk)), headers={"Accept": "application/sparql-results+json"})
        if resp.status != 200:
            raise RuntimeError(f"wikidata http {resp.status}")
        result.update(parse(json.loads(resp.text)))
    return result
