"""Registry overrides keyed by DefiLlama slug.

DefiLlama gives us the app URL and little else. Where docs live on another
domain (GitBook subdomains), or where a protocol has a governance forum or a
Snapshot space we know about, this is the one place to say so. Bootstrap
applies these on top of the DefiLlama record; the crawler still verifies
every URL, so a stale entry costs a skipped source, not bad data.

Keep this list small and factual. If the docs guess in `registry.py` finds a
site on its own, do not add it here.
"""

from __future__ import annotations

OVERRIDES: dict[str, dict[str, str]] = {
    # docs on another domain than the app
    "ether.fi-stake": {"docs_url": "https://etherfi.gitbook.io/etherfi-docs", "forum_url": "https://governance.ether.fi"},
    # dao.rocketpool.net and gov.curve.finance sit behind bot protection (403 to the JSON API): no forum_url.
    "rocket-pool": {"docs_url": "https://docs.rocketpool.net", "governance_url": "https://snapshot.org/#/rocketpool-dao.eth"},
    "eigencloud": {"docs_url": "https://docs.eigencloud.xyz", "forum_url": "https://forum.eigenlayer.xyz"},
    "hyperliquid-bridge": {"docs_url": "https://hyperliquid.gitbook.io/hyperliquid-docs"},
    "kelp": {"docs_url": "https://kelp.gitbook.io/kelp"},
    "sanctum-validator-lsts": {"docs_url": "https://learn.sanctum.so"},
    # A forum belongs to one protocol: forum.sky.money is indexed under sky-lending only, so
    # threads are not written three times (a document is keyed by URL, one protocol each).
    "spark-liquidity-layer": {"docs_url": "https://docs.spark.fi"},
    "sparklend": {"docs_url": "https://docs.spark.fi"},
    # governance forums (Discourse) and Snapshot spaces
    "aave-v3": {"forum_url": "https://governance.aave.com", "governance_url": "https://snapshot.org/#/aavedao.eth"},
    "lido": {"forum_url": "https://research.lido.fi", "governance_url": "https://snapshot.org/#/lido-snapshot.eth"},
    "uniswap-v3": {"forum_url": "https://gov.uniswap.org", "governance_url": "https://snapshot.org/#/uniswapgovernance.eth"},
    "uniswap-v4": {"forum_url": "https://gov.uniswap.org", "governance_url": "https://snapshot.org/#/uniswapgovernance.eth"},
    "compound-v3": {"forum_url": "https://www.comp.xyz"},
    "morpho-blue": {"forum_url": "https://forum.morpho.org", "governance_url": "https://snapshot.org/#/morpho.eth"},
    "sky-lending": {"forum_url": "https://forum.sky.money"},
    "arbitrum-bridge": {"forum_url": "https://forum.arbitrum.foundation", "governance_url": "https://snapshot.org/#/arbitrumfoundation.eth"},
    "jito-liquid-staking": {"forum_url": "https://forum.jito.network"},
    "venus-core-pool": {"forum_url": "https://community.venus.io", "governance_url": "https://snapshot.org/#/venus-xvs.eth"},
    "ssv-network": {"forum_url": "https://forum.ssv.network", "governance_url": "https://snapshot.org/#/mainnet.ssvnetwork.eth"},
}


def overrides_for(slug: str) -> dict[str, str]:
    return dict(OVERRIDES.get(slug, {}))
