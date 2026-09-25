"""Defect corpus for Track 1.

D1  -- Agent A's defect: single-function module, wrong binary operator.
D2  -- Agent B's defect: MATERIALLY DIFFERENT shape -- a two-function
       module where one function is correct and the other is defective.
       T1 (replace-the-whole-file) would delete the correct function;
       the technique must be ADAPTED (T2: diagnosis-driven targeting +
       surgical replacement).
R1  -- held-out single-function review instance for T1.
R2  -- held-out multi-function review instance for T2.
H1,H2 -- held-out single-function generality instances for T1 promote.
H3  -- held-out multi-function generality instance for T2 promote.
"""

# --- Agent A's defect: single function, wrong operator (a - b, want a + b)
D1_SRC = '''def add(a, b):
    return a - b
'''
D1_TEST = '''assert add(2, 3) == 5
assert add(0, 0) == 0
assert add(-1, 1) == 0
assert add(10, -4) == 6
'''
D1_FUNC = "add"
D1_EXAMPLES = [((2, 3), 5), ((0, 0), 0), ((-1, 1), 0), ((10, -4), 6)]

# --- Agent B's defect: two functions, one correct (mul), one defective
# --- (power uses * instead of **). Materially different from D1: the
# --- repair must target exactly one function and preserve the other.
D2_SRC = '''def mul(a, b):
    return a * b


def power(a, b):
    return a * b
'''
D2_TEST = '''assert mul(2, 3) == 6
assert mul(0, 5) == 0
assert power(2, 3) == 8
assert power(3, 2) == 9
assert power(2, 0) == 1
'''
D2_TARGET = "power"
D2_PRESERVE = "mul"
D2_EXAMPLES = {
    "mul": [((2, 3), 6), ((0, 5), 0)],
    "power": [((2, 3), 8), ((3, 2), 9), ((2, 0), 1)],
}

# --- review instance for T1 (single function, different defect)
R1_SRC = '''def diff(a, b):
    return a + b
'''
R1_TEST = '''assert diff(5, 3) == 2
assert diff(0, 0) == 0
assert diff(-2, -5) == 3
'''
R1_FUNC = "diff"
R1_EXAMPLES = [((5, 3), 2), ((0, 0), 0), ((-2, -5), 3)]

# --- review instance for T2 (multi-function, different defect)
R2_SRC = '''def total(a, b):
    return a + b


def remainder(a, b):
    return a + b
'''
R2_TEST = '''assert total(2, 3) == 5
assert total(0, 0) == 0
assert remainder(7, 3) == 1
assert remainder(10, 4) == 2
'''
R2_TARGET = "remainder"
R2_PRESERVE = "total"
R2_EXAMPLES = {
    "total": [((2, 3), 5), ((0, 0), 0)],
    "remainder": [((7, 3), 1), ((10, 4), 2)],
}

# --- held-out generality instances for T1 promote
H1_SRC = '''def prod(a, b):
    return a + b
'''
H1_TEST = '''assert prod(2, 3) == 6
assert prod(0, 5) == 0
assert prod(-2, 4) == -8
'''
H1_FUNC = "prod"
H1_EXAMPLES = [((2, 3), 6), ((0, 5), 0), ((-2, 4), -8)]

H2_SRC = '''def pw(a, b):
    return a * b
'''
H2_TEST = '''assert pw(2, 3) == 8
assert pw(5, 0) == 1
assert pw(3, 2) == 9
'''
H2_FUNC = "pw"
H2_EXAMPLES = [((2, 3), 8), ((5, 0), 1), ((3, 2), 9)]

# --- held-out generality instance for T2 promote
H3_SRC = '''def keep(a, b):
    return a - b


def broken(a, b):
    return a - b
'''
H3_TEST = '''assert keep(5, 3) == 2
assert keep(0, 0) == 0
assert broken(2, 3) == 6
assert broken(4, 5) == 20
'''
H3_TARGET = "broken"
H3_PRESERVE = "keep"
H3_EXAMPLES = {
    "keep": [((5, 3), 2), ((0, 0), 0)],
    "broken": [((2, 3), 6), ((4, 5), 20)],
}
