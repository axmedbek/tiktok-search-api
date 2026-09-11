"""Bounds that more than one layer has to enforce on the same value.

Here rather than in `api/schemas.py` because the API is no longer the only
caller. The broker worker validates the same inbound strings before spending a
signed request on them, and reaching for a bound inside the API package made
`import tiktoksearch.broker` execute `api/__init__.py`, which eagerly builds
the FastAPI app module — measured: 156 ms of import time, and `fastapi` plus
`starlette` resident, in a process that never serves HTTP, for five integers
and a regex.

The repo already points this way: `api/schemas.py` imports
`MAX_PAGE_TOKEN_CHARS` from `..paging` and the filter enums from `..filters`.
A bound lives in the domain module and the API layer imports upward.

Every value here is unchanged from the one `api/schemas.py` held, and
`api/schemas.py` re-exports all of them, so nothing that imported them from
there has to change.
"""
from __future__ import annotations

# Ceiling on any string this service sends upstream as a search keyword. Named
# rather than inline because `/user/posts` DERIVES a keyword (the account's
# display name) and has to bound it against the same limit the search path
# accepts from a caller — see `app._posts_keywords`. The broker bounds
# `keyword_name` against it too, so a message the worker accepts is never one
# `POST /search` would then reject.
MAX_QUERY_CHARS = 200

# Ceiling on `limit` for one search page. The server also caps it by the
# profile's `max_results_per_search` (300 on the shipped profile), so this is
# the ceiling on what a caller may ASK for, not a promise about what arrives.
MAX_SEARCH_LIMIT = 300
# `/user/posts` is SERVED BY the search path, so it cannot meaningfully ask for
# more than a search page may return. DERIVED from the search ceiling rather
# than written out as a second 300: the two are REQUIRED to agree, and a
# comment saying they agree is not a mechanism that keeps them agreeing.
MAX_POSTS_LIMIT = MAX_SEARCH_LIMIT

# `/profile` and `/user/posts` are both entered by HANDLE, and that handle
# reaches a SIGNED TikTok URL as the search keyword — so it is bounded and
# charset-checked at whatever boundary it arrives at (HTTP body, broker
# message), never downstream. TikTok handles are letters, digits, '.' and '_',
# up to 24 characters; anything else is a client error, not something to
# forward upstream and hope.
MAX_USERNAME_CHARS = 24
USERNAME_PATTERN = r'^[A-Za-z0-9._]+$'
