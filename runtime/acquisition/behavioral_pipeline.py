"""Generic behavioral pipeline synthesis (M+29.10)."""
from __future__ import annotations

from typing import List
from swarm_engine.acquisition.spec_synthesis import SoftwareSpecIR, SynthesizedFile
from swarm_engine.acquisition.obligation_driven import (
    ImplPrimitive, ModuleSpec, FunctionSpec, module_to_source,
)


def primitives_to_behavioral_modules(ir, prims):
    kinds = {p.kind for p in prims}
    has_log = bool(kinds & {"parse_lines", "group_count", "mode_of"})
    has_price = bool(kinds & {"arith_pipeline", "validate_ranges"})
    if not (has_log or has_price):
        return []
    pkg = "pipeline"
    if has_log:
        engine_helpers = [
            'def parse_records(text: str):',
            '    """Parse lines into records; reject malformed."""',
            '    records = []',
            '    rejected = 0',
            "    for line in (text or '').splitlines():",
            '        line = line.strip()',
            '        if not line:',
            '            continue',
            '        parts = line.split()',
            '        if len(parts) < 3:',
            '            rejected += 1',
            '            continue',
            '        ts, severity, category = parts[0], parts[1].upper(), parts[2]',
            "        if severity not in ('INFO', 'WARN', 'ERROR', 'DEBUG'):",
            '            rejected += 1',
            '            continue',
            "        records.append({'ts': ts, 'severity': severity, 'category': category})",
            '    return records, rejected',
            '',
            'def count_by_key(records, key: str):',
            '    counts = {}',
            '    for r in records:',
            "        k = r.get(key, '')",
            '        counts[k] = counts.get(k, 0) + 1',
            '    return counts',
            '',
            'def mode_of(records, key: str, filter_severity: str = None):',
            '    filtered = records',
            '    if filter_severity:',
            "        filtered = [r for r in records if r.get('severity') == filter_severity]",
            '    if not filtered:',
            '        return None',
            '    counts = count_by_key(filtered, key)',
            '    return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None',
            '',
            'def analyze_log(text: str) -> dict:',
            '    records, rejected = parse_records(text)',
            "    by_sev = count_by_key(records, 'severity')",
            "    top_err = mode_of(records, 'category', 'ERROR')",
            '    return {',
            "        'total': len(records),",
            "        'rejected': rejected,",
            "        'by_severity': dict(sorted(by_sev.items())),",
            "        'top_error_category': top_err,",
            '    }',
            '',
            'def format_report(report: dict) -> str:',
            '    lines = [',
            '        f"total={report.get(\'total\', 0)}",',
            '        f"rejected={report.get(\'rejected\', 0)}",',
            '        f"by_severity={report.get(\'by_severity\', {})}",',
            '        f"top_error_category={report.get(\'top_error_category\')}",',
            '    ]',
            "    return '\\n'.join(lines)",
        ]
        engine = ModuleSpec(package=pkg, module="engine",
            imports=["from __future__ import annotations"],
            helpers=engine_helpers, functions=[])
        cli = ModuleSpec(package=pkg, module="cli",
            imports=["from __future__ import annotations", "import argparse", "import sys",
                     "from pipeline.engine import analyze_log, format_report"],
            functions=[FunctionSpec(name="main", args=["argv=None"], returns="int",
                body_lines=[
                    '    p = argparse.ArgumentParser(prog="analyze")',
                    '    p.add_argument("path", nargs="?")',
                    '    p.add_argument("--text", default=None)',
                    "    args = p.parse_args(argv)",
                    "    if args.text is not None:", "        text = args.text",
                    "    elif args.path:",
                    '        with open(args.path, encoding="utf-8") as fh:',
                    "            text = fh.read()",
                    "    else:", "        text = sys.stdin.read()",
                    "    print(format_report(analyze_log(text)))",
                    "    return 0",
                ])])
        return [engine, cli]
    # pricing
    engine_helpers = [
        'def validate_price_inputs(base_price, discount_percent, adjustment=0.0):',
        '    if base_price is None or float(base_price) < 0:',
        "        raise ValueError('invalid base_price')",
        '    d = float(discount_percent)',
        '    if d < 0 or d > 100:',
        "        raise ValueError('invalid discount_percent')",
        '    return float(base_price), d, float(adjustment)',
        '',
        'def compute_final_price(base_price, discount_percent, adjustment=0.0) -> float:',
        '    base, disc, adj = validate_price_inputs(base_price, discount_percent, adjustment)',
        '    return base * (1.0 - disc / 100.0) + adj',
    ]
    engine = ModuleSpec(package=pkg, module="engine",
        imports=["from __future__ import annotations"],
        helpers=engine_helpers, functions=[])
    cli = ModuleSpec(package=pkg, module="cli",
        imports=["from __future__ import annotations", "import argparse",
                 "from pipeline.engine import compute_final_price"],
        functions=[FunctionSpec(name="main", args=["argv=None"], returns="int",
            body_lines=[
                '    p = argparse.ArgumentParser(prog="price")',
                '    p.add_argument("base_price", type=float)',
                '    p.add_argument("discount_percent", type=float)',
                '    p.add_argument("adjustment", type=float, nargs="?", default=0.0)',
                "    args = p.parse_args(argv)",
                "    try:",
                "        result = compute_final_price(args.base_price, args.discount_percent, args.adjustment)",
                "    except ValueError as exc:",
                "        print(exc)", "        return 1",
                "    print(result)", "    return 0",
            ])])
    return [engine, cli]


def tests_for_behavioral(ir, prims):
    kinds = {p.kind for p in prims}
    if kinds & {"parse_lines", "group_count"}:
        return (
            '"""Behavioral log tests."""\n'
            "from __future__ import annotations\nimport sys\nfrom pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parents[1]\nsys.path.insert(0, str(ROOT))\n"
            "from pipeline.engine import analyze_log\nfrom pipeline.cli import main\n\n"
            "SAMPLE = \"2024-01-01T00:00:00 INFO auth login\\n\"\n"
            "SAMPLE += \"2024-01-01T00:00:01 ERROR db timeout\\n\"\n"
            "SAMPLE += \"2024-01-01T00:00:02 ERROR db timeout\\n\"\n"
            "SAMPLE += \"2024-01-01T00:00:03 WARN cache miss\\n\"\n"
            "SAMPLE += \"not-a-record\\n\"\n"
            "SAMPLE += \"2024-01-01T00:00:04 ERROR net reset\\n\"\n\n"
            "def test_parse_and_counts():\n"
            "    report = analyze_log(SAMPLE)\n"
            "    assert report['total'] == 5\n"
            "    assert report['rejected'] >= 1\n"
            "    assert report['by_severity'].get('ERROR') == 3\n"
            "    assert report['top_error_category'] == 'db'\n\n"
            "def test_cli():\n    assert main(['--text', SAMPLE]) == 0\n\n"
            "if __name__ == '__main__':\n    test_parse_and_counts()\n    test_cli()\n    print('PASS')\n"
        )
    return (
        '"""Behavioral pricing tests."""\n'
        "from __future__ import annotations\nimport sys\nfrom pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\nsys.path.insert(0, str(ROOT))\n"
        "from pipeline.engine import compute_final_price\nfrom pipeline.cli import main\n\n"
        "def test_normal():\n    assert abs(compute_final_price(100, 10, 5) - 95.0) < 1e-9\n\n"
        "def test_invalid_price():\n    try:\n        compute_final_price(-1, 10, 0)\n"
        "        assert False\n    except ValueError:\n        pass\n\n"
        "def test_invalid_discount():\n    try:\n        compute_final_price(100, 150, 0)\n"
        "        assert False\n    except ValueError:\n        pass\n\n"
        "def test_cli():\n    assert main(['100', '10', '0']) == 0\n\n"
        "if __name__ == '__main__':\n    test_normal()\n    test_invalid_price()\n"
        "    test_invalid_discount()\n    test_cli()\n    print('PASS')\n"
    )

