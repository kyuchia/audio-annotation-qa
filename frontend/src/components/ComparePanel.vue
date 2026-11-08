<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({ layers: { type: Array, default: () => [] } })

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

const comparable = computed(() => props.layers.filter((l) => l.segment_count > 0))

watch(
  comparable,
  (list) => {
    if (!a.value && list.length) a.value = list[0].id
    if (!b.value && list.length > 1) b.value = list[1].id
  },
  { immediate: true },
)

async function run() {
  busy.value = true
  error.value = ''
  result.value = null
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
  Worded as disagreement, not as error. Each category says what the two annotations do
  differently; none of them says either one is wrong. "Annotated only by A" is a fact about
  the pair, whereas "missing from B" would be a verdict on B that this comparison is in no
  position to reach.
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
</script>

<template>
  <div class="panel">
    <h2>Annotation comparison</h2>
    <p class="note">
      Where two independent annotations differ, that is a disagreement between annotators, not
      an error in the data: neither one is the reference for the other. Problems with an
      annotation itself are reported separately, by the validators.
    </p>
    <p class="note">
      Pitch is always compared in cents, never in Hz directly, since two annotations may have
      been made against different tuning references. Pitch and timing are reported separately
      rather than collapsed into a single agreement score.
    </p>

    <div class="controls">
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
        <label for="ot">Onset tolerance (seconds)</label>
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

    <div v-if="error" class="error-box">{{ error }}</div>

    <template v-if="result">
      <div class="summary num">
        <span><b>{{ result.summary.matched }}</b> matched</span>
        <span class="tag warning" v-if="result.summary.pitch_only_mismatch">
          {{ result.summary.pitch_only_mismatch }} pitch disagreements
        </span>
        <span class="tag warning" v-if="result.summary.timing_only_mismatch">
          {{ result.summary.timing_only_mismatch }} timing disagreements
        </span>
        <span class="tag warning" v-if="result.summary.both_mismatch">
          {{ result.summary.both_mismatch }} disagree on both
        </span>
        <span class="tag info" v-if="result.summary.only_in_a">
          {{ result.summary.only_in_a }} only in A
        </span>
        <span class="tag info" v-if="result.summary.only_in_b">
          {{ result.summary.only_in_b }} only in B
        </span>
        <span class="faint">
          A4: A {{ result.summary.tuning_reference_a_hz }} Hz ·
          B {{ result.summary.tuning_reference_b_hz }} Hz
        </span>
      </div>

      <p v-if="!rows.length" class="muted">
        The two annotations agree at these tolerances.
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>Disagreement</th>
            <th>Time</th>
            <th>Onset delta</th>
            <th>A pitch</th>
            <th>B pitch</th>
            <th>Pitch delta</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(d, i) in rows" :key="i">
            <td><span class="tag" :class="d.kind.startsWith('only') ? 'info' : 'warning'">{{ kindLabel[d.kind] }}</span></td>
            <td class="num">{{ (d.a_start_sec ?? d.b_start_sec)?.toFixed(2) }}s</td>
            <td class="num">{{ d.onset_delta_sec != null ? `${d.onset_delta_sec >= 0 ? '+' : ''}${d.onset_delta_sec.toFixed(3)}s` : '—' }}</td>
            <td class="num">{{ d.a_hz ? `${d.a_hz.toFixed(1)} Hz` : '—' }}</td>
            <td class="num">{{ d.b_hz ? `${d.b_hz.toFixed(1)} Hz` : '—' }}</td>
            <td class="num">
              <template v-if="d.pitch_delta_cents != null">
                {{ d.pitch_delta_cents >= 0 ? '+' : '' }}{{ d.pitch_delta_cents.toFixed(0) }} cents
                <span v-if="d.octaves_apart" class="tag warning">{{ d.octaves_apart }} octaves apart</span>
              </template>
              <template v-else>—</template>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="result.discrepancies.length > rows.length" class="faint">
        Showing the first {{ rows.length }} of {{ result.discrepancies.length }}.
      </p>
    </template>
  </div>
</template>

<style scoped>
.controls { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; margin: 12px 0; }
.controls select, .controls input { min-width: 120px; }
.summary { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin: 12px 0 8px; font-size: 13px; }
</style>
