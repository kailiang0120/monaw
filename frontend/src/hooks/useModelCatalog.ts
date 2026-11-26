import { useEffect, useSyncExternalStore } from 'react'
import { fetchModelOptions } from '../lib/api/settings'
import { FALLBACK_MODEL_OPTIONS } from '../lib/modelSelection'
import type { ModelOptions } from '../lib/api/types'

interface ModelCatalogState {
  catalog: ModelOptions
  status: 'loading' | 'ready' | 'error'
}

let snapshot: ModelCatalogState = { catalog: FALLBACK_MODEL_OPTIONS, status: 'loading' }
let pending: Promise<void> | null = null
let generation = 0
const listeners = new Set<() => void>()

/** Publish catalog data and its loading state together to every consumer. */
function publish(next: ModelCatalogState) {
  snapshot = next
  listeners.forEach((listener) => listener())
}

/** Refresh all consumers together, retaining the last good catalog during outages. */
export function refreshModelCatalog(force = false): Promise<void> {
  if (pending && !force) return pending
  const request = ++generation
  if (snapshot.catalog.providers.length === 0 && snapshot.status !== 'loading') {
    publish({ ...snapshot, status: 'loading' })
  }
  pending = fetchModelOptions().then((next) => {
    if (request !== generation) return
    publish({ catalog: next, status: 'ready' })
  }).catch(() => {
    if (request === generation) publish({ ...snapshot, status: 'error' })
  }).finally(() => {
    if (request === generation) pending = null
  })
  return pending
}

/** Force fresh credentials and capabilities after connection changes or sign-in. */
function refreshConnections() {
  void refreshModelCatalog(true)
}

/** Share one set of refresh listeners while any model picker is mounted. */
function subscribe(listener: () => void) {
  if (listeners.size === 0) {
    window.addEventListener('focus', refreshConnections)
    window.addEventListener('monaw:model-catalog-changed', refreshConnections)
  }
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) {
      window.removeEventListener('focus', refreshConnections)
      window.removeEventListener('monaw:model-catalog-changed', refreshConnections)
    }
  }
}

/** Give chat, settings, and task dialogs the same cached registry snapshot. */
export function useModelCatalogState(): ModelCatalogState {
  const state = useSyncExternalStore(subscribe, () => snapshot)
  useEffect(() => { void refreshModelCatalog() }, [])
  return state
}

/** Read catalog data where the caller does not need loading feedback. */
export function useModelCatalog(): ModelOptions {
  return useModelCatalogState().catalog
}
