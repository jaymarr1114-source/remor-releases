"""M+29.07 — Obligation-driven source synthesis.

Path:
  behaviors/obligations
      → implementation primitives (shared vocabulary)
      → module representation
      → Python source

Class-specific synthesizers are NOT used on this path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.acquisition.spec_synthesis import (
    SoftwareSpecIR,
    SynthesizedFile,
    SynthesizedProject,
)


@dataclass
class ImplPrimitive:
    kind: str
    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    obligation: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name,
                "params": dict(self.params), "obligation": self.obligation}


@dataclass
class FunctionSpec:
    name: str
    args: List[str]
    body_lines: List[str]
    returns: str = ""
    docstring: str = ""


@dataclass
class ModuleSpec:
    package: str
    module: str
    imports: List[str] = field(default_factory=list)
    helpers: List[str] = field(default_factory=list)
    functions: List[FunctionSpec] = field(default_factory=list)
    module_level: List[str] = field(default_factory=list)



@dataclass
class FieldSpec:
    name: str
    type_name: str  # str | int | bool
    required: bool = True
    default: Any = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type_name": self.type_name,
            "required": self.required,
            "default": self.default,
        }

    def py_annotation(self) -> str:
        return {"str": "str", "int": "int", "bool": "bool"}.get(self.type_name, "str")

    def py_default(self) -> str:
        if self.default is not None:
            return repr(self.default)
        if not self.required:
            return {"str": '""', "int": "0", "bool": "False"}.get(self.type_name, "None")
        return ""


@dataclass
class EntitySchema:
    entity_singular: str
    entity_plural: str
    fields: List[FieldSpec]
    identity_field: str = "id"
    status_field: Optional[str] = None  # bool field used for complete/filter

    def as_dict(self) -> Dict[str, Any]:
        return {
            "entity_singular": self.entity_singular,
            "entity_plural": self.entity_plural,
            "fields": [f.as_dict() for f in self.fields],
            "identity_field": self.identity_field,
            "status_field": self.status_field,
        }

    def data_fields(self) -> List[FieldSpec]:
        return [f for f in self.fields if f.name != self.identity_field]


_BOOL_HINTS = (
    "available", "availability", "favorite", "completed", "complete", "active",
    "enabled", "done", "open", "published", "archived",
)
_INT_HINTS = (
    "year", "age", "count", "quantity", "qty", "score", "rating", "number", "num",
    "publication_year", "pub_year",
)


def _infer_field_type(name: str) -> str:
    n = name.lower().replace("-", "_")
    if n in _BOOL_HINTS or any(h in n for h in ("available", "favorite", "complete", "active")):
        return "bool"
    if n in _INT_HINTS or n.endswith("_year") or n.endswith("_count"):
        return "int"
    return "str"


def _tokenize(word: str) -> List[str]:
    w = word.lower().strip().replace("-", "_")
    out = {w}
    if w.endswith("ies") and len(w) > 3:
        out.add(w[:-3] + "y")
    if w.endswith("s") and not w.endswith("ss") and len(w) > 2:
        out.add(w[:-1])
    else:
        out.add(w + "s")
        if w.endswith("y") and len(w) > 1:
            out.add(w[:-1] + "ies")
    return list(out)


def derive_entity_schema(ir: SoftwareSpecIR, text: str = "") -> EntitySchema:
    """Derive entity schema from objective text + obligations (no fixed template)."""
    singular = ir.entity_singular or "item"
    plural = ir.entity_plural or (singular + "s")
    joined = " ".join(
        [text or "", ir.title or ""]
        + [b.description for b in ir.behaviors]
        + list(ir.acceptance or [])
    ).lower()

    # Known field vocabulary (not project-specific schemas)
    known = [
        "title", "author", "year", "isbn", "publisher", "price",
        "name", "email", "phone", "address", "company",
        "description", "notes", "tag", "status",
        "available", "availability", "favorite", "completed", "active",
        "quantity", "score", "rating", "age",
    ]
    found: List[str] = []
    for k in known:
        if re.search(r"\b" + re.escape(k) + r"\b", joined):
            name = "available" if k == "availability" else ("year" if k == "publication_year" else k)
            if name not in found:
                found.append(name)

    # Explicit list pattern: "title, author, year" (tokens must be known-ish)
    for m in re.finditer(
        r"([a-z][a-z0-9_]*(?:\\s*,\\s*[a-z][a-z0-9_]*){1,6})",
        joined,
    ):
        parts = [p.strip().replace(" ", "_") for p in m.group(1).split(",")]
        if all(len(p) > 2 for p in parts) and len(parts) >= 2:
            for part in parts:
                base = part
                if base in ("and", "with", "the", "for", "from"):
                    continue
                if base in _tokenize(singular) or base in _tokenize(plural):
                    continue
                # accept if known or looks like field
                if base in known or (base.isidentifier() and base not in found):
                    if base == "availability":
                        base = "available"
                    if base not in found and base not in ("users", "tasks", "books", "notes", "contacts"):
                        found.append(base)

    # Drop entity name tokens that leaked in
    drop = set(_tokenize(singular)) | set(_tokenize(plural)) | {
        "users", "user", "can", "create", "list", "mark", "persist", "between",
        "executions", "only", "open", "cli", "tests", "automated", "manage",
        "track", "with", "and", "the", "status",
    }
    found = [x for x in found if x not in drop]

    if not found:
        found = ["description"]

    # "favorite status" should not create a separate status field
    if "status" in found and any(x in found for x in ("favorite", "available", "completed", "active")):
        found = [x for x in found if x != "status"]

    # "remain available" / "available later" is persistence language, not a field
    if "available" in found and re.search(
        r"remain available|available later|available in a later|still available",
        joined,
    ):
        # only keep if explicit attribute language also present
        if not re.search(r"\bavailability\b|with[^.]{0,40}\bavailable\b", joined):
            found = [x for x in found if x != "available"]

    # Create obligations need at least one non-bool data field
    if any(b.kind == "create" or b.name == "create" for b in ir.behaviors):
        if not any(_infer_field_type(x) != "bool" for x in found):
            found.insert(0, "description")

    status = None
    for cand in ("completed", "available", "favorite", "active"):
        if cand in found:
            status = cand
            break
    behaviors_names = {b.name for b in ir.behaviors} | {b.kind for b in ir.behaviors}
    if status is None and (
        "complete" in behaviors_names or "filter_list" in behaviors_names
    ):
        status = "completed"
        if "completed" not in found:
            found.append("completed")

    # Order: id, non-bool required, bools last (dataclass defaults)
    non_bool = [n for n in found if _infer_field_type(n) != "bool"]
    bools = [n for n in found if _infer_field_type(n) == "bool"]
    ordered = non_bool + bools

    fields: List[FieldSpec] = [FieldSpec(name="id", type_name="int", required=True)]
    for name in ordered:
        tname = _infer_field_type(name)
        fields.append(FieldSpec(
            name=name,
            type_name=tname,
            required=(tname != "bool"),
            default=(False if tname == "bool" else None),
        ))

    return EntitySchema(
        entity_singular=singular,
        entity_plural=plural,
        fields=fields,
        identity_field="id",
        status_field=status,
    )


def obligations_to_primitives(ir: SoftwareSpecIR) -> List[ImplPrimitive]:
    prims: List[ImplPrimitive] = []
    for b in ir.behaviors:
        kind, name = b.kind, b.name
        desc = (b.description or "").lower()
        if kind == "analyze" or name in (
            "count_headings", "count_paragraphs", "count_words",
            "extract_links", "summary", "count_code_fences", "extract_emails",
        ):
            if "heading" in name or "heading" in desc:
                prims.append(ImplPrimitive(
                    "count_matches", "count_headings",
                    {"pattern": r"^(#{1,6})\s+", "flags": "M"}, obligation=name))
            elif "paragraph" in name or "paragraph" in desc:
                prims.append(ImplPrimitive(
                    "count_blocks", "count_paragraphs",
                    {"split": r"\n\s*\n", "exclude_pattern": r"^(#{1,6})\s+"},
                    obligation=name))
            elif "word" in name or "word" in desc:
                prims.append(ImplPrimitive(
                    "count_matches", "count_words",
                    {"pattern": r"[A-Za-z0-9_]+", "flags": ""}, obligation=name))
            elif "link" in name or "link" in desc:
                prims.append(ImplPrimitive(
                    "extract_matches", "extract_links",
                    {"pattern": r"\[([^\]]*)\]\(([^)]+)\)|https?://\S+", "group": 2},
                    obligation=name))
            elif "summary" in name or "summary" in desc:
                prims.append(ImplPrimitive("format_summary", "summary", {}, obligation=name))
            elif "fence" in name or "fence" in desc:
                prims.append(ImplPrimitive(
                    "count_matches", "count_code_fences",
                    {"pattern": r"```", "flags": "M"}, obligation=name))
            elif "email" in name or "email" in desc:
                prims.append(ImplPrimitive(
                    "extract_matches", "extract_emails",
                    {"pattern": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "group": 0},
                    obligation=name))
        elif kind == "convert" or name in ("convert", "validate"):
            if name == "validate" or "unsupported" in desc or "validate" in desc:
                prims.append(ImplPrimitive("raise_unsupported", "validate_conversion", {}, obligation=name))
            else:
                prims.append(ImplPrimitive("unit_convert", "convert", {
                    "length": {"m": 1.0, "meter": 1.0, "meters": 1.0, "ft": 0.3048, "feet": 0.3048, "foot": 0.3048},
                    "mass": {"kg": 1.0, "kilogram": 1.0, "kilograms": 1.0, "lb": 0.453592, "pound": 0.453592, "pounds": 0.453592},
                    "temp": ["c", "celsius", "f", "fahrenheit"],
                }, obligation=name))
        elif kind == "api":
            prims.append(ImplPrimitive("wrap_api", "convert_value", {}, obligation=name))
        elif kind in ("parse", "aggregate", "report", "transform", "validate") or name in (
            "parse_records", "count_by_severity", "mode_category", "summary_report",
            "price_transform", "validate_price_inputs",
        ):
            if name == "parse_records" or kind == "parse":
                prims.append(ImplPrimitive("parse_lines", "parse_records",
                    {"separator": " ", "min_parts": 3}, obligation=name))
            elif name == "count_by_severity":
                prims.append(ImplPrimitive("group_count", "count_by_severity",
                    {"key": "severity"}, obligation=name))
            elif name == "mode_category":
                prims.append(ImplPrimitive("mode_of", "mode_category",
                    {"key": "category"}, obligation=name))
            elif name == "summary_report" or kind == "report":
                prims.append(ImplPrimitive("format_report", "summary_report", {}, obligation=name))
            elif name == "price_transform" or kind == "transform":
                prims.append(ImplPrimitive("arith_pipeline", "price_transform", {}, obligation=name))
            elif name == "validate_price_inputs" or (kind == "validate" and "price" in (name or "")):
                prims.append(ImplPrimitive("validate_ranges", "validate_price_inputs", {}, obligation=name))
        elif kind in ("create", "list", "complete", "persist") or name in (
            "create", "list", "complete", "persist", "filter_list",
        ):
            # Entity CRUD obligations → shared store primitives (no class body copy)
            # Check name before kind so filter_list is not swallowed by kind==list
            if name == "filter_list":
                prims.append(ImplPrimitive(
                    "entity_filter", "filter_entities",
                    {"field": "completed", "equals": False},
                    obligation=name,
                ))
            elif kind == "create" or name == "create":
                prims.append(ImplPrimitive(
                    "entity_create", "create_entity",
                    {"fields": ["id", "description", "completed"]},
                    obligation=name,
                ))
            elif kind == "list" or name == "list":
                prims.append(ImplPrimitive(
                    "entity_list", "list_entities",
                    {},
                    obligation=name,
                ))
            elif kind == "complete" or name == "complete":
                prims.append(ImplPrimitive(
                    "entity_mutate", "complete_entity",
                    {"field": "completed", "value": True},
                    obligation=name,
                ))
            elif kind == "persist" or name == "persist":
                prims.append(ImplPrimitive(
                    "entity_persist", "persist_entities",
                    {"format": "json"},
                    obligation=name,
                ))
    seen, out = set(), []
    for p in prims:
        if p.name not in seen:
            seen.add(p.name); out.append(p)
    return out


def _emit_count_matches(p: ImplPrimitive) -> Tuple[List[str], List[str]]:
    flags = p.params.get("flags") or ""
    flag_expr = "re.M" if "M" in flags else "0"
    pat = p.params["pattern"]
    ml = [f'_{p.name.upper()}_RE = re.compile(r"{pat}", {flag_expr})']
    body = [f'    {p.name} = len(_{p.name.upper()}_RE.findall(text or ""))']
    return ml, body


def _emit_count_blocks(p: ImplPrimitive) -> Tuple[List[str], List[str]]:
    split = p.params.get("split", r"\n\s*\n")
    excl = p.params.get("exclude_pattern")
    ml: List[str] = []
    if excl:
        ml.append(f'_{p.name.upper()}_EXCL = re.compile(r"{excl}", re.M)')
    body = [f'    _blocks = [b.strip() for b in re.split(r"{split}", text or "") if b.strip()]']
    if excl:
        body.append(f"    {p.name} = sum(1 for b in _blocks if not _{p.name.upper()}_EXCL.match(b))")
    else:
        body.append(f"    {p.name} = len(_blocks)")
    return ml, body


def _emit_extract_matches(p: ImplPrimitive) -> Tuple[List[str], List[str]]:
    pat = p.params["pattern"]
    group = p.params.get("group", 0)
    ml = [f'_{p.name.upper()}_RE = re.compile(r"{pat}")']
    if group == 0:
        body = [f"    {p.name}: List[str] = []",
                f'    for m in _{p.name.upper()}_RE.finditer(text or ""):',
                f"        {p.name}.append(m.group(0))"]
    else:
        body = [f"    {p.name}: List[str] = []",
                f'    for m in _{p.name.upper()}_RE.finditer(text or ""):',
                f"        {p.name}.append(m.group({group}) if m.lastindex and m.group({group}) else m.group(0))"]
    return ml, body


def primitives_to_text_modules(ir: SoftwareSpecIR, prims: List[ImplPrimitive]) -> List[ModuleSpec]:
    analyze_prims = [p for p in prims if p.kind in (
        "count_matches", "count_blocks", "extract_matches", "format_summary")]
    if not analyze_prims:
        return []
    ml: List[str] = []
    field_names: List[str] = []
    body: List[str] = []
    for p in analyze_prims:
        if p.kind == "count_matches":
            m, b = _emit_count_matches(p); ml.extend(m); body.extend(b); field_names.append(p.name)
        elif p.kind == "count_blocks":
            m, b = _emit_count_blocks(p); ml.extend(m); body.extend(b); field_names.append(p.name)
        elif p.kind == "extract_matches":
            m, b = _emit_extract_matches(p); ml.extend(m); body.extend(b); field_names.append(p.name)
    report_fields = []
    for fn in field_names:
        if fn.startswith("extract") or "links" in fn or "emails" in fn:
            report_fields.append(f"    {fn}: List[str]")
        else:
            report_fields.append(f"    {fn}: int")
    helpers = ["@dataclass", "class AnalysisReport:", *report_fields, "",
               "    def to_dict(self) -> Dict[str, Any]:", "        return asdict(self)", "",
               "    def summary(self) -> str:", "        parts = []"]
    for fn in field_names:
        if fn.startswith("extract") or "links" in fn or "emails" in fn:
            helpers.append(f'        parts.append(f"{fn}={{len(self.{fn})}}")')
        else:
            helpers.append(f'        parts.append(f"{fn}={{self.{fn}}}")')
    helpers.append('        return " ".join(parts)')
    ret_args = ", ".join(f"{fn}={fn}" for fn in field_names)
    body.append(f"    return AnalysisReport({ret_args})")
    engine = ModuleSpec(package="analyzer", module="engine",
        imports=["from __future__ import annotations", "import re",
                 "from dataclasses import dataclass, asdict",
                 "from typing import Any, Dict, List"],
        helpers=helpers, module_level=ml,
        functions=[FunctionSpec(name="analyze", args=["text: str"], body_lines=body,
                                returns="AnalysisReport",
                                docstring="Analyze text using obligation-derived primitives.")])
    list_print = []
    for fn in field_names:
        if fn.startswith("extract") or "links" in fn or "emails" in fn:
            list_print.append(f"    for item in report.{fn}:")
            list_print.append(f'        print(f"{fn}:{{item}}")')
    cli = ModuleSpec(package="analyzer", module="cli",
        imports=["from __future__ import annotations", "import argparse", "import sys",
                 "from analyzer.engine import analyze"],
        functions=[FunctionSpec(name="main", args=["argv=None"], returns="int",
            docstring="CLI entry.",
            body_lines=[
                '    p = argparse.ArgumentParser(prog="analyze")',
                '    p.add_argument("path", nargs="?", help="path to text file")',
                '    p.add_argument("--text", default=None)',
                "    args = p.parse_args(argv)",
                "    if args.text is not None:", "        text = args.text",
                "    elif args.path:",
                '        with open(args.path, encoding="utf-8") as fh:',
                "            text = fh.read()",
                "    else:", "        text = sys.stdin.read()",
                "    report = analyze(text)", "    print(report.summary())",
                *list_print, "    return 0"])])
    return [engine, cli]


def primitives_to_convert_modules(ir: SoftwareSpecIR, prims: List[ImplPrimitive]) -> List[ModuleSpec]:
    convert_p = next((p for p in prims if p.kind == "unit_convert"), None)
    if convert_p is None:
        return []
    length = convert_p.params.get("length", {})
    mass = convert_p.params.get("mass", {})
    temp = convert_p.params.get("temp", [])
    service = ModuleSpec(package="convert", module="service",
        imports=["from __future__ import annotations"],
        module_level=[
            f"_LENGTH = {length!r}", f"_MASS = {mass!r}", f"_TEMP = {set(temp)!r}", "",
            "class ConversionError(ValueError):", "    pass"],
        functions=[
            FunctionSpec(name="convert", args=["value: float", "from_unit: str", "to_unit: str"],
                returns="float", docstring="Convert value between units using factor tables.",
                body_lines=[
                    "    fu, tu = from_unit.lower().strip(), to_unit.lower().strip()",
                    "    if fu in _TEMP or tu in _TEMP:",
                    "        return _convert_temp(value, fu, tu)",
                    "    for table in (_LENGTH, _MASS):",
                    "        if fu in table and tu in table:",
                    "            si = value * table[fu]",
                    "            return si / table[tu]",
                    '    raise ConversionError(f"unsupported conversion: {from_unit} -> {to_unit}")']),
            FunctionSpec(name="_convert_temp", args=["value: float", "fu: str", "tu: str"],
                returns="float", body_lines=[
                    '    if fu in ("c", "celsius"):', "        c = value",
                    '    elif fu in ("f", "fahrenheit"):',
                    "        c = (value - 32.0) * 5.0 / 9.0", "    else:",
                    '        raise ConversionError(f"unsupported conversion: {fu} -> {tu}")',
                    '    if tu in ("c", "celsius"):', "        return c",
                    '    if tu in ("f", "fahrenheit"):',
                    "        return c * 9.0 / 5.0 + 32.0",
                    '    raise ConversionError(f"unsupported conversion: {fu} -> {tu}")'])])
    api = ModuleSpec(package="convert", module="api",
        imports=["from __future__ import annotations",
                 "from convert.service import convert, ConversionError"],
        functions=[FunctionSpec(name="convert_value",
            args=["value: float", "from_unit: str", "to_unit: str"], returns="dict",
            body_lines=["    try:",
                "        result = convert(float(value), from_unit, to_unit)",
                '        return {"ok": True, "value": result, "from": from_unit, "to": to_unit}',
                "    except ConversionError as exc:",
                '        return {"ok": False, "error": str(exc)}'])])
    cli = ModuleSpec(package="convert", module="cli",
        imports=["from __future__ import annotations", "import argparse",
                 "from convert.api import convert_value"],
        functions=[FunctionSpec(name="main", args=["argv=None"], returns="int",
            body_lines=[
                '    p = argparse.ArgumentParser(prog="convert")',
                '    p.add_argument("value", type=float)',
                '    p.add_argument("from_unit")', '    p.add_argument("to_unit")',
                "    args = p.parse_args(argv)",
                "    out = convert_value(args.value, args.from_unit, args.to_unit)",
                '    if out["ok"]:', '        print(out["value"])', "        return 0",
                '    print(out["error"])', "    return 1"])])
    return [service, api, cli]


def module_to_source(mod: ModuleSpec) -> str:
    lines: List[str] = []
    lines.extend(mod.imports); lines.append("")
    lines.extend(mod.module_level)
    if mod.module_level: lines.append("")
    lines.extend(mod.helpers)
    if mod.helpers: lines.append("")
    for fn in mod.functions:
        args = ", ".join(fn.args)
        ret = f" -> {fn.returns}" if fn.returns else ""
        lines.append(f"def {fn.name}({args}){ret}:")
        if fn.docstring: lines.append(f'    """{fn.docstring}"""')
        if not fn.body_lines: lines.append("    pass")
        else: lines.extend(fn.body_lines)
        lines.append("")
    if mod.module == "cli":
        lines.append('if __name__ == "__main__":')
        lines.append("    raise SystemExit(main())")
        lines.append("")
    return "\n".join(lines)


def _tests_for_analyzer(field_names: List[str]) -> str:
    asserts = []
    for fn in field_names:
        if "heading" in fn: asserts.append(f"    assert r.{fn} >= 2")
        elif "paragraph" in fn: asserts.append(f"    assert r.{fn} >= 1")
        elif "word" in fn: asserts.append(f"    assert r.{fn} >= 5")
        elif "link" in fn: asserts.append(f'    assert any("example.com" in x for x in r.{fn})')
        elif "email" in fn: asserts.append(f'    assert any("@" in x for x in r.{fn})')
        elif "fence" in fn: asserts.append(f"    assert r.{fn} >= 2")
    body = "\n".join(asserts) if asserts else "    assert r is not None"
    # SAMPLE covers standard analyze + held-out fence/email obligations
    sample = (
        'SAMPLE = """# Title\n## Section\n\n'
        "First paragraph with a [link](https://example.com).\n\n"
        "Second paragraph here.\n\n"
        "```\ncode\n```\n\n"
        'Contact: scout@example.com\n"""\n\n\n'
    )
    return (
        '"""Obligation-driven analyzer tests."""\n'
        "from __future__ import annotations\n"
        "import sys\nfrom pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "sys.path.insert(0, str(ROOT))\n"
        "from analyzer.engine import analyze\nfrom analyzer.cli import main\n\n"
        + sample
        + "def test_counts():\n    r = analyze(SAMPLE)\n" + body + "\n\n\n"
        + "def test_cli():\n    assert main([\"--text\", SAMPLE]) == 0\n\n\n"
        + 'if __name__ == "__main__":\n    test_counts()\n    test_cli()\n    print("PASS")\n'
    )


def _tests_for_convert() -> str:
    return (
        '"""Obligation-driven conversion tests."""\n'
        "from __future__ import annotations\nimport sys\nfrom pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\nsys.path.insert(0, str(ROOT))\n"
        "from convert.service import convert, ConversionError\n"
        "from convert.api import convert_value\nfrom convert.cli import main\n\n\n"
        "def test_length():\n    assert abs(convert(1.0, \"m\", \"ft\") - 3.28084) < 0.01\n\n\n"
        "def test_temp():\n    assert abs(convert(0.0, \"c\", \"f\") - 32.0) < 1e-6\n\n\n"
        "def test_unsupported():\n    out = convert_value(1.0, \"m\", \"celsius\")\n"
        "    assert out[\"ok\"] is False\n\n\n"
        "def test_cli():\n    assert main([\"1\", \"m\", \"ft\"]) == 0\n\n\n"
        'if __name__ == "__main__":\n    test_length()\n    test_temp()\n'
        '    test_unsupported()\n    test_cli()\n    print("PASS")\n'
    )





def primitives_to_entity_modules(
    ir: SoftwareSpecIR, prims: List[ImplPrimitive], schema: EntitySchema
) -> List[ModuleSpec]:
    """Compose persisted-entity modules from schema + CRUD primitives."""
    entity_kinds = {p.kind for p in prims}
    if not entity_kinds & {"entity_create", "entity_list", "entity_mutate", "entity_persist"}:
        return []
    ent = schema.entity_singular
    ents = schema.entity_plural
    Ent = ent[:1].upper() + ent[1:]
    data_fields = schema.data_fields()

    # models helpers from schema
    field_lines = []
    for f in schema.fields:
        if f.name == schema.identity_field:
            field_lines.append(f"    {f.name}: int")
        elif f.type_name == "bool":
            field_lines.append(f"    {f.name}: bool = False")
        elif f.type_name == "int":
            field_lines.append(f"    {f.name}: int = 0")
        else:
            field_lines.append(f"    {f.name}: str")

    from_dict_lines = [f'            {f.name}=']
    from_parts = []
    for f in schema.fields:
        if f.type_name == "int":
            from_parts.append(f'            {f.name}=int(data["{f.name}"]),')
        elif f.type_name == "bool":
            from_parts.append(
                f'            {f.name}=bool(data.get("{f.name}", False)),'
            )
        else:
            from_parts.append(f'            {f.name}=str(data["{f.name}"]),')

    helpers = [
        "@dataclass",
        f"class {Ent}:",
        *field_lines,
        "",
        "    def to_dict(self) -> Dict[str, Any]:",
        "        return asdict(self)",
        "",
        "    @classmethod",
        f'    def from_dict(cls, data: Dict[str, Any]) -> "{Ent}":',
        "        return cls(",
        *from_parts,
        "        )",
    ]
    models = ModuleSpec(
        package="app",
        module="models",
        imports=[
            "from __future__ import annotations",
            "from dataclasses import dataclass, asdict",
            "from typing import Any, Dict",
        ],
        helpers=helpers,
        functions=[],
    )

    # CLI args for create: all required non-id non-bool fields as positional;
    # bools as optional flags
    add_args = []
    add_parse = []
    ctor_kwargs = []
    for f in data_fields:
        if f.type_name == "bool":
            add_parse.append(f'    p_add.add_argument("--{f.name}", action="store_true")')
            ctor_kwargs.append(f"{f.name}=bool(getattr(args, '{f.name}', False))")
        elif f.type_name == "int":
            add_args.append(f'    p_add.add_argument("{f.name}", type=int)')
            ctor_kwargs.append(f"{f.name}=args.{f.name}")
        else:
            add_args.append(f'    p_add.add_argument("{f.name}")')
            ctor_kwargs.append(f"{f.name}=args.{f.name}")

    list_loop = [
        "        for item in items:",
        "            _cols = [str(item.id)]",
    ]
    for f in data_fields:
        if f.type_name == "bool":
            list_loop.append(
                f'            _cols.append("yes" if item.{f.name} else "no")'
            )
        else:
            list_loop.append(f"            _cols.append(str(item.{f.name}))")
    list_loop.append('            print("\\t".join(_cols))')

    cli_body = [
        f'    parser = argparse.ArgumentParser(prog="{ents}")',
        '    sub = parser.add_subparsers(dest="cmd", required=True)',
        '    p_add = sub.add_parser("add")',
        *add_args,
        *add_parse,
        '    sub.add_parser("list")',
    ]
    if "entity_mutate" in entity_kinds and schema.status_field:
        cli_body += [
            '    p_done = sub.add_parser("complete")',
            '    p_done.add_argument("id", type=int)',
        ]
    if "entity_filter" in entity_kinds and schema.status_field:
        cli_body.append('    sub.add_parser("list-open")')
    cli_body += [
        "    args = parser.parse_args(argv)",
        "    store = Storage(default_db())",
        '    if args.cmd == "add":',
        f"        item = store.add({', '.join(ctor_kwargs)})",
        '        print(f"added {item.id}")',
        "        return 0",
        '    if args.cmd == "list":',
        "        items = store.list_items()",
        '        if not items:',
        '            print("(empty)")',
        "            return 0",
        *list_loop,
        "        return 0",
    ]
    if "entity_mutate" in entity_kinds and schema.status_field:
        cli_body += [
            '    if args.cmd == "complete":',
            "        item = store.complete(args.id)",
            '        print(f"completed {item.id}")',
            "        return 0",
        ]
    if "entity_filter" in entity_kinds and schema.status_field:
        sf = schema.status_field
        cli_body += [
            '    if args.cmd == "list-open":',
            "        items = store.list_open()",
            "        for item in items:",
            f'            print(f"{{item.id}}\\t{{item.{data_fields[0].name if data_fields else "id"}}}")',
            "        return 0",
        ]
    cli_body.append("    return 1")

    cli = ModuleSpec(
        package="app",
        module="cli",
        imports=[
            "from __future__ import annotations",
            "import argparse",
            "import os",
            "from app.storage import Storage",
        ],
        functions=[
            FunctionSpec(
                name="default_db",
                args=[],
                returns="str",
                body_lines=[
                    f'    return os.environ.get("ENTITY_DB", os.path.join(os.path.dirname(__file__), "..", "data", "{ents}.json"))',
                ],
            ),
            FunctionSpec(
                name="main",
                args=["argv=None"],
                returns="int",
                body_lines=cli_body,
            ),
        ],
    )
    # storage placeholder; real source from _entity_storage_source
    storage = ModuleSpec(
        package="app",
        module="storage",
        imports=[],
        functions=[],
    )
    return [models, storage, cli]


def _entity_storage_source(
    ir: SoftwareSpecIR, prims: List[ImplPrimitive], schema: EntitySchema
) -> str:
    entity_kinds = {p.kind for p in prims}
    ent = schema.entity_singular
    Ent = ent[:1].upper() + ent[1:]
    data_fields = schema.data_fields()
    add_params = []
    for f in data_fields:
        if f.type_name == "bool":
            add_params.append(f"{f.name}: bool = False")
        elif f.type_name == "int":
            add_params.append(f"{f.name}: int = 0")
        else:
            add_params.append(f"{f.name}: str")
    add_sig = ", ".join(add_params)
    ctor_args = ", ".join(
        ["id=new_id"] + [f"{f.name}={f.name}" for f in data_fields]
    )

    lines = [
        '"""JSON-file persistence for entities (schema-driven)."""',
        "from __future__ import annotations",
        "import json",
        "from pathlib import Path",
        "from typing import List",
        f"from app.models import {Ent}",
        "",
        "",
        "class Storage:",
        "    def __init__(self, path: str):",
        "        self.path = Path(path)",
        "        self.path.parent.mkdir(parents=True, exist_ok=True)",
        "",
        f"    def load(self) -> List[{Ent}]:",
        "        if not self.path.exists():",
        "            return []",
        "        data = json.loads(self.path.read_text())",
        f"        return [{Ent}.from_dict(x) for x in data]",
        "",
        f"    def save(self, items: List[{Ent}]) -> None:",
        "        payload = [x.to_dict() for x in items]",
        "        self.path.write_text(json.dumps(payload, indent=2))",
        "",
    ]
    if "entity_create" in entity_kinds:
        lines += [
            f"    def add(self, {add_sig}) -> {Ent}:",
            "        items = self.load()",
            "        new_id = (max((x.id for x in items), default=0) + 1)",
            f"        item = {Ent}({ctor_args})",
            "        items.append(item)",
            "        self.save(items)",
            "        return item",
            "",
        ]
    if "entity_list" in entity_kinds:
        lines += [
            f"    def list_items(self) -> List[{Ent}]:",
            "        return self.load()",
            "",
        ]
    if "entity_filter" in entity_kinds and schema.status_field:
        sf = schema.status_field
        lines += [
            f"    def list_open(self) -> List[{Ent}]:",
            f"        return [x for x in self.load() if not x.{sf}]",
            "",
        ]
    if "entity_mutate" in entity_kinds and schema.status_field:
        sf = schema.status_field
        lines += [
            f"    def complete(self, item_id: int) -> {Ent}:",
            "        items = self.load()",
            "        for item in items:",
            "            if item.id == item_id:",
            f"                item.{sf} = True",
            "                self.save(items)",
            "                return item",
            f'        raise KeyError(f"{ent} id {{item_id}} not found")',
            "",
        ]
    return "\n".join(lines)


def _tests_for_entity(
    ir: SoftwareSpecIR, prims: List[ImplPrimitive], schema: EntitySchema
) -> str:
    kinds = {p.kind for p in prims}
    ents = schema.entity_plural
    data_fields = schema.data_fields()
    # Build sample add argv
    add_vals = []
    for f in data_fields:
        if f.type_name == "bool":
            continue  # leave default
        if f.type_name == "int":
            add_vals.append("2020")
        else:
            add_vals.append(f"sample_{f.name}")
    add_argv = ", ".join(repr(v) for v in add_vals)
    first_str = next((f.name for f in data_fields if f.type_name == "str"), None)

    lines = [
        '"""Schema-driven entity tests."""',
        "from __future__ import annotations",
        "import os",
        "import tempfile",
        "from pathlib import Path",
        "import sys",
        "ROOT = Path(__file__).resolve().parents[1]",
        "sys.path.insert(0, str(ROOT))",
        "from app.storage import Storage",
        "from app.cli import main",
        "",
        "",
        "def test_create_and_list():",
        "    with tempfile.TemporaryDirectory() as td:",
        f'        db = str(Path(td) / "{ents}.json")',
        '        os.environ["ENTITY_DB"] = db',
        f'        assert main(["add", {add_argv}]) == 0',
        "        store = Storage(db)",
        "        items = store.list_items()",
        "        assert len(items) == 1",
    ]
    if first_str:
        lines.append(f'        assert items[0].{first_str} == "sample_{first_str}"')
    lines += ["", ""]
    if "entity_mutate" in kinds and schema.status_field:
        lines += [
            "def test_complete():",
            "    with tempfile.TemporaryDirectory() as td:",
            f'        db = str(Path(td) / "{ents}.json")',
            '        os.environ["ENTITY_DB"] = db',
            f'        main(["add", {add_argv}])',
            '        assert main(["complete", "1"]) == 0',
            "        store = Storage(db)",
            f"        assert store.list_items()[0].{schema.status_field} is True",
            "",
            "",
        ]
    if "entity_persist" in kinds:
        lines += [
            "def test_persist_across_instances():",
            "    with tempfile.TemporaryDirectory() as td:",
            f'        db = str(Path(td) / "{ents}.json")',
            '        os.environ["ENTITY_DB"] = db',
            f'        main(["add", {add_argv}])',
            "        store2 = Storage(db)",
            "        items = store2.list_items()",
            "        assert len(items) == 1",
            "",
            "",
        ]
    if "entity_filter" in kinds and schema.status_field:
        lines += [
            "def test_list_open():",
            "    with tempfile.TemporaryDirectory() as td:",
            f'        db = str(Path(td) / "{ents}.json")',
            '        os.environ["ENTITY_DB"] = db',
            f'        main(["add", {add_argv}])',
            f'        main(["add", {add_argv}])',
            '        main(["complete", "2"])',
            "        store = Storage(db)",
            "        open_items = store.list_open()",
            "        assert len(open_items) == 1",
            "",
            "",
        ]
    lines += ['if __name__ == "__main__":', "    test_create_and_list()"]
    if "entity_mutate" in kinds and schema.status_field:
        lines.append("    test_complete()")
    if "entity_persist" in kinds:
        lines.append("    test_persist_across_instances()")
    if "entity_filter" in kinds and schema.status_field:
        lines.append("    test_list_open()")
    lines += ['    print("PASS")', ""]
    return "\n".join(lines)



def can_synthesize_from_obligations(ir: SoftwareSpecIR) -> bool:
    kinds = {p.kind for p in obligations_to_primitives(ir)}
    return bool(kinds & {"count_matches", "count_blocks", "extract_matches", "unit_convert",
                         "entity_create", "entity_list", "entity_mutate", "entity_persist",
                         "parse_lines", "group_count", "mode_of", "arith_pipeline",
                         "validate_ranges", "format_report"})


def synthesize_from_obligations(ir: SoftwareSpecIR, root: Path, *, force: bool = False) -> Optional[SynthesizedProject]:
    """Public obligation-driven path. Does not call class synthesizers."""
    prims = obligations_to_primitives(ir)
    if not prims:
        return None
    kinds = {p.kind for p in prims}
    modules: List[ModuleSpec] = []
    tests_src = ""
    arch: List[str] = []
    if kinds & {"parse_lines", "group_count", "mode_of", "arith_pipeline", "validate_ranges"}:
        from swarm_engine.acquisition.behavioral_pipeline import (
            primitives_to_behavioral_modules, tests_for_behavioral,
        )
        modules = primitives_to_behavioral_modules(ir, prims)
        tests_src = tests_for_behavioral(ir, prims)
        arch = [m.module for m in modules]
    elif kinds & {"count_matches", "count_blocks", "extract_matches", "format_summary"}:
        modules = primitives_to_text_modules(ir, prims)
        field_names = [p.name for p in prims if p.kind in (
            "count_matches", "count_blocks", "extract_matches")]
        tests_src = _tests_for_analyzer(field_names)
        arch = [m.module for m in modules]
    elif kinds & {"unit_convert"}:
        modules = primitives_to_convert_modules(ir, prims)
        tests_src = _tests_for_convert()
        arch = [m.module for m in modules]
    elif kinds & {"entity_create", "entity_list", "entity_mutate", "entity_persist"}:

        src_text = (ir.provenance or {}).get("source_text", "") or ""
        schema = derive_entity_schema(ir, text=src_text or ir.title or "")
        modules = primitives_to_entity_modules(ir, prims, schema)
        tests_src = _tests_for_entity(ir, prims, schema)
        arch = [m.module for m in modules]
    else:
        return None
    if not modules:
        return None
    root = Path(root)
    files: List[SynthesizedFile] = []
    for pkg in sorted({m.package for m in modules}):
        files.append(SynthesizedFile(f"{pkg}/__init__.py",
            f'"""Package for {ir.title} (obligation-driven)."""\n'))
    storage_src = None
    schema_dict = None
    if kinds & {"entity_create", "entity_list", "entity_mutate", "entity_persist"}:
        schema = derive_entity_schema(ir, text=(ir.provenance or {}).get("source_text", "") or ir.title or "")
        schema_dict = schema.as_dict()
        storage_src = _entity_storage_source(ir, prims, schema)
    for m in modules:
        if m.module == "storage" and storage_src is not None:
            files.append(SynthesizedFile(f"{m.package}/{m.module}.py", storage_src))
        else:
            files.append(SynthesizedFile(f"{m.package}/{m.module}.py", module_to_source(m)))
    files.append(SynthesizedFile("tests/test_app.py", tests_src))
    return SynthesizedProject(
        root=str(root), ir=ir, files=files, test_command_rel="tests/test_app.py",
        provenance={
            "origin": "obligation_driven.synthesize_from_obligations",
            "software_class": (ir.provenance or {}).get("software_class", "unknown"),
            "architecture": arch,
            "primitives": [p.as_dict() for p in prims],
            "path": "obligation_driven",
            "class_synthesizer_used": False,
            "entity_schema": schema_dict,
        },
    )

