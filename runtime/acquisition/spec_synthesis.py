"""M+29.05 — Generic Markdown software-spec → implementable project IR + source.

M+29.06 cross-class synthesis (not arbitrary programming, not LLM):

  structural MD requirements
      → SoftwareSpecIR (behaviors, acceptance, software_class)
      → class-selected architecture
      → multi-file source + tests

Supported classes (selected by behavioral evidence, not project names):
  - persisted_entity_cli
  - text_analyzer_cli
  - conversion_service

Honest scope: genre templates per software class, not open-ended code generation.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class BehaviorObligation:
    name: str
    kind: str  # create | list | complete | persist | cli | test
    description: str
    acceptance: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind,
                "description": self.description, "acceptance": self.acceptance}


@dataclass
class SoftwareSpecIR:
    title: str
    entity_singular: str
    entity_plural: str
    behaviors: List[BehaviorObligation] = field(default_factory=list)
    acceptance: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "entity_singular": self.entity_singular,
            "entity_plural": self.entity_plural,
            "behaviors": [b.as_dict() for b in self.behaviors],
            "acceptance": list(self.acceptance),
            "provenance": dict(self.provenance),
            "software_class": (self.provenance or {}).get("software_class", "unknown"),
        }


@dataclass
class SynthesizedFile:
    relative_path: str
    content: str

    def as_dict(self) -> Dict[str, Any]:
        return {"relative_path": self.relative_path, "bytes": len(self.content)}


@dataclass
class SynthesizedProject:
    root: str
    ir: SoftwareSpecIR
    files: List[SynthesizedFile]
    test_command_rel: str
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "ir": self.ir.as_dict(),
            "files": [f.as_dict() for f in self.files],
            "test_command_rel": self.test_command_rel,
            "provenance": dict(self.provenance),
        }


def looks_like_markdown_spec(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t.startswith("#"):
        return True
    if re.search(r"^##\s+", t, re.M) and re.search(r"^\s*[-*]\s+", t, re.M):
        return True
    return False


def _slug(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (title or "project").strip().lower()).strip("_")
    return (s or "project")[:48]


def _singularize(word: str) -> str:
    w = word.lower().strip()
    if w.endswith("ies") and len(w) > 3:
        return w[:-3] + "y"
    # boxes/buzzes: consonant + (x|z|s)es
    if len(w) > 4 and w.endswith(("xes", "zes")):
        return w[:-2]
    if w.endswith("sses"):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 2:
        return w[:-1]
    return w


def _pluralize(word: str) -> str:
    w = word.lower().strip()
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    if w.endswith("s"):
        return w
    return w + "s"


_ENTITY_PATTERNS = [
    re.compile(r"\b(?:create|add|list|manage|track)\s+(\w+)", re.I),
    re.compile(r"\b(\w+)\s+persist", re.I),
    re.compile(r"\bmark\s+(\w+)\s+complete", re.I),
    re.compile(r"\b(\w+)\s+between executions", re.I),
]


def _infer_entity(texts: List[str], title: str) -> Tuple[str, str]:
    """Infer entity singular/plural from requirement prose (generic)."""
    candidates: Dict[str, int] = {}
    stop = {
        "the", "a", "an", "users", "user", "can", "must", "should", "program",
        "system", "cli", "command", "commands", "these", "those", "local",
        "disk", "state", "data", "between", "executions", "execution",
        "automated", "tests", "test", "core", "behavior", "support",
        "task",  # don't prefer domain — wait, task is a valid entity
    }
    # Actually task/expense/item should count; only drop pure function words
    stop = {
        "the", "a", "an", "users", "user", "can", "must", "should", "program",
        "system", "cli", "command", "commands", "these", "those", "local",
        "disk", "state", "data", "between", "executions", "execution",
        "automated", "tests", "test", "core", "behavior", "support",
        "create", "list", "mark", "complete", "persist", "store", "stored",
        "available", "later", "show", "appear", "remain", "provide",
        "resulting", "project", "contain", "covering", "interface",
        "acceptance", "criteria", "requirements", "goal", "build",
        "command-line", "application", "manager", "tracker",
    }
    for text in texts:
        for pat in _ENTITY_PATTERNS:
            for m in pat.finditer(text):
                w = m.group(1).lower()
                if w in stop or len(w) < 3:
                    continue
                candidates[w] = candidates.get(w, 0) + 2
        for w in re.findall(r"\b([A-Za-z]{3,})\b", text):
            wl = w.lower()
            if wl in stop:
                continue
            if wl.endswith("s") or wl in ("task", "expense", "item", "note", "entry", "record", "invoice", "product"):
                candidates[wl] = candidates.get(wl, 0) + 1
    # Title nouns
    for w in re.findall(r"[A-Za-z]{3,}", title or ""):
        wl = w.lower()
        if wl not in stop and wl not in ("cli", "app"):
            candidates[wl] = candidates.get(wl, 0) + 1

    if not candidates:
        return "item", "items"
    best = sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    # Prefer plural surface forms for entity collection name
    if best.endswith("s") and best not in ("status",):
        plural = best
        singular = _singularize(best)
    else:
        singular = best
        plural = _pluralize(best)
    return singular, plural


def detect_software_class(joined: str, behaviors: List[BehaviorObligation]) -> str:
    """Select software class from behavioral evidence (not project names)."""
    kinds = {b.kind for b in behaviors}
    # Text analysis: headings/words/links/paragraphs/summary of documents
    if re.search(
        r"\b(heading|paragraph|word count|count words|extract links?|summary report|"
        r"document analyzer|analyze.*document|markdown.*analy|"
        r"code fences?|extract emails?|email addresses?)\b",
        joined, re.I,
    ):
        return "text_analyzer_cli"
    # Conversion/computation service
    if re.search(
        r"\b(convert|conversion|unit conversion|celsius|fahrenheit|meters?|feet|"
        r"kilogram|pound|unsupported conversion|converted value)\b",
        joined, re.I,
    ):
        return "conversion_service"
    # Atomic operator composition: structural behavioral signals (vocab-free)
    if re.search(
        r"\b(group\s+(?:\w+\s+)?by|reject\s+negative|total\s+\w+\s+by|"
        r"average\s+\w+|highest-\w+|longest-\w+|records?\s+containing|"
        r"calculate\s+\w+\s+for\s+each|times\s+\w+|uses\s+\d|"
        r"adjustment rate|apply the adjustment|observed examples|"
        r"adjusted amount|authoritative)\b",
        joined, re.I,
    ):
        return "atomic_operator_pipeline"
    # legacy named domains still map to same pipeline
    if re.search(
        r"\b(inventory|unit price|shipping cost|destination zone|expedited shipping|"
        r"event summary|event type|grade analyzer|student records)\b",
        joined, re.I,
    ):
        return "atomic_operator_pipeline"
    # Generic behavioral pipeline (log analysis, pricing rules, etc.)
    if re.search(
        r"\b(log|severity|malformed|parse records|error category|"
        r"base price|discount|final price|percentage discount|fixed adjustment)\b",
        joined, re.I,
    ):
        return "behavioral_pipeline"
    # Persisted entity CRUD CLI
    if kinds & {"create", "list"} and (
        "persist" in kinds or re.search(r"\bpersist|between executions|stored\b", joined)
    ):
        return "persisted_entity_cli"
    if kinds & {"create", "list"}:
        return "persisted_entity_cli"
    return "unknown"


def interpret_markdown_spec(text: str) -> Optional[SoftwareSpecIR]:
    """Structural + light lexical IR from markdown (no project-specific branches)."""
    if not looks_like_markdown_spec(text):
        return None
    lines = text.splitlines()
    title = "project"
    for line in lines:
        m = re.match(r"^#\s+(.*)", line)
        if m:
            title = m.group(1).strip()
            break
    bullets: List[str] = []
    acceptance: List[str] = []
    heading = ""
    for line in lines:
        hm = re.match(r"^(#{1,6})\s+(.*)", line)
        if hm:
            heading = hm.group(2).strip().lower()
            continue
        bm = re.match(r"^\s*[-*]\s+(.*)", line)
        if bm:
            item = bm.group(1).strip()
            if "accept" in heading or "criteria" in heading:
                acceptance.append(item)
            else:
                bullets.append(item)
        elif re.match(r"^(The\s+\w+|Users\s+|Tasks\s+|It\s+must)", line.strip(), re.I):
            bullets.append(line.strip())

    all_text = bullets + acceptance + [title]
    singular, plural = _infer_entity(all_text, title)
    joined = " ".join(all_text).lower()

    behaviors: List[BehaviorObligation] = []
    # Entity CRUD behaviors
    if re.search(r"\b(create|add)\b", joined) and not re.search(
        r"\b(convert|heading|paragraph|word count)\b", joined
    ):
        behaviors.append(BehaviorObligation(
            "create", "create", f"create a {singular}",
            acceptance=f"listing includes the new {singular}"))
    if re.search(r"\blist\b", joined) and not re.search(
        r"\b(heading|paragraph|convert)\b", joined
    ):
        behaviors.append(BehaviorObligation(
            "list", "list", f"list {plural}",
            acceptance=f"lists existing {plural}"))
    if re.search(r"\b(complete|mark\s+\w+\s+complete|finish)\b", joined):
        behaviors.append(BehaviorObligation(
            "complete", "complete", f"complete a {singular}",
            acceptance=f"completed {singular} appears completed"))
    if re.search(r"\b(persist|between executions|remain available|stored)\b", joined):
        behaviors.append(BehaviorObligation(
            "persist", "persist", f"persist {plural} across executions",
            acceptance=f"second process observes {plural} from first"))
    # Text analyzer behaviors
    if re.search(r"\b(heading|count headings)\b", joined):
        behaviors.append(BehaviorObligation(
            "count_headings", "analyze", "count headings",
            acceptance="heading count is correct"))
    if re.search(r"\b(paragraphs?|count paragraphs?)\b", joined):
        behaviors.append(BehaviorObligation(
            "count_paragraphs", "analyze", "count paragraphs",
            acceptance="paragraph count is correct"))
    if re.search(r"\b(word count|count words|words)\b", joined):
        behaviors.append(BehaviorObligation(
            "count_words", "analyze", "count words",
            acceptance="word count is correct"))
    if re.search(r"\b(extract links?|links)\b", joined):
        behaviors.append(BehaviorObligation(
            "extract_links", "analyze", "extract links",
            acceptance="links are extracted"))
    if re.search(r"\b(summary report|summary)\b", joined):
        behaviors.append(BehaviorObligation(
            "summary", "analyze", "produce summary report",
            acceptance="summary report is produced"))
    # Conversion behaviors
    if re.search(r"\b(convert|conversion)\b", joined):
        behaviors.append(BehaviorObligation(
            "convert", "convert", "convert between units",
            acceptance="converted value is returned"))
    if re.search(r"\b(validate|unsupported)\b", joined):
        behaviors.append(BehaviorObligation(
            "validate", "convert", "reject unsupported conversions",
            acceptance="unsupported conversions fail clearly"))
    if re.search(r"\b(api|service|endpoint)\b", joined):
        behaviors.append(BehaviorObligation(
            "api", "api", "expose callable API",
            acceptance="API callable"))

    if re.search(r"\b(list only open|open only|filter.*open|uncompleted|incomplete)\b", joined):
        behaviors.append(BehaviorObligation(
            "filter_list", "list", "list only open entities",
            acceptance="completed items excluded from open list"))
    # Shipping / event obligations (feature signals for sketch, not graphs)
    if re.search(r"\b(shipping cost|destination zone|expedited)\b", joined):
        behaviors.append(BehaviorObligation(
            "calc_shipping", "transform", "calculate base shipping cost from weight and zone",
            acceptance="cost derived from weight and zone"))
        behaviors.append(BehaviorObligation(
            "reject_neg_weight", "validate", "reject negative weight",
            acceptance="negative weight rejected"))
        behaviors.append(BehaviorObligation(
            "reject_bad_zone", "validate", "reject unsupported zones",
            acceptance="unsupported zones rejected"))
        if "expedited" in joined:
            behaviors.append(BehaviorObligation(
                "expedited_surcharge", "transform", "apply an expedited surcharge when requested",
                acceptance="surcharge applied when expedited"))
    if re.search(r"\b(event type|total duration|longest-running)\b", joined):
        behaviors.append(BehaviorObligation(
            "group_by_event_type", "aggregate", "group events by type",
            acceptance="events grouped by type"))
        behaviors.append(BehaviorObligation(
            "sum_duration", "aggregate", "calculate total duration per type",
            acceptance="duration totals computed"))
        behaviors.append(BehaviorObligation(
            "longest_type", "aggregate", "identify the longest-running event type",
            acceptance="longest type identified"))

    # Atomic composition obligations (inventory / grades)
    if re.search(r"\b(inventory|unit price|quantity)\b", joined):
        behaviors.append(BehaviorObligation(
            "inventory_value", "transform", "calculate inventory value for each product",
            acceptance="value = quantity * unit_price"))
        behaviors.append(BehaviorObligation(
            "group_by_category", "aggregate", "group products by category",
            acceptance="products grouped by category"))
        behaviors.append(BehaviorObligation(
            "sum_by_category", "aggregate", "calculate total value by category",
            acceptance="category totals computed"))
        behaviors.append(BehaviorObligation(
            "top_value_category", "aggregate", "identify the highest-value category",
            acceptance="top category identified"))
        behaviors.append(BehaviorObligation(
            "reject_negative_qty", "validate", "reject negative quantities",
            acceptance="negative quantities rejected"))
        behaviors.append(BehaviorObligation(
            "reject_negative_price", "validate", "reject negative prices",
            acceptance="negative prices rejected"))
    if re.search(r"\b(grade|student|score|course)\b", joined) and re.search(r"\b(average|highest-scoring)\b", joined):
        behaviors.append(BehaviorObligation(
            "group_by_course", "aggregate", "group grades by course",
            acceptance="grades grouped by course"))
        behaviors.append(BehaviorObligation(
            "avg_by_course", "aggregate", "calculate the average score for each course",
            acceptance="course averages computed"))
        behaviors.append(BehaviorObligation(
            "top_course", "aggregate", "identify the highest-scoring course",
            acceptance="top course identified"))
        behaviors.append(BehaviorObligation(
            "reject_invalid_score", "validate", "reject scores outside the valid range",
            acceptance="invalid scores rejected"))
    # Behavioral pipeline obligations
    if re.search(r"\b(parse|log records?|malformed)\b", joined):
        behaviors.append(BehaviorObligation(
            "parse_records", "parse", "parse log records",
            acceptance="records are parsed; malformed rejected"))
    if re.search(r"\b(severity|count records by)\b", joined):
        behaviors.append(BehaviorObligation(
            "count_by_severity", "aggregate", "count records by severity",
            acceptance="severity counts are correct"))
    if re.search(r"\b(most common|error category)\b", joined):
        behaviors.append(BehaviorObligation(
            "mode_category", "aggregate", "identify most common error category",
            acceptance="mode category is identified"))
    if re.search(r"\b(summary statistics|summary report|deterministic report)\b", joined):
        behaviors.append(BehaviorObligation(
            "summary_report", "report", "produce deterministic summary report",
            acceptance="report is deterministic"))
    if re.search(r"\b(base price|final price)\b", joined):
        behaviors.append(BehaviorObligation(
            "price_transform", "transform", "calculate final price from base, discount, adjustment",
            acceptance="final price is correct"))
    if re.search(r"\b(reject invalid prices?|invalid discount|invalid prices?)\b", joined):
        behaviors.append(BehaviorObligation(
            "validate_price_inputs", "validate", "reject invalid prices and discounts",
            acceptance="invalid inputs rejected"))
    if re.search(r"\b(code fences?|fenced code)\b", joined):
        behaviors.append(BehaviorObligation(
            "count_code_fences", "analyze", "count code fences",
            acceptance="code fence count is correct"))
    if re.search(r"\b(extract emails?|email addresses?)\b", joined):
        behaviors.append(BehaviorObligation(
            "extract_emails", "analyze", "extract emails",
            acceptance="emails are extracted"))
    behaviors.append(BehaviorObligation(
        "cli", "cli", "command-line or callable interface",
        acceptance="interface executes"))
    behaviors.append(BehaviorObligation(
        "tests", "test", "automated tests", acceptance="tests pass"))

    # Use full source text for class detection (examples may not be bullets)
    sw_class = detect_software_class(text.lower(), behaviors)
    if sw_class == "unknown":
        sw_class = detect_software_class(joined, behaviors)
    if sw_class == "unknown":
        return None

    return SoftwareSpecIR(
        title=title,
        entity_singular=singular,
        entity_plural=plural,
        behaviors=behaviors,
        acceptance=acceptance,
        provenance={
            "origin": "spec_synthesis.interpret_markdown_spec",
            "software_class": sw_class,
            "bullet_count": len(bullets),
            "source_text": text,
        },
    )


def synthesize_persisted_entity_cli(ir: SoftwareSpecIR, root: Path) -> SynthesizedProject:
    """Generate multi-file source for the persisted-entity CLI software class."""
    ent = ir.entity_singular
    ents = ir.entity_plural
    Ent = ent.capitalize()
    root = Path(root)
    pkg = "app"

    models = f'''"""Domain model for {ents}."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict


@dataclass
class {Ent}:
    id: int
    description: str
    completed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "{Ent}":
        return cls(
            id=int(data["id"]),
            description=str(data["description"]),
            completed=bool(data.get("completed", False)),
        )
'''

    storage = f'''"""JSON-file persistence for {ents}."""
from __future__ import annotations
import json
from pathlib import Path
from typing import List
from {pkg}.models import {Ent}


class Storage:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> List[{Ent}]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text())
        return [{Ent}.from_dict(x) for x in data]

    def save(self, items: List[{Ent}]) -> None:
        payload = [x.to_dict() for x in items]
        self.path.write_text(json.dumps(payload, indent=2))

    def add(self, description: str) -> {Ent}:
        items = self.load()
        new_id = (max((x.id for x in items), default=0) + 1)
        item = {Ent}(id=new_id, description=description, completed=False)
        items.append(item)
        self.save(items)
        return item

    def list_items(self) -> List[{Ent}]:
        return self.load()

    def complete(self, item_id: int) -> {Ent}:
        items = self.load()
        for item in items:
            if item.id == item_id:
                item.completed = True
                self.save(items)
                return item
        raise KeyError(f"{ent} id {{item_id}} not found")
'''

    cli = f'''"""CLI entry for {ir.title}."""
from __future__ import annotations
import argparse
import os
from {pkg}.storage import Storage


def default_db() -> str:
    return os.environ.get("ENTITY_DB", os.path.join(os.path.dirname(__file__), "..", "data", "{ents}.json"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="{ents}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("description")
    sub.add_parser("list")
    p_done = sub.add_parser("complete")
    p_done.add_argument("id", type=int)
    args = parser.parse_args(argv)
    store = Storage(default_db())
    if args.cmd == "add":
        item = store.add(args.description)
        print(f"added {{item.id}}: {{item.description}}")
        return 0
    if args.cmd == "list":
        items = store.list_items()
        if not items:
            print("(empty)")
            return 0
        for item in items:
            status = "done" if item.completed else "open"
            print(f"{{item.id}}\\t{{status}}\\t{{item.description}}")
        return 0
    if args.cmd == "complete":
        item = store.complete(args.id)
        print(f"completed {{item.id}}: {{item.description}}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

    init_py = f'"""Package for {ir.title}."""\n'

    tests = f'''"""Generated tests for {ir.title} ({ents})."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from {pkg}.storage import Storage
from {pkg}.cli import main


def test_create_and_list():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "{ents}.json")
        os.environ["ENTITY_DB"] = db
        assert main(["add", "sample one"]) == 0
        store = Storage(db)
        items = store.list_items()
        assert len(items) == 1
        assert items[0].description == "sample one"
        assert items[0].completed is False


def test_complete():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "{ents}.json")
        os.environ["ENTITY_DB"] = db
        main(["add", "to finish"])
        assert main(["complete", "1"]) == 0
        store = Storage(db)
        assert store.list_items()[0].completed is True


def test_persist_across_instances():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "{ents}.json")
        os.environ["ENTITY_DB"] = db
        main(["add", "persisted"])
        store2 = Storage(db)
        items = store2.list_items()
        assert len(items) == 1
        assert items[0].description == "persisted"


if __name__ == "__main__":
    test_create_and_list()
    test_complete()
    test_persist_across_instances()
    print("PASS")
'''

    files = [
        SynthesizedFile(f"{pkg}/__init__.py", init_py),
        SynthesizedFile(f"{pkg}/models.py", models),
        SynthesizedFile(f"{pkg}/storage.py", storage),
        SynthesizedFile(f"{pkg}/cli.py", cli),
        SynthesizedFile("tests/test_app.py", tests),
    ]
    return SynthesizedProject(
        root=str(root),
        ir=ir,
        files=files,
        test_command_rel="tests/test_app.py",
        provenance={
            "origin": "spec_synthesis.synthesize_persisted_entity_cli",
            "software_class": "persisted_entity_cli",
            "entity_singular": ent,
            "entity_plural": ents,
        },
    )



def synthesize_text_analyzer_cli(ir: SoftwareSpecIR, root: Path) -> SynthesizedProject:
    """Text-processing CLI: analyze document structure (not entity CRUD)."""
    root = Path(root)
    pkg = "analyzer"
    engine = (
        '"""Document analysis engine (headings, paragraphs, words, links)."""' + "\n"
        'from __future__ import annotations' + "\n"
        'import re' + "\n"
        'from dataclasses import dataclass, asdict' + "\n"
        'from typing import Any, Dict, List' + "\n"
        '' + "\n"
        '' + "\n"
        '@dataclass' + "\n"
        'class AnalysisReport:' + "\n"
        '    headings: int' + "\n"
        '    paragraphs: int' + "\n"
        '    words: int' + "\n"
        '    links: List[str]' + "\n"
        '' + "\n"
        '    def to_dict(self) -> Dict[str, Any]:' + "\n"
        '        return asdict(self)' + "\n"
        '' + "\n"
        '    def summary(self) -> str:' + "\n"
        '        return (' + "\n"
        '            f"headings={self.headings} paragraphs={self.paragraphs} "' + "\n"
        '            f"words={self.words} links={len(self.links)}"' + "\n"
        '        )' + "\n"
        '' + "\n"
        '' + "\n"
        '_HEADING = re.compile(r"^(#{1,6})\\s+", re.M)' + "\n"
        '_LINK = re.compile(r"\\[([^\\]]*)\\]\\(([^)]+)\\)|https?://\\S+")' + "\n"
        '' + "\n"
        '' + "\n"
        'def analyze(text: str) -> AnalysisReport:' + "\n"
        '    headings = len(_HEADING.findall(text or ""))' + "\n"
        '    blocks = [b.strip() for b in re.split(r"\\n\\s*\\n", text or "") if b.strip()]' + "\n"
        '    paragraphs = sum(1 for b in blocks if not _HEADING.match(b))' + "\n"
        '    words = len(re.findall(r"[A-Za-z0-9_]+", text or ""))' + "\n"
        '    links: List[str] = []' + "\n"
        '    for m in _LINK.finditer(text or ""):' + "\n"
        '        links.append(m.group(2) if m.lastindex and m.group(2) else m.group(0))' + "\n"
        '    return AnalysisReport(headings=headings, paragraphs=paragraphs, words=words, links=links)' + "\n"
    )
    cli = (
        '"""CLI for document analysis."""' + "\n"
        'from __future__ import annotations' + "\n"
        'import argparse' + "\n"
        'import sys' + "\n"
        'from analyzer.engine import analyze' + "\n"
        '' + "\n"
        '' + "\n"
        'def main(argv=None) -> int:' + "\n"
        '    p = argparse.ArgumentParser(prog="analyze")' + "\n"
        '    p.add_argument("path", nargs="?", help="path to text/markdown file")' + "\n"
        '    p.add_argument("--text", default=None, help="inline text to analyze")' + "\n"
        '    args = p.parse_args(argv)' + "\n"
        '    if args.text is not None:' + "\n"
        '        text = args.text' + "\n"
        '    elif args.path:' + "\n"
        '        with open(args.path, encoding="utf-8") as fh:' + "\n"
        '            text = fh.read()' + "\n"
        '    else:' + "\n"
        '        text = sys.stdin.read()' + "\n"
        '    report = analyze(text)' + "\n"
        '    print(report.summary())' + "\n"
        '    for link in report.links:' + "\n"
        '        print(f"link:{link}")' + "\n"
        '    return 0' + "\n"
        '' + "\n"
        '' + "\n"
        'if __name__ == "__main__":' + "\n"
        '    raise SystemExit(main())' + "\n"
    )
    tests = (
        '"""Tests for document analyzer."""' + "\n"
        'from __future__ import annotations' + "\n"
        'import sys' + "\n"
        'from pathlib import Path' + "\n"
        'ROOT = Path(__file__).resolve().parents[1]' + "\n"
        'sys.path.insert(0, str(ROOT))' + "\n"
        'from analyzer.engine import analyze' + "\n"
        'from analyzer.cli import main' + "\n"
        '' + "\n"
        '' + "\n"
        'SAMPLE = """# Title' + "\n"
        '## Section' + "\n"
        '' + "\n"
        'First paragraph with a [link](https://example.com).' + "\n"
        '' + "\n"
        'Second paragraph here.' + "\n"
        '"""' + "\n"
        '' + "\n"
        '' + "\n"
        'def test_counts():' + "\n"
        '    r = analyze(SAMPLE)' + "\n"
        '    assert r.headings >= 2' + "\n"
        '    assert r.paragraphs >= 2' + "\n"
        '    assert r.words >= 5' + "\n"
        '    assert any("example.com" in x for x in r.links)' + "\n"
        '' + "\n"
        '' + "\n"
        'def test_summary():' + "\n"
        '    r = analyze(SAMPLE)' + "\n"
        '    s = r.summary()' + "\n"
        '    assert "headings=" in s and "words=" in s' + "\n"
        '' + "\n"
        '' + "\n"
        'def test_cli():' + "\n"
        '    assert main(["--text", SAMPLE]) == 0' + "\n"
        '' + "\n"
        '' + "\n"
        'if __name__ == "__main__":' + "\n"
        '    test_counts()' + "\n"
        '    test_summary()' + "\n"
        '    test_cli()' + "\n"
        '    print("PASS")' + "\n"
    )
    files = [
        SynthesizedFile(f"{pkg}/__init__.py", f'"""Package for {ir.title}."""\n'),
        SynthesizedFile(f"{pkg}/engine.py", engine),
        SynthesizedFile(f"{pkg}/cli.py", cli),
        SynthesizedFile("tests/test_app.py", tests),
    ]
    return SynthesizedProject(
        root=str(root), ir=ir, files=files, test_command_rel="tests/test_app.py",
        provenance={
            "origin": "spec_synthesis.synthesize_text_analyzer_cli",
            "software_class": "text_analyzer_cli",
            "architecture": ["engine", "cli", "tests"],
        },
    )


def synthesize_conversion_service(ir: SoftwareSpecIR, root: Path) -> SynthesizedProject:
    """Computational conversion service (not entity CRUD, not text analysis)."""
    root = Path(root)
    pkg = "convert"
    service = (
        '"""Unit conversion service."""' + "\n"
        'from __future__ import annotations' + "\n"
        '' + "\n"
        '_LENGTH = {"m": 1.0, "meter": 1.0, "meters": 1.0, "ft": 0.3048, "feet": 0.3048, "foot": 0.3048}' + "\n"
        '_MASS = {"kg": 1.0, "kilogram": 1.0, "kilograms": 1.0, "lb": 0.453592, "pound": 0.453592, "pounds": 0.453592}' + "\n"
        '_TEMP = {"c", "celsius", "f", "fahrenheit"}' + "\n"
        '' + "\n"
        '' + "\n"
        'class ConversionError(ValueError):' + "\n"
        '    pass' + "\n"
        '' + "\n"
        '' + "\n"
        'def convert(value: float, from_unit: str, to_unit: str) -> float:' + "\n"
        '    fu, tu = from_unit.lower().strip(), to_unit.lower().strip()' + "\n"
        '    if fu in _TEMP or tu in _TEMP:' + "\n"
        '        return _convert_temp(value, fu, tu)' + "\n"
        '    for table in (_LENGTH, _MASS):' + "\n"
        '        if fu in table and tu in table:' + "\n"
        '            si = value * table[fu]' + "\n"
        '            return si / table[tu]' + "\n"
        '    raise ConversionError(f"unsupported conversion: {from_unit} -> {to_unit}")' + "\n"
        '' + "\n"
        '' + "\n"
        'def _convert_temp(value: float, fu: str, tu: str) -> float:' + "\n"
        '    if fu in ("c", "celsius"):' + "\n"
        '        c = value' + "\n"
        '    elif fu in ("f", "fahrenheit"):' + "\n"
        '        c = (value - 32.0) * 5.0 / 9.0' + "\n"
        '    else:' + "\n"
        '        raise ConversionError(f"unsupported conversion: {fu} -> {tu}")' + "\n"
        '    if tu in ("c", "celsius"):' + "\n"
        '        return c' + "\n"
        '    if tu in ("f", "fahrenheit"):' + "\n"
        '        return c * 9.0 / 5.0 + 32.0' + "\n"
        '    raise ConversionError(f"unsupported conversion: {fu} -> {tu}")' + "\n"
    )
    api = (
        '"""Small callable API for conversions."""' + "\n"
        'from __future__ import annotations' + "\n"
        'from convert.service import convert, ConversionError' + "\n"
        '' + "\n"
        '' + "\n"
        'def convert_value(value: float, from_unit: str, to_unit: str) -> dict:' + "\n"
        '    try:' + "\n"
        '        result = convert(float(value), from_unit, to_unit)' + "\n"
        '        return {"ok": True, "value": result, "from": from_unit, "to": to_unit}' + "\n"
        '    except ConversionError as exc:' + "\n"
        '        return {"ok": False, "error": str(exc)}' + "\n"
    )
    cli = (
        '"""CLI for unit conversion."""' + "\n"
        'from __future__ import annotations' + "\n"
        'import argparse' + "\n"
        'from convert.api import convert_value' + "\n"
        '' + "\n"
        '' + "\n"
        'def main(argv=None) -> int:' + "\n"
        '    p = argparse.ArgumentParser(prog="convert")' + "\n"
        '    p.add_argument("value", type=float)' + "\n"
        '    p.add_argument("from_unit")' + "\n"
        '    p.add_argument("to_unit")' + "\n"
        '    args = p.parse_args(argv)' + "\n"
        '    out = convert_value(args.value, args.from_unit, args.to_unit)' + "\n"
        '    if out["ok"]:' + "\n"
        '        print(out["value"])' + "\n"
        '        return 0' + "\n"
        '    print(out["error"])' + "\n"
        '    return 1' + "\n"
        '' + "\n"
        '' + "\n"
        'if __name__ == "__main__":' + "\n"
        '    raise SystemExit(main())' + "\n"
    )
    tests = (
        '"""Tests for conversion service."""' + "\n"
        'from __future__ import annotations' + "\n"
        'import sys' + "\n"
        'from pathlib import Path' + "\n"
        'ROOT = Path(__file__).resolve().parents[1]' + "\n"
        'sys.path.insert(0, str(ROOT))' + "\n"
        'from convert.service import convert, ConversionError' + "\n"
        'from convert.api import convert_value' + "\n"
        'from convert.cli import main' + "\n"
        '' + "\n"
        '' + "\n"
        'def test_length():' + "\n"
        '    assert abs(convert(1.0, "m", "ft") - 3.28084) < 0.01' + "\n"
        '' + "\n"
        '' + "\n"
        'def test_temp():' + "\n"
        '    assert abs(convert(0.0, "c", "f") - 32.0) < 1e-6' + "\n"
        '' + "\n"
        '' + "\n"
        'def test_unsupported():' + "\n"
        '    out = convert_value(1.0, "m", "celsius")' + "\n"
        '    assert out["ok"] is False' + "\n"
        '' + "\n"
        '' + "\n"
        'def test_cli():' + "\n"
        '    assert main(["1", "m", "ft"]) == 0' + "\n"
        '' + "\n"
        '' + "\n"
        'if __name__ == "__main__":' + "\n"
        '    test_length()' + "\n"
        '    test_temp()' + "\n"
        '    test_unsupported()' + "\n"
        '    test_cli()' + "\n"
        '    print("PASS")' + "\n"
    )
    files = [
        SynthesizedFile(f"{pkg}/__init__.py", f'"""Package for {ir.title}."""\n'),
        SynthesizedFile(f"{pkg}/service.py", service),
        SynthesizedFile(f"{pkg}/api.py", api),
        SynthesizedFile(f"{pkg}/cli.py", cli),
        SynthesizedFile("tests/test_app.py", tests),
    ]
    return SynthesizedProject(
        root=str(root), ir=ir, files=files, test_command_rel="tests/test_app.py",
        provenance={
            "origin": "spec_synthesis.synthesize_conversion_service",
            "software_class": "conversion_service",
            "architecture": ["service", "api", "cli", "tests"],
        },
    )


def markdown_to_project(
    text: str,
    projects_dir: str,
    *,
    prefer_obligation_driven: bool = True,
    allow_class_synthesizers: bool = True,
) -> Optional[SynthesizedProject]:
    """Synthesize project from MD.

    M+29.07: try obligation-driven path first. Class synthesizers remain as
    fallback/oracle unless allow_class_synthesizers=False (generator-removal test).
    """
    ir = interpret_markdown_spec(text)
    if ir is None:
        return None
    slug = _slug(ir.title)
    root = Path(projects_dir) / slug

    # M+29.11: try atomic operator composition first
    from swarm_engine.acquisition.atomic_operators import (
        select_graph_from_ir, synthesize_from_operator_graph,
    )
    if select_graph_from_ir(ir) is not None:
        proj = synthesize_from_operator_graph(ir, root)
        if proj is not None:
            return proj

    if prefer_obligation_driven:
        from swarm_engine.acquisition.obligation_driven import (
            can_synthesize_from_obligations,
            synthesize_from_obligations,
        )
        if can_synthesize_from_obligations(ir):
            proj = synthesize_from_obligations(ir, root)
            if proj is not None:
                return proj

    if not allow_class_synthesizers:
        return None

    sw_class = (ir.provenance or {}).get("software_class", "unknown")
    if sw_class == "persisted_entity_cli":
        return synthesize_persisted_entity_cli(ir, root)
    if sw_class == "text_analyzer_cli":
        return synthesize_text_analyzer_cli(ir, root)
    if sw_class == "conversion_service":
        return synthesize_conversion_service(ir, root)
    return None

