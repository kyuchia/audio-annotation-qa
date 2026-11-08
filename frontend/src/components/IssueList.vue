<script setup>
import { computed } from 'vue'

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
      <div class="head">
        <span class="tag" :class="issue.severity">{{ label[issue.severity] }}</span>
        <code>{{ issue.code }}</code>
        <span v-if="showTarget && issue.item_song_id" class="faint num">{{ issue.item_song_id }}</span>
        <span v-if="issue.layer_label" class="faint num">{{ issue.layer_label }}</span>
        <button
          v-if="!issue.acknowledged && issue.severity !== 'info'"
          class="ack"
          @click="emit('acknowledge', issue)"
        >
          Mark as seen
        </button>
      </div>
      <p>{{ issue.message }}</p>
    </li>
  </ul>
</template>

<style scoped>
.issues { list-style: none; margin: 0; padding: 0; }
.issues li { padding: 9px 0 9px 11px; border-left: 2px solid var(--rule); border-bottom: 1px solid var(--rule); }
.issues li.error { border-left-color: var(--error); }
.issues li.warning { border-left-color: var(--warn); }
.issues li:last-child { border-bottom: none; }
.head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 12px; }
.head code { font-size: 11px; color: var(--ink-muted); }
p { margin: 4px 0 0; font-size: 13px; }
.ack { margin-left: auto; padding: 2px 8px; font-size: 11px; }
.empty { font-size: 13px; color: var(--ink-muted); padding: 10px 0; }
</style>
