import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ROW_TOP_ALIGN_HEIGHT_PX, SettingRow, SettingsCard, SwitchRow } from './Panel'

type ObserverCallback = (entries: Array<{ target: Element }>) => void

describe('Panel row alignment', () => {
  const heightByElement = new WeakMap<Element, number>()
  const observerEntries: Array<{ targets: Set<Element>; callback: ObserverCallback }> = []
  let originalResizeObserver: typeof ResizeObserver | undefined
  let originalGetBoundingClientRect: typeof HTMLElement.prototype.getBoundingClientRect

  beforeEach(() => {
    originalResizeObserver = globalThis.ResizeObserver
    originalGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect

    class MockResizeObserver {
      private entry: { targets: Set<Element>; callback: ObserverCallback }

      constructor(callback: ResizeObserverCallback) {
        this.entry = {
          targets: new Set(),
          callback: callback as unknown as ObserverCallback,
        }
        observerEntries.push(this.entry)
      }

      observe(target: Element) {
        this.entry.targets.add(target)
      }

      unobserve(target: Element) {
        this.entry.targets.delete(target)
      }

      disconnect() {
        this.entry.targets.clear()
      }
    }

    vi.stubGlobal('ResizeObserver', MockResizeObserver)

    HTMLElement.prototype.getBoundingClientRect = function mockRect() {
      const height = heightByElement.get(this) ?? 0
      return {
        width: 400,
        height,
        top: 0,
        left: 0,
        bottom: height,
        right: 400,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      } as DOMRect
    }
  })

  afterEach(() => {
    cleanup()
    observerEntries.length = 0
    HTMLElement.prototype.getBoundingClientRect = originalGetBoundingClientRect
    if (originalResizeObserver) {
      vi.stubGlobal('ResizeObserver', originalResizeObserver)
    } else {
      vi.unstubAllGlobals()
    }
  })

  function triggerResize(target: Element, height: number) {
    heightByElement.set(target, height)
    act(() => {
      for (const entry of observerEntries) {
        if (entry.targets.has(target)) {
          entry.callback([{ target }])
        }
      }
    })
  }

  it('centers compact rows and top-aligns tall SettingRow and SwitchRow instances', () => {
    const { container } = render(
      <div>
        <SettingRow label="Compact row" description="Short text">
          <button type="button">Action</button>
        </SettingRow>
        <SwitchRow label="Tall switch" description="Long description" checked onChange={() => {}} />
      </div>,
    )

    const rows = Array.from(container.querySelectorAll<HTMLElement>('.st-row'))
    expect(rows).toHaveLength(2)

    triggerResize(rows[0], 48)
    triggerResize(rows[1], ROW_TOP_ALIGN_HEIGHT_PX + 16)

    expect(rows[0]).not.toHaveAttribute('data-align')
    expect(rows[1]).toHaveAttribute('data-align', 'top')
  })

  it('updates alignment dynamically when a row resizes across the height threshold', () => {
    render(
      <SettingRow label="Dynamic row">
        <button type="button">Control</button>
      </SettingRow>,
    )

    const row = screen.getByText('Dynamic row').closest('.st-row') as HTMLElement
    triggerResize(row, 48)
    expect(row).not.toHaveAttribute('data-align')

    triggerResize(row, 96)
    expect(row).toHaveAttribute('data-align', 'top')

    triggerResize(row, 56)
    expect(row).not.toHaveAttribute('data-align')
  })

  it('manages raw .st-row elements inside SettingsCard', () => {
    const { container } = render(
      <SettingsCard title="Raw rows">
        <div className="st-row">
          <p className="st-label">Compact raw row</p>
          <button type="button">One</button>
        </div>
        <div className="st-row">
          <p className="st-label">Tall raw row</p>
          <button type="button">Two</button>
        </div>
      </SettingsCard>,
    )

    const rows = Array.from(container.querySelectorAll<HTMLElement>('.st-row'))
    expect(rows).toHaveLength(2)

    triggerResize(rows[0], 60)
    triggerResize(rows[1], 88)

    expect(rows[0]).not.toHaveAttribute('data-align')
    expect(rows[1]).toHaveAttribute('data-align', 'top')
  })
})
