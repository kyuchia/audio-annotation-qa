<script setup>
import { computed, onMounted } from 'vue'
import { RouterView, RouterLink, useRoute } from 'vue-router'
import { loadSession } from './session'

const route = useRoute()

/* Asked once, here, so every view below reads one answer. */
onMounted(loadSession)

/* The admin is for someone already working in the data. The landing page is for someone
   who has yet to see any of it, so it keeps to the two doors it offers. */
const showAdmin = computed(() => route.name !== 'landing')
</script>

<template>
  <div class="shell">
    <header>
      <RouterLink to="/" class="brand">
        <!-- One path, drawn inline so the header mark and the favicon stay identical. -->
        <svg width="26" height="20" viewBox="0 0 26 20" aria-hidden="true">
          <path
            d="M2 10 C5 1, 8 1, 10.5 10 S16 19, 18.5 10 S22 3, 24 6"
            fill="none"
            stroke="currentColor"
            stroke-width="2.2"
            stroke-linecap="round"
          />
        </svg>
        <strong>Annotation QA</strong>
      </RouterLink>
      <nav class="nav">
        <RouterLink class="nav-link" to="/items">Demo</RouterLink>
        <a
          class="nav-link"
          href="https://github.com/kyuchia/audio-annotation-qa"
          target="_blank"
          rel="noopener"
        >
          GitHub
        </a>
        <a v-if="showAdmin" class="nav-link" href="/admin/" target="_blank" rel="noopener">
          Admin
        </a>
      </nav>
    </header>
    <main><RouterView /></main>
  </div>
</template>

<style scoped>
.shell { min-height: 100%; display: flex; flex-direction: column; }
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--s4);
  padding: var(--s4) var(--s6);
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 10;
}
.brand {
  display: flex;
  gap: var(--s3);
  align-items: center;
  text-decoration: none;
  color: var(--text);
}
.brand svg { color: var(--text); flex: none; }
.brand strong {
  font-weight: 600;
  font-size: var(--fs-h2);
  letter-spacing: -0.02em;
  display: block;
  line-height: 1.15;
}
.nav { display: flex; gap: var(--s4); align-items: center; flex: none; }
.nav-link {
  color: var(--text-muted);
  font-size: var(--fs-small);
  font-weight: 500;
  text-decoration: none;
  flex: none;
}
.nav-link:hover { color: var(--text); text-decoration: underline; }
main {
  padding: var(--s7) var(--s6) var(--s8);
  max-width: 84rem;
  width: 100%;
  margin: 0 auto;
}
@media (max-width: 640px) {
  header { padding: var(--s3) var(--s4); }
  .nav { gap: var(--s3); }
  main { padding: var(--s5) var(--s4) var(--s7); }
}
</style>
