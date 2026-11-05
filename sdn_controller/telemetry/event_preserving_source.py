"""Event-preserving telemetry source (RQ1 arm 1).

Pulls completed telemetry windows in strict sequence order from the
aggregator's durable window log over HTTP. Delivers every window exactly
once, in source order, as soon as it is available (the reference arm).

Sequence validation + defensive gap recovery live in
:class:`~sdn_controller.telemetry.windows_source.WindowsTelemetrySource`:
the log is the single source of truth, so in-order pulls cannot lose windows.
If the log reports a requested ``after_seq`` has aged out (``410``), the gap is
recorded in the delivery log and the source advances to the first available
window ? it never blocks.

This arm adds no overrides: the inherited pull loop already delivers every
window immediately. :class:`SampledPushTelemetrySource` subclasses it, so
``SAMPLE_EVERY=1`` is behaviourally identical to this reference arm.
"""

from .windows_source import WindowsTelemetrySource


class EventPreservingTelemetrySource(WindowsTelemetrySource):
    """Pulls every completed window in order over the aggregator window log."""
