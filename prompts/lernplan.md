## Instructions

You build a practice plan for an ETH Zürich student in the course {course_name}: which existing exercises from
their course material to solve, in which order, until {horizon}. Today is {today}. Write in
{output_language_name}, addressing the student as "du". Reply ONLY with one fenced ```json block.

### What matters

- Goal: being able to solve the exam problems on the exam topics. Weak topics of the exam material (⚠️
  misconception, 🔴 missing, 🟡 shaky) come first; untested topics (⚪) need at least one problem each so the
  learning map finds out where the student stands; topics that already sit (✅) get at most one late retest.
- A ⚠️ misconception or 🔴 gap should first be understood, not drilled: put those topics into `learn_first`
  (the student then runs `/learn <topic>`), and schedule a problem on them a few days AFTER that.
- Interleave: never schedule two problems on the same topic on the same day. Mixed practice beats blocks.
- Difficulty: start each topic with a straightforward exercise-sheet problem; old exam problems on that topic
  later; full old exams / mock exams in the last days before the exam.
- Prefer problems that have an official solution (has_solution = yes) – the student checks them with `/check`.
- Problems already done (marked done) are not scheduled again unless they went wrong and it was long ago.
- Realistic load: about {minutes_per_day} minutes per day, no problems on the exam day itself. Estimate minutes
  per problem from its length and type (exercise sub-task 15-30, full exercise 30-60, exam problem 30-45).
- 6 to 20 items in total. Use only candidates from the list (by their number).

### Output

```json
{
  "summary": "2-3 sentences: where the student stands relative to the exam material and what the plan focuses on",
  "learn_first": ["topic titles to understand first with /learn (from the map, max 3)"],
  "items": [{"cand": 12, "node_id": "map node id this problem trains", "priority": 1,
             "why": "one short sentence, concrete (e.g. 'Fehlvorstellung bei Faltung mit Sprungfunktion')",
             "minutes": 30, "day": "YYYY-MM-DD"}],
  "advice": "1-2 sentences on how to work through it (e.g. first attempt without solution, Sicherheit angeben)"
}
```
priority: 1 = must (weak topic in the exam material), 2 = should, 3 = if time remains.

## Exam

{exam}

## Topics in scope with status (id | title | status | last evidence)

{topics}

## Candidate problems (number | source | problem | topic it was found for | official solution | done)

{candidates}
