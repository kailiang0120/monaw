import { apiFetch, BASE, decodeApiError, JSON_HEADERS } from './client'
import { getPersistentIdempotencyKey } from './idempotency'

export type UndoCapabilityStatus =
  | 'available'
  | 'unavailable'
  | 'expired'
  | 'undone'
  | 'needs_review'
  | 'conflict'

export interface UndoCapability {
  run_id: string
  operation_id: string
  status: UndoCapabilityStatus
  reason_code: string | null
  revision: number
}

export interface FileMutationPreviewInput {
  run_id: string
  conversation_id: string
  expected_revision: number
  operation?: 'write'
  path: string
  content: string
  overwrite?: boolean
  create_parents?: boolean
  encoding?: string
}

export interface FileMutationOperand {
  path: string
  role: string
  before: Record<string, unknown>
  after: Record<string, unknown>
}

export interface FileMutationPlanPreview {
  version: number
  operation: string
  files: FileMutationOperand[]
  undo_capability: string
  preview: Record<string, unknown>
}

export interface FileMutationPreview {
  mutation_id: string
  run_id: string
  conversation_id: string
  revision: number
  operation: string
  files: FileMutationOperand[]
  preview: Record<string, unknown>
  undo_capability: string
  apply_available: boolean
  reason_code: string | null
  expires_in_seconds: number
}

export interface FileMutationApplyResult {
  status: 'applied' | string
  mutation_id: string
  run_id: string
  conversation_id: string
  operation_id: string
  revision: number
  verification_result: string
  preview: FileMutationPlanPreview
  undo_capability: string
}

export interface FileMutationUndoInput {
  run_id: string
  conversation_id: string
  expected_revision: number
  confirmation_id: string
}

export interface UndoPreviewFile {
  path: string
  role: string
  action: 'restore' | 'remove' | string
  before: Record<string, unknown>
  after: Record<string, unknown>
  preview: Record<string, unknown>
}

export interface UndoPreview {
  confirmation_id: string
  run_id: string
  conversation_id: string
  operation_id: string
  revision: number
  operation: string
  files: UndoPreviewFile[]
  expires_in_seconds: number
}

export interface FileMutationUndoResult {
  run_id: string
  operation_id: string
  undo_operation_id: string
  status: 'undone' | 'needs_review' | 'failed' | string
  reason_code: string | null
  evidence: Record<string, string | number | boolean>
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) throw await decodeApiError(response, fallback)
  return response.json() as Promise<T>
}

function mutationUrl(path: string): string {
  return `${BASE}/api/file-mutations${path}`
}

function withIdempotencyKey(headersInit: HeadersInit | undefined, key: string): Headers {
  const headers = new Headers(headersInit)
  headers.set('Idempotency-Key', key)
  return headers
}

export async function previewFileMutation(
  input: FileMutationPreviewInput,
  idempotencyKey?: string,
  signal?: AbortSignal,
): Promise<FileMutationPreview> {
  const body = { operation: 'write' as const, ...input }
  const key = idempotencyKey ?? getPersistentIdempotencyKey(['file-mutation', 'preview', body])
  const response = await apiFetch(mutationUrl('/preview'), {
    method: 'POST',
    headers: withIdempotencyKey(JSON_HEADERS, key),
    body: JSON.stringify(body),
    signal,
  })
  return readJson<FileMutationPreview>(response, 'Failed to prepare file mutation preview')
}

export async function applyFileMutation(
  mutationId: string,
  expectedRevision: number,
  idempotencyKey?: string,
  signal?: AbortSignal,
): Promise<FileMutationApplyResult> {
  const body = { expected_revision: expectedRevision }
  const key = idempotencyKey ?? getPersistentIdempotencyKey([
    'file-mutation', 'apply', mutationId, body,
  ])
  const response = await apiFetch(mutationUrl(`/${encodeURIComponent(mutationId)}/apply`), {
    method: 'POST',
    headers: withIdempotencyKey(JSON_HEADERS, key),
    body: JSON.stringify(body),
    signal,
  })
  return readJson<FileMutationApplyResult>(response, 'Failed to apply file mutation')
}

export async function fetchUndoCapability(
  operationId: string,
  runId: string,
  conversationId: string,
  signal?: AbortSignal,
): Promise<UndoCapability> {
  const params = new URLSearchParams({ run_id: runId, conversation_id: conversationId })
  const response = await apiFetch(
    mutationUrl(`/${encodeURIComponent(operationId)}/undo-capability?${params}`),
    { signal },
  )
  return readJson<UndoCapability>(response, 'Failed to check Undo availability')
}

export async function previewUndoFileMutation(
  operationOrMutationId: string,
  input: Omit<FileMutationUndoInput, 'confirmation_id'>,
  signal?: AbortSignal,
): Promise<UndoPreview> {
  const response = await apiFetch(
    mutationUrl(`/${encodeURIComponent(operationOrMutationId)}/undo-preview`),
    {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(input),
      signal,
    },
  )
  return readJson<UndoPreview>(response, 'Failed to prepare Undo preview')
}

export async function undoFileMutation(
  operationOrMutationId: string,
  input: FileMutationUndoInput,
  idempotencyKey?: string,
  signal?: AbortSignal,
): Promise<FileMutationUndoResult> {
  const body = input
  const key = idempotencyKey ?? getPersistentIdempotencyKey([
    'file-mutation', 'undo', operationOrMutationId, body,
  ])
  const response = await apiFetch(
    mutationUrl(`/${encodeURIComponent(operationOrMutationId)}/undo`),
    {
      method: 'POST',
      headers: withIdempotencyKey(JSON_HEADERS, key),
      body: JSON.stringify(body),
      signal,
    },
  )
  return readJson<FileMutationUndoResult>(response, 'Failed to undo file mutation')
}
