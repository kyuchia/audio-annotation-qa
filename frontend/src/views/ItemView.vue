<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import CoverageRail from '../components/CoverageRail.vue'
import ComparePanel from '../components/ComparePanel.vue'
import IssueList from '../components/IssueList.vue'
import LayerPanel from '../components/LayerPanel.vue'
import SignInNote from '../components/SignInNote.vue'
import UploadPanel from '../components/UploadPanel.vue'
import { session } from '../session'

const props = defineProps({ id: { type: String, required: true } })

const item = ref(null)
const issues = ref([])
const segmentsByLayer = ref({})
const loading = ref(true)
const error = ref('')
const busy = ref(false)

/*
  The segments of every layer are loaded here, once, because the timeline draws all of the
  item's streams in one card and the layer rows below report on the same data. One request
  per layer, the same endpoint each layer used to ask for itself.
*/
async function load() {
  loading.value = true
  error.value = ''
  try {
    item.value = await api.item(props.id)
    issues.value = await api.itemIssues(props.id)
    const layers = item.value.layers || []
    const lists = await Promise.all(layers.map((l) => api.segments(l.id)))
    const byLayer = {}
    layers.forEach((l, i) => {
      byLayer[l.id] = lists[i]
    })
    segmentsByLayer.value = byLayer
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.id, load)

async function revalidate() {
  busy.value = true
  try {
    await api.validateItem(props.id)
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

async function acknowledge(issue) {
  await api.acknowledge(issue.id, '')
  issues.value = issues.value.filter((i) => i.id !== issue.id)
}

/*
  Which file an exported SV session will actually open, mirroring Item.playback_audio() on
  the backend. Shown because a session pointed at the stand-in looks and plays entirely
  normally; the only symptom is onsets that do not line up, which reads as a bad annotation
  long before it reads as the wrong audio file.
*/
const exportAudioPath = computed(() => {
  const it = item.value
  if (!it) return ''
  if (it.recording_path) return it.recording_path
  return `${it.disk_path}/${it.audio_filename}`
})

/*
  The denominator for coverage. This mirrors Item.coverage_duration() on the
  backend exactly, wording included — if the timeline divided by the header duration
  while the validators divided by the recording length, the same screen would
  show two different tail gaps for the same layer, and neither number would be
  obviously the wrong one.
*/
const coverage = computed(() => {
  const it = item.value
  if (it?.audio_is_proxy && it?.reference_duration_sec) {
    return { sec: it.reference_duration_sec, source: 'recording length claimed in metadata' }
  }
  if (it?.duration_sec) return { sec: it.duration_sec, source: 'audio header' }
  return { sec: 0, source: 'unknown' }
})

/* Display only: a note layer is counted in notes, anything else in segments. */
function unitFor(layerType) {
  return layerType === 'note_pitch' ? 'notes' : 'segments'
}

function countLabel(layer) {
  return `${layer.segment_count} ${unitFor(layer.layer_type)}`
}

/*
  A track per layer. The label is the stream on its own, and carries the version as well
  once a stream has more than one layer on the page — otherwise two tracks would answer to
  the same name.
*/
const timelineStreams = computed(() => {
  const layers = item.value?.layers || []
  const perStream = {}
  for (const l of layers) {
    const key = l.stream_key || l.layer_type
    perStream[key] = (perStream[key] || 0) + 1
  }
  return layers.map((l) => {
    const key = l.stream_key || l.layer_type
    return {
      key,
      label: perStream[key] > 1 ? `${key} v${l.version}` : key,
      unit: unitFor(l.layer_type),
      segments: segmentsByLayer.value[l.id] || [],
    }
  })
})

function khz(rate) {
  return rate ? `${Number((rate / 1000).toFixed(3))} kHz` : '—'
}

function channelName(channels) {
  if (channels === 1) return 'mono'
  if (channels === 2) return 'stereo'
  return channels ? `${channels} ch` : '—'
}
</script>

<template>
  <p v-if="loading" class="muted">Loading…</p>
  <div v-else-if="error" class="error-box">{{ error }}</div>

  <div v-else-if="item" class="stack">
    <div class="header">
      <div class="spread">
        <div class="titles">
          <div class="eyebrow crumb">
            <RouterLink to="/items">Items</RouterLink> / {{ item.corpus_slug }}
          </div>
          <h1>{{ item.display_name }}</h1>
          <p class="title-sub">
            {{ item.layers.length }} annotation layers ·
            {{ item.duration_sec?.toFixed(2) ?? '—' }} s
          </p>
          <p class="ids">
            <code>{{ item.song_id }}</code>
            <span v-if="item.song_id_is_provisional" class="pill warning">provisional</span>
            <code class="path">{{ exportAudioPath }}</code>
          </p>
        </div>
        <div class="actions">
          <div class="row">
            <a class="btn" :href="api.sessionUrl(item.id)">Download SV session</a>
            <button v-if="session.authenticated" :disabled="busy" @click="revalidate">
              {{ busy ? 'Checking…' : 'Re-run validators' }}
            </button>
          </div>
          <SignInNote v-if="session.ready && !session.authenticated" class="signin-slot" />
        </div>
      </div>

      <div class="meta-row">
        <span class="chip">Sample rate <span class="chip-value">{{ khz(item.sample_rate) }}</span></span>
        <span class="chip">Channels <span class="chip-value">{{ channelName(item.channels) }}</span></span>
        <span class="chip">Duration <span class="chip-value">{{ item.duration_sec?.toFixed(2) ?? '—' }} s</span></span>
        <span class="chip">Format <span class="chip-value">{{ item.audio_format || '—' }}</span></span>
        <span v-if="item.audio_is_proxy" class="pill warning">Stand-in audio</span>
        <span v-if="!item.in_metadata" class="pill warning">Listed on disk only</span>
        <span v-else-if="item.metadata_match_method !== 'exact'" class="tag">
          matched by the “{{ item.metadata_match_method }}” rule (inferred)
        </span>
        <span v-else class="ok-state">Metadata matches exactly</span>
      </div>
    </div>

    <CoverageRail
      v-if="item.layers.length"
      :streams="timelineStreams"
      :duration-sec="coverage.sec"
      :reference-source="coverage.source"
    />

    <ComparePanel v-if="item.layers.length > 1" class="section" :layers="item.layers" />

    <div class="section">
      <h2>Annotation layers</h2>
      <p v-if="!item.layers.length" class="muted">
        No annotations yet. Use the upload panel below to import a note or F0 CSV.
      </p>
      <div class="layers">
        <LayerPanel
          v-for="layer in item.layers"
          :key="layer.id"
          :layer="layer"
          :segments="segmentsByLayer[layer.id] || []"
          :count-label="countLabel(layer)"
          @changed="load"
        />
      </div>
    </div>

    <div class="section">
      <div class="spread findings-head">
        <h2>Open findings</h2>
        <span class="faint num">{{ issues.length }}</span>
      </div>
      <IssueList :issues="issues" @acknowledge="acknowledge" />
    </div>

    <UploadPanel
      v-if="session.authenticated"
      class="section"
      :item-id="item.id"
      @uploaded="load"
    />
  </div>
</template>

<style scoped>
.header { display: flex; flex-direction: column; gap: var(--s4); }
.titles { min-width: 0; }
.crumb { margin-bottom: var(--s2); }
.actions { flex: none; display: flex; flex-direction: column; align-items: flex-end; gap: var(--s3); }
.signin-slot { text-align: right; }
.ids {
  font-size: var(--fs-small);
  margin: var(--s3) 0 0;
  color: var(--text-muted);
  display: flex;
  flex-wrap: wrap;
  gap: var(--s1) var(--s3);
  align-items: baseline;
}
.ids code { color: var(--text-muted); }
.ids .path { overflow-wrap: anywhere; }
.meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s2) var(--s3);
  align-items: center;
}
.layers { margin-top: var(--s2); }
.findings-head { align-items: baseline; margin-bottom: var(--s3); }
.findings-head h2 { margin-bottom: 0; }
/* A heading and its count are short enough to stay a row at any width. */
@media (max-width: 640px) {
  .spread.findings-head { flex-direction: row; }
}
</style>
