"""Explicit workspace egress selection; loopback clients never use this policy."""
from __future__ import annotations
import os

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def external_trust_env() -> bool:
    mode = os.getenv("JOBHOUND_EXTERNAL_NETWORK_MODE", "direct")
    if mode not in {"direct", "environment"}:
        raise ValueError("JOBHOUND_EXTERNAL_NETWORK_MODE must be direct or environment")
    return mode == "environment"


def network_failure_hint() -> str:
    if not external_trust_env() and any(os.getenv(key) for key in PROXY_KEYS):
        return " Ambient proxy detected; restart with JOBHOUND_EXTERNAL_NETWORK_MODE=environment if required by this workspace."
    return ""
