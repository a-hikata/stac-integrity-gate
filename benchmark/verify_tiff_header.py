#!/usr/bin/env python3
"""Independent GeoTIFF header reader for benchmark verification.

Deliberately uses neither GDAL nor Rasterio: it fetches byte ranges over
HTTP(S) (or reads a local file) and parses the first TIFF IFD directly, so
that stac-integrity findings can be cross-checked against a reader that
shares no code with the tool under test.

Usage:
    python benchmark/verify_tiff_header.py <tif-url-or-path> [...]

s3://bucket/key hrefs are rewritten to https://bucket.s3.amazonaws.com/key
(anonymous access only).
"""
from __future__ import annotations

import json
import struct
import sys
import urllib.request
from pathlib import Path

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8, 17: 8, 18: 8}
TYPE_FMT = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d", 16: "Q", 17: "q", 18: "Q"}
SAMPLE_FORMAT = {1: "uint", 2: "int", 3: "float"}


def _https(href: str) -> str:
    if href.startswith("s3://"):
        bucket, _, key = href[5:].partition("/")
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    return href


class Reader:
    def __init__(self, href: str):
        self.href = _https(href)
        self.local = not self.href.startswith(("http://", "https://"))

    def read(self, offset: int, length: int) -> bytes:
        if self.local:
            with open(self.href, "rb") as fh:
                fh.seek(offset)
                return fh.read(length)
        req = urllib.request.Request(
            self.href,
            headers={"Range": f"bytes={offset}-{offset + length - 1}", "User-Agent": "tiff-header-verify/1"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()[:length]


def parse(href: str) -> dict:
    rd = Reader(href)
    head = rd.read(0, 16)
    bo = {b"II": "<", b"MM": ">"}[head[:2]]
    magic = struct.unpack(bo + "H", head[2:4])[0]
    big = magic == 43
    if big:
        ifd_off = struct.unpack(bo + "Q", head[8:16])[0]
        cnt_fmt, cnt_size, entry_size, inline = "Q", 8, 20, 8
    else:
        ifd_off = struct.unpack(bo + "I", head[4:8])[0]
        cnt_fmt, cnt_size, entry_size, inline = "H", 2, 12, 4

    n = struct.unpack(bo + cnt_fmt, rd.read(ifd_off, cnt_size))[0]
    raw = rd.read(ifd_off + cnt_size, n * entry_size)
    tags: dict[int, object] = {}
    for i in range(n):
        e = raw[i * entry_size:(i + 1) * entry_size]
        tag, typ = struct.unpack(bo + "HH", e[:4])
        count = struct.unpack(bo + ("Q" if big else "I"), e[4:4 + (8 if big else 4)])[0]
        size = TYPE_SIZES.get(typ, 1) * count
        vbytes = e[-inline:]
        if size > inline:
            voff = struct.unpack(bo + ("Q" if big else "I"), vbytes)[0]
            if tag in (273, 279, 324, 325) and size > 4096:
                continue  # strip/tile offsets: not needed
            data = rd.read(voff, size)
        else:
            data = vbytes[:size]
        if typ == 2:
            tags[tag] = data.rstrip(b"\x00").decode("latin-1")
        elif typ in TYPE_FMT:
            tags[tag] = list(struct.unpack(bo + TYPE_FMT[typ] * count, data))
        elif typ in (5, 10):
            vals = struct.unpack(bo + ("I" if typ == 5 else "i") * (2 * count), data)
            tags[tag] = [vals[j] / vals[j + 1] for j in range(0, len(vals), 2)]

    width = tags[256][0]
    height = tags[257][0]
    spp = tags.get(277, [1])[0]
    bits = tags.get(258, [1])[0]
    fmt = SAMPLE_FORMAT.get(tags.get(339, [1])[0], "?")
    dtype = f"{fmt}{bits}" if bits > 1 else "bool"
    out = {
        "href": href,
        "bigtiff": big,
        "width": width,
        "height": height,
        "shape_hw": [height, width],
        "band_count": spp,
        "dtype": dtype,
        "nodata": tags.get(42113),
    }
    scale = tags.get(33550)
    tie = tags.get(33922)
    if scale and tie:
        sx, sy = scale[0], scale[1]
        i, j, _, x, y, _ = tie[:6]
        ox = x - i * sx
        oy = y + j * sy
        out["transform"] = [sx, 0.0, ox, 0.0, -sy, oy]
        out["bounds"] = [ox, oy - height * sy, ox + width * sx, oy]
    elif 34264 in tags:
        m = tags[34264]
        out["transform"] = [m[0], m[1], m[3], m[4], m[5], m[7]]
    geokeys = tags.get(34735)
    if geokeys:
        nkeys = geokeys[3]
        for k in range(nkeys):
            key, loc, _cnt, val = geokeys[4 + 4 * k:8 + 4 * k]
            if loc == 0 and key == 3072:
                out["epsg_projected"] = val
            if loc == 0 and key == 2048:
                out["epsg_geographic"] = val
            if loc == 0 and key == 1025:
                out["raster_type"] = {1: "PixelIsArea", 2: "PixelIsPoint"}.get(val, val)
    if out.get("raster_type") == "PixelIsPoint" and "transform" in out:
        # GDAL (and STAC proj:transform, which follows GDAL) reports the
        # pixel-corner grid: shift the tiepoint by half a pixel, as GDAL does
        # by default (GTIFF_POINT_GEO_IGNORE=FALSE).
        a, _, c, _, e, f = out["transform"]
        out["tiepoint_transform_pixel_is_point"] = list(out["transform"])
        out["transform"] = [a, 0.0, c - a / 2, 0.0, e, f - e / 2]
        c2, f2 = out["transform"][2], out["transform"][5]
        out["bounds"] = [c2, f2 + height * e, c2 + width * a, f2]
    if 42112 in tags:
        out["gdal_metadata"] = tags[42112][:400]
    return out


def main(argv: list[str]) -> int:
    rc = 0
    for href in argv:
        try:
            print(json.dumps(parse(href)))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"href": href, "error": f"{type(exc).__name__}: {exc}"}))
            rc = 2
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
