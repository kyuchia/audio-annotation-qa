<script setup>
import { ref } from 'vue'
import { api } from '../api'

const props = defineProps({ itemId: { type: Number, required: true } })
const emit = defineEmits(['uploaded'])

const open = ref(false)
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
    <div class="spread head">
      <div>
        <h2>Upload a new version</h2>
        <p class="note">
          The file is parsed into structured data and opened as a new version, leaving every
          existing layer as it is.
        </p>
      </div>
      <button class="quiet" @click="open = !open">{{ open ? 'Close' : 'Upload +' }}</button>
    </div>

    <template v-if="open">
    <p class="note">
      Say which columns the file holds and which stream it belongs to, and the upload is
      checked against that choice.
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

    <button :disabled="!file || busy" @click="upload">
      {{ busy ? 'Parsing…' : 'Upload and create a new version' }}
    </button>
    </template>

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
.head { align-items: center; gap: var(--s4); }
.head h2 { margin-bottom: var(--s2); }
.head .note { margin-bottom: 0; }
.head button { flex: none; }
@media (max-width: 640px) { .head { align-items: flex-start; } }
.grid {
  display: grid;
  grid-template-columns: 2fr 1fr 1fr 1fr;
  gap: var(--s3) var(--s4);
  margin: var(--s4) 0;
}
.grid .wide { grid-column: 1 / -1; }
.grid input, .grid select { width: 100%; }
.result { margin-top: var(--s4); font-size: var(--fs-body); }
.result ul { margin: var(--s2) 0 0; padding-left: var(--s5); font-size: var(--fs-small); }
@media (max-width: 900px) { .grid { grid-template-columns: 1fr 1fr; } }
@media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
</style>
