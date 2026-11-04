import type { ToolCall } from '../hooks/useChat'

export type TurnChangeAction = 'created' | 'edited' | 'appended' | 'folder' | 'copied' | 'moved' | 'deleted'

export interface TurnChange {
  path: string
  action: TurnChangeAction
  /** Source path for a copy or move. */
  from?: string
}

export interface TurnChanges {
  files: TurnChange[]
  /** Commands that ran in a shell; they may change files Monaw cannot list. */
  commands: number
}

type ChangeTool = { action: TurnChangeAction; kind: 'path' | 'pair' | 'paths' | 'pairs' }

// Filesystem tools that change files, by the names the model calls them.
const CHANGE_TOOLS: Record<string, ChangeTool> = {
  fs_write: { action: 'created', kind: 'path' },
  file_write: { action: 'created', kind: 'path' },
  create_file: { action: 'created', kind: 'path' },
  fs_append: { action: 'appended', kind: 'path' },
  file_append: { action: 'appended', kind: 'path' },
  fs_patch: { action: 'edited', kind: 'path' },
  file_patch: { action: 'edited', kind: 'path' },
  fs_mkdir: { action: 'folder', kind: 'path' },
  create_folder: { action: 'folder', kind: 'path' },
  fs_copy: { action: 'copied', kind: 'pair' },
  copy: { action: 'copied', kind: 'pair' },
  fs_move: { action: 'moved', kind: 'pair' },
  move: { action: 'moved', kind: 'pair' },
  fs_rename: { action: 'moved', kind: 'pair' },
  rename: { action: 'moved', kind: 'pair' },
  fs_delete: { action: 'deleted', kind: 'path' },
  delete: { action: 'deleted', kind: 'path' },
  fs_delete_many: { action: 'deleted', kind: 'paths' },
  fs_copy_many: { action: 'copied', kind: 'pairs' },
  fs_move_many: { action: 'moved', kind: 'pairs' },
}

const COMMAND_TOOLS = new Set(['exec', 'exec_start'])
const FAILED_STATUSES = new Set(['error', 'failed', 'blocked', 'denied', 'cancelled', 'rejected', 'pending_approval', 'approval_required'])

function parseObject(value: string | undefined): Record<string, unknown> | null {
  if (!value) return null
  try {
    const parsed: unknown = JSON.parse(value)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null
  } catch {
    return null
  }
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

const PATH_KEYS = ['path', 'source', 'destination', 'new_path', 'old_path', 'target'] as const

/**
 * Saved history truncates long tool inputs (a large write), so the JSON may not
 * parse. The path keys usually come before the content; read them as text.
 */
function partialInput(value: string | undefined): Record<string, unknown> | null {
  if (!value) return null
  const found: Record<string, unknown> = {}
  for (const key of PATH_KEYS) {
    const match = new RegExp(`"${key}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`).exec(value)
    if (!match) continue
    try {
      found[key] = JSON.parse(`"${match[1]}"`)
    } catch {
      // A key cut off mid-escape is not a usable path.
    }
  }
  if (/"dry_run"\s*:\s*true/.test(value)) found.dry_run = true
  if (/"overwrite"\s*:\s*true/.test(value)) found.overwrite = true
  return Object.keys(found).length ? found : null
}

/** True only for a call that finished and did not report a failure. */
function succeeded(toolCall: ToolCall): boolean {
  if (toolCall.pending) return false
  if (FAILED_STATUSES.has(String(toolCall.status || '').toLowerCase())) return false
  const output = toolCall.output
  if (output === undefined) return Boolean(toolCall.status)
  if (/^\s*error:/i.test(output)) return false
  const result = parseObject(output)
  if (!result) return true
  if (FAILED_STATUSES.has(String(result.status ?? '').toLowerCase())) return false
  if (result.ok === false || result.success === false || result.dry_run === true) return false
  return !(typeof result.error === 'string' && result.error.trim())
}

function changesFor(toolCall: ToolCall, tool: ChangeTool): TurnChange[] {
  const input = parseObject(toolCall.input) ?? partialInput(toolCall.input)
  if (!input || input.dry_run === true) return []
  if (tool.kind === 'path') {
    const path = text(input.path)
    if (!path) return []
    const action = tool.action === 'created' && input.overwrite === true ? 'edited' : tool.action
    return [{ path, action }]
  }
  if (tool.kind === 'pair') {
    const path = text(input.destination) || text(input.new_path) || text(input.target)
    const from = text(input.source) || text(input.path) || text(input.old_path)
    return path ? [{ path, action: tool.action, ...(from ? { from } : {}) }] : []
  }
  if (tool.kind === 'paths') {
    const paths = Array.isArray(input.paths) ? input.paths : []
    return paths.map(text).filter(Boolean).map((path) => ({ path, action: tool.action }))
  }
  const items = Array.isArray(input.items) ? input.items : []
  return items.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const record = item as Record<string, unknown>
    const path = text(record.destination)
    const from = text(record.source)
    return path ? [{ path, action: tool.action, ...(from ? { from } : {}) }] : []
  })
}

/** What one reply changed on disk, from its finished tool calls, newest action per path. */
export function collectTurnChanges(toolCalls: ToolCall[] | undefined): TurnChanges {
  const byPath = new Map<string, TurnChange>()
  let commands = 0
  for (const toolCall of toolCalls ?? []) {
    if (COMMAND_TOOLS.has(toolCall.tool)) {
      if (!toolCall.pending) commands += 1
      continue
    }
    const tool = CHANGE_TOOLS[toolCall.tool]
    if (!tool || !succeeded(toolCall)) continue
    for (const change of changesFor(toolCall, tool)) {
      const key = change.path.replace(/\\/g, '/').toLowerCase()
      const previous = byPath.get(key)
      // A file created and then edited in the same reply is still new.
      const action = previous?.action === 'created' && (change.action === 'edited' || change.action === 'appended')
        ? 'created'
        : change.action
      byPath.delete(key)
      byPath.set(key, { ...change, action })
    }
  }
  return { files: [...byPath.values()], commands }
}
