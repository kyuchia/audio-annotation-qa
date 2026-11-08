<script setup>
import { RouterView, RouterLink } from 'vue-router'
</script>

<template>
  <div class="shell">
    <header>
      <RouterLink to="/items" class="brand">
        <span class="mark">◍</span>
        <span>
          <strong>Annotation console</strong>
          <span class="sub">The data lives in the system; Sonic Visualiser is the viewer</span>
        </span>
      </RouterLink>
      <a class="btn" href="/admin/" target="_blank" rel="noopener">Researcher console</a>
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
  gap: 16px;
  padding: 12px 22px;
  background: var(--panel);
  border-bottom: 1px solid var(--rule-strong);
  position: sticky;
  top: 0;
  z-index: 10;
}
.brand { display: flex; gap: 11px; align-items: center; text-decoration: none; color: var(--ink); }
.mark { color: var(--accent); font-size: 20px; line-height: 1; }
.brand strong { font-family: var(--cond); font-size: 16px; display: block; }
.sub { font-size: 11px; color: var(--ink-faint); display: block; }
main { padding: 20px 22px 60px; max-width: 1320px; width: 100%; margin: 0 auto; }
@media (max-width: 640px) { .sub { display: none; } main { padding: 14px; } }
</style>
