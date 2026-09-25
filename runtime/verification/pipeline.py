"""
swarm_engine/verification/pipeline.py

Multi-stage verification for anything the engine synthesizes for itself.
Nothing synthesized by CapabilitySynthesizer is executed or registered
until it passes ASTVerifier. This is the gate that makes self-extension
safe: the engine is allowed to write its own code, but never allowed to
run code it wrote without auditing it first.
"""
import ast
import time

RESTRICTED_MODULES = {
    "os", "subprocess", "sys", "socket", "shutil", "ctypes",
    "importlib", "pickle", "marshal", "pty", "signal", "multiprocessing",
}
RESTRICTED_CALLS = {"exec", "eval", "compile", "__import__", "open", "input"}
RESTRICTED_ATTRS = {"system", "popen", "spawn", "remove", "rmtree", "unlink", "chmod"}


class ASTVerifier(ast.NodeVisitor):
    def __init__(self):
        self.violations = []

    def visit_Import(self, node):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in RESTRICTED_MODULES:
                self.violations.append(f"Restricted import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.module.split(".")[0] in RESTRICTED_MODULES:
            self.violations.append(f"Restricted import: {node.module}")
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id in RESTRICTED_CALLS:
            self.violations.append(f"Restricted call: {func.id}()")
        if isinstance(func, ast.Attribute) and func.attr in RESTRICTED_ATTRS:
            self.violations.append(f"Restricted call: .{func.attr}()")
        self.generic_visit(node)

    def check(self, source: str):
        self.violations = []
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return {"safe": False, "violations": [f"SyntaxError: {e}"]}
        self.visit(tree)
        return {"safe": len(self.violations) == 0, "violations": self.violations}


def basic_schema_check(payload: dict):
    return {"stage": "schema", "passed": isinstance(payload, dict)}


class VerificationPipeline:
    """Runs a synthesized artifact through whatever stages are registered.
    Used both for generic capability payloads and, via verify_code(),
    for auditing synthesized source before it's ever exec'd."""

    def __init__(self):
        self.stages = []
        self.verifier = ASTVerifier()

    def add_stage(self, fn):
        self.stages.append(fn)
        return self

    async def verify(self, payload: dict) -> dict:
        results = []
        for stage in self.stages:
            r = stage(payload)
            results.append(r)
            if not r.get("passed", True):
                return {"status": "failed", "score": 0.0, "stages": results}
        return {"status": "passed", "score": 1.0, "stages": results}

    def verify_code(self, source: str) -> dict:
        """Static safety audit of synthesized Python source. Synchronous —
        this must happen before anything is scheduled or executed."""
        result = self.verifier.check(source)
        return {
            "status": "passed" if result["safe"] else "failed",
            "score": 1.0 if result["safe"] else 0.0,
            "violations": result["violations"],
            "checked_at": time.time(),
        }
