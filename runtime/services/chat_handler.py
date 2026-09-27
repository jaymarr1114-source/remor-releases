"""Conversational minimum - the honest chat handler (Worker 2).

`answer(text, services) -> dict`.

GENUINE BOUNDARY, stated up front: there is NO language substrate anywhere
in this runtime. No LLM, no dialogue model, no paraphraser, nothing that can
infer, improvise, or make small talk. This handler therefore answers ONLY
from real readable state, through real store reads, using fixed
structured templates. Everywhere else it says "I don't know". It never
guesses, never invents facts, never claims actions it did not take, and
never executes anything when the message is flagged ambiguous.

Routing contract (Worker 1's discriminator owns the outer decision; this
module only ever sees chat-mode text or an ambiguity flag):
  - ambiguous=True  -> clarifying question (zero store reads, zero actions)
  - chat-mode text  -> classified into a message kind; each kind maps to
    real read operations on the services dict returned by
    http_adapter.build_services():
      capabilities  -> CapabilityAPI.list_capabilities("all")  [real store]
      runs          -> scheduler.list_runs()                    [real runs]
      run_status    -> scheduler.get_run(run_id)                [real record]
                        + acceptance overlay state when present
      accept_run    -> acceptance.record_verdict(satisfied=True)
                        [STATE-CHANGING: the only write kinds in this
                        handler; writes go ONLY to the acceptance overlay,
                        never to the capability/evidence/scheduler DBs]
      reject_run    -> acceptance.record_verdict(satisfied=False, feedback)
                        [STATE-CHANGING: same overlay-only rule]
      identity      -> EvidenceStore.get_document("soul.md")     [hosted doc]
      document      -> EvidenceStore.get_document(name)          [hosted doc]
      status        -> metering.tier() + metering.tasks_today()  [real caps]
      evidence      -> EvidenceStore.list_entries()              [real store]
      file_action   -> honest "no": this handler is read-only
      name          -> honest "I don't know": no identity store exists
      chit_chat     -> minimal acknowledgment, never invents facts
      unknown       -> honest "I don't know" (the fallback is refusal,
                       never confabulation)

Return shape:
  {"mode": "answer"|"clarify"|"acknowledge"|"refuse",
   "kind": "<classification>",
   "text": "<the reply>",
   "grounded": {provenance} | None}

"grounded" carries exactly which store was read and what it returned, so
tests can prove causality (answer changes when the store changes) and so
a caller can audit every claim. For "clarify"/"acknowledge"/"refuse" no
store is read and grounded is None (or notes the absence).

This module imports nothing from Worker 1's discriminator and works
standalone: `services` is just a dict, and every lookup degrades honestly
when a service is absent.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# classification patterns (first match wins; order documented in _classify)
# ---------------------------------------------------------------------------

_RUN_ID = r"([A-Za-z0-9][A-Za-z0-9\-_.]{1,63})"

_PATTERNS: List[Tuple[str, List[str]]] = [
    ("run_status", [
        r"\bstatus of (?:the )?run\s+" + _RUN_ID,
        r"\brun\s+" + _RUN_ID + r"\s+(?:status|complete[sd]?|finish(?:ed)?|"
        r"succeed(?:ed)?|fail(?:ed)?)\b",
        r"\bdid (?:the )?run\s+" + _RUN_ID + r"\b",
        r"\bwhat happened (?:to|with) (?:the )?run\s+" + _RUN_ID + r"\b",
    ]),
    ("accept_run", [
        r"\baccept (?:the )?run\s+" + _RUN_ID + r"\b",
        r"\b(?:i'm|i am) (?:happy|satisfied) with (?:the )?run\s+"
        + _RUN_ID + r"\b",
        r"\brun\s+" + _RUN_ID + r"\s+(?:looks good|is good|is fine)\b",
    ]),
    ("reject_run", [
        r"\breject (?:the )?run\s+" + _RUN_ID + r"\b",
        r"\b(?:i'm|i am) not (?:happy|satisfied) with (?:the )?run\s+"
        + _RUN_ID + r"\b",
        r"\brun\s+" + _RUN_ID + r"\s+(?:is wrong|is not (?:right|good enough)|"
        r"failed me|missed)\b",
    ]),
    ("capabilities", [
        r"\bwhat can you do\b",
        r"\bcapabilit(?:y|ies)\b",
        r"\bwhat are your (?:features|functions|capabilities)\b",
        r"\blist (?:your |the )?(?:features|functions|capabilities)\b",
        r"\bshow me (?:your |the )?(?:features|functions|capabilities)\b",
    ]),
    ("runs", [
        r"\bwhat have you been doing\b",
        r"\bwhat did you (?:do|work on)\b",
        r"\bwhat are you working on\b",
        r"\brecent runs\b",
        r"\blatest runs\b",
        r"\brun history\b",
        r"\bshow me (?:your |the )?(?:recent |latest )?(?:runs|tasks)\b",
        r"\byour (?:recent |latest )?(?:runs|tasks)\b",
    ]),
    ("document", [
        # explicit hosted-document mentions come before the identity kind
        # ("who are you" carries no document mention and falls through).
        r"\b(?:theory\.md|soul\.md|hypothetical inferences\.md)\b",
        r"\bwhat does (?:the )?(.{1,60}?) (?:say|contain|state)\b",
        r"\bshow me (?:the )?(.{1,60}?\.md)\b",
        r"\b(hosted )?documents?\b",
    ]),
    ("identity", [
        r"\bwho are you\b",
        r"\bwhat are you\b",
        r"\bintroduce yourself\b",
        r"\byour name\b",
    ]),
    ("status", [
        r"\btier\b",
        r"\btasks today\b",
        r"\btasks per day\b",
        r"\bquota\b",
        r"\blimits?\b",
        r"\bhow busy\b",
        r"\bsystem status\b",
        r"\bcapacity\b",
        r"\bwhat.{0,25}status\b",
    ]),
    ("evidence", [
        r"\bhypothes[ie]s\b",
        r"\bobservations?\b",
        r"\binferences?\b",
        r"\bevidence\b",
        r"\bwhat have you learned\b",
    ]),
    ("file_action", [
        r"\bdid you (?:delete|modify|change|create|write|remove|touch)\b",
        r"\bhave you deleted\b",
    ]),
    ("name", [
        r"\bmy name\b",
        r"\bwho am i\b",
        r"\bdo you know my name\b",
    ]),
]

_CHITCHAT = re.compile(
    r"^(hi|hey|hello|yo|thanks|thank you|thx|good morning|good afternoon|"
    r"good evening|how'?s it going|how are you|how is it going|sup)"
    r"\s*[?!.]*$",
    re.IGNORECASE,
)

_DOCUMENTS = ("soul.md", "theory.md", "hypothetical inferences.md")

_DONT_KNOW = "I don't know"


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def answer(text: Any, services: Optional[Dict[str, Any]],
           ambiguous: bool = False,
           readings: Optional[List[str]] = None) -> Dict[str, Any]:
    """Answer one chat-mode message from real readable state.

    `text`: the message. `services`: the dict from
    http_adapter.build_services() (only dict access is used, so any dict
    with the same keys works). `ambiguous`: set by Worker 1's
    discriminator when the message has multiple readings -- the handler
    then asks a clarifying question, performs zero store reads, and
    executes nothing. `readings`: the discriminator's candidate readings;
    when given they are restated verbatim (never invented).
    """
    services = services or {}

    # Empty / non-text input is refused honestly, never interpreted.
    if not isinstance(text, str) or not text.strip():
        return {"mode": "refuse", "kind": "refuse",
                "text": "I can't answer that: the message was empty "
                        "(or not text).",
                "grounded": None}

    # Ambiguity: clarifying question only. No store reads, no actions,
    # no guessing at the intended meaning.
    if ambiguous:
        return _clarify(readings)

    lowered = text.strip().lower()
    kind, match = _classify(lowered)

    if kind == "run_status":
        return _answer_run_status(match, services)
    if kind == "accept_run":
        return _answer_accept_run(match, services)
    if kind == "reject_run":
        return _answer_reject_run(match, text, services)
    if kind == "capabilities":
        return _answer_capabilities(services)
    if kind == "runs":
        return _answer_runs(services)
    if kind == "document":
        return _answer_document(match, lowered, services)
    if kind == "identity":
        return _answer_identity(services)
    if kind == "status":
        return _answer_status(services)
    if kind == "evidence":
        return _answer_evidence(services)
    if kind == "file_action":
        return _answer_file_action()
    if kind == "name":
        return _answer_name()
    if kind == "chit_chat":
        return _answer_chitchat(lowered)
    return _answer_fallback()


def _classify(lowered: str) -> Tuple[str, Optional[re.Match]]:
    for kind, patterns in _PATTERNS:
        for pat in patterns:
            m = re.search(pat, lowered, re.IGNORECASE)
            if m:
                return kind, m
    if _CHITCHAT.match(lowered.strip()):
        return "chit_chat", None
    return "unknown", None


def _clarify(readings: Optional[List[str]]) -> Dict[str, Any]:
    if readings:
        numbered = "; ".join(
            f"({i + 1}) {r}" for i, r in enumerate(readings))
        question = (
            f"That could mean two different things: {numbered}. "
            "Which did you mean? I won't guess -- and I won't act "
            "until you clarify.")
    else:
        question = (
            "I'm not sure what you mean -- could you rephrase? I can "
            "answer grounded questions about my recorded capabilities "
            "('what can you do'), recent runs ('what have you been "
            "doing'), hosted documents ('what does theory.md say'), and "
            "system status ('what is my tier'). I won't guess at your "
            "meaning, and I won't act on an ambiguous message.")
    return {"mode": "clarify", "kind": "ambiguous", "text": question,
            "grounded": {"kind": "ambiguous", "store": None,
                         "readings": list(readings) if readings else None,
                         "note": "no store read; no action taken"}}


# ---------------------------------------------------------------------------
# grounded answers (each reads real state, or says so honestly)
# ---------------------------------------------------------------------------

def _safe(fn):
    try:
        return True, fn()
    except Exception as exc:  # never invent; report the unreadable store
        return False, f"{type(exc).__name__}: {exc}"


def _answer_capabilities(services: Dict[str, Any]) -> Dict[str, Any]:
    api = services.get("capabilities")
    if api is None:
        return _unreadable("capabilities", "capability service",
                           "the capability store is not available in this "
                           "service graph")
    ok, res = _safe(lambda: api.list_capabilities("all"))
    if not ok:
        return _unreadable("capabilities", "capability store", res)
    caps = res.get("capabilities", []) if isinstance(res, dict) else []
    names = [c.get("name", "?") for c in caps]
    grounded = {"kind": "capabilities",
                "store": "CapabilityAPI.list_capabilities('all') over the "
                         "real capability store",
                "count": len(caps), "names": names}
    if not caps:
        return {"mode": "answer", "kind": "capabilities", "grounded": grounded,
                "text": f"{_DONT_KNOW} what I can do: the capability store "
                        "is empty -- no capabilities are recorded. I will "
                        "not guess at abilities I might have."}
    shown = ", ".join(
        f"{c.get('name', '?')} ({c.get('status', '?')})" for c in caps[:20])
    extra = f" (+{len(caps) - 20} more)" if len(caps) > 20 else ""
    return {"mode": "answer", "kind": "capabilities", "grounded": grounded,
            "text": f"From the real capability store ({len(caps)} recorded): "
                    f"{shown}{extra}. These are capabilities admitted "
                    "through the engine's real admission machinery; this "
                    "list is the whole record -- I have no other abilities "
                    "to report."}


def _answer_runs(services: Dict[str, Any]) -> Dict[str, Any]:
    sched = services.get("scheduler")
    if sched is None:
        return _unreadable("runs", "scheduler",
                           "the run history is not available in this "
                           "service graph")
    ok, rows = _safe(lambda: sched.list_runs(limit=10))
    if not ok:
        return _unreadable("runs", "scheduler", rows)
    grounded = {"kind": "runs", "store": "scheduler.list_runs(limit=10)",
                "count": len(rows)}
    if not rows:
        return {"mode": "answer", "kind": "runs", "grounded": grounded,
                "text": f"{_DONT_KNOW} what I've been doing: no runs are "
                        "recorded in the scheduler."}
    parts = [f"{r.get('id', '?')[:8]} '{r.get('goal', '?')}' -- "
             f"{r.get('status', '?')}" for r in rows]
    return {"mode": "answer", "kind": "runs", "grounded": grounded,
            "text": "From the real scheduler (most recent first): "
                    + "; ".join(parts) + "."}


def _answer_run_status(match: Optional[re.Match],
                       services: Dict[str, Any]) -> Dict[str, Any]:
    run_id = match.group(1) if match else ""
    sched = services.get("scheduler")
    if sched is None:
        return _unreadable("run_status", "scheduler",
                           "the run history is not available in this "
                           "service graph")
    ok, rec = _safe(lambda: sched.get_run(run_id))
    if not ok:
        return _unreadable("run_status", "scheduler", rec)
    grounded = {"kind": "run_status", "store": "scheduler.get_run(run_id)",
                "run_id": run_id, "found": rec is not None}
    # Acceptance overlay (M6): consulted for acceptance state, and as a
    # fallback record when the scheduler never saw the run (a presented
    # attempt is a real record even without a scheduler row). The scheduler
    # is never edited here.
    acc = services.get("acceptance")
    arec = None
    if acc is not None:
        ok_a, got = _safe(lambda: acc.store.get(run_id))
        if ok_a:
            arec = got
    if rec is None:
        if arec is None:
            return {"mode": "answer", "kind": "run_status",
                    "grounded": grounded,
                    "text": f"I have no record of run '{run_id}' -- I can't "
                            "report on a run that never existed, and I won't "
                            "claim it completed or did anything."}
        return {"mode": "answer", "kind": "run_status",
                "grounded": {**grounded, "acceptance_overlay": True,
                             "acceptance_state": arec.state.value},
                "text": f"Run '{run_id}' (goal '{arec.goal}'): the scheduler "
                        f"holds no system record, but the acceptance loop "
                        f"does -- acceptance state '{arec.state.value}' "
                        f"(rounds: {arec.rounds}, near-misses: "
                        f"{len(arec.near_miss_ids)}). System 'completed' is "
                        "a candidate state -- only your satisfaction (or a "
                        "terminal condition) closes the loop."}
    bits = [f"Run '{rec.get('id')}': goal '{rec.get('goal')}'; "
            f"status '{rec.get('status')}'."]
    if rec.get("error"):
        bits.append(f"Error: {rec['error']}.")
    # Acceptance overlay (M6): completed vs accepted are distinct statuses.
    # The scheduler record is never edited here; the overlay is read only.
    if arec is not None:
        bits.append(
            f"Acceptance: '{arec.state.value}' "
            f"(rounds: {arec.rounds}, near-misses: "
            f"{len(arec.near_miss_ids)}"
            + (f", closed: {arec.close_reason}"
               if arec.close_reason else "") + "). "
            "System 'completed' is a candidate state -- only your "
            "satisfaction (or a terminal condition) closes the loop.")
    return {"mode": "answer", "kind": "run_status", "grounded": grounded,
            "text": " ".join(bits)}


def _acceptance_loop(services: Dict[str, Any]):
    acc = services.get("acceptance")
    if acc is None:
        return None, ("the acceptance loop is not available in this "
                      "service graph")
    return acc, None


def _answer_accept_run(match: Optional[re.Match],
                       services: Dict[str, Any]) -> Dict[str, Any]:
    run_id = match.group(1) if match else ""
    acc, why = _acceptance_loop(services)
    if acc is None:
        return _unreadable("accept_run", "acceptance loop", why)
    try:
        rec = acc.record_verdict(run_id, satisfied=True)
    except (KeyError, ValueError) as exc:
        return {"mode": "answer", "kind": "accept_run",
                "grounded": {"kind": "accept_run", "run_id": run_id,
                             "error": str(exc)},
                "text": f"I can't accept run '{run_id}': {exc}. I won't "
                        "invent a verdict for a run I have no record of."}
    return {"mode": "answer", "kind": "accept_run",
            "grounded": {"kind": "accept_run", "run_id": run_id,
                         "state": rec.state.value,
                         "close_reason": rec.close_reason},
            "text": f"Run '{run_id}' accepted -- the loop is closed by your "
                    "satisfaction."}


def _answer_reject_run(match: Optional[re.Match], text: str,
                       services: Dict[str, Any]) -> Dict[str, Any]:
    run_id = match.group(1) if match else ""
    acc, why = _acceptance_loop(services)
    if acc is None:
        return _unreadable("reject_run", "acceptance loop", why)
    feedback = _extract_feedback(text, run_id)
    try:
        rec = acc.record_verdict(run_id, satisfied=False,
                                 feedback=feedback)
    except (KeyError, ValueError) as exc:
        return {"mode": "answer", "kind": "reject_run",
                "grounded": {"kind": "reject_run", "run_id": run_id,
                             "error": str(exc)},
                "text": f"I can't record rejection for run '{run_id}': "
                        f"{exc}."}
    nm_id = rec.near_miss_ids[-1] if rec.near_miss_ids else None
    return {"mode": "answer", "kind": "reject_run",
            "grounded": {"kind": "reject_run", "run_id": run_id,
                         "state": rec.state.value,
                         "near_miss_id": nm_id, "feedback": feedback},
            "text": f"Recorded: run '{run_id}' did not satisfy you. The "
                    f"attempt plus your feedback is kept as a near-miss "
                    f"record ({nm_id}); the loop reopens at pool expansion, "
                    "steered away from what failed."}


def _extract_feedback(text: str, run_id: str) -> str:
    """Pull the user's free-text feedback after the run id. The loop never
    invents unmet criteria from prose — feedback is stored verbatim."""
    low = text.lower()
    idx = low.find(run_id.lower())
    tail = text[idx + len(run_id):] if idx >= 0 else text
    tail = tail.lstrip(" :,-")
    for marker in ("because", "since", "as"):
        m = re.search(r"\b" + marker + r"\b", tail, re.IGNORECASE)
        if m:
            return tail[m.end():].strip(" :,-")
    return tail.strip()


def _answer_identity(services: Dict[str, Any]) -> Dict[str, Any]:
    ev = services.get("evidence")
    if ev is None:
        return _unreadable("identity", "evidence store",
                           "the document store is not available in this "
                           "service graph")
    ok, doc = _safe(lambda: ev.get_document("soul.md"))
    if not ok:
        return _unreadable("identity", "evidence store", doc)
    exists = bool(doc.get("exists"))
    grounded = {"kind": "identity",
                "store": "EvidenceStore.get_document('soul.md')",
                "exists": exists}
    if not exists:
        return {"mode": "answer", "kind": "identity", "grounded": grounded,
                "text": f"{_DONT_KNOW} who I am: no identity document "
                        "(soul.md) is hosted."}
    return {"mode": "answer", "kind": "identity", "grounded": grounded,
            "text": "From the hosted document soul.md (saved by a user or "
                    "agent -- I did not write it):\n\n"
                    + str(doc.get("markdown", ""))}


def _answer_document(match: Optional[re.Match], lowered: str,
                     services: Dict[str, Any]) -> Dict[str, Any]:
    ev = services.get("evidence")
    if ev is None:
        return _unreadable("document", "evidence store",
                           "the document store is not available in this "
                           "service graph")
    # Name the document if the message names one.
    name = None
    for doc_name in _DOCUMENTS:
        if doc_name in lowered:
            name = doc_name
            break
    asked = None
    if name is None and match is not None:
        for group in match.groups():
            if not group or group.strip().lower() == "hosted":
                continue
            asked = group.strip().rstrip("?.!")
            break
    grounded_base = {"kind": "document",
                     "store": "EvidenceStore.get_document(name)"}
    if name is None and asked is None:
        # Generic "documents?" question: report which hosted docs exist.
        ok, presence = _safe(
            lambda: {d: bool(ev.get_document(d).get("exists"))
                     for d in _DOCUMENTS})
        if not ok:
            return _unreadable("document", "evidence store", presence)
        listing = "; ".join(
            f"{d}: {'hosted' if p else 'not hosted'}"
            for d, p in presence.items())
        return {"mode": "answer", "kind": "document",
                "grounded": {**grounded_base, "presence": presence},
                "text": "Hosted documents (saved by users or agents, never "
                        f"written by me): {listing}. Ask 'what does "
                        "theory.md say' and I'll read it back."}
    if name is None:
        # The message named something: resolve "<name>" -> "<name>.md";
        # if it names a hostable document, read it; otherwise say so.
        cand = asked if asked.endswith(".md") else asked + ".md"
        if cand in _DOCUMENTS:
            name = cand
        else:
            return {"mode": "answer", "kind": "document",
                    "grounded": {**grounded_base, "asked": cand,
                                 "exists": False},
                    "text": f"{_DONT_KNOW}: '{cand}' is not one of the "
                            "hosted documents. Only soul.md, theory.md, and "
                            "'hypothetical inferences.md' can be hosted "
                            "here."}
    ok, doc = _safe(lambda: ev.get_document(name))
    if not ok:
        return _unreadable("document", "evidence store", doc)
    exists = bool(doc.get("exists"))
    markdown = str(doc.get("markdown", "") or "")
    grounded = {**grounded_base, "name": name, "exists": exists,
                "empty": exists and not markdown.strip()}
    if not exists or not markdown.strip():
        return {"mode": "answer", "kind": "document", "grounded": grounded,
                "text": f"{_DONT_KNOW}: the document '{name}' is "
                        + ("hosted but empty." if exists
                           else "not hosted.")}
    return {"mode": "answer", "kind": "document", "grounded": grounded,
            "text": f"From the hosted document {name}:\n\n{markdown}"}


def _answer_status(services: Dict[str, Any]) -> Dict[str, Any]:
    met = services.get("metering")
    if met is None:
        return _unreadable("status", "metering",
                           "the metering service is not available in this "
                           "service graph")
    ok_t, tier = _safe(met.tier)
    ok_n, today = _safe(met.tasks_today)
    if not (ok_t and ok_n):
        return _unreadable("status", "metering",
                           str(tier if not ok_t else today))
    n = today.get("tasks_today")
    limit = today.get("limit")
    remaining = today.get("remaining")
    grounded = {"kind": "status",
                "store": "metering.tier() + metering.tasks_today()",
                "tier": tier.get("tier"), "tasks_today": n,
                "limit": limit, "remaining": remaining}
    return {"mode": "answer", "kind": "status", "grounded": grounded,
            "text": f"System status, read from the real metering store. "
                    f"Tier: '{tier.get('tier')}'. Tasks in the last 24h: "
                    f"{n} of {limit} allowed ({remaining} remaining). "
                    f"Concurrency is 1 (single FIFO worker; the plan's 3 is "
                    f"a ceiling, not a fact). Honestly unavailable -- no "
                    f"substrate exists: token balances, chat-turn metering, "
                    f"purchasing permanent agents."}


def _answer_evidence(services: Dict[str, Any]) -> Dict[str, Any]:
    ev = services.get("evidence")
    if ev is None:
        return _unreadable("evidence", "evidence store",
                           "the evidence store is not available in this "
                           "service graph")
    ok, res = _safe(lambda: ev.list_entries(limit=10))
    if not ok:
        return _unreadable("evidence", "evidence store", res)
    entries = res.get("entries", [])
    by_kind = res.get("by_kind", {})
    grounded = {"kind": "evidence", "store": "EvidenceStore.list_entries()",
                "count": len(entries), "by_kind": by_kind}
    if not entries:
        return {"mode": "answer", "kind": "evidence", "grounded": grounded,
                "text": f"{_DONT_KNOW}: the evidence store is empty -- no "
                        "hypotheses, observations, or inferences are "
                        "recorded."}
    latest = "; ".join(
        f"[{e.get('kind')}] {e.get('text', '')[:120]}" for e in entries[:3])
    return {"mode": "answer", "kind": "evidence", "grounded": grounded,
            "text": f"Evidence store: hypotheses {by_kind.get('hypothesis', 0)}, "
                    f"observations {by_kind.get('observation', 0)}, "
                    f"inferences {by_kind.get('inference', 0)}. Latest: "
                    f"{latest}."}


def _answer_file_action() -> Dict[str, Any]:
    return {"mode": "answer", "kind": "file_action",
            "grounded": {"kind": "file_action", "store": None,
                         "note": "read-only handler; no action substrate "
                                 "exists"},
            "text": "No. This chat handler only reads stores -- it "
                    "performs no actions at all: no file writes, no "
                    "deletions, no modifications. If a file was deleted, "
                    "it was not through me."}


def _answer_name() -> Dict[str, Any]:
    return {"mode": "answer", "kind": "name",
            "grounded": {"kind": "name", "store": None,
                         "note": "no user-identity store exists in this "
                                 "runtime"},
            "text": f"{_DONT_KNOW} your name: no user-identity store "
                    "exists in this runtime, and none of my readable "
                    "state records it."}


def _answer_chitchat(lowered: str) -> Dict[str, Any]:
    stripped = lowered.strip()
    if stripped.startswith("thank") or stripped.startswith("thx"):
        opener = "You're welcome."
    else:
        opener = "Hello."
    return {"mode": "acknowledge", "kind": "chit_chat",
            "grounded": {"kind": "chit_chat", "store": None,
                         "note": "no store read; no facts claimed"},
            "text": opener + " I answer grounded questions from recorded "
                    "state: my capabilities ('what can you do'), recent "
                    "runs ('what have you been doing'), hosted documents "
                    "('what does theory.md say'), and system status ('what "
                    "is my tier'). I don't have moods or make small talk "
                    "-- there is no language substrate here."}


def _answer_fallback() -> Dict[str, Any]:
    return {"mode": "answer", "kind": "unknown", "grounded": None,
            "text": f"{_DONT_KNOW}. I have no language substrate: I cannot "
                    "infer, paraphrase, or improvise -- I only answer from "
                    "recorded state. I can answer grounded questions about: "
                    "my recorded capabilities ('what can you do'), recent "
                    "runs ('what have you been doing', 'status of run "
                    "<id>'), hosted documents ('what does theory.md say'), "
                    "and system status ('what is my tier', 'tasks today')."}


def _unreadable(kind: str, store_name: str, why: str) -> Dict[str, Any]:
    return {"mode": "answer", "kind": kind,
            "grounded": {"kind": kind, "store": None, "error": str(why)},
            "text": f"{_DONT_KNOW}: the {store_name} could not be read "
                    f"({why}). I won't answer from anywhere else."}
