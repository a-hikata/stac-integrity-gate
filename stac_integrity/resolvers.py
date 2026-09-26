"""Href resolvers: the transport/authentication hook.

A resolver turns the href declared in STAC (already made absolute against the
Item location) into the string that is handed to ``rasterio.open``. It is the
only place where signing, alternate endpoints or pre-signed URLs enter the
audit. Semantic comparison always uses the declared STAC metadata.

See docs/auth-design.md.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urljoin, urlparse
from pathlib import Path

__all__ = [
    "HrefContext",
    "HrefResolver",
    "ResolverUnavailableError",
    "alternate_resolver",
    "identity",
    "load_resolver",
    "planetary_computer_resolver",
]


@dataclass(frozen=True)
class HrefContext:
    """What a resolver may know about the href it resolves."""

    asset_key: str
    item_id: str
    source: str
    asset: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


class HrefResolver(Protocol):
    def __call__(self, href: str, context: HrefContext) -> str: ...


class ResolverUnavailableError(RuntimeError):
    """A resolver cannot be created (e.g. its optional dependency is missing)."""


def identity(href: str, context: HrefContext) -> str:
    """Default resolver: open the href exactly as declared."""
    return href


def planetary_computer_resolver() -> Callable[[str, HrefContext], str]:
    """Sign Microsoft Planetary Computer blob hrefs with a SAS token.

    Requires the optional ``planetary-computer`` package
    (``pip install "stac-integrity-gate[planetary-computer]"``). The import is
    checked here, once, so a missing package fails at creation rather than
    turning every asset into a warning.
    """
    try:
        import planetary_computer  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ResolverUnavailableError(
            "The Planetary Computer resolver needs the 'planetary-computer' package: "
            'pip install "stac-integrity-gate[planetary-computer]"'
        ) from exc

    def sign(href: str, context: HrefContext) -> str:
        return planetary_computer.sign(href)

    return sign


def _absolute(source: str, href: str) -> str:
    if urlparse(href).scheme:
        return href
    if urlparse(source).scheme in {"http", "https"}:
        return urljoin(source, href)
    return str((Path(source).resolve().parent / href).resolve())


def alternate_resolver(name: str) -> Callable[[str, HrefContext], str]:
    """Prefer ``asset["alternate"][name]["href"]`` (STAC Alternate Assets extension).

    Falls back to the original href when the asset has no such alternate.
    Example: CDSE publishes ``s3://eodata/...`` hrefs with an ``alternate.https``.
    """
    if not name:
        raise ValueError("alternate resolver needs an alternate name, e.g. 'https'")

    def pick(href: str, context: HrefContext) -> str:
        alternates = context.asset.get("alternate")
        if isinstance(alternates, Mapping):
            alt = alternates.get(name)
            if isinstance(alt, Mapping) and alt.get("href"):
                return _absolute(context.source, str(alt["href"]))
        return href

    return pick


def load_resolver(spec: str) -> Callable[[str, HrefContext], str]:
    """Create a resolver from a CLI spec.

    Built-in names (no ``:``): ``identity``, ``planetary-computer``,
    ``alternate-<name>`` (e.g. ``alternate-https``). Anything with ``:`` is an
    import path ``package.module:function`` naming a resolver callable.
    """
    spec = spec.strip()
    if spec == "identity":
        return identity
    if spec == "planetary-computer":
        return planetary_computer_resolver()
    if spec.startswith("alternate-"):
        return alternate_resolver(spec[len("alternate-"):])
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(
            f"Unknown resolver {spec!r}: use 'planetary-computer', 'alternate-<name>' "
            "or an import path 'module:function'"
        )
    module = importlib.import_module(module_name)
    obj: Any = module
    for part in attr.split("."):
        obj = getattr(obj, part)
    if not callable(obj):
        raise TypeError(f"Resolver {spec!r} is not callable")
    return obj
