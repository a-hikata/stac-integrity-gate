"""Redaction of credentials that may appear in URLs and error messages.

GDAL error messages echo the full URL they tried to open. When hrefs are signed
(Azure SAS, AWS presigned URLs, token query parameters) the signature would
otherwise leak into findings, JSON output and CI logs.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "REDACTED"

# Query parameter names whose values are credentials or signing material.
# Azure SAS: sig, se, st, sp, sv, sr, spr, srt, ss, sip, skoid, sktid, skt, ske,
# sks, skv, saoid, suoid, scid, sdd, ses. AWS SigV4: X-Amz-*. GCS: X-Goog-*.
# CloudFront: Signature, Policy, Key-Pair-Id. Generic token/key names.
_SENSITIVE_NAMES = {
    "sig", "se", "st", "sp", "sv", "sr", "spr", "srt", "ss", "sip",
    "skoid", "sktid", "skt", "ske", "sks", "skv", "saoid", "suoid", "scid", "sdd", "ses",
    "signature", "policy", "key-pair-id", "googleaccessid", "awsaccesskeyid",
    "token", "access_token", "id_token", "refresh_token", "auth", "authorization",
    "key", "api_key", "apikey", "api-key", "code", "password", "passwd", "pwd",
    "secret", "client_secret", "credential", "credentials", "session_token",
    "security_token", "jwt", "bearer",
}
_SENSITIVE_PREFIXES = ("x-amz-", "x-goog-")

# name=value inside a query string (after ? or & or ;). Value ends at the next
# separator, whitespace, quote, bracket, fragment, or a ':' that ends a clause
# ("...sig=abc: 403"); ':' inside a value (unencoded timestamps) is kept.
_QUERY_PARAM = re.compile(r"(?P<sep>[?&;])(?P<name>[^=&;?#\s'\"<>()\[\]]+)=(?P<value>(?:[^&;#\s'\"<>()\[\]:]|:(?!\s|$))*)")
# scheme://user:password@host  or  scheme://token@host
_USERINFO = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^/@\s'\"]+)@")
# Authorization: Bearer xyz / Authorization: Basic xyz / bare "Bearer xyz"
_AUTH_HEADER = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:(bearer|basic|token)\s+)?[^\s,'\"]+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]+=*")


def _is_sensitive(name: str) -> bool:
    n = name.lower()
    return n in _SENSITIVE_NAMES or n.startswith(_SENSITIVE_PREFIXES)


def _query_sub(match: re.Match[str]) -> str:
    if not _is_sensitive(match.group("name")) or not match.group("value"):
        return match.group(0)
    return f"{match.group('sep')}{match.group('name')}={REDACTED}"


def _auth_sub(match: re.Match[str]) -> str:
    scheme = f"{match.group(2)} " if match.group(2) else ""
    return f"{match.group(1)}{scheme}{REDACTED}"


def redact(text: str) -> str:
    """Return ``text`` with credential-bearing URL parts replaced by ``REDACTED``."""
    if not text:
        return text
    text = _QUERY_PARAM.sub(_query_sub, text)
    text = _USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)
    text = _AUTH_HEADER.sub(_auth_sub, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return text


def redact_value(value: Any) -> Any:
    """Redact strings (and strings inside lists/tuples); leave other values alone."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    return value
