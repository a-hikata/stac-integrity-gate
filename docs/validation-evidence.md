# Validation evidence

This page summarizes the live validation of `stac-integrity-gate` against public STAC catalogs, run on 2026-09-25 (Waves 1–2) and 2026-09-26 (Wave 3, blind audit).

**This is a validation sample, not a prevalence study.** It also covers experimental checks (Zarr, GeoParquet). Items were chosen to test whether the tool finds real mismatches, and whether it avoids false positives on clean data. The counts below do not estimate how common these problems are across STAC catalogs in general. Nor do they show commercial demand for a hosted service.

## Method

- For every opened asset, the STAC declaration was compared with the raster header in three independent ways:
  1. `stac-integrity-gate` (rasterio/GDAL)
  2. `rio info` from a separate rasterio installation
  3. `benchmark/verify_tiff_header.py`, a TIFF/GeoTIFF IFD parser that uses HTTP range requests and **does not use GDAL or rasterio**
- Access failures (403, missing credentials, timeouts) were classified as INCONCLUSIVE and never counted as mismatches.
- Where sampling was needed, it was mechanical: a fixed seed, and time- or region-stratified.
- Raw JSON outputs are kept outside the package and are not distributed with it.

## Element84 Earth Search — Sentinel-2 L2A (legacy collection)

- Collection `sentinel-2-l2a` (legacy), asset `aot`.
- STAC declares a **20 m grid** (`proj:shape` 5490×5490, 20 m `proj:transform`). The actual COG is a **60 m grid** (1830×1830).
- Reproduced on **19/19** Items: the known issue Item plus 18 time-stratified Items from 2018 to 2026, 7 MGRS tiles. Some were ingested in July 2026, so the problem is still current.
- **Collection-1** (`sentinel-2-c1-l2a`) is clean for the same asset: 28/28 Items matched.
- Structural validation (`stac_valid`) reported `valid_stac: true` for affected Items. Schema validation cannot detect this class of error.
- Public issue: <https://github.com/Element84/earth-search/issues/48>

## Microsoft Planetary Computer — `3dep-seamless`

- **56 Items** had their data asset opened and measured (SAS-signed hrefs; metadata otherwise unmodified). They cover 32 regions: CONUS, Alaska, Hawaii and the Pacific islands.
- **37 Items** had a serious grid mismatch:
  - 2013-derived 10 m Items: **28/28** mismatched. `proj:transform` declares a pixel size of `1e-05°`, but the COG has `9.259e-05°`.
  - 2013-derived 30m Items: **6/6** mismatched. `1e-05°` is declared, but the COG has `2.778e-04°`.
  - 3 later Items (2019) had `proj:shape`/origin mismatches, for example 10813 declared vs 10812 actual.
- All **5** mismatching Items that were structurally validated were **VALID** in both PySTAC 1.15.2 (`Item.validate()`) and `stac_valid` 4.6.1.
- `rio info`, the independent TIFF parser and `stac-integrity-gate` agreed on **56/56** Items. There were no tool false negatives.
- **Final false positives: 0.** None of the 19 clean Items produced an ERROR.
  - Before the 0.3.0 fixes, every Item raised a false `CRS_MISMATCH`. The STAC declares compound `EPSG:5498` (NAD83 + NAVD88 height) while the 2D COG is `EPSG:4269` (NAD83). This is now reported as `CRS_VERTICAL_UNVERIFIED` (WARN).
- Catalog-wide, **3,081 of 5,173 Items** declare the suspicious `1e-05` pixel size in their **metadata**. Only a sample of these was opened (34/34 confirmed). The 3,081 figure is a metadata-level count of suspect Items, not a count of verified asset mismatches.
- Public issue: <https://github.com/microsoft/PlanetaryComputer/issues/126>

## NASA GHG Center / VEDA (additional controls)

- 12 collections were chosen mechanically (seeded shuffle of all collections): 19 Items, 135 assets.
- The tool could not open the assets: the S3 buckets return 403 and VEDA declares OIDC auth. All results are therefore **INCONCLUSIVE** (auth required). The tool reported them as `ASSET_UNREADABLE` warnings, not mismatches.
- Supplementary check (single method, not a tool run): the publisher's public titiler `/cog/info` endpoint was compared with the STAC declarations. There were **no mismatches in 135 assets**.
- This data exposed the float `proj:epsg` form (`4326.0`), which the tool now handles.

## Wave 3 — blind audit of new publishers (2026-09-26)

**Method.** Collections were listed and then shuffled with a fixed seed (`20260926`). The first eligible collections were taken, and the first 3 Items of each. No issue trackers or known-bug lists were consulted before sampling and measurement. Publishers already used in Waves 1–2 (Element84, Planetary Computer, NASA) were excluded. This is a **blind validation sample, not a prevalence study.**

### Digital Earth Australia — `nidem` (blind discovery)

- **CRS mismatch on 12/12 Items.** STAC declares `proj:code: EPSG:4326`. The actual COG is **EPSG:3577** (GDA94 / Australian Albers).
- The declared `proj:transform` (25 m, Albers metres) and `proj:shape` do match the asset. Only the CRS is wrong, so the declaration contradicts itself.
- Every Item is **VALID** in PySTAC 1.15.2 and `stac_valid` 4.6.1. stac-check passes it.
- odc-stac 0.5.3 loads the Item **without any warning** and builds a nonsensical geobox: WGS 84 with a "25-degree" resolution.
- Confirmed with `rio info` (separate rasterio installation) and with the non-GDAL TIFF parser.

### Digital Earth Australia — `multi_scale_topographic_position` (blind discovery)

- **Shape mismatch on 3 Items / 9 assets.** STAC declares `proj:shape` **1199×1199**. The actual rasters are **1200×1200**.
- `proj:transform` matches the asset, so the declared grid is one pixel short.
- **VALID** in PySTAC and `stac_valid`.
- Confirmed with `rio info` and the non-GDAL TIFF parser.

No public issue was found for either finding after measurement. The search covered GitHub issues only.

### False positives found and fixed

The blind audit also surfaced **two tool false positives**, both fixed (released in 0.4.0) with offline regression fixtures (`tests/test_live_regressions.py`):

1. **DLR terrabyte:** a remote catalog whose asset hrefs are HPC-internal `file://` paths gave `ASSET_UNREADABLE` **ERROR** (exit 1). This is an access failure and is now WARN.
2. **DEA `nidem`:** STAC `nodata: -9999` with no nodata tag in the header gave `NODATA_MISMATCH` ERROR. Yet 99.4% of the pixels are `-9999`, so the STAC value is correct. This is now `NODATA_NOT_IN_HEADER` WARN.

A third false positive (**FP-3**) was found later, by the GeoParquet sweep of the NRP catalog. Float32 raster nodata `-3.4e38` declared in STAC reads back from GDAL as `-3.3999999521e38`, which is the same float32 value. It was reported as `NODATA_MISMATCH` ERROR. Nodata is now compared at the band's own precision (fixed in 0.4.0, with regression tests).

The tool never mis-read an asset value: every value it reported matched both independent readers.

### Inconclusive or not comparable

- Inconclusive, access only:
  - DLR terrabyte (`file://` HPC paths)
  - Copernicus Data Space (S3 credentials required)
  - USGS LandsatLook (login redirect)
  - Digital Earth Africa (server error)
  - the remaining DEA collections (the API applied bot protection during sampling)
- Hub Ocean exposes no raster assets in its Items.
- swisstopo: 58 assets were opened, but only `proj:epsg` is declared. 57 CRS comparisons, all matching.
- Brazil Data Cube: 42 assets were opened, but it uses no `proj:*`/`raster:bands` fields, so there was nothing to compare.

## Experimental extras — Zarr and GeoParquet (2026-09-26)

These checks ship as **experimental** optional extras. The evidence below comes from small, targeted samples. It shows that each failure class exists today, not how common it is.

### Zarr (`[zarr]`) — EOPF double-scaling risk

- EOPF sample service STAC (`https://stac.core.eopf.eodc.eu`), collection `sentinel-2-l2a`. In a live Item, 14 assets declare STAC `raster:scale`/`raster:offset` that repeat the CF `scale_factor`/`add_offset` already stored on the Zarr arrays. A client that applies both scales the values twice.
- The newer `sentinel-2-l2a-zarr3` collection no longer duplicates the scaling, and the tool reports nothing there.
- The tool reports this as `ZARR_DOUBLE_SCALING_RISK` (**WARN**, never ERROR). Whether data is corrupted depends on the reader, which the gate cannot see.
- Public reports:
  - <https://github.com/EOPF-Sample-Service/eopf-stac/issues/82>
  - <https://github.com/EOPF-Explorer/data-pipeline/issues/384>
  - Related: <https://github.com/EOPF-Explorer/data-model/issues/195>

### GeoParquet (`[geoparquet]`) — NRP row-count mismatch

- NRP public-data catalog, `public-wyoming/blm-sma/stac-collection.json`, asset `blm-sma-parquet`. `table:row_count` declares **439,200** rows; the Parquet footer reports **865,059**. This is `TABLE_ROW_COUNT_MISMATCH` (ERROR: the footer was read and the value is exact).
- It was the only Parquet ERROR in a sweep of 638 single-file Parquet assets in that catalog. Partitioned (glob) assets could not be verified and produced WARNs.
- The sweep also found 61 declared-vs-Arrow type differences. These are WARN, because Arrow types have no one-to-one mapping to STAC types.
- No public issue reporting this mismatch was found.

## Other targets

- Boettiger Lab / NRP `rap-pfg-cover`: the previously reported 1-band declaration vs 6-band COG has been fixed upstream. The current catalog is clean.
  - Public issue: <https://github.com/boettiger-lab/data-workflows/issues/666>
- Copernicus Data Space Ecosystem `sentinel-2-l2a` and Earth Search `naip`: INCONCLUSIVE, because authentication or requester-pays access is required.

## Related public reports

- odc-stac: <https://github.com/opendatacube/odc-stac/issues/220> (proposal for STAC-vs-file analysis)
- openEO GeoPySpark driver: <https://github.com/Open-EO/openeo-geopyspark-driver/issues/1557>
