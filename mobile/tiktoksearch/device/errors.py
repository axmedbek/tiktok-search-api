"""The device driver's own boundary errors.

Here and NOT in `tiktoksearch/errors.py`, for the reason
`broker/errors.py` gives for its own two classes: that module is one hierarchy
rooted at `TikTokSearchError`, every member describes an UPSTREAM outcome of a
SIGNED request (risk-control, transport, the device pool) and every member is
mapped to an HTTP status by `api/app.py._domain_errors`. Nothing here is any of
those. The device path issues no signed request at all — it opens a profile in
the app and reads what mitmproxy spooled — so it cannot be rate-limited, cannot
be shadow-banned, and has no HTTP mapping.

**Every one of these classifies TRANSIENT**, and that is the whole reason they
share a base class: `broker/device_source.py` maps `DeviceError` onto the
EXISTING ack policy's `Failure.TRANSIENT` in one place, so the broker's ack
table stays the single policy (`.claude/rules/learned-lessons.md` — one policy,
not two). A Waydroid container that is down, a session that is not on screen,
an app that did not answer in time and a body we could not read are all
conditions that a later redelivery can succeed at, so the job is requeued
rather than acked empty.
"""
from __future__ import annotations


class DeviceError(Exception):
    """Any failure of the device harvest path. Always transient.

    Its message text is always built from our own literals plus non-secret
    values (a public `user_id`, a timeout, a binary name, an exit code). No
    cookie, token, broker credential or response body ever reaches it — it is
    logged by the consumer at WARNING and can reach an operator's terminal.
    """


class IntentFailed(DeviceError):
    """`waydroid app intent` could not be run, or exited non-zero.

    The binary is missing from PATH, the Waydroid session is not running, the
    Wayland session variables do not point at a live compositor, or the CLI
    itself hung. Transient: the container comes back.
    """


class HarvestTimeout(DeviceError):
    """The intent fired but no spool entry appeared for this visit in time.

    A NORMAL outcome, not a defect — the app may have been slow, backgrounded,
    or shown a login wall. It is raised (never swallowed into an empty result)
    precisely so the job is requeued instead of being acked with zero posts:
    "no response" and "this account has no posts" are different facts and the
    broker must not publish the second when it measured the first.
    """


class UnreadableResponse(DeviceError):
    """A response for this visit arrived, and its body could not be read.

    Distinct from `HarvestTimeout` on purpose: this is the `tt_orcas_res: 1`
    shape (HTTP 200, 0 bytes), a body mitmproxy could not decode, or a JSON
    object carrying no `aweme_list` list. All of those mean "we got an answer
    and did not understand it", which `.claude/rules/anti-block.md` forbids
    reporting as an empty success.
    """


class UnusableUserId(DeviceError):
    """The `user_id` handed to the driver is not a TikTok uid.

    A defensive guard on the value that is about to become a deep-link URI:
    the driver's caller reads it off `POST /profile`, which answers TikTok's
    own numeric uid, so this cannot fire on the live path. It is a
    `DeviceError` — and so transient — rather than its own classification,
    because inventing a second ack outcome for a condition that cannot occur
    would be a policy nobody ever exercises.
    """
