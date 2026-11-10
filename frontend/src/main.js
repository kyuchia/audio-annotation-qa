import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import LandingView from './views/LandingView.vue'
import ItemsView from './views/ItemsView.vue'
import ItemView from './views/ItemView.vue'
import './styles.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'landing', component: LandingView },
    { path: '/items', name: 'items', component: ItemsView },
    { path: '/items/:id', name: 'item', component: ItemView, props: true },
  ],
})

createApp(App).use(router).mount('#app')
