<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import { session } from '../session'

const props = defineProps({
  layers: { type: Array, default: () => [] },
  itemId: { type: Number, required: true },
})

const a = ref(null)
const b = ref(null)
const onsetTol = ref(0.1)
const centsTol = ref(50)
// Matches NOTE_PITCH_DEFAULTS on the backend. A different default here would mean the
// first comparison a user runs is not the one the stored defaults describe.
const strategy = ref('onset')
const busy = ref(false)
const error = ref('')
const result = ref(null)
// When the result on screen came from the database rather than from this visit.
const storedAt = ref(null)

const comparable = computed(() => props.layers.filter((l) => l.segment_count > 0))

watch(
  comparable,
  (list) => {
    if (!a.value && list.length) a.value = list[0].id
    if (!b.value && list.length > 1) b.value = list[1].id
  },
  { immediate: true },
)

/*
  The import stores a comparison for every item, so the page can open on an answer instead
  of an empty form. The form stays, set to the pair and the tolerances that produced what
  is on screen, so pressing Compare re-runs the same question or a different one.
*/
async function loadStored() {
  try {
    const page = await api.comparisons({ item: props.itemId, page_size: 1 })
    const rows = page.results ?? page
    if (!rows.length) return
    const stored = rows[0]
    result.value = stored
    storedAt.value = stored.created_at
    if (comparable.value.some((l) => l.id === stored.layer_a)) a.value = stored.layer_a
    if (comparable.value.some((l) => l.id === stored.layer_b)) b.value = stored.layer_b
    const p = stored.params || {}
    if (p.onset_tolerance_sec != null) onsetTol.value = p.onset_tolerance_sec
    if (p.pitch_tolerance_cents != null) centsTol.value = p.pitch_tolerance_cents
    if (p.match_strategy) strategy.value = p.match_strategy
  } catch {
    // A stored comparison is a convenience. Failing to find one leaves the form, which is
    // the whole panel for anyone who wants a different answer anyway.
  }
}

onMounted(loadStored)

async function run() {
  busy.value = true
  error.value = ''
  result.value = null
  storedAt.value = null
  try {
    result.value = await api.compare({
      layer_a: a.value,
      layer_b: b.value,
      params: {
        onset_tolerance_sec: Number(onsetTol.value),
        pitch_tolerance_cents: Number(centsTol.value),
        match_strategy: strategy.value,
        min_iou: 0.5,
      },
    })
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

/*
  Each label states what the two annotations do differently and leaves it there. "Annotated
  only by A" is a fact about the pair; a verdict on either annotator is beyond what a
  comparison can tell.
*/
const kindLabel = {
  pitch_only: 'Pitch disagrees',
  timing_only: 'Timing disagrees',
  pitch_and_timing: 'Pitch and timing disagree',
  only_in_a: 'Annotated only by A',
  only_in_b: 'Annotated only by B',
}
const rows = computed(() => (result.value?.discrepancies || []).slice(0, 300))
function label(l) {
  return `${l.stream_key || l.layer_type} v${l.version}`
}

/* Which two annotations the result on screen is about, in the same words as the form. */
const shownPair = computed(() => {
  const r = result.value
  if (!r) return ''
  const la = props.layers.find((l) => l.id === r.layer_a)
  const lb = props.layers.find((l) => l.id === r.layer_b)
  return la && lb ? `${label(la)} against ${label(lb)}` : ''
})

const storedDate = computed(() =>
  storedAt.value
    ? new Date(storedAt.value).toLocaleDateString('en-GB', {
        day: 'numeric',
        month: 'short',
        year: 'numeric',
      })
    : '',
)
</script>

<template>
  <div class="panel">
    <h2>Annotation comparison</h2>
    <p class="note">
      Differences show where the two annotators disagree. Pitch is compared in cents.
    </p>

    <div v-if="session.authenticated" class="controls">
      <div>
        <label for="a">Annotation A</label>
        <select id="a" v-model="a">
          <option v-for="l in comparable" :key="l.id" :value="l.id">{{ label(l) }}</option>
        </select>
      </div>
      <div>
        <label for="b">Annotation B</label>
        <select id="b" v-model="b">
          <option v-for="l in comparable" :key="l.id" :value="l.id">{{ label(l) }}</option>
        </select>
      </div>
      <div>
        <label for="st">Matching strategy</label>
        <select id="st" v-model="strategy">
          <option value="iou">Interval overlap (IoU)</option>
          <option value="onset">Onset tolerance window</option>
        </select>
      </div>
      <div>
        <label for="ot">Onset tolerance (s)</label>
        <input id="ot" v-model="onsetTol" class="num" type="number" step="0.01" />
      </div>
      <div>
        <label for="ct">Pitch tolerance (cents)</label>
        <input id="ct" v-model="centsTol" class="num" type="number" step="5" />
      </div>
      <button class="primary" :disabled="!a || !b || a === b || busy" @click="run">
        {{ busy ? 'Comparing…' : 'Compare' }}
      </button>
    </div>

    <p v-else class="note quiet">
      Signing in opens the controls for this comparison: which two annotations to pair, how
      to pair them, and the tolerances to pair them at.
    </p>

    <div v-if="error" class="error-box">{{ error }}</div>

    <template v-if="result">
      <p v-if="storedAt" class="stored">
        Stored comparison: {{ shownPair }}, run on {{ storedDate }}.
      </p>
      <p v-else-if="shownPair" class="stored">{{ shownPair }}.</p>
      <div class="stats">
        <div class="stat">
          <div class="stat-index">01</div>
          <div class="stat-value">{{ result.summary.matched }}</div>
          <div class="stat-label">Matched</div>
        </div>
        <div class="stat">
          <div class="stat-index">02</div>
          <div class="stat-value">{{ result.summary.pitch_only_mismatch }}</div>
          <div class="stat-label">Pitch disagreements</div>
        </div>
        <div class="stat">
          <div class="stat-index">03</div>
          <div class="stat-value">{{ result.summary.timing_only_mismatch }}</div>
          <div class="stat-label">Timing disagreements</div>
        </div>
        <div class="stat">
          <div class="stat-index">04</div>
          <div class="stat-value">{{ result.summary.both_mismatch }}</div>
          <div class="stat-label">Disagree on both</div>
        </div>
        <div class="stat">
          <div class="stat-index">05</div>
          <div class="stat-value stream-a1">{{ result.summary.only_in_a }}</div>
          <div class="stat-label">Only in A</div>
        </div>
        <div class="stat">
          <div class="stat-index">06</div>
          <div class="stat-value stream-a2">{{ result.summary.only_in_b }}</div>
          <div class="stat-label">Only in B</div>
        </div>
      </div>
      <p class="faint num refs">
        A4: A {{ result.summary.tuning_reference_a_hz }} Hz ·
        B {{ result.summary.tuning_reference_b_hz }} Hz
      </p>

      <p v-if="!rows.length" class="muted">
        The two annotations agree at these tolerances.
      </p>
      <div v-else class="table-wrap">
        <table>
        <thead>
          <tr>
            <th>Disagreement</th>
            <th class="num">Time</th>
            <th class="num">Onset delta</th>
            <th class="num">A pitch</th>
            <th class="num">B pitch</th>
            <th class="num">Pitch delta</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(d, i) in rows" :key="i">
            <td><span class="cat" :class="d.kind">{{ kindLabel[d.kind] }}</span></td>
            <td class="num">{{ (d.a_start_sec ?? d.b_start_sec)?.toFixed(2) }}s</td>
            <td class="num">{{ d.onset_delta_sec != null ? `${d.onset_delta_sec >= 0 ? '+' : ''}${d.onset_delta_sec.toFixed(3)}s` : '—' }}</td>
            <td class="num">{{ d.a_hz ? `${d.a_hz.toFixed(1)} Hz` : '—' }}</td>
            <td class="num">{{ d.b_hz ? `${d.b_hz.toFixed(1)} Hz` : '—' }}</td>
            <td class="num">
              <template v-if="d.pitch_delta_cents != null">
                {{ d.pitch_delta_cents >= 0 ? '+' : '' }}{{ d.pitch_delta_cents.toFixed(0) }} cents
                <span v-if="d.octaves_apart" class="tag info">{{ d.octaves_apart }} octaves apart</span>
              </template>
              <template v-else>—</template>
            </td>
          </tr>
        </tbody>
        </table>
      </div>
      <p v-if="result.discrepancies.length > rows.length" class="faint tally">
        Showing the first {{ rows.length }} of {{ result.discrepancies.length }}.
      </p>
    </template>
  </div>
</template>

<style scoped>
.controls {
  display: flex;
  gap: var(--s3) var(--s3);
  align-items: flex-start;
  flex-wrap: wrap;
  margin: var(--s4) 0 var(--s5);
}
.controls > div { min-width: 0; }
.controls label { white-space: nowrap; }
/* One height for every control in the row, so the labels above them line up. */
.controls select, .controls input, .controls button { height: 2.25rem; }
.controls button { display: inline-flex; align-items: center; align-self: flex-end; }
.controls select { min-width: 7.5rem; }
.controls input { width: 7rem; }

/* The headline of the comparison: six counts, read across. */
.stats {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--s4);
  margin: 0 0 var(--s4);
}
.stat { min-width: 0; }
.stat-index {
  font-family: var(--mono);
  font-size: var(--fs-index);
  color: var(--text-faint);
  margin-bottom: var(--s1);
}
.stat-value {
  font-size: var(--fs-stat);
  font-weight: 600;
  line-height: 1.1;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}
.stat-value.stream-a1 { color: var(--stream-a1); }
.stat-value.stream-a2 { color: var(--stream-a2); }
.stat-label {
  font-size: var(--fs-small);
  color: var(--text-muted);
  margin-top: var(--s1);
}
.quiet { margin-bottom: var(--s5); }
.stored { font-size: var(--fs-small); color: var(--text-muted); margin: 0 0 var(--s4); }
.refs { font-size: var(--fs-micro); margin: 0 0 var(--s4); }
.tally { margin: var(--s2) 0 0; font-size: var(--fs-micro); }
@media (max-width: 900px) {
  .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s4) var(--s3); }
}
@media (max-width: 560px) {
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .controls > div { flex: 1 1 9rem; }
  .controls select, .controls input { width: 100%; }
}
</style>
