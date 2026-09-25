# Question export prompt (for other Claude chats)

**Purpose:** use the prompt below in any *other* Claude conversation where
you've generated 2027-season questions (MCQ/TF/Numerical/FRQ, with
images/answers/justification/difficulty). Paste it as-is — it's
self-contained and doesn't assume the other chat has any context from this
repo. It has that chat package everything into a zip bundle, which you then
import on the event's Generate page → **Import a question bundle** (see
HOWTO.md, "Importing a question bundle"). The import runs as a background
job with a preview first; spec.md §11d.2 documents exactly how each field
is stored.

Why a bundle rather than the older JSON import
(`POST /event/<slug>/api/sources/import-generated`): that path only
round-trips MCQ/FRQ text, and drops difficulty, real image files,
true/false, numerical questions and shared context blocks.

The schema is deliberately a superset of the internal Question shape
persisted in `.qbank_state.json` (spec.md §4) and the `import-generated`
candidate shape (`qgen.py` `candidate_to_question()`), so importing is a
mapping, not a conversion.

Numerical questions are stored as real numerical questions (value + unit +
quantity + significant figures, graded with unit conversion — spec.md §4b).
Since v1.1 the prompt asks for `unit`, `quantity` and `sig_figs` explicitly;
bundles from v1.0 still import, with those read from the answer text.

## Version history

| Version | Date       | Notes |
|---------|------------|-------|
| 1.0     | 2026-09-11 | Initial version. |
| 1.0     | 2026-09-24 | Header only: the importer now exists (prompt text unchanged). |
| 1.1     | 2026-09-25 | Numerical questions: optional `unit`, `quantity`, `sig_figs` fields. |
| 1.2     | 2026-09-25 | Quantity list adds `flow_rate` and `count`. |

## The prompt

Copy everything in the fenced block below into the target conversation.

```text
Export all the questions we've generated in this conversation as a
self-contained bundle for later import into a Science Olympiad question-bank
server, for the 2027 season. Do this now, without asking me to confirm the
plan first.

## What to collect
Scan this entire conversation and pull out every question you generated —
across every topic, and every type: multiple choice (MCQ), true/false (TF),
numerical answer, and free response (FRQ). Don't skip any because they look
like drafts or intermediate iterations — if a later message revised a
question, use the final revised version only.

## Per-question schema
Normalize every question into this JSON shape:

{
  "id": "q0001",                 // stable within this bundle, zero-padded, in the order you emit them
  "topic": "exact topic/unit name you used for this question",
  "qtype": "mcq | tf | numerical | frq",
  "text": "question stem; use $...$ for inline LaTeX math",
  "choices": [{"letter": "A", "text": "..."}, ...],   // [] for tf/numerical/frq
  "answer": "canonical answer — see rules below",
  "select_multiple": false,      // mcq only: true iff the answer has more than one letter
  "justification": "full explanation / derivation / worked solution",
  "difficulty": 0.0,             // float 0.0-1.0, or null if you didn't rate it — see scale below
  "images": ["q0001_fig1.png"],  // filenames inside this bundle's images/ folder; [] if none
  "image_description": "",       // only if a figure is needed but you didn't/couldn't generate one
  "source_snippet": "",          // optional short excerpt this question was derived from, if any
  "context_id": null,            // set to a contexts[].id if this question shares a passage/table/figure with others
  "unit": "m/s",                 // numerical only: the answer's unit ("" for a plain number or ratio)
  "quantity": "velocity",        // numerical only: what it measures — see the list below
  "sig_figs": 3                  // numerical only: significant figures the key is given to
}

Answer format by qtype:
- mcq: choice letter(s), comma-separated if more than one is correct (e.g. "A" or "A, C")
- tf: exactly the string "True" or "False"
- numerical: the value with units as plain text (e.g. "4.20 m/s"); put the
  derivation/equation in "justification", not in "answer". Also fill
  "unit" (as in the answer), "sig_figs" (how many significant figures the
  answer is given to — write the value to that many, e.g. "4.20" for 3),
  and "quantity", one of: length, area, volume, time, mass, velocity,
  acceleration, force, momentum, energy, torque, power, pressure, density,
  flow_rate, frequency, angle, temperature, amount, concentration, charge,
  current, voltage, resistance, conductance, resistivity, capacitance,
  inductance, magnetic_field, magnetic_flux, electric_field, heat_capacity,
  specific_heat, fraction, count, or other.
  Use "count" when the answer is a number of things ("24 runs", "48
  chromatids"): its "unit" is the thing counted, one or two words.
- frq: the expected short-answer text

Difficulty scale (0.0 = easiest, 1.0 = hardest): if you or I rated questions
on a different scale earlier in this conversation (1-5, Easy/Medium/Hard,
stars, etc.), convert it to this 0.0-1.0 float. Rough bands: Easy ≤0.3,
Medium 0.3-0.5, Hard 0.5-0.7, Very Hard >0.7. Leave "difficulty": null for
anything genuinely unrated — don't guess a number just to fill the field.

## Shared context blocks
If two or more questions share a passage, data table, or figure, don't repeat
it per-question. Instead add one entry to a top-level "contexts" array:
  {"id": "ctx1", "title": "optional title", "text": "shared passage/table text", "images": ["ctx1_fig.png"]}
and set those questions' "context_id" to "ctx1".

## Images
For any image you actually generated/attached earlier in this conversation
(a diagram, a plot, a labeled figure), include the real image file in the
bundle's images/ folder under the filename referenced in that question's (or
context's) "images" list. If a question needs a figure you described but
never generated an actual image for, leave "images": [] and put the
description in "image_description" instead — don't fabricate a placeholder
image.

## Bundle wrapper
{
  "bundle_version": "1.2",
  "event": "<name of the Science Olympiad event this conversation has been
            working on>",
  "season": "2027",
  "generated_at": "<ISO 8601 timestamp, now>",
  "contexts": [ ... ],
  "questions": [ ... ]
}

## Package it
Build a zip file named "<event>_2027_export_<YYYYMMDD>.zip" containing:
  manifest.json          (the bundle wrapper above)
  images/                (every real image file referenced by any question or context)
  README.txt             (plain-text summary — counts by topic and by qtype,
                          how many have images, how many have a difficulty
                          rating, how many are missing a justification — so
                          I can sanity-check the export before importing it)

If you have code execution / file-creation available, actually build and
return the zip. If you don't have a way to produce a zip in this
environment, instead: (1) output manifest.json in full as a labeled code
block, (2) list every image file that needs to be saved separately with its
intended filename, and (3) tell me explicitly that I'll need to zip these
together myself — don't silently skip the packaging step.

## Finish with
A short summary table: question count broken down by topic × qtype, plus
total count, image count, and any questions you couldn't fully populate
(e.g. missing answer, missing justification) so I know what needs manual
cleanup before this gets imported.
```
