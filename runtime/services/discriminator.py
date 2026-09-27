"""Task-vs-chat discriminator (conversational-minimum mission, Worker 1).

``classify(text)`` -> ``{"mode": "chat"|"task"|"ambiguous",
"signals": [...], "reason": str}``.

A REAL three-way classifier with no language substrate: this runtime has
no parser, no embeddings, no LM, so the decision is lexical/feature
based -- imperative verbs, request constructions ("can you <verb>",
"<verb> ... for me", "i need you to <verb>"), greetings/farewells,
capability questions, time/date patterns, knowledge-question forms,
bare arithmetic expressions, and infrastructure-state statements.

Decision order (first decisive match wins):

  1. empty -> ambiguous
  2. whole message is a greeting / thanks / farewell / ack -> chat
  3. bare arithmetic expression ("5 times 6") -> task
  4. decisive TASK construction -> task
     (imperative work verb with an object, "can/could/would you <verb>
     <object>", "<verb> ... for me", "i want/need you to <verb>",
     "remind me", "help me (to) <verb>", "calculate/what's <arith>")
  5. decisive AMBIGUOUS construction -> ambiguous
     (infrastructure state: "the server is down" / "is the db up?",
     status questions: "what's the status of the build",
     "help me" with no work verb, deictic fragments:
     "what about this?", "can you do something for me?")
  6. decisive CHAT construction -> chat
     (capability/identity questions, time/date, leading thanks/farewell,
     knowledge questions, "do you know ...", jokes)
  7. fallback -> ambiguous. The classifier NEVER guesses: anything
     without a decisive pattern is ambiguous, and ambiguous turns go to
     the chat handler, which asks a clarifying question.

Precedence notes: a greeting followed by a work request ("hey, can you
write a parser for me?") is a TASK -- the work request dominates the
incidental greeting. A "can you <verb>" construction always beats the
knowledge-question form ("can you set a timer" is a task even though it
is phrased as a question).

Structural bound (honest): surface form is all this sees. It cannot
resolve referents ("this", "it"), cannot tell a hypothetical from a
command when the wording is identical, and cannot know which nouns name
real capabilities. Bare arithmetic is classified TASK deliberately: the
inherited dispatch contract pins "5 times 6" to the dispatch path
(unknown_intent refusal + slot), and this classifier must not silently
re-route pinned behavior. Residual task<->chat confusion is MEASURED
(tests/test_discriminator.py prints per-class precision/recall and the
confusion matrix), not asserted.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

# -- work verbs: synthesis / side-effect / investigative actions ---------
# Base + inflected forms. Past forms are included: they only ever fire
# inside an explicit request construction (step 4), never on their own.
_WORK_VERBS = frozenset(
    """
    create creates creating created
    make makes making made
    generate generates generating generated
    write writes writing wrote written
    build builds building built
    draft drafts drafting drafted
    compose composes composing composed
    design designs designing designed
    implement implements implementing implemented
    produce produces producing produced
    synthesize synthesizes synthesizing synthesized
    run runs running ran
    execute executes executing executed
    deploy deploys deploying deployed
    fix fixes fixing fixed
    debug debugs debugging debugged
    refactor refactors refactoring refactored
    analyze analyzes analyzing analyzed
    search searches searching searched
    find finds finding found
    fetch fetches fetching fetched
    retrieve retrieves retrieving retrieved
    get gets getting got
    send sends sending sent
    schedule schedules scheduling scheduled
    set sets setting
    add adds adding added
    remove removes removing removed
    delete deletes deleting deleted
    update updates updating updated
    install installs installing installed
    configure configures configuring configured
    start starts starting started
    stop stops stopping stopped
    restart restarts restarting restarted
    book books booking booked
    order orders ordering ordered
    buy buys buying bought
    remind reminds reminding reminded
    summarize summarizes summarizing summarized
    translate translates translating translated
    convert converts converting converted
    check checks checking checked
    plan plans planning planned
    call calls calling called
    """.split()
)

# -- chat: whole-message conversational turns ---------------------------
_CHAT_EXACT = frozenset({
    "hey", "hi", "hello", "yo", "sup", "howdy", "hiya",
    "hey there", "hi there", "hello there",
    "good morning", "good afternoon", "good evening",
    "good night", "goodnight",
    "how are you", "how are you doing", "hows it going",
    "how's it going", "whats up", "what's up",
    "thanks", "thank you", "thankyou", "thx", "thanks a lot",
    "thank you so much",
    "bye", "goodbye", "good-bye", "see you", "see ya",
    "see you later", "see you tomorrow",
    "ok", "okay", "k", "got it", "cool", "nice", "sure",
    "yep", "yeah", "yup", "no", "nope", "alright", "all right",
    "fine", "great", "awesome", "perfect", "wonderful", "lovely",
    "help",
})

_CAPABILITY_RE = re.compile(
    r"\b(what can you do|who are you|what are you|list your capabilities|"
    r"what are your capabilities|your capabilities|what is remor|"
    r"who made you|who created you|what are you capable of)\b"
)
_TIME_RE = re.compile(
    r"\b(what time is it|what's the time|whats the time|tell me the time|"
    r"do you have the time|what day is it|what's today's date|"
    r"whats todays date|current time|what time is)\b"
)
_THANKS_LEAD_RE = re.compile(r"^(thanks|thank you|thx)\b")
_FAREWELL_LEAD_RE = re.compile(r"^(bye|goodbye|good ?night|see you)\b")
_JOKE_RE = re.compile(r"\b(tell me|give me) a (joke|story)\b")
_DO_YOU_KNOW_RE = re.compile(r"\bdo you know\b")
_ARE_YOU_THERE_RE = re.compile(r"\bare you (there|awake|listening)\b")
_WH_STARTERS = ("what", "who", "when", "where", "why", "which", "how")
_YESNO_STARTERS = ("is", "are", "was", "were", "do", "does", "did",
                   "will", "would", "should", "has", "have")

# -- ambiguous: infrastructure state / status / under-specified help ----
_INFRA_NOUNS = (r"server|service|app|website|site|build|pipeline|"
                r"database|\bdb\b|deploy|deployment")
_INFRA_STATE_RE = re.compile(
    r"\b(?:" + _INFRA_NOUNS + r")\b.{0,24}\b(down|broken|offline|"
    r"not working|isn't working|arent working|slow|failing|failed|"
    r"unreachable)\b"
)
_INFRA_Q_RE = re.compile(
    r"^(is|are)\s+the\s+(?:" + _INFRA_NOUNS + r")\s+"
    r"(down|broken|offline|slow|failing|up|working)\b"
)
_STATUS_Q_RE = re.compile(
    r"\bstatus\b.{0,30}\b(?:build|pipeline|deploy|server|service)\b|"
    r"\b(?:build|pipeline|deploy|server|service)\b.{0,30}\bstatus\b"
)
_BUILD_RESULT_RE = re.compile(
    r"^(did|has|have)\s+the\s+(build|pipeline|deployment|tests?)\s+"
    r"(pass|fail|complete|finish|succeed)"
)
_HELP_ME_RE = re.compile(r"\bhelp\s+me\b")
_DEICTIC_RE = re.compile(
    r"^(what about|how about|and)\b.{0,24}\b(this|that|it|one)\b.*\?$"
)
_DO_SOMETHING_RE = re.compile(r"\bdo\s+(something|anything)\b")
_I_NEED_HELP_RE = re.compile(r"^i\s+need\s+help\b")

# Leading interjections stripped before step-4 task matching so
# "hey, can you write a parser for me?" still sees its request.
_GREETING_STRIP_RE = re.compile(
    r"^(?:hey|hi|hello|yo|sup|howdy|hiya|ah|oh|ok|okay|well|so|now)"
    r"[,!\s]+"
)

_ARITH_FULL_RE = re.compile(r"[\d\s+\-*/×÷^%().]+")
_ARITH_OP_RE = re.compile(r"[+\-*/×÷^%()]")
_ARITH_OPWORDS = frozenset(
    "times plus minus divided multiplied multiply over percent".split())
_ARITH_CORE_RE = re.compile(
    r"\d[^?!.]{0,30}?(?:\+|-|\*|/|×|÷|\^|%|\btimes\b|\bplus\b|\bminus\b|"
    r"\bdivided\b|\bmultiplied\b|\bmultiply\b|\bover\b|\bpercent\b)"
    r"[^?!.]{0,30}?\d"
)
_ARITH_LEAD_RE = re.compile(
    r"^(what is|what's|whats|calculate|compute|solve|evaluate)\b")

_CAN_YOU_RE = re.compile(r"^(?:can|could|would)\s+you\b")
_POLITE_STRIP_RE = re.compile(r"^\s*(?:please|kindly)\s+")
_I_NEED_YOU_RE = re.compile(r"^i\s+(?:want|need)\s+you\s+to\s+([a-z]+)\b")
_HELP_ME_VERB_RE = re.compile(
    r"\bhelp\s+me\s+(?:to\s+)?([a-z]+)\b")
_REMIND_ME_RE = re.compile(r"\bremind\s+me\b")
_FOR_ME_RE_TMPL = r"\b(%s)\b[\w\s',-]{0,40}\bfor\s+me\b"


def _normalize(text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return norm


def _tokens(norm: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", norm)


def _is_arithmetic(norm: str, tokens: List[str]) -> bool:
    # "2 + 2" / "(3+4)*2": only digits, operators, parens, whitespace.
    if (_ARITH_FULL_RE.fullmatch(norm) and re.search(r"\d", norm)
            and _ARITH_OP_RE.search(norm)):
        return True
    # "5 times 6": every token is a number or an operator word, with at
    # least one of each.
    if tokens and all(
            t in _ARITH_OPWORDS or t.replace(".", "", 1).isdigit()
            for t in tokens):
        has_digit = any(t.replace(".", "", 1).isdigit() for t in tokens)
        has_op = any(t in _ARITH_OPWORDS for t in tokens)
        if has_digit and has_op:
            return True
    # "what's 15% of 240" / "calculate 3*4": lead-in + arithmetic core.
    if _ARITH_LEAD_RE.match(norm) and _ARITH_CORE_RE.search(norm):
        return True
    return False


def _strip_greetings(norm: str) -> str:
    for _ in range(2):
        new = _GREETING_STRIP_RE.sub("", norm)
        if new == norm:
            break
        norm = new
    return norm


def _task_construction(norm: str, tokens: List[str]
                       ) -> Tuple[bool, str]:
    """Decisive TASK patterns. Returns (matched, signal)."""
    stripped = _strip_greetings(norm)
    swords = _tokens(stripped)

    # Imperative work verb with an object: "make an image",
    # "please write the deployment note". A bare verb alone ("write")
    # is a fragment, not a command -> left for ambiguous.
    m = re.match(r"^(?:please\s+|kindly\s+)?([a-z]+)\b", stripped)
    if m and m.group(1) in _WORK_VERBS and len(swords) > 1:
        return True, "task:imperative:%s" % m.group(1)

    # "can/could/would you <verb> <object>": "can you write a parser
    # for me?", "could you please draft an email". The verb needs an
    # object after it -- "can you check?" alone is under-specified.
    m = _CAN_YOU_RE.match(stripped)
    if m:
        rest = _POLITE_STRIP_RE.sub("", stripped[m.end():])
        words = _tokens(rest)
        for i, w in enumerate(words[:8]):
            if w in _WORK_VERBS and i + 1 < len(words):
                return True, "task:request:%s" % w

    # "<verb> ... for me": "write a parser for me".
    vm = _FOR_ME_RE_TMPL % "|".join(sorted(_WORK_VERBS))
    m = re.search(vm, stripped)
    if m:
        return True, "task:for-me:%s" % m.group(1)

    # "i want/need you to <verb>": "i need you to check the logs".
    m = _I_NEED_YOU_RE.match(stripped)
    if m and m.group(1) in _WORK_VERBS:
        return True, "task:i-need-you-to:%s" % m.group(1)

    # "help me (to) <verb>": "help me write a parser".
    m = _HELP_ME_VERB_RE.search(stripped)
    if m and m.group(1) in _WORK_VERBS:
        return True, "task:help-me:%s" % m.group(1)

    # "remind me ...": side-effect request by construction.
    if _REMIND_ME_RE.search(stripped):
        return True, "task:remind-me"

    return False, ""


def _ambiguous_construction(norm: str, tokens: List[str]
                            ) -> Tuple[bool, str]:
    """Decisive AMBIGUOUS patterns. Returns (matched, signal)."""
    if _INFRA_Q_RE.match(norm):
        return True, "ambiguous:infra-question"
    if _INFRA_STATE_RE.search(norm):
        return True, "ambiguous:infra-state"
    if _BUILD_RESULT_RE.match(norm):
        return True, "ambiguous:build-result"
    if _STATUS_Q_RE.search(norm):
        return True, "ambiguous:status-question"
    # "help me" with a work verb already went to task in step 4; what
    # remains is under-specified: "can you help me with this?"
    if _HELP_ME_RE.search(norm):
        return True, "ambiguous:help-me"
    if _I_NEED_HELP_RE.match(norm):
        return True, "ambiguous:i-need-help"
    if _DO_SOMETHING_RE.search(norm):
        return True, "ambiguous:do-something"
    if _DEICTIC_RE.match(norm):
        return True, "ambiguous:deictic"
    return False, ""


def _chat_construction(norm: str, tokens: List[str]
                       ) -> Tuple[bool, str]:
    """Decisive CHAT patterns. Returns (matched, signal)."""
    if _CAPABILITY_RE.search(norm):
        return True, "chat:capability"
    if _TIME_RE.search(norm):
        return True, "chat:time"
    if _THANKS_LEAD_RE.match(norm):
        return True, "chat:thanks"
    if _FAREWELL_LEAD_RE.match(norm):
        return True, "chat:farewell"
    if _JOKE_RE.search(norm):
        return True, "chat:joke"
    if _DO_YOU_KNOW_RE.search(norm):
        return True, "chat:do-you-know"
    if _ARE_YOU_THERE_RE.search(norm):
        return True, "chat:presence"
    if tokens and tokens[0] in _WH_STARTERS:
        # Knowledge / how-to questions. Task constructions were already
        # ruled out in step 4, so a work verb here is topical
        # ("who wrote hamlet"), not a request.
        return True, "chat:wh-question"
    if tokens and tokens[0] in _YESNO_STARTERS and norm.endswith("?"):
        # "can/could/would you" never reach here as a question form:
        # step 4 already decided the request-shaped ones.
        return True, "chat:yesno-question"
    return False, ""


def classify(text: object) -> Dict[str, object]:
    """Classify one message. Always returns a dict with mode/chat/task/
    ambiguous, the fired signals, and a human-readable reason. Pure
    function: no state, no I/O, thread-safe."""
    if not isinstance(text, str) or not text.strip():
        return {"mode": "ambiguous", "signals": ["ambiguous:empty"],
                "reason": "empty input carries no intent signal"}
    norm = _normalize(text)
    tokens = _tokens(norm)

    # 2. whole-message conversational turns
    if norm in _CHAT_EXACT:
        return {"mode": "chat", "signals": ["chat:exact"],
                "reason": "whole message is a greeting/thanks/farewell/ack"}

    # 3. bare arithmetic -> task (inherited dispatch contract)
    if _is_arithmetic(norm, tokens):
        return {"mode": "task", "signals": ["task:arithmetic"],
                "reason": "bare arithmetic expression requests a computed "
                          "result (dispatch path, per inherited contract)"}

    # 4. decisive task constructions
    hit, sig = _task_construction(norm, tokens)
    if hit:
        return {"mode": "task", "signals": [sig],
                "reason": "explicit work-request construction: %s" % sig}

    # 5. decisive ambiguous constructions
    hit, sig = _ambiguous_construction(norm, tokens)
    if hit:
        return {"mode": "ambiguous", "signals": [sig],
                "reason": "under-specified: could be chat or task: %s" % sig}

    # 6. decisive chat constructions
    hit, sig = _chat_construction(norm, tokens)
    if hit:
        return {"mode": "chat", "signals": [sig],
                "reason": "conversational/informational construction: %s"
                % sig}

    # 7. never guess
    return {"mode": "ambiguous", "signals": ["ambiguous:fallback"],
            "reason": "no decisive pattern fired; refusing to guess"}
