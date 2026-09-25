"""
swarm_engine/cognition/semantic_interpreter.py

SemanticInterpreter: sentence + behavioral examples -> verified program.

The compositional interpretation mechanism. NOT a parser, NOT keyword
routing. The causal chain:

  1. The SemanticLexicon proposes candidate OPS for the sentence
     (distributionally learned from REMOR's own verified experience).
  2. The interpreter builds a FILTERED registry view containing only those
     ops (+ structural glue). The sentence DETERMINES the search space.
  3. The EXISTING GeneralSynthesizer searches that restricted space against
     the INDEPENDENT behavioral examples. The examples determine the program.
  4. If synthesis finds a program satisfying all examples -> interpreted.
     If the lexicon yields no confident ops, or synthesis fails -> None
     (fail closed, never guess).

Why this is compositional:
  The sentence does not select a program; it selects the *vocabulary* from
  which the program is composed. Novel sentences compose learned phrase->op
  associations into programs never seen during training. Paraphrases that
  map to the same ops yield the same program (semantic equivalence, not
  string matching).

Why this is honest:
  - The lexicon is learned, not authored (see semantic_lexicon.py).
  - The synthesizer is unmodified; only the registry VIEW is filtered.
  - Behavioral examples are independent of the sentence (they come from
    the task, not from the lexicon).
  - Every interpretation is verified against the examples before acceptance.
  - Failure modes are explicit: no confident ops -> None; no program in
    the restricted space -> None.

Architectural separation:
  The interpreter uses a SEPARATE SearchBias (fresh, not the engine's) and
  a filtered registry VIEW (not a modified registry). General synthesis,
  SearchBias, and the main registry are untouched. Language evidence does
  not contaminate ordinary synthesis.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


# Structural glue: the compositional operators that combine lexicon-
# suggested ops into programs. These are architectural (the fixed
# compositional vocabulary), not per-sentence. Always available in the
# filtered view so that learned ops can be composed.
_STRUCTURAL_OPS = frozenset({"if_else"})


@dataclass
class Interpretation:
    """The result of interpreting a sentence."""
    sentence: str
    program: Any                    # verified Expr, or None
    candidate_ops: Dict[str, float] # lexicon-proposed ops with confidence
    verified: bool                  # True iff program satisfies all examples
    failure_reason: Optional[str] = None
    trace: List[str] = field(default_factory=list)

    def ok(self) -> bool:
        return self.verified and self.program is not None


class FilteredRegistryView:
    """A read-only view over a PrimitiveRegistry exposing only allowed ops.
    The underlying registry is never modified."""

    def __init__(self, base_registry, allowed_ops: Set[str]):
        self._base = base_registry
        self._allowed = frozenset(allowed_ops)

    def names(self):
        return [n for n in self._base.names() if n in self._allowed]

    def get(self, name):
        if name not in self._allowed:
            return None
        return self._base.get(name)

    def resolve(self, name):
        if name not in self._allowed:
            return None
        return self._base.resolve(name)

    def __getattr__(self, item):
        # Delegate anything else (governor, etc.) to the base registry.
        return getattr(self._base, item)


class SemanticInterpreter:
    """Interprets natural-language sentences into verified programs."""

    def __init__(self, base_registry, lexicon,
                 min_op_confidence: float = 0.3,
                 search_timeout: float = 60.0,
                 max_size: int = 3):
        self.base_registry = base_registry
        self.lexicon = lexicon
        self.min_op_confidence = min_op_confidence
        self.search_timeout = search_timeout
        self.max_size = max_size

    def interpret(self, sentence: str,
                  examples: Sequence[Tuple[Dict[str, Any], Any]],
                  param_names: Sequence[str],
                  extra_literals: Sequence[Any] = ()
                  ) -> Interpretation:
        """Interpret a sentence against behavioral examples.

        Returns an Interpretation. If .ok() is False, check .failure_reason.
        Never raises on linguistic failure; fail-closed by design.
        """
        trace: List[str] = []
        if not sentence or not sentence.strip():
            return Interpretation(sentence, None, {}, False,
                                  "empty sentence", trace)
        if not examples:
            return Interpretation(sentence, None, {}, False,
                                  "no behavioral examples", trace)

        # 1. Lexicon proposes candidate ops.
        candidate_ops = self.lexicon.ops_for(
            sentence, min_confidence=self.min_op_confidence)
        trace.append(f"lexicon proposed {len(candidate_ops)} ops: "
                     f"{sorted(candidate_ops)[:12]}")
        if not candidate_ops:
            return Interpretation(
                sentence, None, {}, False,
                "fail-closed: no confident phrase->op associations",
                trace)

        # 2. Build filtered registry view: candidate ops + structural glue
        #    (only glue ops actually present in the base registry).
        allowed = set(candidate_ops)
        for g in _STRUCTURAL_OPS:
            if self.base_registry.get(g) is not None:
                allowed.add(g)
        # Ensure every candidate op actually exists in the registry.
        allowed = {o for o in allowed
                   if self.base_registry.get(o) is not None}
        trace.append(f"filtered registry: {len(allowed)} ops")
        if not allowed:
            return Interpretation(sentence, None, candidate_ops, False,
                                  "fail-closed: no candidate ops in registry",
                                  trace)

        # 3. Lexicon-suggested literals become extra literals for search.
        suggested_lits = self.lexicon.literals_for(
            sentence, min_confidence=self.min_op_confidence)
        all_lits = list(extra_literals) + [l for l in suggested_lits
                                           if l not in extra_literals]
        if suggested_lits:
            trace.append(f"lexicon suggested literals: {suggested_lits[:8]}")

        # 4. Synthesize in the restricted space with an INDEPENDENT bias
        #    (fresh temp DB: language evidence stays out of SearchBias).
        from swarm_engine.cognition.synthesis import GeneralSynthesizer
        from swarm_engine.cognition.representations import SearchBias
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            bias = SearchBias(db_path=tmp.name)
            view = FilteredRegistryView(self.base_registry, allowed)
            synth = GeneralSynthesizer(view, bias, max_size=self.max_size)
            hyp, syn_trace = synth.search(
                list(examples), list(param_names), tuple(all_lits))
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        if hyp is None or hyp.expr is None:
            return Interpretation(
                sentence, None, candidate_ops, False,
                "synthesis found no program in lexicon-restricted space",
                trace)

        # 5. The synthesizer already verified against all examples
        #    (it only returns on full match). Record and return.
        trace.append(f"synthesized: {hyp.expr.canonical()[:120]}")
        return Interpretation(sentence, hyp.expr, candidate_ops, True,
                              None, trace)

    def to_semantic_structure(self, interp: Interpretation):
        """Lift a verified interpretation to a SemanticStructure.

        The structure is the compositional meaning representation:
        nodes for the sentence's phrases (labeled by their learned ops),
        edges for the program's dataflow. This is the IR that downstream
        decomposition and reasoning consume -- NOT the raw sentence.
        """
        if not interp.ok():
            return None
        from swarm_engine.acquisition.semantic_structure import (
            SemanticStructure, SemanticNode, SemanticEdge)
        from swarm_engine.cognition.semantic_lexicon import (
            tokenize, ngrams)
        import uuid

        def _attrs(d: dict):
            return tuple((k, v) for k, v in d.items())

        nodes: List[Any] = []
        edges: List[Any] = []
        # Root: the interpreted sentence.
        root = SemanticNode("sentence", "SENTENCE",
                            _attrs({"text": interp.sentence}))
        nodes.append(root)
        # Phrase nodes: each n-gram with a confident op association.
        for gram in ngrams(tokenize(interp.sentence)):
            for e in self.lexicon.lookup(gram, kind="op",
                                         min_confidence=self.min_op_confidence):
                node = SemanticNode(f"phrase:{gram}", "PHRASE",
                                    _attrs({"phrase": gram, "op": e.value,
                                            "confidence": e.confidence}))
                nodes.append(node)
                edges.append(SemanticEdge(root.node_id, "REALIZES",
                                          node.node_id))
        # Program node: the verified executable meaning.
        prog = SemanticNode("program", "PROGRAM",
                            _attrs({"canonical": interp.program.canonical(),
                                    "ops": list(interp.program.ops_used())}))
        nodes.append(prog)
        edges.append(SemanticEdge(root.node_id, "MEANS", prog.node_id))
        # Op nodes: dataflow from the program tree.
        all_nodes, all_edges = self._expr_graph(
            interp.program, prog.node_id, nodes, edges)
        return SemanticStructure(
            structure_id=f"interp_{uuid.uuid4().hex[:12]}",
            nodes=tuple(all_nodes), edges=tuple(all_edges),
            provenance={"sentence": interp.sentence,
                        "candidate_ops": dict(interp.candidate_ops)})

    def _expr_graph(self, expr, parent_id: str,
                    nodes: List[Any], edges: List[Any]):
        from swarm_engine.acquisition.semantic_structure import (
            SemanticNode, SemanticEdge)

        def _attrs(d: dict):
            return tuple((k, v) for k, v in d.items())

        try:
            if expr.is_leaf():
                label = (f"lit:{expr.literal!r}" if expr.is_literal
                         else f"param:{expr.name}")
                node = SemanticNode(label,
                                    "LITERAL" if expr.is_literal else "PARAM",
                                    _attrs({"value": repr(getattr(
                                        expr, 'literal',
                                        getattr(expr, 'name', '')))}))
            else:
                node = SemanticNode(f"op:{expr.op}", "OP",
                                    _attrs({"op": expr.op}))
            nodes.append(node)
            edges.append(SemanticEdge(parent_id, "COMPOSED_OF", node.node_id))
            if not expr.is_leaf():
                for _, child in expr.children:
                    self._expr_graph(child, node.node_id, nodes, edges)
        except Exception:
            pass
        return nodes, edges
