import { describe, expect, it } from 'vitest'

import { BASE } from './api/client'
import type { UploadedAttachment } from './api/types'
import { chatMarkdownUrlTransform, resolveChatLink } from './chatLinks'

const screenshot: UploadedAttachment = {
  id: 'shot-1',
  name: 'region_1.png',
  path: 'attachment://shot-1',
  mime_type: 'image/png',
  size: 10,
}

describe('chat link resolution', () => {
  it('keeps local references for the link renderer but still strips unsafe schemes', () => {
    expect(chatMarkdownUrlTransform('attachment://shot-1', 'href')).toBe('attachment://shot-1')
    expect(chatMarkdownUrlTransform('C:\\shots\\region_1.png', 'href')).toBe('C:\\shots\\region_1.png')
    expect(chatMarkdownUrlTransform('file:///C:/shots/region_1.png', 'href')).toBe('file:///C:/shots/region_1.png')
    expect(chatMarkdownUrlTransform('javascript:alert(1)', 'href')).toBe('')
    expect(chatMarkdownUrlTransform('file:///C:/shots/region_1.png', 'src')).toBe('')
  })

  it('resolves attachment handles, API file URLs, and local paths to the attachment', () => {
    for (const href of [
      'attachment://shot-1',
      '/api/files/shot-1',
      `${BASE}/api/files/shot-1/preview?conversation_id=c1`,
      'C:\\Users\\KL\\.monaw\\browser\\screenshots\\region_1.png',
      'C:%5CUsers%5CKL%5C.monaw%5Cbrowser%5Cscreenshots%5Cregion_1.png',
      '/C:/Users/KL/.monaw/browser/screenshots/region_1.png',
      'file:///C:/Users/KL/.monaw/browser/screenshots/REGION_1.png',
      'region_1.png',
    ]) {
      expect(resolveChatLink(href, [screenshot])).toEqual({ kind: 'attachment', attachment: screenshot })
    }
  })

  it('keeps web links external, even when they end in an attachment name', () => {
    expect(resolveChatLink('https://example.com/region_1.png', [screenshot]))
      .toEqual({ kind: 'external', href: 'https://example.com/region_1.png' })
    expect(resolveChatLink('mailto:hi@example.com', [])).toEqual({ kind: 'external', href: 'mailto:hi@example.com' })
  })

  it('never treats in-app or unresolved local targets as openable links', () => {
    for (const href of [
      undefined,
      '',
      '/',
      'attachment://missing',
      'C:/Users/KL/other.png',
      `${BASE}/api/files/missing`,
      `${window.location.origin}/settings`,
    ]) {
      expect(resolveChatLink(href, [screenshot])).toEqual({ kind: 'inert' })
    }
    expect(resolveChatLink('#user-content-fn-1', [])).toEqual({ kind: 'anchor', href: '#user-content-fn-1' })
  })
})
