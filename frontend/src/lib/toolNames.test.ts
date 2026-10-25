import { describe, expect, it } from 'vitest'
import { isBrowserTool, toolDisplayName } from './toolNames'

describe('toolDisplayName', () => {
  it('names browser tools by what they do', () => {
    expect(toolDisplayName('mcp__chrome-devtools__navigate_page')).toBe('browser · open page')
    expect(toolDisplayName('mcp__Chrome-dev-tools__take_snapshot')).toBe('browser · read page')
    expect(toolDisplayName('mcp__chrome-devtools__some_new_tool')).toBe('browser · some new tool')
  })

  it('drops the MCP prefix for other servers and keeps built-in names', () => {
    expect(toolDisplayName('mcp__notes__find_note')).toBe('notes · find note')
    expect(toolDisplayName('web_search')).toBe('web_search')
  })
})

describe('isBrowserTool', () => {
  it('covers Chrome DevTools and browser_ tools only', () => {
    expect(isBrowserTool('mcp__chrome-devtools__click')).toBe(true)
    expect(isBrowserTool('browser_fetch')).toBe(true)
    expect(isBrowserTool('mcp__notes__find_note')).toBe(false)
  })
})
