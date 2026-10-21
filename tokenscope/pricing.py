"""API-equivalent prices and cost-weighted tokens.

Prices: USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing (fetched 2026-10-10).
Tuple = (input, 5-minute cache write, 1-hour cache write, cache read, output). Model ids match a key exactly or the
key followed by a date suffix (-20251001) or a bracket tag ([1m]); the longest such key wins. Anything else (and
"<synthetic>") is unpriced: cost None, counted as "unpriced". On a subscription none of this is billed; it is the
price the same calls would have on the API.

Cost-weighted tokens put the four token types on one scale using the standard price ratios relative to uncached
input (the same for every model in the table): a cache read costs a tenth of an input token, a cache write 1.25x
(5m TTL), an output token 5x. Unlike "all-in" (a plain sum), cache reads no longer dominate.
"""
import re

PRICES = {'claude-fable-5-1': (10, 12.5, 20, 0.25, 50), 'claude-fable-5': (10, 12.5, 20, 1, 50),
          'claude-opus-5-5': (4, 5, 8, 0.20, 20), 'claude-opus-5': (5, 6.25, 10, 0.5, 25),
          'claude-opus-4-8': (5, 6.25, 10, 0.5, 25), 'claude-opus-4-7': (5, 6.25, 10, 0.5, 25),
          'claude-sonnet-5-5': (2, 2.5, 4, 0.10, 10), 'claude-sonnet-5': (2, 2.5, 4, 0.2, 10),
          'claude-sonnet-4-6': (3, 3.75, 6, 0.3, 15), 'claude-sonnet-4-5': (3, 3.75, 6, 0.3, 15),
          'claude-haiku-4-5': (1, 1.25, 2, 0.1, 5),
          # Gemini models (Google Antigravity)
          'gemini-4-argon': (2.00, 2.00, 2.00, 0.10, 10.00),
          'gemini-3.8-flash': (0.15, 0.20, 0.20, 0.0375, 0.60),
          'gemini-3.8-pro': (1.50, 2.00, 2.50, 0.375, 6.00),
          'gemini-2.5-pro': (1.25, 1.50, 2.00, 0.3125, 5.00),
          'gemini-2.0-flash': (0.10, 0.15, 0.15, 0.025, 0.40),
          'gemini-2.0-pro': (1.25, 1.50, 2.00, 0.3125, 5.00),
          'gemini-1.5-flash': (0.075, 0.10, 0.10, 0.01875, 0.30),
          'gemini-1.5-pro': (1.25, 1.50, 2.00, 0.3125, 5.00),
          'gemini-flash': (0.15, 0.20, 0.20, 0.0375, 0.60),
          'gemini-pro': (1.25, 1.50, 2.00, 0.3125, 5.00)}
PRICES_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing (fetched 2026-10-10)"

# cost-weighted tokens = input x W_INPUT + cache_write x W_CACHE_WRITE + cache_read x W_CACHE_READ + output x W_OUTPUT
W_INPUT = 1.0
W_CACHE_WRITE = 1.25
W_CACHE_READ = 0.1
W_OUTPUT = 5.0
COST_WEIGHTED_FORMULA = "input x1 + cache write x1.25 + cache read x0.1 + output x5"

_SUFFIX_RE = re.compile(r"^(-\d{8})?(\[[^\]]*\])?$")


def price_of(model):
    """Price tuple for a model id, or None if unknown."""
    if not isinstance(model, str) or not model or model.startswith("<"):
        return None
    best = None
    for key in PRICES:
        if model.startswith(key) and _SUFFIX_RE.match(model[len(key):]):
            if best is None or len(key) > len(best):
                best = key
    if best:
        return PRICES[best]
    # Check normalized version for human / UI model names (e.g. 'Gemini 3.8 Flash (Low)', 'Gemini 3.8 Flash (High)')
    norm = re.sub(r"[()\[\]]", "", model.lower()).strip()
    norm = re.sub(r"[\s_]+", "-", norm)
    for key in PRICES:
        if norm == key or norm.startswith(key + "-") or norm.startswith(key):
            rem = norm[len(key):]
            if not rem or rem.startswith("-") or _SUFFIX_RE.match(rem):
                # Ensure we don't match unpriced version numbers like claude-opus-5-7 as claude-opus-5
                if key.startswith("claude-") and not _SUFFIX_RE.match(rem):
                    continue
                if best is None or len(key) > len(best):
                    best = key
    return PRICES[best] if best else None


def cost_usd(model, u):
    """API-equivalent USD for one call's normalized usage (analyze.norm_usage), or None if unpriced."""
    p = price_of(model)
    if p is None:
        return None
    w1h = u.get("cache_write_1h", 0)
    w5m = max(u.get("cache_write", 0) - w1h, 0)  # unsplit writes are priced at the 5m rate
    return (u.get("input", 0) * p[0] + w5m * p[1] + w1h * p[2] + u.get("cache_read", 0) * p[3]
            + u.get("output", 0) * p[4]) / 1e6


def cost_weighted(u):
    return (u.get("input", 0) * W_INPUT + u.get("cache_write", 0) * W_CACHE_WRITE
            + u.get("cache_read", 0) * W_CACHE_READ + u.get("output", 0) * W_OUTPUT)


def cost_weighted_of(new_input=0, cache_write=0, cache_read=0, output=0):
    return new_input * W_INPUT + cache_write * W_CACHE_WRITE + cache_read * W_CACHE_READ + output * W_OUTPUT
