<script setup>
import { computed, ref } from 'vue'
import { api } from '../api'
import { session } from '../session'
import { streamColour } from './CoverageRail.vue'

const props = defineProps({
  layer: { type: Object, required: true },
  // The item's segments for this layer, loaded once by the view that draws the timeline.
  segments: { type: Array, default: () => [] },
  countLabel: { type: String, default: '' },
})
const emit = defineEmits(['changed'])

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

const colour = computed(() => streamColour(props.layer.stream_key || props.layer.layer_type))

async function submitReview() {
  saving.value = true
  error.value = ''
  try {
    await api.review(props.layer.id, {
      ...review.value,
      metrics: {
        segment_count: props.segments.length,
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
      <div class="who">
        <div class="row name-row">
          <i class="swatch" :style="{ background: colour }" />
          <strong class="layer-name">{{ layer.stream_key || layer.layer_type }}</strong>
          <span class="tag accent num">v{{ layer.version }}</span>
          <span class="tag" :class="statusTag[layer.status] || ''">{{ layer.status }}</span>
          <span class="tag">{{ layer.author_kind }}</span>
          <span
            v-if="!layer.is_authoritative"
            class="pill warning"
            title="Comparisons use the authoritative layer as their reference"
          >
            derived view
          </span>
          <span v-if="layer.active_correction_count" class="pill warning">
            {{ layer.active_correction_count }} corrections applied
          </span>
        </div>
        <p class="meta tnum">
          {{ countLabel }} ·
          A4 {{ layer.tuning_reference_hz.toFixed(2) }} Hz ·
          <span v-if="layer.latest_review">
            {{ layer.latest_review.decision }}
            <template v-if="layer.latest_review.quality_class">
              · class {{ layer.latest_review.quality_class }}
            </template>
            <template v-if="layer.latest_review.timing_usable !== null">
              · timing {{ layer.latest_review.timing_usable ? 'usable' : 'needs work' }}
            </template>
          </span>
          <span v-else>awaiting review</span>
        </p>
      </div>
      <div class="row actions">
        <a class="btn" :href="api.svlUrl(layer.id)">Download .svl</a>
        <button v-if="session.authenticated" @click="open = !open">
          {{ open ? 'Collapse' : 'Review' }}
        </button>
      </div>
    </div>

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
              <option :value="false">Needs work</option>
            </select>
          </div>
          <div class="field">
            <label for="qc">Quality label (optional)</label>
            <input id="qc" v-model="review.quality_class" placeholder="this corpus's own term" />
            <p class="hint">
              Free text, because the vocabulary belongs to the corpus and changes with it.
              A label is an aid to interpretation, and data keeps its place in the corpus
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
          <button :disabled="saving" @click="submitReview">Submit review</button>
        </section>

        <section>
          <h3>Add a correction</h3>
          <p class="hint">
            A correction is a switchable overlay with an author and a reason, applied when the
            data is read or exported. The original segments stay as they were imported.
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
/* A row, not a card: the layer list is a list, and the one box on the page is the timeline. */
.layer { padding: var(--s4) 0; border-top: 1px solid var(--border); }
.layer:first-child { border-top: none; padding-top: var(--s3); }
.head { align-items: center; gap: var(--s4); }
.who { min-width: 0; }
.name-row { gap: var(--s2); }
.swatch { width: 10px; height: 10px; border-radius: 2px; flex: none; }
.layer-name {
  font-family: var(--mono);
  font-size: var(--fs-body);
  font-weight: 500;
  margin-right: var(--s1);
}
.meta { font-size: var(--fs-small); margin: var(--s2) 0 0; color: var(--text-muted); }
.actions { flex: none; }

.drawer { margin-top: var(--s5); border-top: 1px solid var(--border); padding-top: var(--s5); }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: var(--s6); }
.cols section h3 { padding-bottom: var(--s2); border-bottom: 1px solid var(--border); }
.field { margin-bottom: var(--s3); }
.field select, .field input, .field textarea { width: 100%; }
.hint { font-size: var(--fs-micro); color: var(--text-faint); margin: var(--s1) 0 0; line-height: 1.5; }
@media (max-width: 720px) {
  .cols { grid-template-columns: 1fr; gap: var(--s5); }
  .head { align-items: flex-start; }
}
</style>
