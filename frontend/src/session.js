/*
  Who the app is being read by.

  One request on load, shared by every view. The answer decides what the interface offers,
  and nothing else: the API keeps its own permissions, so a reader who reaches a write by
  another route is refused by the server exactly as before. Hiding a control a visitor
  cannot use is a courtesy to them, never a lock.

  `ready` is its own flag because "we have yet to ask" and "we asked, and nobody is signed
  in" look alike in a boolean, and only one of them should make a button disappear while
  the page is settling.
*/
import { reactive } from 'vue'
import { api } from './api'

export const session = reactive({
  ready: false,
  authenticated: false,
  username: null,
})

let asked = null

export function loadSession() {
  if (!asked) {
    asked = api
      .me()
      .then((who) => {
        session.authenticated = Boolean(who?.authenticated)
        session.username = who?.username ?? null
      })
      .catch(() => {
        // An unanswered question leaves the app in its read-only shape, which is the one
        // that works for everyone.
        session.authenticated = false
        session.username = null
      })
      .finally(() => {
        session.ready = true
      })
  }
  return asked
}

/*
  Where Django asks for a password, with the way back to the page the reader was reading.
*/
export function signInUrl(returnTo) {
  return `/admin/login/?next=${encodeURIComponent(returnTo || '/')}`
}
