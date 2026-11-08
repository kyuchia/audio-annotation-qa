<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import CoverageRail from './CoverageRail.vue'

const props = defineProps({
  layer: { type: Object, required: true },
  // The coverage denominator, chosen by the parent to match the backend's
  // Item.coverage_duration(). This is not always the header duration.
  durationSec: { type: Number, default: 0 },
  referenceSource: { type: String, default: '' },
})
const emit = defineEmits(['changed'])

const segments = ref([])
const loadingSegments = ref(true)
const open = ref(false)
const error = ref('')
const saving = ref(false)

const review = ref({
  decision: 'accept',
  timing_usable: null,
  quality_class: '',
  notes: '',
})

const correction = ref({ kind: 'octave_shift', params: '{"octaves": 1}', reason: '' })

onMounted(async () => {
  try {
    segments.value = await api.segments(props.layer.id)
  } catch (e) {
    error.value = e.message
  } finally {
    loadingSegments.value = false
  }
})

async function submitReview() {
  saving.value = true
  error.value = ''
  try {
    await api.review(props.layer.id, {
      ...review.value,
      metrics: {
        segment_count: segments.value.length,
        open_issues: props.layer.open_issue_counts,
      },
    })
    emit('changed')
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

async function addCorrection() {
  saving.value = true
  error.value = ''
  try {
    await api.addCorrection({
      layer: props.layer.id,
      kind: correction.value.kind,
      params: JSON.parse(correction.value.params || '{}'),
      reason: correction.value.reason,
    })
    segments.value = await api.segments(props.layer.id)
    correction.value.reason = ''
    emit('changed')
  } catch (e) {
    error.value = e.message.includes('JSON')
      ? 'Params must be valid JSON, for example {"octaves": 1}'
      : e.message
  } finally {
    saving.value = false
  }
}

async function revalidate() {
  saving.value = true
  try {
    await api.validateLayer(props.layer.id)
    emit('changed')
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

const statusTag = { accepted: 'ok', rejected: 'error', changes_requested: 'warning', superseded: 'info' }
</script>

<template>
  <div class="layer">
    <div class="spread head">
      <div>
        <div class="row">
          <strong class="num">{{ layer.stream_key || layer.layer_type }}</strong>
          <span class="tag accent num">v{{ layer.version }}</span>
          <span class="tag" :class="statusTag[layer.status] || 'info'">{{ layer.status }}</span>
          <span class="tag info">{{ layer.author_kind }}</span>
          <span
            v-if="!layer.is_authoritative"
            class="tag warning"
            title="A derived representation is never the reference for comparison"
          >
            derived view
          </span>
          <span v-if="layer.active_correction_count" class="tag warning">
            {{ layer.active_correction_count }} corrections applied
          </span>
        </div>
        <p class="meta num faint">
          A4 = {{ layer.tuning_reference_hz.toFixed(2) }} Hz ·
          {{ layer.segment_count }} segments ·
          <span v-if="layer.latest_review">
            {{ layer.latest_review.decision }}
            <template v-if="layer.latest_review.quality_class">
              · class {{ layer.latest_review.quality_class }}
            </template>
            <template v-if="layer.latest_review.timing_usable !== null">
              · timing {{ layer.latest_review.timing_usable ? 'usable' : 'not usable' }}
            </template>
          </span>
          <span v-else>not reviewed yet</span>
        </p>
      </div>
      <div class="row">
        <a class="btn" :href="api.svlUrl(layer.id)">Download .svl</a>
        <button @click="open = !open">{{ open ? 'Collapse' : 'Review / correct' }}</button>
      </div>
    </div>

    <p v-if="loadingSegments" class="muted">Loading segments…</p>
    <CoverageRail
      v-else
      :segments="segments"
      :duration-sec="durationSec"
      :reference-source="referenceSource"
      :stream-key="layer.stream_key"
    />

    <div v-if="open" class="drawer">
      <div v-if="error" class="error-box">{{ error }}</div>

      <div class="cols">
        <section>
          <h3>Review</h3>
          <div class="field">
            <label for="dec">Decision</label>
            <select id="dec" v-model="review.decision">
              <option value="accept">Accept</option>
              <option value="accept_with_notes">Accept with notes</option>
              <option value="request_changes">Request changes</option>
              <option value="reject">Reject</option>
            </select>
          </div>
          <div class="field">
            <label for="tu">Is the timing usable</label>
            <select id="tu" v-model="review.timing_usable">
              <option :value="null">Undecided</option>
              <option :value="true">Usable</option>
              <option :value="false">Not usable</option>
            </select>
          </div>
          <div class="field">
            <label for="qc">Quality label (optional)</label>
            <input id="qc" v-model="review.quality_class" placeholder="this corpus's own term" />
            <p class="hint">
              Free text, because the vocabulary belongs to the corpus and changes with it —
              ReviewRecord.quality_class deliberately defines no fixed choices, and a dropdown
              here would narrow what the API accepts without saying so. A label is an aid to
              interpretation, never a deletion criterion: data keeps its place in the corpus
              whatever it is labelled.
            </p>
          </div>
          <div class="field">
            <label for="notes">Evidence and notes</label>
            <textarea
              id="notes"
              v-model="review.notes"
              rows="3"
              placeholder="What is this decision based on"
            />
          </div>
          <button class="primary" :disabled="saving" @click="submitReview">Submit review</button>
        </section>

        <section>
          <h3>Add a correction</h3>
          <p class="hint">
            A correction never rewrites the original segments. It is a switchable overlay with an
            author and a reason, applied only when the data is read or exported.
          </p>
          <div class="field">
            <label for="kind">Kind</label>
            <select id="kind" v-model="correction.kind">
              <option value="octave_shift">Octave shift</option>
              <option value="cents_shift">Cents shift</option>
              <option value="time_offset">Global time offset</option>
              <option value="trim_tail">Trim tail</option>
              <option value="drop_segments">Drop segments</option>
            </select>
          </div>
          <div class="field">
            <label for="params">Params (JSON)</label>
            <input id="params" v-model="correction.params" class="num" />
          </div>
          <div class="field">
            <label for="reason">Reason (required)</label>
            <input
              id="reason"
              v-model="correction.reason"
              placeholder="e.g. the export tool transposed this layer two octaves down"
            />
          </div>
          <div class="row">
            <button :disabled="saving || !correction.reason" @click="addCorrection">
              Add correction
            </button>
            <button :disabled="saving" @click="revalidate">Re-run checks for this layer</button>
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.layer { border: 1px solid var(--rule); border-radius: var(--r); padding: 11px 13px; background: var(--panel); }
.head { margin-bottom: 8px; }
.meta { font-size: 12px; margin: 3px 0 0; }
.drawer { margin-top: 12px; border-top: 1px solid var(--rule); padding-top: 12px; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }
.field { margin-bottom: 9px; }
.field select, .field input, .field textarea { width: 100%; }
.hint { font-size: 11px; color: var(--ink-faint); margin: 3px 0 0; }
@media (max-width: 720px) { .cols { grid-template-columns: 1fr; } }
</style>
