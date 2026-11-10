<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import SignInNote from '../components/SignInNote.vue'
import { session } from '../session'

const items = ref([])
const corpora = ref([])
const loading = ref(true)
const busy = ref('')
const error = ref('')
const notice = ref(null)

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

/* An empty list means two different things, and telling a reader to import data they have
   already imported is the wrong one. */
const narrowed = computed(() => filter.value !== 'all' || Boolean(q.value))

/*
  What a rescan did, read off its counts. The report also carries totals a reader has no
  decision to make about — segments written, metadata rows seen — so the sentence names
  the numbers that changed the page, and drops the layers clause when nothing new came in.
*/
function rescanSummary(r) {
  const touched = r.items_created + r.items_updated
  const parts = [
    r.items_created ? `${r.items_created} added` : 'none added',
    r.items_updated ? `${r.items_updated} refreshed` : 'none refreshed',
  ]
  if (r.layers_created) {
    parts.push(`${r.layers_created} ${r.layers_created === 1 ? 'layer' : 'layers'} added`)
  }
  return `Rescanned ${touched} ${touched === 1 ? 'item' : 'items'}: ${parts.join(', ')}.`
}

async function rescan() {
  if (!current.value) return
  busy.value = 'scan'
  error.value = ''
  notice.value = null
  try {
    const r = await api.ingestVocadito(current.value.id, {})
    await load()
    notice.value = {
      text: rescanSummary(r),
      warnings: (r.warnings || []).slice(0, 12),
    }
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

async function validateAll() {
  if (!current.value) return
  busy.value = 'validate'
  error.value = ''
  notice.value = null
  try {
    await api.validateCorpus(current.value.id)
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

/*
  Display order. The API sorts song_id as text, which puts vocadito_16 ahead of
  vocadito_8 because "1" sorts before "8". Comparing with numeric collation reads the
  digits as a number, so the list runs the way the corpus is numbered.
*/
const sortedItems = computed(() =>
  [...items.value].sort((a, b) =>
    a.song_id.localeCompare(b.song_id, undefined, { numeric: true, sensitivity: 'base' }),
  ),
)

/* "44.1 kHz · mono · 17.28 s" — the three audio facts as one readable line. */
function audioSummary(item) {
  const khz = `${Number((item.sample_rate / 1000).toFixed(3))} kHz`
  const channels = item.channels === 1 ? 'mono' : item.channels === 2 ? 'stereo' : `${item.channels} ch`
  return `${khz} · ${channels} · ${item.duration_sec.toFixed(2)} s`
}

const totals = computed(() => ({
  errors: items.value.reduce((n, i) => n + i.open_error_count, 0),
  warnings: items.value.reduce((n, i) => n + i.open_warning_count, 0),
  orphans: items.value.filter((i) => !i.in_metadata).length,
  unprobed: items.value.filter((i) => !i.media_is_probed).length,
  // Display only: the second line of the page title.
  layers: items.value.reduce((n, i) => n + i.layer_count, 0),
}))
</script>

<template>
  <div class="stack">
    <div class="spread">
      <div class="titles">
        <h1>{{ current?.name || 'All corpora' }}</h1>
        <p class="title-sub" v-if="!loading">
          {{ items.length }} items · {{ totals.layers }} layers
        </p>
        <p class="status-line row" v-if="!loading">
          <span v-if="totals.errors" class="pill error">{{ totals.errors }} errors</span>
          <span v-if="totals.warnings" class="pill warning">{{ totals.warnings }} warnings</span>
          <span v-if="totals.orphans" class="pill warning">
            {{ totals.orphans }} listed on disk only
          </span>
          <span v-if="totals.unprobed" class="pill warning">
            {{ totals.unprobed }} awaiting a header read
          </span>
          <span
            v-if="!totals.errors && !totals.warnings && !totals.orphans && !totals.unprobed"
            class="ok-state"
          >
            All items clean
          </span>
        </p>
      </div>
      <div v-if="session.authenticated" class="row">
        <!--
          Boolean, because `busy` is a string: `!current || busy` yielded '' whenever a
          corpus was selected, and Vue renders disabled="" for anything other than
          false/null/undefined — which disabled both buttons permanently.
        -->
        <button :disabled="!current || Boolean(busy)" @click="rescan">
          {{ busy === 'scan' ? 'Scanning…' : 'Rescan dataset' }}
        </button>
        <button class="primary" :disabled="!current || Boolean(busy)" @click="validateAll">
          {{ busy === 'validate' ? 'Checking…' : 'Run validators' }}
        </button>
      </div>
      <SignInNote v-else-if="session.ready" class="signin-slot" />
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>

    <div v-if="notice" class="notice-box">
      <p>{{ notice.text }}</p>
      <template v-if="notice.warnings.length">
        <p class="notice-head">Needs attention</p>
        <ul>
          <li v-for="(w, i) in notice.warnings" :key="i">{{ w }}</li>
        </ul>
      </template>
    </div>

    <div class="panel controls">
      <div>
        <label for="corpus">Corpus</label>
        <select id="corpus" v-model="corpus">
          <option value="">All</option>
          <option v-for="c in corpora" :key="c.id" :value="c.slug">{{ c.name }}</option>
        </select>
      </div>
      <div class="grow">
        <label for="q">Search</label>
        <input id="q" v-model="q" placeholder="vocadito_21" />
      </div>
      <div>
        <label>Show only</label>
        <div class="row">
          <button :class="{ selected: filter === 'all' }" @click="filter = 'all'">All</button>
          <button :class="{ selected: filter === 'issues' }" @click="filter = 'issues'">
            Errors and warnings
          </button>
          <button :class="{ selected: filter === 'no-metadata' }" @click="filter = 'no-metadata'">
            On disk only
          </button>
          <button :class="{ selected: filter === 'unprobed' }" @click="filter = 'unprobed'">
            Awaiting header read
          </button>
        </div>
      </div>
    </div>

    <div class="panel">
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!items.length && narrowed" class="muted">
        No items match this filter.
      </p>
      <p v-else-if="!items.length" class="muted">
        No items match. Pick a corpus and press “Rescan dataset” to bring the data in.
      </p>
      <div v-else class="table-wrap">
        <table>
        <thead>
          <tr>
            <th>Song ID</th>
            <th>Name</th>
            <th class="tnum">Audio</th>
            <th class="tnum right">Layers</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in sortedItems" :key="item.id">
            <td>
              <RouterLink :to="`/items/${item.id}`" class="num">{{ item.song_id }}</RouterLink>
              <span v-if="item.song_id_is_provisional" class="pill warning">provisional</span>
            </td>
            <td>{{ item.display_name }}</td>
            <td class="tnum audio-cell">
              <template v-if="item.media_is_probed">{{ audioSummary(item) }}</template>
              <span v-else class="pill error">awaiting a header read</span>
            </td>
            <td class="tnum right">{{ item.layer_count }}</td>
            <td>
              <div class="status-cell">
                <span v-if="item.open_error_count" class="pill error">
                  {{ item.open_error_count }} errors
                </span>
                <span v-if="item.open_warning_count" class="pill warning">
                  {{ item.open_warning_count }} warnings
                </span>
                <span v-if="!item.in_metadata" class="pill warning">listed on disk only</span>
                <span v-else-if="item.metadata_match_method !== 'exact'" class="tag">
                  {{ item.metadata_match_method }} (inferred)
                </span>
                <span
                  v-if="
                    !item.open_error_count &&
                    !item.open_warning_count &&
                    item.in_metadata &&
                    item.metadata_match_method === 'exact'
                  "
                  class="ok-state"
                >
                  clean
                </span>
              </div>
            </td>
          </tr>
        </tbody>
        </table>
      </div>
    </div>

  </div>
</template>

<style scoped>
.titles { min-width: 0; }
/* The line stands where the buttons stood, on the title's own baseline. */
.signin-slot { flex: none; padding-top: var(--s2); text-align: right; }
.status-line { margin: var(--s4) 0 0; gap: var(--s2); }
/*
  The three fields start at the same line: every label is one line of the same size, and
  every control below it is the same height, so "Show only" and its chips sit on the grid
  the Corpus and Search fields set.
*/
.controls {
  display: flex;
  gap: var(--s4);
  align-items: flex-start;
  flex-wrap: wrap;
  padding-bottom: var(--s5);
  border-bottom: 1px solid var(--border);
}
.controls select, .controls input, .controls button { height: 2.25rem; }
.controls button { display: inline-flex; align-items: center; }
.grow { flex: 1; min-width: 11rem; }
.grow input { width: 100%; }
td .pill, td .tag { margin-left: var(--s2); }
/* The id is the row's handle, so it reads as an identifier first and a link second: no
   rule under it until a reader goes looking for one. */
td a.num { font-weight: 500; text-decoration: none; }
td a.num:hover { text-decoration: underline; text-decoration-color: currentColor; }
.table-wrap { max-height: none; }
/* A chosen filter, which is a state rather than an action: black is reserved for the one
   primary action on the page. */
.controls .selected {
  border: 1.2px solid var(--text);
  background: var(--surface);
  font-weight: 600;
}
.audio-cell { white-space: nowrap; }
.status-cell { display: flex; flex-wrap: wrap; gap: var(--s1) var(--s2); align-items: center; }
@media (max-width: 720px) {
  .controls { gap: var(--s3); }
}
</style>
