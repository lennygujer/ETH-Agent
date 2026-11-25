## Instructions

An ETH Zürich student told their study agent about an upcoming exam or deadline. Extract it so the agent can
plan towards it. Today is {today} ({weekday}). Reply ONLY with one fenced ```json block.

- date: resolve relative dates ("nächsten Freitag", "in zwei Wochen", "am 12.12.") relative to today; if the
  year is missing take the next occurrence that is not in the past. Format YYYY-MM-DD.
- kind: midterm | pruefung (end-of-semester / session exam) | test (quiz, short test) | abgabe (graded
  submission, bonus series) | sonstiges.
- title: short and in the student's words, e.g. "Midterm", "Sessionsprüfung", "Bonus-Serie 8".
- course: the id from the course list below. If the message does not name one, use the current course.
- topics: the topics exactly as the student named them (keep their language). Empty if none were named.
- node_ids: the ids from the learning map outline below that these topics cover. A topic that is a whole chapter
  maps to the chapter id; a narrower topic to the matching sub-section ids. Translate between German and English
  titles where needed. Leave empty if no outline is given or nothing matches. If the student says the exam
  covers "everything up to chapter X" or "Kapitel 1-5", list all those chapter ids.
- If the message contains several exams or deadlines, return several entries.
- If the date cannot be determined, return no exam entry and ask in "question" (in German, one short sentence).

```json
{
  "exams": [{"course": "", "kind": "midterm", "title": "", "date": "YYYY-MM-DD", "time": "",
             "topics": [], "node_ids": [], "note": ""}],
  "question": ""
}
```

## Current course

{current_course}

## Courses (id | name)

{courses}

## Learning map outline of the current course (id | title)

{outline}

## The student's message

{message}
