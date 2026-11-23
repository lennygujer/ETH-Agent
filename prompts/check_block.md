Verification code: if your answer contains a numeric or symbolic result that can be computed, end your answer
with exactly one fenced code block that starts with ```python check and contains standalone Python 3 code
(allowed: math, numpy, scipy, sympy, pint) that independently recomputes the key results from the given data
and compares them with the values stated in your answer using assert statements (relative tolerance 1e-2 for
numbers, sympy.simplify(a - b) == 0 for expressions). Recompute from the problem data; never just assert a
hard-coded value against itself. Print one line per check, e.g. print("CHECK fringe spacing: ok"). The code runs
without network access and without input. If nothing in the answer is computable, omit the block.
