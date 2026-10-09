"""Production acquisition bridge for the CuriosityExecutive.

The executive's BOUNDARY_MISSING_TEACHER hair-trigger invokes
``acquisition_bridge(requirement) -> result``, where requirement is a
CapabilityRequirement built from the trigger. This module builds the
production bridge: a thin adapter that invokes a REAL AcquisitionPipeline
with REAL governed sources.

No stubs, no hard-coded candidates, no faked search. The pipeline's search
really executes against its sources; the result is the pipeline's actual
output (candidate found, or a truthful no-candidate with reasons).

Ownership: the bridge construction lives here (acquisition track). The
executive's production construction site that binds it lives at
runtime/curiosity/executive/production.py. Curiosity's refusal logic is
untouched -- the bridge adds acquisition; it never weakens refusal.
"""

from __future__ import annotations

from typing import Any, Callable


def build_acquisition_bridge(pipeline: Any) -> Callable[[Any], Any]:
    """Bind an executive acquisition_bridge to a real AcquisitionPipeline.

    Returns ``bridge(requirement)`` which invokes ``pipeline.acquire`` and
    returns the pipeline's real AcquisitionResult. The bridge itself holds
    no logic beyond the invocation -- discovery, scanning, sandboxing and
    the verdict all happen inside the pipeline, against its real sources.
    """
    def bridge(requirement: Any) -> Any:
        return pipeline.acquire(requirement)

    # Name it for traceability in logs and decision notes.
    bridge.__name__ = "governed_acquisition_bridge"
    bridge.__doc__ = (
        "Production acquisition bridge: invokes the bound real "
        "AcquisitionPipeline and returns its actual result."
    )
    return bridge
