import { defaultUrlTransform } from 'react-markdown'
import { BASE } from './api/client'
import type { UploadedAttachment } from './api/types'

export type ChatLinkTarget =
  | { kind: 'external'; href: string }
  | { kind: 'anchor'; href: string }
  | { kind: 'attachment'; attachment: UploadedAttachment }
  | { kind: 'inert' }

const ATTACHMENT_HANDLE_RE = /^attachment:\/\/([A-Za-z0-9_-]+)/i
const API_FILE_PATH_RE = /\/api\/files\/([^/?#]+)/
// Markdown percent-encodes backslashes in link targets ("C:%5CUsers").
const WINDOWS_DRIVE_PATH_RE = /^\/?[A-Za-z]:(?:[\\/]|%5c)/i
const EXTERNAL_PROTOCOLS = new Set(['http:', 'https:', 'mailto:'])

/**
 * react-markdown blanks hrefs with unknown schemes, and an empty href opened
 * with target=_blank loads the app itself in a new window. Keep local file and
 * attachment references intact so the link renderer can resolve them in-app;
 * they are never written back into the DOM as hrefs.
 */
export function chatMarkdownUrlTransform(value: string, key: string): string {
  if (key === 'href' && isLocalReference(value)) return value
  return defaultUrlTransform(value)
}

export function resolveChatLink(
  href: string | undefined,
  attachments: UploadedAttachment[] | undefined,
): ChatLinkTarget {
  const value = (href ?? '').trim()
  if (!value) return { kind: 'inert' }
  if (value.startsWith('#')) return { kind: 'anchor', href: value }

  const attachment = findLinkedAttachment(value, attachments ?? [])
  if (attachment) return { kind: 'attachment', attachment }

  const url = parseAbsoluteUrl(value)
  if (url && EXTERNAL_PROTOCOLS.has(url.protocol) && !isInAppUrl(url)) {
    return { kind: 'external', href: value }
  }
  // Relative paths, in-app URLs, and local files have nothing to open outside
  // the app; navigating to them would only reload Monaw in another window.
  return { kind: 'inert' }
}

function isLocalReference(value: string): boolean {
  return ATTACHMENT_HANDLE_RE.test(value)
    || /^file:/i.test(value)
    || WINDOWS_DRIVE_PATH_RE.test(value)
}

function findLinkedAttachment(
  href: string,
  attachments: UploadedAttachment[],
): UploadedAttachment | undefined {
  if (attachments.length === 0) return undefined

  const id = ATTACHMENT_HANDLE_RE.exec(href)?.[1] ?? apiFileId(href)
  if (id) return attachments.find((attachment) => attachment.id === id)

  const url = parseAbsoluteUrl(href)
  if (url && url.protocol !== 'file:') return undefined

  const linkedPath = normalizeLocalPath(href)
  const byPath = attachments.find((attachment) => normalizeLocalPath(attachment.path) === linkedPath)
  if (byPath) return byPath

  const linkedName = baseName(linkedPath)
  if (!linkedName) return undefined
  return attachments.find((attachment) =>
    attachment.name.toLowerCase() === linkedName
    || baseName(normalizeLocalPath(attachment.path)) === linkedName)
}

function apiFileId(href: string): string | undefined {
  const url = parseAbsoluteUrl(href)
  if (url && !isInAppUrl(url)) return undefined
  const match = API_FILE_PATH_RE.exec(url ? url.pathname : href)
  return match ? safeDecode(match[1]) : undefined
}

function isInAppUrl(url: URL): boolean {
  const origins = [BASE, typeof window !== 'undefined' ? window.location.href : '']
  return origins.some((origin) => parseAbsoluteUrl(origin)?.origin === url.origin)
}

function parseAbsoluteUrl(value: string): URL | undefined {
  // A drive letter ("C:") would otherwise parse as a URL scheme.
  if (WINDOWS_DRIVE_PATH_RE.test(value)) return undefined
  try {
    return new URL(value)
  } catch {
    return undefined
  }
}

function normalizeLocalPath(value: string): string {
  return safeDecode(value.replace(/^file:\/\/(?:localhost)?/i, '').replace(/[?#].*$/, ''))
    .replace(/\\/g, '/')
    .replace(/^\/(?=[A-Za-z]:\/)/, '')
    .toLowerCase()
}

function baseName(path: string): string {
  return path.split('/').filter(Boolean).pop() ?? ''
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}
