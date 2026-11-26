import type { AgentSettings, ModelOptions } from './api/types'

export type ModelSelection = Pick<AgentSettings['llm'], 'provider' | 'model_name' | 'reasoning_effort'>
export type ModelOptionsCatalog = ModelOptions
export const FALLBACK_MODEL_OPTIONS: ModelOptions = { providers: [] }

/** Return server-provided connections without inventing an offline catalog. */
export function providerOptions(catalog: ModelOptions = FALLBACK_MODEL_OPTIONS) {
  return catalog.providers
}

/** Preserve the connection identity when its display metadata is unavailable. */
export function providerLabel(provider: ModelSelection['provider'], catalog: ModelOptions = FALLBACK_MODEL_OPTIONS): string {
  return catalog.providers.find((entry) => entry.id === provider)?.label ?? 'this connection'
}

/** Return only models advertised by this connection. */
export function modelsForProvider(provider: ModelSelection['provider'], catalog: ModelOptions = FALLBACK_MODEL_OPTIONS): string[] {
  return catalog.providers.find((entry) => entry.id === provider)?.models ?? []
}

/** Read authoritative capabilities; missing metadata never implies reasoning support. */
export function reasoningEffortsForProvider(provider: ModelSelection['provider'], model = '', catalog: ModelOptions = FALLBACK_MODEL_OPTIONS) {
  return catalog.providers.find((entry) => entry.id === provider)?.reasoning_efforts?.[model] ?? []
}

/** Use exactly the same model and connection text on every screen. */
export function modelLabel(selection: Pick<ModelSelection, 'provider' | 'model_name'>, catalog: ModelOptions): string {
  const connection = catalog.providers.find((entry) => entry.id === selection.provider)
  if (!connection) return 'Saved model'
  return `${connection.labels?.[selection.model_name] ?? selection.model_name} · ${connection.label}`
}

/** Serialize a pair without confusing colons inside remote model IDs. */
export function modelSelectionKey(selection: Pick<ModelSelection, 'provider' | 'model_name'>): string {
  return JSON.stringify([selection.provider, selection.model_name])
}

/** Keep saved values stable; normalize reasoning only during an explicit model switch. */
export function selectModel(current: ModelSelection, provider: ModelSelection['provider'], model_name: string, catalog: ModelOptions): ModelSelection {
  const efforts = reasoningEffortsForProvider(provider, model_name, catalog)
  const reasoning_effort = efforts.includes(current.reasoning_effort) || efforts.length === 0
    ? current.reasoning_effort
    : efforts.includes('medium') ? 'medium' : efforts[0]
  return { provider, model_name, reasoning_effort }
}

/** Explain an unavailable selection without replacing it with another model. */
export function modelUnavailableReason(selection: ModelSelection, catalog: ModelOptions): string {
  const connection = catalog.providers.find((entry) => entry.id === selection.provider)
  if (!connection) return 'Couldn’t load models.'
  if (connection.available === false) return connection.unavailable_reason || 'Configure this connection in Settings.'
  if (!connection.models.includes(selection.model_name)) return 'Saved model is not listed.'
  return ''
}

/** Normalize editable custom IDs without changing model names. */
export function parseModelNames(text: string): string[] {
  return [...new Set(text.split(/[,\n]/).map((name) => name.trim()).filter(Boolean))]
}
