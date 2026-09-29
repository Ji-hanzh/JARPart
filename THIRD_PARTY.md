# Dependencies and acknowledgments

- Community detection uses [python-igraph](https://github.com/igraph/python-igraph),
  version 0.11.8, distributed upstream under GPL v2 or later.
- CP-SAT optimization uses [OR-Tools](https://github.com/google/or-tools),
  version 9.15.6755, distributed upstream under Apache-2.0.
- Both dependencies are installed separately and are not bundled here.
- The method builds on the MFSPart partitioning problem and ComPart-inspired
  joint refinement. No external ComPart source tree is bundled.
- Circuit community caches correspond to Titan23 and TopoPart benchmark
  inputs identified by the checksums in `config/cache_lock.json`. Their raw
  netlists are not bundled.

These acknowledgments do not replace the upstream projects' license texts.
