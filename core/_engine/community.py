"""Community."""
#!/usr/bin/env python3

from __future__ import annotations

import random
import time


COMMUNITY_SEED = 250


def pair_louvain_membership(request):
    import igraph as ig

    started = time.perf_counter()
    graph = ig.Graph(n=request.n, directed=False)
    edges = list(request.pair_weights)
    weights = [float(request.pair_weights[edge]) for edge in edges]
    if edges:
        graph.add_edges(edges, attributes={"weight": weights})
    ig.set_random_number_generator(random.Random(COMMUNITY_SEED))
    if request.n <= 1 or not edges:
        membership = list(range(request.n))
        modularity = 0.0
    else:
        clustering = graph.community_multilevel(weights="weight")
        membership = list(clustering.membership)
        modularity = float(clustering.modularity)
    return membership, {
        "source": "mfs_pair_louvain",
        "communities": len(set(membership)),
        "nodes": request.n,
        "pair_edges": len(edges),
        "modularity": modularity,
        "seed": COMMUNITY_SEED,
        "seconds": time.perf_counter() - started,
        "igraph_version": ig.__version__,
    }
