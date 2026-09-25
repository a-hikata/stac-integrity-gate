# Evidence matrix — why this tool exists

Status reviewed: 2026-09-25.

| Independent system | Observed failure class | Downstream consequence | Tool relevance |
|---|---|---|---|
| Element84 Earth Search #48 | STAC `proj:shape` / `proj:transform` / spatial resolution disagree with an AOT GeoTIFF | Consumers plan the wrong raster grid | Direct: shape/transform checks |
| Microsoft Planetary Computer #126 | 3DEP STAC resolution/projection metadata inconsistencies | stackstac loading failures / wrong dimensions | Direct: projection checks |
| Open Data Cube `odc-stac` #220 | Maintainer explicitly notes STAC GeoBox is assumed to equal file GeoBox and proposes an analysis tool | Wrong output can look like a software bug | Direct validation of the product thesis |
| OpenEO GeoPySpark #1557 / related fix | Bad projection metadata can break extent/resolution logic | Runtime failure; later heuristic added for one transform-order class | Shows narrow heuristics do not replace general asset truth checks |
| boettiger-lab/data-workflows #666 | STAC declares one raster band; actual COG has six; existing 2,196-line validator did not open COGs | Layer fails to render; existing QA reported CLEAN | Direct: band-count check; strongest CI-gate use case |
| EOPF Sample Service #82 | STAC scale/offset duplicates self-describing Zarr scaling | Silent double-scaling destroys value range | Roadmap: Zarr semantic checks |
| EOPF Explorer data-model #195 | Distinct VV/VH data assets share identical hrefs and item semantics do not match store scope | Non-render clients cannot resolve intended data | Partial: duplicate-data-href warning; roadmap for Zarr scope checks |

## Kill criteria

Do not continue expanding this tool merely because bugs exist.

KILL or freeze the project if live cross-provider tests show all of the following:

1. Current public catalogs no longer reproduce meaningful asset/STAC inconsistencies.
2. Existing maintained tooling adds equivalent asset-aware checks with CI-ready behavior.
3. False positives require enough catalog-specific configuration that a generic gate becomes impractical.

Continue if current catalogs still show mismatches that structural STAC + COG validation pass, especially across independent publishers.
