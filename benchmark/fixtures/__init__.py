"""Offline fixture registry: deterministic minimal GeoTIFF + STAC builders."""
from .builders import BUILDERS, FixtureSpec, build

__all__ = ["BUILDERS", "FixtureSpec", "build"]
