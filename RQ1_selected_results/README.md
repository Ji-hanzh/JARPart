# RQ1 selected results and runtime

`RQ1_results.csv` contains the 120 selected JARPart results corresponding to the RQ1 quality tables. Each row represents one circuit/configuration. The three quality metrics and the appended runtime belong to the **same selected feasible run**.

RQ1 selects one feasible run from ten seeds per configuration using the mean of the three metrics normalized by frozen configuration-specific reference values, with ties broken by the smaller seed. This file reports those selected runs; it is not a distribution of all runs.

| Column | Meaning |
|---|---|
| Circuit | Circuit name |
| Benchmark | Titan23 or Synthetic |
| Topology | MFS1 or MFS2, respectively |
| Assignment | Fixed or Non-Fixed |
| Hmax | Maximum permitted communication hop count |
| Cut_K | Cut divided by 1,000, matching the table unit |
| Con_K | Connectivity divided by 1,000, matching the table unit |
| Hop_x1000 | Average hop count multiplied by 1,000, matching the table unit |
| Runtime_s | Recorded complete partitioning-run elapsed time in seconds for this selected run |

The CSV retains numerical precision before manuscript display rounding. The quality columns follow the original RQ1 metric convention used in the supplied manuscript, including its repeated-connection accounting.

Runtime is the recorded `end_to_end_seconds` of the selected run, not just the final optimization call and not the cumulative cost of the ten-seed selection procedure. It excludes one-time community-cache construction, queueing, failed retries, and the separately recorded evidence-collection overhead. The runs were collected on the historical Intel Core i9-13900K server with concurrent container jobs.
