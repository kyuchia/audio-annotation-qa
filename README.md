# Audio Annotation QA

**Web-based validation, comparison, and review tracking for research audio annotations.**

---

## What it is

A Django + DRF + Vue application for the stage *after* annotation: you already have
annotation files — note onsets, pitches, event labels — produced in Tony, Sonic Visualiser,
a script, or by hand, and you need to know whether they are sound, where two annotators
disagree, and what anyone has decided about them.

### The problem it addresses

A folder of annotation CSVs is a reasonable way to produce annotations and a poor way to
keep them. Specifically, a CSV on its own has:

- **No validation.** Nothing notices that a file's annotation stops ten seconds before the
  audio ends, that two segments overlap, that a sample rate was assumed rather than read,
  or that an onset column has been read as an offset column. Each of these imports silently
  and looks fine.
- **No history.** When a file is corrected, the previous state is gone. "Somebody changed
  it and nobody knows who" is the normal end state of a shared annotation folder.
- **No record of review.** Decisions about annotation quality end up as extra columns in a
  metadata spreadsheet — no author, no timestamp, no evidence — so a decision cannot be
  re-examined later, because nothing says what it was based on.
- **No way to express disagreement.** Two annotators working independently produce two
  files. A filesystem can only place one of them "after" the other; it has no way to say
  they are parallel and equally valid.

This project stores annotations as structured segments with their provenance, runs
validators that report rather than silently fix, compares two annotations of the same audio
without treating either as the reference, and keeps review decisions as first-class records
with the evidence behind them.

---

## Demo data

The demo corpus is a five-excerpt subset of **vocadito**: short solo singing recordings,
mono, 44.1 kHz, 9–39 seconds each. Every excerpt carries **two note annotations, A1 and A2,
made independently by two annotators**. See
[`demo-data/vocadito/ATTRIBUTION.md`](demo-data/vocadito/ATTRIBUTION.md) for the dataset's
authors, licence and citation.

Two independent annotations of the same singing is exactly the case the comparison exists
for. Neither is the correct one. Where they differ, the difference is a fact about the
material — how a phrase divides into notes, where a slide becomes a new pitch — and the
system's job is to show it precisely, not to adjudicate it.

### What the comparison shows

Five excerpts, chosen so that each shows a different *kind* of disagreement. Run with the
default tolerances (onset matching, 0.1 s onset tolerance, 50 cents pitch tolerance):

| Excerpt | A1 / A2 notes | Matched | Only A1 | Only A2 | Pitch | Timing | Both | What it shows |
|---|---|---|---|---|---|---|---|---|
| `vocadito_8`  | 40 / 37 | 37 | 3 | 0 | 1 | 1 | 1 | Near-total agreement — the baseline case |
| `vocadito_16` | 47 / 36 | 36 | 11 | 0 | 0 | 0 | 9 | Pitch: every A2 note matched, yet 9 pairs disagree by 137–219 cents |
| `vocadito_17` | 54 / 47 | 33 | 21 | 14 | 0 | 3 | 1 | Segmentation in both directions, with pitch largely agreed |
| `vocadito_21` | 97 / 62 | 57 | 40 | 5 | 0 | 3 | 12 | A1 subdivides sustained notes A2 keeps whole |
| `vocadito_34` | 89 / 95 | 75 | 14 | 20 | 5 | 8 | 8 | A2 is the finer-grained annotator here, plus the most pitch disagreement |

The median absolute onset difference is 0.000 s in all five: the two annotators agree
closely on *where* events begin, and disagree about how a phrase divides into notes and
what pitch to assign a moving one. That is the useful signal, and it is only visible
because pitch and timing are reported separately rather than collapsed into one agreement
score.

### A validator finding worth looking at

Running the validators over the demo corpus produces **12 `info` findings and no warnings
or errors**. Eleven of the twelve are routine: a coverage summary per layer, plus one
metadata summary for the corpus. The twelfth is the interesting one, and it is a good
illustration of why magnitude belongs in a finding.

`segments_monotonic` found that two segments in `vocadito_21` stream `A1` overlap in time —
something a layer holding a single voice normally should not do. The overlap is **2.2676 ×
10⁻⁵ s, or 23 microseconds**. The two segments, as stored:

| | onset (s) | offset (s) | duration (s) | pitch (Hz) |
|---|---|---|---|---|
| index 0 | 0.304761905 | 0.336712018 | 0.031950113 | 135.019 |
| index 1 | 0.336689342 | 0.412154195 | 0.075464853 | 151.248 |

Overlap = `0.336712018 − 0.336689342` = 2.2676 × 10⁻⁵ s.

That size is the whole story. The source file states each note as `(onset, pitch_hz,
duration)` — the first two rows of
`demo-data/vocadito/Annotations/Notes/vocadito_21_notesA1.csv` are:

```
0.304761905,135.019,0.031950113
0.336689342,151.248,0.075464853
```

A segment's offset is therefore not a number the file contains; it is `onset + duration`,
a sum of two values that were each rounded to nine decimal places on their own. Note 0's
sum lands 23 µs past note 1's onset. **Nothing is wrong with the annotation**: no annotator
marked two notes as sounding at once, and no tool mis-exported anything. The arithmetic of
the file format produced it.

This is exactly why overlaps carry a tolerance. A real conflict — an annotator genuinely
placing two overlapping notes — and a rounding artifact are the same shape in the data, and
only the magnitude separates them. `segments_monotonic` takes
`segment_overlap_tolerance_sec`, which `note_pitch` sets to 1 ms: far above the rounding
these files carry, far below any overlap a person could place by hand. So this finding is
reported as `segments_overlap_within_tolerance`, at `info`:

```
1 pair of segments overlap by at most 2.2676e-05s, which is within this layer type's
rounding tolerance of 0.001s. An overlap this small is what independent rounding of an
onset and a duration in the source produces, not two events annotated as sounding at
once.
```

An overlap above the tolerance stays a `warning` under the `segments_overlap` code. The
tolerance grades a finding; it never suppresses one. Reporting the small case as `info`
rather than dropping it keeps "there is an overlap, and it is the size rounding would
produce" distinguishable from silence — a tolerance that quietly swallowed findings is how
a real conflict would eventually go unnoticed. The default tolerance is `0`, so a layer
type that has not reasoned about its own source format inherits no leniency.

---

## Quickstart

Requires Docker. From the repository root:

```bash
cp .env.example .env
```

```bash
docker compose up -d
```

```bash
docker compose exec django python manage.py migrate
```

```bash
docker compose exec django python manage.py createsuperuser
```

Import the demo corpus and validate it in one step:

```bash
docker compose exec django python manage.py import_vocadito --slug vocadito-demo --name "vocadito (demo subset)" --root /data/vocadito --validate
```

Then open:

- **UI** — http://localhost:5173
- **Admin** — http://localhost:8000/admin/
- **API** — http://localhost:8000/api/

Reads are open; any write (uploading an annotation, creating a comparison, recording a
review) requires the superuser you just created.

To run the validators again, or over one item:

```bash
docker compose exec django python manage.py run_validators --corpus vocadito-demo --show
```

To use the full dataset instead of the five-excerpt subset, download vocadito, point
`DATASET_ROOT` in `.env` at its parent directory, and re-run `import_vocadito` against it.
The subset has the same layout as the full release, so the same command imports either.

Tests:

```bash
docker compose exec django python manage.py test annotations
```

---

## Architecture

### Models

```
Corpus                     a dataset root, its metadata CSV, its validator settings
└─ Item                    one piece of audio; specs always read from the file header
   └─ AnnotationLayer      one annotation of that audio: type, stream, version, author
      └─ Segment           start_sec, end_sec, payload JSON
```

Three design rules are fixed at the model layer:

**Times are seconds, never frames.** Frame indices exist only in the Sonic Visualiser
exporter, computed with that item's own sample rate. With no sample rate read, the export
refuses rather than assuming one.

**Identity fields are independent.** An item's `song_id`, `display_name` and `disk_path` are
stored separately, none derived from another. A directory name may have lost characters on
its way to disk, and the tool that *deleted* a non-ASCII character and the tool that
*transliterated* it do not agree — so neither name can be recomputed from the other.

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

---

## Validators

| Key | Scope | Reports |
|---|---|---|
| `disk_metadata_consistency` | corpus | Items with no metadata row, provisional keys, inferred metadata matches, and how many items carry metadata at all |
| `media_probed` | item | The audio header has not been read, so anything needing a sample rate will fail |
| `metadata_duration_agreement` | item | The metadata's claimed duration disagrees with the header. The header always wins; this notices that the metadata has gone stale |
| `recording_linked` | item | Whether an exported session points at the recording the annotations were made against or at a stand-in, plus duration, sample-rate and channel mismatches against the linked file |
| `segments_monotonic` | layer | Segments whose offset precedes their onset, and overlapping pairs. An overlap above `segment_overlap_tolerance_sec` is a warning; one at or below it is reported as `info` with its magnitude, since a real conflict and a rounding artifact differ only in size. Tolerance defaults to `0` |
| `annotation_coverage` | layer | How much of the reference length is annotated, an unannotated stretch at the end, and annotation running past the audio |
| `annotation_internal_gaps` | layer | Unannotated stretches mid-recording. Always `info`, and the item's notes travel with the finding: a silence in the material and a missed annotation are identical in the data |
| `clip_single_segment` | layer | A clip-level annotation holding more than one segment |

Severities are `error`, `warning` and `info`. A finding is never deleted — it is
*acknowledged*, with a note, so that "a human looked at this and judged it acceptable"
stays on the record.

---

## Comparison semantics

**Disagreement is not error.** When two independent annotations differ, neither is the
reference for the other, and the comparator reports the difference without ruling on it.
Problems with an annotation in itself are the validators' business, reported separately.
The UI words the categories accordingly: "annotated only by A", not "missing from B".

Matched pairs fall into categories that are kept apart rather than summed:

| Category | Meaning |
|---|---|
| `pitch_only` | Same interval, pitches differ beyond the cents tolerance |
| `timing_only` | Same pitch, intervals differ beyond the onset/overlap tolerance |
| `pitch_and_timing` | Both differ |
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

---

## Sonic Visualiser session export

Two products, both export-only — Sonic Visualiser is a viewer here, never a source, so
there is no parser and the direction of truth stays unambiguous.

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
recording the annotations were made against, the audio model's name says so, and the frame
count describes whichever file the URI actually names. A reviewer who is not told this hears
onsets that do not line up and reasonably concludes the annotation is wrong.

---

## Relation to existing tools

[Label Studio](https://labelstud.io/) and [audino](https://github.com/midas-research/audino)
are general-purpose annotation platforms: their primary job is producing labels, with a
browser UI for annotators to work in. This project does not produce annotations and has no
annotation interface. It takes annotation files produced elsewhere — in Tony, Sonic
Visualiser, or a script — and provides what the production step leaves out: validation,
comparison between independent annotations of the same audio, and review decisions kept
with their evidence. The two kinds of tool sit at different stages, and a project that
annotates in Label Studio could still use this one to check and compare the results.

---

## Known limitations

- **Adjudication is modelled but not implemented.** The `Adjudication` model exists —
  several candidate layers, a chosen one, the reasoning — but there is no workflow, no API
  flow and no UI for it, and nothing creates one.
- **No role-based permissions.** Reads are open and *any* authenticated user can write:
  upload annotations, record reviews, add corrections, acknowledge findings. There is no
  distinction between an annotator, a reviewer and an administrator.
- **`link_recordings` assumes one directory per item.** It looks for
  `<root>/<last component of disk_path>/<filename>`, so it does not fit a corpus whose audio
  sits in a single shared directory with items distinguished by filename. Such a corpus is
  usually one whose own audio already *is* the recording, in which case there is nothing to
  link — switch `recording_linked` off via `Corpus.skip_validators`. A flat recording tree
  that genuinely needs linking is not supported; it wants a per-corpus rule for turning an
  item into a candidate path.
- **Frame-level f0 is not imported.** The demo dataset ships a dense pitch contour per
  excerpt — one sample every 256/44100 s, 1,500–6,700 rows each — and it is deliberately
  left on disk. It is a different kind of annotation from what `Segment` holds: those rows
  are instants, not intervals, so stored as segments every one would be zero-length and
  `annotation_coverage` would report 0% on a densely annotated file. "A note was sung here
  at this pitch" and "the pitch estimate at this instant was this" are also different
  claims — around 40% of the frames are marked unvoiced, and a contour crosses between notes
  with no note boundary. Onset matching against a 5.8 ms grid would pair frames with
  whichever neighbour happened to be nearest and swamp the comparison. Frame-level pitch
  wants its own layer type, and choosing its representation is a modelling decision rather
  than an import detail. The importer reports these files as present and unimported instead
  of skipping them silently.
- **Not deployed.** This runs under `docker compose` with Django's development server. There
  is no production configuration: no WSGI/ASGI server wired up, no static-file serving, no
  TLS, no backups.

---

## Configuration

Copy `.env.example` to `.env` and edit. Everything is read from the environment.

| Variable | Default | Purpose |
|---|---|---|
| `DATASET_ROOT` | `./demo-data` | Host directory mounted read-only at `/data` in the container. A corpus's `root_path` is a path beneath it |
| `RECORDING_ROOT` | `./recordings` | Host directory mounted read-only at `/recordings`. Only needed for a corpus whose annotations were made against audio not shipped beside them |
| `DJANGO_SECRET_KEY` | a development placeholder | **Change this for any installation that is not a throwaway local one.** It signs sessions and password-reset tokens |
| `DJANGO_DEBUG` | `0` | Off unless explicitly set to `1`. With it on, any error response carries a traceback, your settings and local variables — the default is what runs wherever nobody set the variable, which is exactly where that does the most damage. `docker-compose.yml` sets it to `1` for local development |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated `Host` headers this installation answers to; anything else gets a 400. This is what stops an attacker-supplied `Host` being reflected into absolute URLs and cache keys, so `*` disables a real protection rather than relaxing a formality. A deployment reachable under any other name must list its own hostnames; a leading dot matches subdomains (`.example.org`) |
| `POSTGRES_DB` / `_USER` / `_PASSWORD` / `_HOST` / `_PORT` | `annotations` / `annotations` / `annotations` / `db` / `5432` | Database connection |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Origins allowed to call the API with credentials. Also used for `CSRF_TRUSTED_ORIGINS` |
| `LOG_LEVEL` | `INFO` | Root logger level |
| `TZ` | `Asia/Taipei` | Server time zone |

The dataset volume is mounted **read-only**: the importer reads audio headers and CSVs, and
never writes back into the source tree.

---

## License and data attribution

This software is released under the **MIT License** — see [`LICENSE`](LICENSE).
Copyright © 2026 Yu-Chia Kuo.

The demo data under `demo-data/vocadito/` is **not** covered by that licence. It consists of
unmodified excerpts from the vocadito dataset, used under **CC BY 4.0**. Dataset authors,
the Zenodo record, the licence and the citation are in
[`demo-data/vocadito/ATTRIBUTION.md`](demo-data/vocadito/ATTRIBUTION.md). If you
redistribute this repository, that attribution must travel with the data.
