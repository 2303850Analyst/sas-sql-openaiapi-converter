"""Versioned SAS guidance, not an equivalence verifier or a hidden SQL repair.

See fixtures/semantic_eval/SOURCES.md for primary documentation and limitations.
All candidates must still pass the independent database comparison.
"""

SAS_GUIDANCE = """
Apply SAS semantics explicitly, using the source_semantics and typed bindings in the plan.
Never infer missing-value encoding or original observation order from a column's name.
If a necessary representation is unconfirmed, list that unresolved precondition in assumptions.

MISSING AND COMPARISON:
With ordinary_as_null, SAS ordinary numeric missing is SQL NULL. In SAS DATA step comparisons
it is smaller than every nonmissing number, so x<0 includes x IS NULL; numeric truth excludes
both missing and zero. SQL three-valued comparisons are not the SAS truth table.
With null_empty_ascii_spaces, character MISSING includes NULL, empty, and only ASCII spaces.
Tabs and NBSP are not ASCII spaces. Character equality ignores trailing ASCII padding only;
preserve leading and internal spaces, case and punctuation. Use TRIM(TRAILING ' ' FROM value)
for equality and COALESCE(...,'') to equate character missing representations. Do not trim
both ends for equality. Pass-through character values keep their transported representation.
SAS scalar SUM(x,y,...) ignores missing arguments and returns missing if all are missing;
it differs from x+y, which propagates missing. SQL aggregate SUM has separate group semantics.
In ClickHouse preserve NULL for an all-missing aggregate group using sumOrNull,
avgOrNull (or an equivalent explicit count guard); plain sum returns zero. Make
unmatched LEFT JOIN values nullable explicitly: default ClickHouse join_use_nulls=0
otherwise introduces zero/empty values. The generated SQL must carry any required
per-query setting instead of assuming a server setting.
Only when numeric_arithmetic=exact_decimal_transport is approved, preserve exact decimal
input semantics. ClickHouse avg/avgOrNull returns Float64 even on Decimal input.
For a decimal AVG output compute decimal SUM divided by the nonmissing COUNT with
divideDecimal, enough guard digits derived from input scale and count range, then one
final rounding at the declared storage scale. Use an explicit nullable result on
COUNT=0 and a nonzero internal denominator even when branches can be evaluated eagerly.
The supported denominator form is toDecimal128(greatest(count(value),1),0).
Use sumOrNull for the numerator, and avoid toDecimal256 in the current validator.
The review must distinguish the agreed storage rounding from QA normalization.
Do not infer this policy from SAS in general or apply it to source_binary_float.
SAS sum statement total+amount retains total, initializes it to zero, and treats missing as zero.
Explicit FIRST.group resets take effect before this addition. Use a ROWS running window with
the complete proven order, not the default RANGE frame. All-missing running totals are zero.
If special_tags_lost and source distinguishes .A-.Z or ._, information has been destroyed:
do not turn the special tag into IS NULL; return an unresolved assumption and no executable guess.

ORDER AND STATE:
For numeric SAS BY order use ASC NULLS FIRST and DESC NULLS LAST explicitly, also inside windows.
SAS FIRST/LAST applies to the full preceding BY hierarchy. Use the original explicit sort keys,
including tie-breakers. A CTAS ORDER BY is not an ordering guarantee for the next SELECT.
When an intermediate binding provides logical_order, reproduce that exact order in the
dependent computation, including any ROW_NUMBER used as an ordinal. Never prepend a
row ID. A declared non-null unique key that is a subset of the ordered columns already
proves absence of ties; do not ask for a redundant composite uniqueness declaration.
Never invent an original observation sequence from physical SQL order, ctid, values, or row_number
without a proven order. NODUPKEY EQUALS retains the first original observation within equal keys;
if duplicates may exist and that sequence is unavailable, refuse instead of choosing a row or
merely sorting all duplicates. Reproducible sorting is not the same as the original SAS order.
Conditional LAG maintains a queue of calls, not all observations. First filter to the observations
which execute that particular LAG occurrence, compute LAG in proven order, and join those values
back by a unique row key. A missing queued value is retained; do not skip nulls. Different LAG
occurrences have different queues. CASE WHEN condition THEN LAG(...) over all rows is incorrect.

MATCH MERGE:
All BY keys use SAS equality, including missing keys: numeric IS NOT DISTINCT FROM, and character
trailing-padding/blank normalization. SQL = loses missing-key matches. Never substitute a many-to-many
SQL join for DATA step MERGE. For each BY group, independently number left and right observations
in their original proven order. The group emits max(left_count,right_count) iterations. A side is
read when that iteration exists; after exhaustion its side-specific variables retain the last value.
IN= flags persist for the group, so IF a keeps every iteration of groups with any left observation,
including iterations after left exhaustion. Right-only groups are excluded by IF a.
Construct a distinct (BY keys, ordinal) spine from the two numbered sides, then match each side to
min(ordinal,side_count), using CASE if needed. A spine with UNION, counts and left joins avoids
PostgreSQL FULL JOIN limitations with null-safe non-equality join predicates. Do not use UNION ALL
for the spine or it duplicates iterations. Do not use UNION when appending actual SET data rows.
For a shared non-BY variable, reproduce PDV read events: each actual left read overwrites its value,
then each actual right read overwrites it, even with missing. If only left reads on a later iteration,
its current value wins over the old right value; if only right reads, right wins. Retained values are
not new read events. COALESCE and unconditional preference for either side both change the program.
Use a simple one-to-many join only when the metadata proves the relevant BY group uniqueness and
there are no shared-variable overwrite effects. Declared unique keys are preconditions, not hints.

DATA STEPS, MACROS AND FORMATS:
WHERE reads input values before assignments. Subsetting IF reads current PDV values at its position.
Each assignment and IF sees effects of preceding statements. Model sequential dependencies with
separate CTE stages, not parallel expressions or an ELSE CASE invented from independent IFs.
OUTPUT emits a snapshot at that point; explicit OUTPUT suppresses the implicit final output.
SET multiple datasets appends observations and preserves duplicates: UNION ALL with aligned columns.
PROC APPEND FORCE aligns columns by name, never position. A base column absent from DATA
receives typed missing, and an extra DATA column is discarded. Preserve the BASE schema.
CATX trims leading and trailing ASCII blanks on each argument and skips blank/missing
arguments before inserting separators. This rule does not change ordinary string equality.
When the output contract declares DATE, enforce that type on the final expression,
including after PostgreSQL date/interval arithmetic.
For confirmed_external_assignment use the owner-supplied value for its parameter in
contract.parameters. Never query the process environment or assign that step a SQL table.
Apply input RENAME before expressions; apply output DROP/KEEP after all expressions using those fields.
Expand fixed %LET and macro call arguments with their actual scope, resolve all branches for this run.
Do not emit macro definitions, calls, RUN or QUIT into SQL, or remove those words from string values.
FORMAT attaches display metadata; it does not turn a numeric/date value into a string. DATEPART over
a transported TIMESTAMP yields DATE. INTNX month alignment b yields the first DATE of that month.
Use typed date/timestamp literals and exact inclusive/exclusive bounds. FORMAT YYMMDD10. is not TO_CHAR.
PUT does produce text: Z5. for proven nonnegative integers in range produces five digits with leading
zeros, e.g. LPAD(CAST(value AS TEXT),5,'0'). Respect sign, missing, overflow and range if not excluded.
LENGTH defines a SAS character buffer at its first definition; longer assigned strings are truncated.
For an ASCII literal, SUBSTRING(... FROM 1 FOR declared_length) reproduces this truncation. Do not
claim general UTF-8 byte truncation from character-count SUBSTRING without an encoding contract.

Do not treat these rules or your own confidence as proof. Produce a complete candidate or clearly
identify an unresolved blocker. If impossible, assumptions must be nonempty; an inert comment-only
artifact is acceptable for a refused proposal and will never be executable.
"""
