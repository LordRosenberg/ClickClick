"""Source-local strategy selection; a probe outcome never proves task utility."""
from __future__ import annotations

import copy
import hashlib
from pydantic import BaseModel, ConfigDict, Field


class StrategyChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected: str = Field(min_length=1, max_length=64, description="ID of a recovery option.")
    reason: str = Field(min_length=1, max_length=700, description="Compare mechanism, full cost and unknowns with other options, including the existing route.")


def option_id(option):
    return option.get("id") or hashlib.sha256(option["route"].encode()).hexdigest()[:16]


def select_strategy(analysis, previous):
    options = analysis.get("recovery_options", [])
    viable = [o for o in options if o["status"] != "contradicted"]
    choice = analysis.get("strategy_choice")
    if not choice:
        if len(viable) > 1:
            raise ValueError("multiple recovery strategies require strategy_choice before exploration")
        return None  # Legacy, single-gap and efficient-success paths remain valid.
    choice = StrategyChoice.model_validate(choice).model_dump()
    chosen = next((o for o in viable if option_id(o) == choice["selected"]), None)
    if chosen is None:
        raise ValueError("selected strategy is absent or contradicted")
    mechanisms = [o.get("mechanism", "").strip().casefold() for o in viable]
    if len(viable) > 1 and (any(not m for m in mechanisms) or len(set(mechanisms)) != len(mechanisms)):
        raise ValueError("competing strategies require distinct mechanisms, not wording variants")
    for row in previous:
        old = row.get("strategy_comparison") or {}
        if (old.get("selected") == choice["selected"]
                and old.get("next_test") == chosen["next_test"]):
            raise ValueError("strategy test already dispatched; name a new discriminating gap")
    return {**choice, "next_test": chosen["next_test"], "options": copy.deepcopy(options),
        "policy": "Compared hypotheses at dispatch; outcome/cost below are measurements, not route verification or task success."}


POLICY = """Give recovery options stable id and distinct mechanism; retain id while
the mechanism stays unchanged. Before exploring among multiple viable options, use
strategy_choice:{selected:id,reason:full-cost/uncertainty comparison}. Include the
existing route or explain infeasibility. Test the selected option's earliest gap;
the host records choice, outcome and cost in acquisition_ledger.recent_probes.
Retain untested alternatives; update statuses only with native evidence. No forced
matrix or renamed repeats; all options share existing budget and independent audit.
"""
