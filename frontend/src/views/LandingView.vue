<script setup>
/*
  The landing page. Everything on it is drawn from the demo corpus through the same read
  endpoints the rest of the app uses, so the numbers a visitor reads here are the numbers
  the demo will show them a click later.

  The comparison figures come from a stored comparison. Running one is a write, and a
  visitor who has yet to click anything should not be writing to the database, so the page
  reads what is already there and leaves the section's numbers out when there is none.
*/
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import CoverageRail, { streamColour } from '../components/CoverageRail.vue'

const SONG_ID = 'vocadito_21'
const REPO = 'https://github.com/kyuchia/audio-annotation-qa'
const DATASET = 'https://doi.org/10.5281/zenodo.5557945'

const item = ref(null)
const streams = ref([])
const comparison = ref(null)
const zoom = ref(null)
const error = ref('')

const CAPABILITIES = [
  ['Import', 'Note and event files become segments with an author, a source file and a version.'],
  ['Validate', 'Checks for ordering, overlaps, gaps and coverage, configured per annotation type.'],
  ['Compare', "Pairs two annotators' notes and reports timing and pitch separately."],
  ['Review', 'Corrections become new versions with a stated reason; the original import stays.'],
  ['Export', 'Sonic Visualiser sessions with audio and every layer, plus a REST API for scripts.'],
]

const WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight']

function unitFor(layerType) {
  return layerType === 'note_pitch' ? 'notes' : 'segments'
}

/*
  Whether a stored comparison was run to the parameters the backend chooses when nobody
  picks any. A reader comparing to their own tolerances leaves a comparison behind too, and
  the figures quoted on this page are the default ones.
*/
function isDefaultRun(stored, defaults) {
  if (!stored || !defaults) return false
  const keys = Object.keys(defaults)
  if (Object.keys(stored).length !== keys.length) return false
  return keys.every((k) => stored[k] === defaults[k])
}

function audioSpec(it) {
  if (!it?.sample_rate) return ''
  const khz = `${Number((it.sample_rate / 1000).toFixed(3))} kHz`
  const channels = it.channels === 1 ? 'mono' : it.channels === 2 ? 'stereo' : `${it.channels} ch`
  return `${khz} · ${channels} · ${it.duration_sec.toFixed(2)} s`
}

/*
  The window for the zoom figure: the single annotation in the second stream that the most
  onsets of the first stream fall inside. Where two notes hold the same number, the one
  whose contained notes also end inside it wins: that one reads as a division of the note,
  where the other leaves a bar running off the edge of the window.
*/
function pickZoom(a1, a2) {
  let best = null
  for (const note of a2) {
    const inside = a1.filter((x) => x.start_sec >= note.start_sec && x.start_sec < note.end_sec)
    if (inside.length < 3) continue
    const whole = inside.filter((x) => x.end_sec <= note.end_sec + 1e-6).length
    if (!best || inside.length > best.count || (inside.length === best.count && whole > best.whole)) {
      best = {
        note,
        inside,
        count: inside.length,
        whole,
        splits: inside.filter((x) => x.start_sec > note.start_sec).map((x) => x.start_sec),
      }
    }
  }
  return best
}

onMounted(async () => {
  try {
    const list = await api.items({ q: SONG_ID, page_size: 5 })
    const match = (list.results ?? list).find((i) => i.song_id === SONG_ID)
    if (!match) {
      error.value = `${SONG_ID} is absent from this database.`
      return
    }
    const full = await api.item(match.id)
    item.value = full

    const lists = await Promise.all(full.layers.map((l) => api.segments(l.id)))
    streams.value = full.layers.map((l, i) => ({
      id: l.id,
      key: l.stream_key || l.layer_type,
      label: l.stream_key || l.layer_type,
      unit: unitFor(l.layer_type),
      segments: lists[i],
    }))

    const [page, defaults] = await Promise.all([
      api.comparisons({ page_size: 50 }),
      api.comparatorDefaults(),
    ])
    const byId = new Map(full.layers.map((l) => [l.id, l]))
    comparison.value =
      (page.results ?? page)
        .filter((c) => byId.has(c.layer_a) && byId.has(c.layer_b))
        .filter((c) => isDefaultRun(c.params, defaults[byId.get(c.layer_a).layer_type]))
        .sort((x, y) => new Date(y.created_at) - new Date(x.created_at))[0] || null

    if (streams.value.length > 1) {
      zoom.value = pickZoom(streams.value[0].segments, streams.value[1].segments)
    }
  } catch (e) {
    error.value = e.message
  }
})

const sideA = computed(() => streams.value.find((s) => s.id === comparison.value?.layer_a))
const sideB = computed(() => streams.value.find((s) => s.id === comparison.value?.layer_b))

const figures = computed(() => {
  const s = comparison.value?.summary
  if (!s) return null
  return [
    { value: s.matched, label: 'Matched pairs', colour: 'var(--text)' },
    {
      value: s.only_in_a,
      label: `Only in ${sideA.value?.label ?? 'A'}`,
      colour: streamColour(sideA.value?.key),
    },
    {
      value: s.only_in_b,
      label: `Only in ${sideB.value?.label ?? 'B'}`,
      colour: streamColour(sideB.value?.key),
    },
  ]
})

/*
  The figure's own coordinates. The window is the chosen note and nothing else, so the
  lower row is one bar across the full width and the upper row is the notes that divide it.

  Those notes meet exactly, end to end, which is the whole point of the figure and also the
  reason it needs drawing with care: butted against each other they paint as one unbroken
  bar, and the figure then shows two identical bars and no division at all. Each note is
  inset by half of GAP at both ends, so a boundary between two of them opens to GAP and the
  three notes stay where the data puts them.
*/
const W = 300
const H = 84
const GUTTER = 26
const GAP = 3
const IW = W - GUTTER - 4
const ROW_A = 14
const ROW_B = 50
const BAR = 20

const zoomView = computed(() => {
  const z = zoom.value
  if (!z) return null
  const from = z.note.start_sec
  const span = z.note.end_sec - from
  const x = (t) => Math.min(IW, Math.max(0, ((t - from) / span) * IW))
  const upper = streams.value[0]
  return {
    a1: upper.segments
      .filter((n) => n.end_sec > from + 1e-9 && n.start_sec < z.note.end_sec - 1e-9)
      .map((n) => {
        const left = x(n.start_sec)
        const width = x(n.end_sec) - left
        return { x: left + GAP / 2, w: Math.max(1.5, width - GAP) }
      }),
    a2: { x: 0, w: IW },
    splits: z.splits.map(x),
    labelA: upper.label,
    labelB: streams.value[1].label,
    colourA: streamColour(upper.key),
    colourB: streamColour(streams.value[1].key),
  }
})

const zoomCaption = computed(() => {
  const z = zoom.value
  if (!z) return ''
  const ways = WORDS[z.count] ?? String(z.count)
  return `${streams.value[1].label} note at ${z.note.start_sec.toFixed(2)}–${z.note.end_sec.toFixed(2)} s, split ${ways} ways by ${streams.value[0].label}`
})
</script>

<template>
  <div class="stack landing">
    <section class="section hero">
      <h1>Annotation QA</h1>
      <p class="lede">
        A web app for checking audio annotations. It imports note-level labels, runs
        validators over them, compares two annotators, and records every review with its
        reason.
      </p>
      <div class="row cta">
        <RouterLink class="btn primary" to="/items">Open the demo</RouterLink>
        <a class="plain-link" :href="REPO" target="_blank" rel="noopener">Source on GitHub</a>
      </div>
    </section>

    <p v-if="error" class="error-box">{{ error }}</p>

    <CoverageRail
      v-if="streams.length"
      :streams="streams"
      :duration-sec="item?.duration_sec || 0"
      :title="item?.song_id"
      :spec="audioSpec(item)"
      legend="counts"
    />

    <section class="section">
      <h2>An example from the demo</h2>
      <p class="body">
        Two people annotated the same 22 seconds of solo singing. The comparison pairs their
        notes by onset and checks pitch in cents.
      </p>

      <div v-if="figures" class="figures">
        <div v-for="f in figures" :key="f.label" class="figure">
          <div class="figure-value" :style="{ color: f.colour }">{{ f.value }}</div>
          <div class="figure-label">{{ f.label }}</div>
        </div>
      </div>

      <div class="split">
        <p class="body">
          Most of the 40 unmatched A1 notes tell a consistent story: 18 of them start inside
          a longer A2 note. A1 divides held notes into shorter ones where A2 marks a single
          note. The onsets that do match have a median difference of 0.000 s.
        </p>

        <figure v-if="zoomView" class="zoom">
          <svg :viewBox="`0 0 ${W} ${H}`" role="img" :aria-label="zoomCaption">
            <text x="0" :y="ROW_A + BAR / 2 + 5" class="zoom-key">{{ zoomView.labelA }}</text>
            <text x="0" :y="ROW_B + BAR / 2 + 5" class="zoom-key">{{ zoomView.labelB }}</text>
            <g :transform="`translate(${GUTTER}, 0)`">
              <rect
                v-for="(n, i) in zoomView.a1"
                :key="i"
                :x="n.x"
                :y="ROW_A"
                :width="n.w"
                :height="BAR"
                rx="2"
                :fill="zoomView.colourA"
              />
              <rect
                :x="zoomView.a2.x"
                :y="ROW_B"
                :width="zoomView.a2.w"
                :height="BAR"
                rx="2"
                :fill="zoomView.colourB"
              />
              <!-- Each division carried down through both rows: one note above, where the
                   upper annotator started another, and the single note below that runs
                   straight past it. -->
              <line
                v-for="(x, i) in zoomView.splits"
                :key="`s-${i}`"
                :x1="x"
                :y1="ROW_A - 4"
                :x2="x"
                :y2="ROW_B + BAR + 4"
                stroke="var(--text-muted)"
                stroke-width="1"
              />
            </g>
          </svg>
          <figcaption>{{ zoomCaption }}</figcaption>
        </figure>
      </div>
    </section>

    <section class="section">
      <h2>What it does</h2>
      <dl class="capabilities">
        <div v-for="[name, line] in CAPABILITIES" :key="name" class="capability">
          <dt>{{ name }}</dt>
          <dd>{{ line }}</dd>
        </div>
      </dl>
    </section>

    <footer class="section foot">
      Demo data:
      <a :href="DATASET" target="_blank" rel="noopener">vocadito, CC BY 4.0</a>
    </footer>
  </div>
</template>

<style scoped>
.landing { color: var(--text); }

.hero h1 { color: var(--text); }
.lede {
  font-size: 18px;
  line-height: 1.5;
  color: var(--text-muted);
  max-width: 540px;
  margin: var(--s4) 0 0;
}
.cta { margin-top: var(--s5); gap: var(--s4); }
.plain-link { color: var(--accent); font-size: var(--fs-body); text-decoration: none; }
.plain-link:hover { text-decoration: underline; text-decoration-color: currentColor; }

h2 { color: var(--text); }
.body {
  font-size: var(--fs-body);
  line-height: 1.5;
  color: var(--text-muted);
  max-width: 62ch;
  margin: 0;
}

.figures {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s5) var(--s8);
  margin: var(--s6) 0;
}
.figure-value {
  font-size: var(--fs-stat);
  font-weight: 600;
  line-height: 1.1;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}
.figure-label { font-size: var(--fs-small); color: var(--text-muted); margin-top: var(--s1); }

.split {
  display: flex;
  gap: var(--s6);
  align-items: flex-start;
  justify-content: space-between;
}
.zoom { margin: 0; flex: none; width: 300px; }
.zoom svg {
  width: 100%;
  display: block;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
}
.zoom-key {
  font-family: var(--mono);
  font-size: 13px;
  fill: var(--text-muted);
}
.zoom figcaption {
  font-size: var(--fs-micro);
  color: var(--text-muted);
  margin-top: var(--s2);
  line-height: 1.45;
}

.capabilities { margin: var(--s4) 0 0; }
.capability {
  display: grid;
  grid-template-columns: 9rem 1fr;
  gap: var(--s4);
  padding: var(--s4) 0;
  border-top: 1px solid var(--border);
}
.capability:first-child { border-top: none; padding-top: var(--s2); }
.capability dt { font-weight: 500; color: var(--text); font-size: var(--fs-body); }
.capability dd {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-body);
  line-height: 1.5;
  max-width: 62ch;
}

.foot { font-size: var(--fs-small); color: var(--text-muted); }
.foot a { color: var(--accent); }

@media (max-width: 760px) {
  .split { flex-direction: column; gap: var(--s5); }
  .zoom { width: 100%; max-width: 300px; }
  .figures { gap: var(--s5) var(--s6); }
  .capability { grid-template-columns: 1fr; gap: var(--s1); }
}
</style>
