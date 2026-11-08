<script>
/*
  Module scope on purpose. The body of <script setup> is the setup function and runs
  once per component instance, so a counter declared there would reset to zero for
  every rail and hand them all the same id — the exact collision this is here to avoid.
*/
let railSeq = 0

export function nextGapPatternId() {
  railSeq += 1
  return `coverage-gap-${railSeq}`
}
</script>

<script setup>
/*
  Coverage rail. The full width of the rail is the reference duration for coverage,
  and the bars drawn on it are this layer's segments.

  Two things are drawn loudly on purpose, matching what the annotation_coverage
  validator reports:
    · a stretch at the end with no annotation      → hatched area
    · annotation running past the reference length → the right end turns red

  The reference duration is supplied by the parent rather than taken from the audio
  header: when the file on disk is a stand-in, the header measures the stand-in and
  not the recording the annotations were made against. See Item.coverage_duration()
  on the backend, which this mirrors.

  Nothing here judges an individual segment. A rail that outlines one bar as suspect is
  making a claim the viewer cannot check, and it would fall out of step with the
  validators the moment their set changes — so anomaly reporting stays on the backend,
  where a finding carries a code, a threshold and a reason.
*/
import { computed } from 'vue'

const props = defineProps({
  segments: { type: Array, default: () => [] }, // [{start_sec, end_sec, payload}]
  durationSec: { type: Number, default: 0 },
  referenceSource: { type: String, default: '' },
  streamKey: { type: String, default: '' },
  height: { type: Number, default: 26 },
})

const W = 1000

/*
  Every rail defines its own hatch pattern, and an SVG pattern can only be referenced
  by id. A literal id would mean several elements sharing one id as soon as a page
  shows more than one rail, so each instance gets its own.
*/
const gapId = nextGapPatternId()

/*
  A stable colour per stream key, derived from the key itself. Keyed on a fixed list of
  names instead, every corpus outside that list draws all of its streams in one colour —
  which is exactly when telling two of them apart matters. Hashing the key means A1 and A2
  differ, and the same key keeps the same colour across every rail on the page.
*/
const STREAM_COLOURS = ['var(--stream-1)', 'var(--stream-2)', 'var(--stream-3)', 'var(--stream-4)']

const colour = computed(() => {
  const key = (props.streamKey || '').toLowerCase()
  if (!key) return 'var(--accent)'
  let h = 0
  for (let i = 0; i < key.length; i += 1) h = (h * 31 + key.charCodeAt(i)) % 9973
  return STREAM_COLOURS[h % STREAM_COLOURS.length]
})

const durations = computed(() => props.segments.map((s) => s.end_sec - s.start_sec))

const lastEnd = computed(() =>
  props.segments.length ? Math.max(...props.segments.map((s) => s.end_sec)) : 0,
)

const tailGap = computed(() => Math.max(0, props.durationSec - lastEnd.value))
const overshoot = computed(() => Math.max(0, lastEnd.value - props.durationSec))

const scale = computed(() => W / Math.max(props.durationSec, lastEnd.value, 1))

const bars = computed(() =>
  props.segments.map((s, i) => {
    const dur = s.end_sec - s.start_sec
    return {
      x: s.start_sec * scale.value,
      w: Math.max(0.8, dur * scale.value),
      title: `#${s.index ?? i} ${s.start_sec.toFixed(2)}–${s.end_sec.toFixed(2)}s` +
        (s.payload?.hz ? ` · ${s.payload.hz.toFixed(1)} Hz` : ''),
    }
  }),
)

const covered = computed(() => durations.value.reduce((a, b) => a + b, 0))
const coverPct = computed(() =>
  props.durationSec ? Math.round((covered.value / props.durationSec) * 100) : 0,
)
</script>

<template>
  <div class="rail">
    <svg
      :viewBox="`0 0 ${W} ${height}`"
      preserveAspectRatio="none"
      :style="{ height: `${height}px` }"
      role="img"
      :aria-label="`Annotation coverage ${coverPct}%, reference length ${durationSec.toFixed(1)} seconds`"
    >
      <defs>
        <pattern :id="gapId" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="8" stroke="var(--rule-strong)" stroke-width="2" />
        </pattern>
      </defs>

      <rect x="0" y="0" :width="W" :height="height" fill="var(--panel-sunk)" />

      <rect
        v-if="tailGap > 0.5"
        :x="lastEnd * scale"
        y="0"
        :width="tailGap * scale"
        :height="height"
        :fill="`url(#${gapId})`"
      />

      <rect
        v-if="overshoot > 0"
        :x="durationSec * scale"
        y="0"
        :width="overshoot * scale"
        :height="height"
        fill="var(--error-soft)"
      />

      <rect
        v-for="(b, i) in bars"
        :key="i"
        :x="b.x"
        y="4"
        :width="b.w"
        :height="height - 8"
        :fill="colour"
        fill-opacity="0.85"
        vector-effect="non-scaling-stroke"
      >
        <title>{{ b.title }}</title>
      </rect>

      <line
        :x1="durationSec * scale"
        y1="0"
        :x2="durationSec * scale"
        :y2="height"
        stroke="var(--ink-muted)"
        stroke-width="1"
        vector-effect="non-scaling-stroke"
      />
    </svg>

    <div class="legend num">
      <span>{{ segments.length }} segments · {{ coverPct }}% covered</span>
      <span v-if="tailGap > 0.5" class="tag warning">{{ tailGap.toFixed(1) }}s missing at end</span>
      <span v-if="overshoot > 0" class="tag error">{{ overshoot.toFixed(1) }}s past reference</span>
      <span class="faint">
        reference {{ durationSec.toFixed(2) }}s<template v-if="referenceSource">
          ({{ referenceSource }})</template
        >
      </span>
    </div>
  </div>
</template>

<style scoped>
.rail svg {
  width: 100%;
  display: block;
  border: 1px solid var(--rule);
  border-radius: 2px;
}
.legend {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--ink-muted);
  margin-top: 4px;
}
</style>
