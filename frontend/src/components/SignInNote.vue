<script setup>
/*
  The one line a signed-out reader is shown in place of the controls they cannot use. It
  says what signing in is for, so the offer is worth taking rather than a locked door.
*/
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { signInUrl } from '../session'

const route = useRoute()
const href = computed(() => signInUrl(route.fullPath))
</script>

<template>
  <p class="signin">
    <a :href="href">Sign in to run checks, compare and review.</a>
  </p>
</template>

<style scoped>
.signin { margin: 0; font-size: var(--fs-small); color: var(--text-muted); }
.signin a {
  color: var(--text-muted);
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
.signin a:hover { color: var(--text); text-decoration-color: currentColor; }
</style>
