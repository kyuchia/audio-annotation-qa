# Annotation QA: design notes

**Web-based validation, comparison, and review tracking for research audio annotations.**

---

## What it is

A Django + DRF + Vue application for the stage *after* annotation: you already have
annotation files — note onsets, pitches, event labels — produced in Tony, Sonic Visualiser,
a script, or by hand, and you need to know whether they are sound, where two annotators
disagree, and what anyone has decided about them.

### The problem it addresses

A folder of annotation CSVs is a reasonable way to produce annotations and a poor way to
keep them. On its own, a CSV has:

- **No validation.** An annotation that stops before the audio ends, overlapping segments,
  an assumed sample rate, or an onset column read as an offset column all import silently.
- **No history.** When a file is corrected, the previous state is gone.
- **No record of review.** Quality decisions end up as spreadsheet columns with no author,
  timestamp, or evidence.
- **No way to express disagreement.** A filesystem can place one annotator's file "after"
  the other, but cannot say the two are parallel and equally valid.

This project stores annotations as structured segments with their provenance, runs
validators that report rather than silently fix, compares two annotations of the same audio
without treating either as the reference, and keeps review decisions as first-class records
with the evidence behind them.

---

## Demo data

The demo corpus is a five-excerpt subset of **vocadito**: short solo singing recordings,
mono, 44.1 kHz. Every excerpt carries **two note annotations, A1 and A2, made separately
by two annotators**. See [`demo-data/vocadito/ATTRIBUTION.md`](../demo-data/vocadito/ATTRIBUTION.md)
for the dataset's authors, licence and citation.

The five excerpts were chosen so that each shows a different kind of disagreement under the
default tolerances (onset matching, 0.1 s onset tolerance, 50 cents pitch tolerance):

- `vocadito_8` — near-total agreement; the baseline case.
- `vocadito_16` — every A2 note is matched, yet nine pairs differ in both pitch and timing.
- `vocadito_17` — segmentation differs in both directions, with pitch largely agreed.
- `vocadito_21` — some A1 notes subdivide longer A2 notes.
- `vocadito_34` — A2 is the finer-grained annotator, with the most pitch disagreement.

Among matched note pairs, the median absolute onset difference is 0.000 s in all five
excerpts. Unmatched notes, however, reveal substantial differences in segmentation, and
matched pairs still differ in the pitch assigned to moving notes. Both are visible only
because pitch and timing are reported separately.

---

## Running it

Setup is in the [README](../README.md). A few additions:

- **API** — http://localhost:8000/api/. `GET /api/me/` reports whether the current session
  is signed in.
- **Permissions** — reads are open to everyone; any write (uploading an annotation, running
  validators, creating a comparison, recording a review) requires a signed-in user. Signed-out
  visitors see the UI without write controls.
- **Re-running validators** from the command line:

  ```bash
  docker compose exec django python manage.py run_validators --corpus vocadito-demo --show
  ```

- **Full dataset** — download vocadito, point `DATASET_ROOT` in `.env` at its parent
  directory, and run `import_vocadito` against it. The subset has the same layout as the
  full release.

---

## Architecture

### Models

```
Corpus                     a dataset root, its metadata CSV, its validator settings
└─ Item                    one piece of audio; specs always read from the file header
   └─ AnnotationLayer      one annotation of that audio: type, stream, version, author
      └─ Segment           start_sec, end_sec, payload JSON
```

Two design rules are fixed at the model layer:

**Times are seconds, never frames.** Frame indices exist only in the Sonic Visualiser
exporter, computed with that item's own sample rate. With no sample rate read, the export
refuses rather than assuming one.

**Version lineage and parallel annotation are different things.** `version`, `parent` and
`status` are all scoped to `(item, layer_type, stream_key)`:

- A corrected annotation is a **new version within one stream**. The previous version is
  kept, `parent` points at it, and an accepted predecessor becomes `superseded`.
- Two annotators working independently get **a stream each** — `A1` and `A2`. Both are
  version 1, neither has a parent, and neither is ever superseded by the other. The
  annotator is also recorded in the layer's `provenance`, so the identity does not depend
  on how a stream key happens to be spelled.

An import never overwrites a layer. The stored segments always match the file as it
arrived; a known defect is fixed by adding a `LayerCorrection` — a switchable overlay with
an author and a required reason, applied when the data is read or exported — so the
corrected view and the original both remain available.

### Pluggable layer types

A layer type is one `register_layer_type()` call declaring its payload schema, its
parameters, its default comparator, which validators apply to it, and any threshold
overrides those validators should use for its kind of data. Models, serializers and admin
are untouched by adding one. Three are registered: `note_pitch` (time interval plus pitch),
`sound_event` (time interval plus label) and `audio_tagging` (clip-level labels).

### Validator and comparator registries

Validators and comparators register against layer types rather than being hard-coded. A
validator is a pure function taking a model instance and returning findings; it never
writes, never deletes and never modifies data. Which ones run is decided in three places,
each owning the question it can answer:

- the **layer type** selects the layer-scope checks and sizes their thresholds for its data
- the **corpus** (`skip_validators`) switches off corpus- and item-scope checks that have no
  content for it — and the skip is named in the run summary, because "not run" and "found
  nothing" are different results that an empty finding list cannot distinguish
- the **caller** can override any threshold for a single run

Comparison logic lives in one service (`annotations/services/compare.py`) shared by the API
and the importer, so a comparison created by either path is the same computation.

---

## Validators

| Key | Scope | Reports |
|---|---|---|
| `disk_metadata_consistency` | corpus | Items with no metadata row, provisional keys, inferred metadata matches, and how many items carry metadata at all |
| `media_probed` | item | The audio header has not been read, so anything needing a sample rate will fail |
| `metadata_duration_agreement` | item | The metadata's claimed duration disagrees with the header. The header always wins; this notices that the metadata has gone stale |
| `recording_linked` | item | Whether an exported session points at the recording the annotations were made against or at a stand-in, plus duration, sample-rate and channel mismatches against the linked file |
| `segments_monotonic` | layer | Segments whose offset precedes their onset, and overlapping pairs. An overlap above `segment_overlap_tolerance_sec` is a warning; one at or below it is reported as `info` with its magnitude. Tolerance defaults to `0` |
| `annotation_coverage` | layer | How much of the reference length is annotated, an unannotated stretch at the end, and annotation running past the audio |
| `annotation_internal_gaps` | layer | Unannotated stretches mid-recording. Always `info`, and the item's notes travel with the finding: a silence in the material and a missed annotation are identical in the data |
| `clip_single_segment` | layer | A clip-level annotation holding more than one segment |

Severities are `error`, `warning` and `info`. A finding is never deleted — it is
*acknowledged*, with a note, so that "a human looked at this and judged it acceptable"
stays on the record.

**Current findings.** Every validation run is kept. The UI and API list, for each item or
layer, only the findings from the most recent run that *covered* it — whether or not that
run reported anything — so a problem fixed in a later run stops being listed, and older runs
remain available as history (`/api/validation-runs/<id>/issues/`). An item's status and the
item list's "Errors and warnings" filter both count current errors and warnings only; `info`
findings stay visible on the item page.

**Why overlaps carry a tolerance.** In `vocadito_21`, two adjacent notes in stream `A1`
overlap by about 23 µs: the source states each note as onset plus duration, and the two
values are rounded independently. A real conflict and a rounding artifact differ only in
magnitude, so `note_pitch` sets `segment_overlap_tolerance_sec` to 1 ms. The validator
reports this overlap as an `info` finding with its size, instead of ignoring it or treating
it as an error.

---

## Comparison semantics

**Disagreement is not error.** When two independent annotations differ, neither is the
reference for the other, and the comparator reports the difference without ruling on it.
Problems with an annotation in itself are the validators' business, reported separately.
The UI words the categories accordingly: "annotated only by A", not "missing from B".

Matched pairs fall into categories that are kept apart rather than summed:

| Category | Meaning |
|---|---|
| `pitch_only` | Pitch difference exceeds its tolerance; timing does not |
| `timing_only` | Timing difference exceeds its tolerance; pitch does not |
| `pitch_and_timing` | Both differences exceed their tolerances |
| `only_in_a` | Annotated by A, with no counterpart in B |
| `only_in_b` | Annotated by B, with no counterpart in A |

Pitch is always compared **in cents, never in Hz directly**, and each layer is converted
against its own tuning reference first — two annotations made at different references
describe the same note, and comparing Hz would report that as a pitch disagreement. How to
weigh a pitch difference against a timing difference is a scoring policy, so there is no
single agreement number: a comparison that collapsed both into one score would hide the
distinction the data is actually about.

Matching is greedy: every candidate pair is scored, then paired from the best score down
with each segment used at most once. Weaker than optimal assignment, but a reviewer can see
why two segments were paired.

**Tolerances are stored with every comparison.** The `params` that produced a result are
saved alongside it, because a difference is only meaningful relative to the standard it was
measured against — and defaults move. The matching strategy is explicit in those params:
the comparator refuses to run without one rather than quietly picking a default that the
stored record would then fail to mention.

The importer stores one comparison per item at the default parameters (idempotently), and
the item page shows the most recent stored comparison on load. Comparisons run at other
tolerances are kept alongside it.

---

## Sonic Visualiser session export

Both formats are export-only: the application generates `.svl` and `.sv` files for Sonic
Visualiser but does not parse them. Annotations made in Sonic Visualiser come in as CSV.

- **`.svl`** — a single layer, loaded via *File → Import Annotation Layer*.
- **`.sv`** — a whole session: the audio plus every layer, opening on a double click.

Three details decide whether the result actually opens:

- The audio model carries `mainModel="true"`. Without it the session opens with no main
  model, and therefore no audio and no duration.
- Ids come from one counter shared across models, datasets and display layers, so nothing
  in the file shares an id.
- The session is **bzip2**-compressed, not gzip. Handed a gzipped session, Sonic Visualiser
  does not report a format error: it fails to decompress, falls back to reading the bytes as
  a plain-text annotation layer, and complains about a missing audio file — which points at
  entirely the wrong thing.

The exported session states which audio it points at. If it names a stand-in rather than the
recording the annotations were made against, the audio model's name says so, so a reviewer
does not mistake a misaligned stand-in for a wrong annotation.

---

## Relation to existing tools

[Label Studio](https://labelstud.io/) and [audino](https://github.com/midas-research/audino)
are general-purpose annotation platforms: their primary job is producing labels, with a
browser UI for annotators to work in. This project does not produce annotations and has no
annotation interface. It takes annotation files produced elsewhere — in Tony, Sonic
Visualiser, or a script — and provides what the production step leaves out: validation,
comparison between independent annotations of the same audio, and review decisions kept
with their evidence. A project that annotates in Label Studio could still use this one to
check and compare the results.

---

## Known limitations

- **Adjudication is modelled but not implemented.** The `Adjudication` model exists, but
  there is no workflow, API flow or UI for it.
- **No role-based permissions.** Any signed-in user can upload annotations, record reviews,
  add corrections and acknowledge findings; there is no distinction between annotator,
  reviewer and administrator.
- **`link_recordings` assumes one directory per item.** A corpus whose recordings share a
  single directory is not supported.
- **Frame-level f0 is not imported.** Unlike note annotations, these are instantaneous pitch
  estimates rather than intervals, and they need a separate layer type and comparison
  method. The importer reports these files as present and unimported.
- **Not deployed.** This runs under `docker compose` with Django's development server; there
  is no production server, static-file serving, TLS, or backup configuration.

---

## Configuration

Copy `.env.example` to `.env` and edit. Everything is read from the environment.

| Variable | Default | Purpose |
|---|---|---|
| `DATASET_ROOT` | `./demo-data` | Host directory mounted read-only at `/data`. A corpus's `root_path` is a path beneath it |
| `RECORDING_ROOT` | `./recordings` | Host directory mounted read-only at `/recordings`, for a corpus whose annotations were made against audio not shipped beside them |
| `DJANGO_SECRET_KEY` | a development placeholder, accepted only while `DJANGO_DEBUG` is on | **Change this for any non-local installation.** With `DJANGO_DEBUG` off the app refuses to start without its own key. It signs sessions and password-reset tokens |
| `DJANGO_DEBUG` | `0` | Off unless set to `1`; with it on, error responses expose tracebacks and settings. `docker-compose.yml` defaults it to `1` for local development, and `.env` overrides that |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated hostnames this installation answers to. Avoid `*` |
| `POSTGRES_DB` / `_USER` / `_PASSWORD` / `_HOST` / `_PORT` | `annotations` / `annotations` / `annotations` / `db` / `5432` | Database connection |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Origins allowed to call the API with credentials. Also used for `CSRF_TRUSTED_ORIGINS` |
| `LOG_LEVEL` | `INFO` | Root logger level |
| `TZ` | `UTC` | Server time zone. Timestamps are stored in UTC regardless |

The dataset volume is mounted **read-only**: the importer reads audio headers and CSVs, and
never writes back into the source tree.

---

## License and data attribution

This software is released under the **MIT License** — see [`LICENSE`](../LICENSE).
Copyright © 2026 Yu-Chia Kuo.

The demo data under `demo-data/vocadito/` is **not** covered by that licence. It consists of
unmodified excerpts from the vocadito dataset, used under **CC BY 4.0**. Dataset authors,
the Zenodo record, the licence and the citation are in
[`demo-data/vocadito/ATTRIBUTION.md`](../demo-data/vocadito/ATTRIBUTION.md). If you
redistribute this repository, that attribution must travel with the data.
