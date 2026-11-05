import { describe, expect, it } from 'vitest'
import type { ToolCall } from '../hooks/useChat'
import { collectTurnChanges } from './turnChanges'

const call = (tool: string, input: unknown, overrides: Partial<ToolCall> = {}): ToolCall => ({
  tool,
  input: typeof input === 'string' ? input : JSON.stringify(input),
  output: JSON.stringify({ status: 'ok' }),
  status: 'ok',
  ...overrides,
})

describe('collectTurnChanges', () => {
  it('lists files written, edited, moved and deleted by file tools', () => {
    const { files, commands } = collectTurnChanges([
      call('fs_read', { path: 'README.md' }),
      call('fs_write', { path: 'src/app.ts', content: 'x' }),
      call('fs_patch', { path: 'package.json', old: 'a', new: 'b' }),
      call('fs_move', { source: 'old.txt', destination: 'new.txt' }),
      call('fs_delete', { path: 'tmp.log' }),
      call('fs_mkdir', { path: 'src/lib' }),
    ])
    expect(commands).toBe(0)
    expect(files).toEqual([
      { path: 'src/app.ts', action: 'created' },
      { path: 'package.json', action: 'edited' },
      { path: 'new.txt', action: 'moved', from: 'old.txt' },
      { path: 'tmp.log', action: 'deleted' },
      { path: 'src/lib', action: 'folder' },
    ])
  })

  it('skips failed, pending, blocked and dry-run calls', () => {
    const { files } = collectTurnChanges([
      call('fs_write', { path: 'a.txt' }, { status: 'error' }),
      call('fs_write', { path: 'b.txt' }, { pending: true, output: undefined, status: undefined }),
      call('fs_write', { path: 'c.txt' }, { output: JSON.stringify({ status: 'blocked' }) }),
      call('fs_write', { path: 'd.txt', dry_run: true }),
      call('fs_write', { path: 'e.txt' }, { output: 'Error: permission denied', status: undefined }),
    ])
    expect(files).toEqual([])
  })

  it('keeps one row per path and a new file stays new after edits', () => {
    const { files } = collectTurnChanges([
      call('fs_write', { path: 'src\\page.tsx' }),
      call('fs_patch', { path: 'src/page.tsx', old: 'a', new: 'b' }),
      call('fs_write', { path: 'notes.md', overwrite: true }),
    ])
    expect(files).toEqual([
      { path: 'src/page.tsx', action: 'created' },
      { path: 'notes.md', action: 'edited' },
    ])
  })

  it('reads the path from a write whose saved input was truncated', () => {
    const truncated = '{"path": "C:\\\\work\\\\big.html", "content": "<html><body>' + 'x'.repeat(200)
    const { files } = collectTurnChanges([call('fs_write', truncated, { previewOnly: true })])
    expect(files).toEqual([{ path: 'C:\\work\\big.html', action: 'created' }])
  })

  it('counts shell commands, whose file changes it cannot list', () => {
    const { files, commands } = collectTurnChanges([
      call('exec', { command: 'npx create-next-app@latest .' }),
      call('exec_start', { command: 'npm run dev' }),
      call('exec', { command: 'ls' }, { pending: true }),
    ])
    expect(files).toEqual([])
    expect(commands).toBe(2)
  })
})
