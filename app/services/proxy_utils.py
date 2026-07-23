from __future__ import annotations

from urllib.parse import urlparse


def infer_protocol(value: str, fallback: str = "http") -> str:
    text = value.strip().lower()
    if "://" in text:
        return urlparse(text).scheme or fallback
    if fallback != "auto":
        return fallback
    return "http"


def normalize_proxy(value: str, protocol: str = "auto") -> str:
    text = value.strip()
    if not text:
        raise ValueError("empty proxy")
    if "://" in text:
        return text
    proto = infer_protocol(text, protocol)
    return f"{proto}://{text}"


def proxy_host_port(proxy_url: str) -> tuple[str, int]:
    parsed = urlparse(proxy_url)
    return parsed.hostname or "", parsed.port or 0


def classify_error(error: object) -> str:
    text = str(error).lower()
    if "429" in text or "too many requests" in text:
        return "youtube_rate_limit"
    if "captcha" in text:
        return "captcha"
    if "sign in to confirm" in text or "confirm you're not a bot" in text or "confirm you’re not a bot" in text:
        return "youtube_bot"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "unable to connect" in text or "proxyerror" in text or "connection reset" in text or "host unreachable" in text:
        return "proxy_dead"
    if "ssl" in text or "eoferror" in text or "unexpected_eof" in text or "bytes missing" in text:
        return "proxy_dead"
    return "unknown"
