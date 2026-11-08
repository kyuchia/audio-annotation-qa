<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import CoverageRail from '../components/CoverageRail.vue'
import ComparePanel from '../components/ComparePanel.vue'
import IssueList from '../components/IssueList.vue'
import LayerPanel from '../components/LayerPanel.vue'
import UploadPanel from '../components/UploadPanel.vue'

const props = defineProps({ id: { type: String, required: true } })

const item = ref(null)
const issues = ref([])
const loading = ref(true)
const error = ref('')
const busy = ref(false)
const railKey = ref(0)

async function load() {
  loading.value = true
  error.value = ''
  try {
    item.value = await api.item(props.id)
    issues.value = await api.itemIssues(props.id)
    railKey.value += 1
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

const grouped = computed(() => {
  const out = {}
  for (const layer of item.value?.layers || []) {
    const key = `${layer.layer_type}/${layer.stream_key || '—'}`
    ;(out[key] ||= []).push(layer)
  }
  return out
})

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
  backend exactly, wording included — if the rail divided by the header duration
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
</script>

<template>
  <p v-if="loading" class="muted">Loading…</p>
  <div v-else-if="error" class="error-box">{{ error }}</div>

  <div v-else-if="item" class="stack">
    <div class="spread">
      <div>
        <div class="eyebrow">
          <RouterLink to="/items">Items</RouterLink> / {{ item.corpus_slug }}
        </div>
        <h1>{{ item.display_name }}</h1>
        <p class="ids num faint">
          song_id <code>{{ item.song_id }}</code>
          <span v-if="item.song_id_is_provisional" class="tag warning">provisional</span>
          · disk <code>{{ item.disk_path }}/{{ item.audio_filename }}</code>
        </p>
      </div>
      <div class="row">
        <a class="btn" :href="api.sessionUrl(item.id)">Download SV session</a>
        <button class="primary" :disabled="busy" @click="revalidate">
          {{ busy ? 'Checking…' : 'Re-run validators' }}
        </button>
      </div>
    </div>

    <div class="facts">
      <div class="fact">
        <div class="eyebrow">Sample rate</div>
        <div class="num big">{{ item.sample_rate ?? '—' }}<small>Hz</small></div>
      </div>
      <div class="fact">
        <div class="eyebrow">Channels</div>
        <div class="num big">{{ item.channels ?? '—' }}</div>
      </div>
      <div class="fact">
        <div class="eyebrow">Duration</div>
        <div class="num big">{{ item.duration_sec?.toFixed(2) ?? '—' }}<small>s</small></div>
      </div>
      <div class="fact">
        <div class="eyebrow">Format</div>
        <div class="num big small-text">{{ item.audio_format || '—' }}</div>
      </div>
      <div class="fact wide">
        <div class="eyebrow">Export audio</div>
        <div class="export-audio">
          <span v-if="item.has_recording" class="tag ok">linked recording</span>
          <span v-else-if="item.audio_is_proxy" class="tag warning">stand-in audio</span>
          <span v-else class="tag ok">audio beside the annotations</span>
          <code class="faint">{{ exportAudioPath }}</code>
        </div>
      </div>
      <div class="fact wide">
        <div class="eyebrow">metadata</div>
        <div>
          <span v-if="!item.in_metadata" class="tag warning">on disk, not in CSV</span>
          <span v-else-if="item.metadata_match_method !== 'exact'" class="tag info">
            matched by the “{{ item.metadata_match_method }}” rule (inferred)
          </span>
          <span v-else class="tag ok">exact match</span>

        </div>
      </div>
    </div>
    <p class="note">
      The four values above are read from the audio file header, not from what the metadata
      claims. Frame indices in a Sonic Visualiser export are derived from this sample rate.
      <template v-if="item.audio_is_proxy">
        The file on disk is a stand-in rather than the recording the annotations were made
        against, so coverage is measured against the recording length claimed in the metadata
        ({{ item.reference_duration_sec?.toFixed(2) ?? '—' }}s) instead of this header duration.
      </template>
    </p>

    <div class="panel">
      <div class="spread">
        <h2>Open findings</h2>
        <span class="faint num">{{ issues.length }}</span>
      </div>
      <IssueList :issues="issues" @acknowledge="acknowledge" />
    </div>

    <div class="panel">
      <h2>Annotation layers</h2>
      <p class="note">
        Layers are grouped by stream. Within one stream, a higher version supersedes the one
        before it; separate streams are parallel annotations of the same audio and neither
        supersedes the other.
      </p>
      <p v-if="!item.layers.length" class="muted">
        No annotations yet. Use the upload panel below to import a note or F0 CSV.
      </p>
      <div v-for="(layers, key) in grouped" :key="key" class="group">
        <div class="eyebrow group-head">{{ key }}</div>
        <div class="stack">
          <LayerPanel
            v-for="layer in layers"
            :key="`${layer.id}-${railKey}`"
            :layer="layer"
            :duration-sec="coverage.sec"
            :reference-source="coverage.source"
            @changed="load"
          />
        </div>
      </div>
    </div>

    <ComparePanel v-if="item.layers.length > 1" :layers="item.layers" />

    <UploadPanel :item-id="item.id" @uploaded="load" />
  </div>
</template>

<style scoped>
.ids { font-size: 12px; margin: 4px 0 0; }
.ids code { font-size: 12px; }
.facts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  background: var(--rule);
  border: 1px solid var(--rule);
  border-radius: var(--r);
  overflow: hidden;
}
.fact { background: var(--panel); padding: 10px 13px; }
.fact.wide { grid-column: span 2; }
.big { font-size: 22px; font-weight: 500; line-height: 1.25; }
.big small { font-size: 12px; color: var(--ink-faint); margin-left: 2px; }
.small-text { font-size: 13px; padding-top: 6px; }
.export-audio { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.export-audio code { font-size: 11px; word-break: break-all; }
.group { margin-top: 14px; }
.group-head { margin-bottom: 6px; }
@media (max-width: 820px) {
  .facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
