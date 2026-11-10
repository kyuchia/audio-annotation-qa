<script>
/*
  Stream colours live in module scope so that the timeline and the layer list below it draw
  the same colour for the same stream key. The colour is hashed from the key itself: keyed
  on a fixed list of names instead, every corpus outside that list would draw all of its
  streams in one colour — which is exactly when telling two of them apart matters.
*/
const STREAM_COLOURS = ['var(--stream-1)', 'var(--stream-2)', 'var(--stream-3)', 'var(--stream-4)']

export function streamColour(key) {
  const k = (key || '').toLowerCase()
  if (!k) return 'var(--accent)'
  let h = 0
  for (let i = 0; i < k.length; i += 1) h = (h * 31 + k.charCodeAt(i)) % 9973
  return STREAM_COLOURS[h % STREAM_COLOURS.length]
}
</script>

<script setup>
/*
  The annotation timeline: every stream of one item stacked in a single card on one shared
  time axis, so two annotations of the same audio are compared by looking down a column.

  What it draws, matching what the annotation_coverage validator reports:
    · each stream's segments, as bars on that stream's own track
    · a stretch at the end that no stream annotated, as a lighter band across every track
    · annotation running past the reference length, as a red stretch on that track

  The parent supplies the reference duration, which mirrors Item.coverage_duration() on the
  backend: where the file on disk stands in for the recording the annotations were made
  against, that recording's length is the one coverage means.

  Judging an individual segment stays on the backend, where a finding carries a code, a
  threshold and a reason that a reader can check.
*/
import { computed } from 'vue'

const props = defineProps({
  // [{ key, label, unit, segments: [{ start_sec, end_sec, payload }] }]
  streams: { type: Array, default: () => [] },
  durationSec: { type: Number, default: 0 },
  referenceSource: { type: String, default: '' },
  trackHeight: { type: Number, default: 26 },
  // An optional heading inside the card: what the timeline is of, and the audio it is
  // measured against.
  title: { type: String, default: '' },
  spec: { type: String, default: '' },
  // 'coverage' reports how much of the reference each stream covers and what is missing;
  // 'counts' names the streams and how many segments each holds.
  legend: { type: String, default: 'coverage' },
})

const W = 1000

const lastEnd = computed(() => {
  let end = 0
  for (const s of props.streams) {
    for (const seg of s.segments || []) end = Math.max(end, seg.end_sec)
  }
  return end
})

/* The axis covers the reference length, and any annotation that runs past it. */
const span = computed(() => Math.max(props.durationSec, lastEnd.value, 1))
const scale = computed(() => W / span.value)

/*
  Ticks every five seconds for an excerpt. A longer recording takes a coarser step, so the
  axis carries half a dozen labels whatever its length.
*/
const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
const tickStep = computed(() => STEPS.find((s) => span.value / s <= 6) ?? 900)
const ticks = computed(() => {
  const out = []
  for (let t = 0; t <= span.value + 0.001; t += tickStep.value) out.push(Math.round(t))
  return out
})

function pct(sec) {
  return `${(sec / span.value) * 100}%`
}

const tail = computed(() => Math.max(0, props.durationSec - lastEnd.value))

const tracks = computed(() =>
  props.streams.map((s) => {
    const segments = s.segments || []
    const covered = segments.reduce((a, g) => a + (g.end_sec - g.start_sec), 0)
    const end = segments.reduce((a, g) => Math.max(a, g.end_sec), 0)
    return {
      key: s.key,
      label: s.label || s.key,
      unit: s.unit || 'segments',
      colour: streamColour(s.key),
      count: segments.length,
      coverPct: props.durationSec ? Math.round((covered / props.durationSec) * 100) : 0,
      overshoot: Math.max(0, end - props.durationSec),
      bars: segments.map((g, i) => ({
        x: g.start_sec * scale.value,
        w: Math.max(0.8, (g.end_sec - g.start_sec) * scale.value),
        title:
          `#${g.index ?? i} ${g.start_sec.toFixed(2)}–${g.end_sec.toFixed(2)} s` +
          (g.payload?.hz ? ` · ${g.payload.hz.toFixed(1)} Hz` : ''),
      })),
    }
  }),
)

const overshooting = computed(() => tracks.value.filter((t) => t.overshoot > 0))
</script>

<template>
  <div class="timeline">
    <div v-if="title" class="card-head">
      <span class="card-title">{{ title }}</span>
      <span class="card-spec">{{ spec }}</span>
    </div>

    <div class="grid">
      <div class="axis">
        <span v-for="t in ticks" :key="t" class="tick" :style="{ left: pct(t) }">{{ t }}</span>
      </div>

      <!--
        One band for the whole card, drawn behind the tracks: a tail that no stream reached
        is a fact about the item, so it reads as one shape across all of them.
      -->
      <div class="bands" :style="{ gridRow: `2 / span ${Math.max(tracks.length, 1)}` }">
        <div
          v-if="tail > 0.5"
          class="tail"
          :style="{ left: pct(durationSec - tail), width: pct(tail) }"
        />
      </div>

      <template v-for="(t, i) in tracks" :key="t.key">
        <div class="name" :style="{ gridRow: i + 2 }">{{ t.label }}</div>
        <div class="track" :style="{ gridRow: i + 2 }">
          <svg
            :viewBox="`0 0 ${W} ${trackHeight}`"
            preserveAspectRatio="none"
            :style="{ height: `${trackHeight}px` }"
            role="img"
            :aria-label="`${t.label}: ${t.count} ${t.unit}, ${t.coverPct}% of ${durationSec.toFixed(1)} seconds covered`"
          >
            <line
              v-for="tk in ticks"
              :key="tk"
              :x1="tk * scale"
              y1="0"
              :x2="tk * scale"
              :y2="trackHeight"
              stroke="#2f2f2f"
              stroke-width="1"
              vector-effect="non-scaling-stroke"
            />
            <rect
              v-if="t.overshoot > 0"
              :x="durationSec * scale"
              y="0"
              :width="t.overshoot * scale"
              :height="trackHeight"
              fill="var(--error-bg)"
            />
            <rect
              v-for="(b, j) in t.bars"
              :key="j"
              :x="b.x"
              y="3"
              :width="b.w"
              :height="trackHeight - 6"
              :fill="t.colour"
              fill-opacity="0.9"
            >
              <title>{{ b.title }}</title>
            </rect>
          </svg>
        </div>
      </template>
    </div>

    <div class="legend">
      <span v-for="t in tracks" :key="t.key" class="key">
        <i class="swatch" :style="{ background: t.colour }" />
        <span class="k-name">{{ t.label }}</span>
        <span class="tnum">
          · {{ t.count }} {{ t.unit }}<template v-if="legend === 'coverage'">
            · {{ t.coverPct }}% covered</template
          >
        </span>
      </span>
      <span v-if="legend === 'coverage' && tail > 0.5" class="key">
        <i class="swatch band" />
        <span class="tnum">{{ tail.toFixed(1) }} s unannotated at the end</span>
      </span>
      <span
        v-for="t in legend === 'coverage' ? overshooting : []"
        :key="`o-${t.key}`"
        class="pill error"
      >
        {{ t.label }} runs {{ t.overshoot.toFixed(1) }} s past the reference
      </span>
      <span v-if="legend === 'coverage'" class="ref tnum">
        reference {{ durationSec.toFixed(2) }} s<template v-if="referenceSource">
          ({{ referenceSource }})</template
        >
      </span>
    </div>
  </div>
</template>

<style scoped>
/*
  The one dark surface on the page. The timeline is the thing a reader looks at first, so it
  gets the contrast; everything around it stays paper.
*/
.timeline {
  background: #141414;
  border-radius: var(--r-card);
  padding: var(--s5);
  color: #f0efec;
  /* The tracks read their colour from these slots; brightened here to lift off the dark
     ground while keeping each stream recognisably itself. */
  --stream-1: var(--stream-a1-bright);
  --stream-2: var(--stream-a2-bright);
  --error-bg: #4a2622;
  --error-text: #e8a396;
  --warning-bg: #3f3418;
  --warning-text: #e8c98a;
}

/* In dark the card and the page are nearly the same black, so it takes an edge to stay a
   card. In light the contrast does that on its own. */
:root[data-theme='dark'] .timeline { border: 1px solid var(--border-strong); }

.card-head {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--s2) var(--s4);
  margin-bottom: var(--s4);
  font-size: var(--fs-small);
}
.card-title { font-family: var(--mono); color: #f0efec; }
.card-spec { font-family: var(--mono); color: #8e8b84; font-variant-numeric: tabular-nums; }

.grid {
  display: grid;
  grid-template-columns: auto 1fr;
  column-gap: var(--s4);
  row-gap: var(--s2);
  align-items: center;
}

.axis {
  grid-column: 2;
  grid-row: 1;
  position: relative;
  height: 1.1rem;
}
.tick {
  position: absolute;
  top: 0;
  transform: translateX(-50%);
  font-family: var(--mono);
  font-size: var(--fs-index);
  color: #8e8b84;
  font-variant-numeric: tabular-nums;
}
.tick:first-child { transform: none; }

.name {
  grid-column: 1;
  font-family: var(--mono);
  font-size: var(--fs-small);
  color: #b9b6af;
  white-space: nowrap;
}

/* Tracks sit above the band, which is the only reason either needs a layer. */
.track { grid-column: 2; position: relative; z-index: 1; }
.track svg {
  width: 100%;
  display: block;
  border: 1px solid #343434;
  border-radius: var(--r-sm);
}

.bands { grid-column: 2; position: relative; z-index: 0; align-self: stretch; pointer-events: none; }
.tail {
  position: absolute;
  top: 0;
  bottom: 0;
  background: rgba(255, 255, 255, 0.06);
  border-radius: 3px;
}

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s2) var(--s5);
  align-items: center;
  margin-top: var(--s4);
  padding-top: var(--s3);
  border-top: 1px solid #2c2c2c;
  font-size: var(--fs-small);
  color: #b9b6af;
}
.key { display: inline-flex; align-items: center; gap: var(--s2); white-space: nowrap; }
.k-name { font-family: var(--mono); color: #f0efec; }
.swatch { width: 9px; height: 9px; border-radius: 2px; flex: none; }
.swatch.band { background: rgba(255, 255, 255, 0.16); }
.ref { color: #8e8b84; }

@media (max-width: 720px) {
  .timeline { padding: var(--s4); }
  .grid { column-gap: var(--s3); }
  .legend { gap: var(--s2) var(--s4); }
}
</style>
