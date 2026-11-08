<script setup>
import { ref } from 'vue'
import { api } from '../api'

const props = defineProps({ itemId: { type: Number, required: true } })
const emit = defineEmits(['uploaded'])

const file = ref(null)
const streamKey = ref('A1')
const columnContract = ref('onset_offset_hz')
const tuningRef = ref('')
const note = ref('')
const busy = ref(false)
const error = ref('')
const result = ref(null)

function pick(e) {
  file.value = e.target.files[0] || null
}

async function upload() {
  if (!file.value) return
  busy.value = true
  error.value = ''
  result.value = null
  const fd = new FormData()
  fd.append('file', file.value)
  fd.append('stream_key', streamKey.value)
  fd.append('layer_type', 'note_pitch')
  fd.append('column_contract', columnContract.value)
  if (tuningRef.value) fd.append('tuning_reference_hz', tuningRef.value)
  if (note.value) fd.append('note', note.value)
  try {
    result.value = await api.uploadAnnotation(props.itemId, fd)
    emit('uploaded')
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="panel">
    <h2>Upload annotation</h2>
    <p class="note">
      The file is parsed into structured data and opened as a new version; an existing layer is
      never overwritten. Validators run immediately after the upload.
    </p>
    <p class="note">
      Say which columns the file holds. Three unlabelled numeric columns starting with a time
      can be either layout, and a file read under the wrong one imports without complaint —
      so the upload is refused when the data contradicts the choice rather than stored.
      A stream is a parallel track within this item: one key per independent annotator
      (A1, A2), or per source. Two streams are parallel annotations and never supersede each
      other; a new version within one stream does.
    </p>

    <div class="grid">
      <div>
        <label for="f">Note / F0 CSV</label>
        <input id="f" type="file" accept=".csv,text/csv" @change="pick" />
      </div>
      <div>
        <label for="cc">Columns</label>
        <select id="cc" v-model="columnContract">
          <option value="onset_offset_hz">onset, offset, Hz</option>
          <option value="onset_hz_duration">onset, Hz, duration</option>
          <option value="header">read from header row</option>
        </select>
      </div>
      <div>
        <label for="sk">Stream</label>
        <input id="sk" v-model="streamKey" placeholder="A1" />
      </div>
      <div>
        <label for="tr">Tuning reference A4 (Hz, optional)</label>
        <input id="tr" v-model="tuningRef" class="num" placeholder="440" />
      </div>
      <div class="wide">
        <label for="n">Note</label>
        <input id="n" v-model="note" placeholder="What changed in this version" />
      </div>
    </div>

    <button class="primary" :disabled="!file || busy" @click="upload">
      {{ busy ? 'Parsing…' : 'Upload and create a new version' }}
    </button>

    <div v-if="error" class="error-box">{{ error }}</div>

    <div v-if="result" class="result">
      <p>
        Created <strong class="num">{{ result.layer.stream_key }} v{{ result.layer.version }}</strong>
        with <span class="num">{{ result.layer.segment_count }}</span> segments.
        Validator result:
        <span class="num">{{ JSON.stringify(result.validation.summary.by_severity) }}</span>
      </p>
      <ul v-if="result.parser_warnings?.length">
        <li v-for="(w, i) in result.parser_warnings" :key="i" class="muted">{{ w }}</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: 12px; margin: 12px 0; }
.grid .wide { grid-column: 1 / -1; }
.grid input, .grid select { width: 100%; }
.result { margin-top: 11px; font-size: 13px; }
.result ul { margin: 6px 0 0; padding-left: 18px; font-size: 12px; }
@media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
</style>
