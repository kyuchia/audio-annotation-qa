<script setup>
import { computed } from 'vue'
import { session } from '../session'

const props = defineProps({
  issues: { type: Array, default: () => [] },
  showTarget: { type: Boolean, default: false },
})
const emit = defineEmits(['acknowledge'])

const order = { error: 0, warning: 1, info: 2 }
const sorted = computed(() => [...props.issues].sort((a, b) => order[a.severity] - order[b.severity]))
const label = { error: 'Error', warning: 'Warning', info: 'Info' }
</script>

<template>
  <div v-if="!sorted.length" class="empty">
    This run found no issues. Next: compare two annotations of the same audio, or export a
    Sonic Visualiser session to look at one against the waveform.
  </div>
  <ul v-else class="issues">
    <li v-for="issue in sorted" :key="issue.id" :class="issue.severity">
      <span class="sev" :class="issue.severity">{{ label[issue.severity] }}</span>
      <span class="msg">{{ issue.message }}</span>
      <code class="code">{{ issue.code }}</code>
      <span v-if="showTarget && issue.item_song_id" class="faint num">{{ issue.item_song_id }}</span>
      <span v-if="issue.layer_label" class="faint num">{{ issue.layer_label }}</span>
      <button
        v-if="session.authenticated && !issue.acknowledged && issue.severity !== 'info'"
        class="ack"
        @click="emit('acknowledge', issue)"
      >
        Mark as seen
      </button>
    </li>
  </ul>
</template>

<style scoped>
/* A finding is a line: its severity, then what it says. */
.issues { list-style: none; margin: 0; padding: 0; }
.issues li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--s2) var(--s3);
  padding: var(--s3) 0;
  border-bottom: 1px solid var(--border);
}
.issues li:last-child { border-bottom: none; padding-bottom: 0; }
.sev { flex: none; }
.msg { flex: 1 1 24rem; min-width: 0; font-size: var(--fs-cell); }
.code { font-size: var(--fs-micro); color: var(--text-muted); white-space: nowrap; }
.ack { margin-left: auto; padding: 2px var(--s3); height: 1.75rem; }
.empty { font-size: var(--fs-cell); color: var(--text-muted); padding: var(--s3) 0; max-width: 78ch; }
@media (max-width: 640px) { .ack { margin-left: 0; } }
</style>
