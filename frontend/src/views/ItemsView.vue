<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'

const items = ref([])
const corpora = ref([])
const loading = ref(true)
const busy = ref('')
const error = ref('')

const corpus = ref('')
const q = ref('')
const filter = ref('all') // all | issues | no-metadata | unprobed

/*
  Mount must produce exactly one /api/items/ request. Setting `corpus` from the
  corpora response would otherwise wake the watcher below, and the explicit
  load() at the end of onMounted would fire a second one — so the watchers stay
  inert until the first load has finished.
*/
let ready = false

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = {}
    if (corpus.value) params.corpus = corpus.value
    if (q.value) params.q = q.value
    if (filter.value === 'issues') params.has_open_issues = 'true'
    if (filter.value === 'no-metadata') params.in_metadata = 'false'
    if (filter.value === 'unprobed') params.unprobed = 'true'
    params.page_size = 200
    const data = await api.items(params)
    items.value = data.results ?? data
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const data = await api.corpora()
    corpora.value = data.results ?? data
    if (corpora.value.length === 1) corpus.value = corpora.value[0].slug
  } catch (e) {
    /* A failed corpus list should not block the item list. */
  }
  await load()
  ready = true
})

watch([corpus, filter], () => {
  if (ready) load()
})

let typing
watch(q, () => {
  if (!ready) return
  clearTimeout(typing)
  typing = setTimeout(load, 250)
})

const current = computed(() => corpora.value.find((c) => c.slug === corpus.value))

async function rescan() {
  if (!current.value) return
  busy.value = 'scan'
  error.value = ''
  try {
    const r = await api.ingestVocadito(current.value.id, {})
    await load()
    busy.value = ''
    alert(
      `${r.items_created} items created, ${r.items_updated} updated, ` +
        `${r.layers_created} layers.\n` +
        (r.warnings.length ? `\nNeeds attention:\n${r.warnings.slice(0, 12).join('\n')}` : ''),
    )
  } catch (e) {
    error.value = e.message
    busy.value = ''
  }
}

async function validateAll() {
  if (!current.value) return
  busy.value = 'validate'
  error.value = ''
  try {
    await api.validateCorpus(current.value.id)
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

const totals = computed(() => ({
  errors: items.value.reduce((n, i) => n + i.open_error_count, 0),
  warnings: items.value.reduce((n, i) => n + i.open_warning_count, 0),
  orphans: items.value.filter((i) => !i.in_metadata).length,
  unprobed: items.value.filter((i) => !i.media_is_probed).length,
}))
</script>

<template>
  <div class="stack">
    <div class="spread">
      <div>
        <div class="eyebrow">Items</div>
        <h1>{{ current?.name || 'All corpora' }}</h1>
        <p class="muted summary num" v-if="!loading">
          {{ items.length }} items ·
          <span v-if="totals.errors">{{ totals.errors }} errors · </span>
          <span v-if="totals.warnings">{{ totals.warnings }} warnings · </span>
          <span v-if="totals.orphans">{{ totals.orphans }} not in metadata CSV · </span>
          <span v-if="totals.unprobed">{{ totals.unprobed }} with no header read</span>
          <span v-if="!totals.errors && !totals.warnings && !totals.orphans">clean</span>
        </p>
      </div>
      <div class="row">
        <button :disabled="!current || busy" @click="rescan">
          {{ busy === 'scan' ? 'Scanning…' : 'Rescan dataset' }}
        </button>
        <button class="primary" :disabled="!current || busy" @click="validateAll">
          {{ busy === 'validate' ? 'Checking…' : 'Run validators' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>

    <div class="panel controls">
      <div>
        <label for="corpus">Corpus</label>
        <select id="corpus" v-model="corpus">
          <option value="">All</option>
          <option v-for="c in corpora" :key="c.id" :value="c.slug">{{ c.name }}</option>
        </select>
      </div>
      <div class="grow">
        <label for="q">Search song_id or name</label>
        <input id="q" v-model="q" placeholder="e.g. vocadito_21" />
      </div>
      <div>
        <label>Show only</label>
        <div class="row">
          <button :class="{ primary: filter === 'all' }" @click="filter = 'all'">All</button>
          <button :class="{ primary: filter === 'issues' }" @click="filter = 'issues'">
            Open issues
          </button>
          <button :class="{ primary: filter === 'no-metadata' }" @click="filter = 'no-metadata'">
            Not in metadata
          </button>
          <button :class="{ primary: filter === 'unprobed' }" @click="filter = 'unprobed'">
            Header not read
          </button>
        </div>
      </div>
    </div>

    <div class="panel">
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!items.length" class="muted">
        No items match. Pick a corpus and press “Rescan dataset” to bring the data in.
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>song_id</th>
            <th>Name</th>
            <th>metadata</th>
            <th>Audio (read from header)</th>
            <th>layers</th>
            <th>Open issues</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.id">
            <td>
              <RouterLink :to="`/items/${item.id}`" class="num">{{ item.song_id }}</RouterLink>
              <span v-if="item.song_id_is_provisional" class="tag warning">provisional</span>
            </td>
            <td>{{ item.display_name }}</td>
            <td>
              <span v-if="!item.in_metadata" class="tag warning">on disk, not in CSV</span>
              <span v-else-if="item.metadata_match_method !== 'exact'" class="tag info">
                {{ item.metadata_match_method }} (inferred)
              </span>
              <span v-else class="tag ok">matched</span>
            </td>
            <td class="num">
              <template v-if="item.media_is_probed">
                {{ item.sample_rate }} Hz · {{ item.channels }}ch ·
                {{ item.duration_sec.toFixed(2) }}s
              </template>
              <span v-else class="tag error">not read</span>
            </td>
            <td class="num">{{ item.layer_count }}</td>
            <td>
              <span v-if="item.open_error_count" class="tag error">
                {{ item.open_error_count }} errors
              </span>
              <span v-if="item.open_warning_count" class="tag warning">
                {{ item.open_warning_count }} warnings
              </span>
              <span v-if="!item.open_error_count && !item.open_warning_count" class="tag ok">
                clean
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="note">
      Audio specs are always read from the file header; the values claimed in metadata are not
      trusted. “On disk, not in CSV” is not an error — those items are invisible to every script
      that reads the CSV, and this is the one place they show up at all.
    </p>
  </div>
</template>

<style scoped>
.controls { display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap; }
.grow { flex: 1; min-width: 180px; }
.grow input { width: 100%; }
.summary { font-size: 13px; margin: 4px 0 0; }
td .tag { margin-left: 5px; }
</style>
