from company_dataset.sources import (
    open_jobs, slug_files, workable_discovery, africa_ats, arquivo_cdx, company_sites, builder_jobs, commoncrawl, commoncrawl_gapfill, commoncrawl_indexer, corporate_graph, crtsh, dns_cname, feashliaa, free_job_aggregators, github_lists, headcountlabs,
    hf_datasets, hn_hiring, joa, kaggle_datasets, passive_dns, vc_boards, jobanalytics, kaleb, kalil, lastround, latmay, legacy_subdomains, moonie, openatsfeed, pages_js, search_dorks, slug_prober, wayback_cdx, yc_slugs, surt_hosts, company_registries, free_job_aggregators, historical_warc,
)

SOURCES = [latmay, lastround, kaleb, moonie, feashliaa, commoncrawl, headcountlabs, openatsfeed, kalil, joa,
           africa_ats, builder_jobs, commoncrawl_gapfill, jobanalytics, legacy_subdomains, wayback_cdx, commoncrawl_indexer, slug_prober,
           search_dorks, github_lists, dns_cname, corporate_graph, yc_slugs, hn_hiring, hf_datasets, passive_dns, vc_boards, kaggle_datasets, company_sites, arquivo_cdx, slug_files, workable_discovery, open_jobs,
           crtsh, surt_hosts, pages_js, free_job_aggregators, company_registries, historical_warc]

__all__ = ["SOURCES", "open_jobs", "slug_files", "workable_discovery", "arquivo_cdx", "company_sites", "africa_ats", "builder_jobs", "commoncrawl", "commoncrawl_gapfill", "feashliaa", "headcountlabs", "joa", "jobanalytics",
           "kaleb", "kalil", "lastround", "latmay", "legacy_subdomains", "moonie", "openatsfeed", "commoncrawl_indexer", "corporate_graph", "crtsh", "dns_cname", "github_lists", "hf_datasets", "hn_hiring", "kaggle_datasets", "passive_dns", "search_dorks", "vc_boards", "slug_prober", "wayback_cdx", "yc_slugs", "surt_hosts", "pages_js", "free_job_aggregators", "company_registries", "historical_warc"]
