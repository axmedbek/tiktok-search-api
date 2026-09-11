"""The broker's own boundary errors.

Here and NOT in `tiktoksearch/errors.py`, deliberately. That module is one
hierarchy rooted at `TikTokSearchError`, every member of it describes an
UPSTREAM outcome (risk-control, transport, the device pool) and every member is
mapped to an HTTP status by `api/app.py._domain_errors`. Neither class here is
any of those three things: they describe an inbound broker message and the
worker's own start-up environment, they have no HTTP mapping, and
`MalformedMessage` must be a `ValueError` subclass, which no member of that
hierarchy is. Putting them there would either break that module's single root
or give it a second one.
"""
from __future__ import annotations


class MalformedMessage(ValueError):
    """An inbound broker message this worker can never service.

    Raised in place of a bare `ValueError` so the consumer's ack policy can
    catch it BY TYPE. That is not decoration. The policy is "permanent → ack,
    transient → nack(requeue)", and a bare `except ValueError` on the permanent
    branch would also catch the incidental `ValueError`s that `json`,
    `requests` and `int()` raise on the TRANSIENT path — silently acking a job
    that should have been requeued, i.e. losing it.

    A `ValueError` subclass rather than a fresh hierarchy:
    `pydantic.ValidationError` is itself a `ValueError` (verified on pydantic
    2.13.5), so the two arrive together as
    `except (MalformedMessage, ValidationError)` and a caller that only knows
    `ValueError` still catches this.

    Its message text is always one of the module-level rejection strings that
    raise it — never the offending value. Producer-controlled text must not
    reach our logs, and the consumer logs a permanent error at WARNING.
    """


class BrokerConfigError(RuntimeError):
    """A fault in the worker's OWN wiring, not in the message it was handed.

    A missing or unusable credential, a queue with no parser, a record field
    colliding with a key the envelope layer owns. All three are our bug or our
    configuration, never something a producer sent.

    NOT a `ValueError`, so it can never be mistaken for a malformed body and
    acked: acking here would drop a perfectly good job because of a defect on
    our side. It escapes the ack policy entirely — at start-up it reaches the
    CLI, which reports it and exits non-zero; mid-job it stops the worker with
    the inbound message still unacked, so the broker requeues the job whole.
    Its message names KEYS and types only — never a credential value and never
    a record value.
    """
