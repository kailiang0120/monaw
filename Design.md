# Monaw UI Design Guide (`Design.md`)

> **Authoritative UI & Frontend Engineering Reference for Agents and Contributors**
>
> Every new screen, dialog, settings panel, card, row, button, or interactive control in Monaw must follow this guide. Reuse existing design tokens (`--st-*`), stylesheets, and primitives instead of inventing ad-hoc colors, heights, or layout patterns.

---

## 1. Canonical Sources

Always inspect and reuse these files before writing new UI:

| Layer | Canonical File | What It Owns |
| --- | --- | --- |
| Theme tokens, chat, app bar, markdown prose | [`frontend/src/styles/globals.css`](frontend/src/styles/globals.css) | `.theme-dark`, `.theme-light`, `.chat-interface`, `.app-bar*`, `.primary-button`, `.ghost-button`, `.control`, `.agent-prose`, utility remapping |
| Configuration & dialog system | [`frontend/src/styles/settings.css`](frontend/src/styles/settings.css) | `.settings-shell`, `.st-card*`, `.st-row*`, `.st-btn*`, `.st-input*`, `.st-select*`, `.st-menu*`, `.st-switch*`, `.st-choice*`, `.st-badge*`, `.st-note*`, `.st-segment*`, `.st-obs-*` |
| Scrollbars | [`frontend/electron/scrollbars.css`](frontend/electron/scrollbars.css) | Floating 4 px pill scrollbars, `.st-scroll-hidden`, `.sidebar-scroll`, high-contrast & forced-colors rules |
| Native & startup modal dialogs | [`frontend/electron/message-dialog.css`](frontend/electron/message-dialog.css) | `.monaw-message*`, `.monaw-startup*` (shared by renderer `<dialog>` confirmations and Electron emergency startup window) |
| Tailwind theme & animations | [`frontend/tailwind.config.js`](frontend/tailwind.config.js) | Font families (`sans`, `mono`), `accent` palette, keyframe animations |
| Settings & panel primitives | [`frontend/src/components/ui/Panel.tsx`](frontend/src/components/ui/Panel.tsx) | `PageHeader`, `SettingsCard`, `SettingRow`, `StackedRow`, `Switch`, `SwitchRow`, `NumberRow`, `FlagToggle`, `ChoiceCard`, `Badge`, `Note`, `ReadOnlyValue`, `InfoTip`, height-aware alignment (`ROW_TOP_ALIGN_HEIGHT_PX`) |
| Modal confirmations | [`frontend/src/components/ui/MessageDialog.tsx`](frontend/src/components/ui/MessageDialog.tsx) | Native `<dialog>` confirmation & alert primitive |
| Inline confirmations | [`frontend/src/components/ui/InlineConfirm.tsx`](frontend/src/components/ui/InlineConfirm.tsx) | Compact inline destructive confirmation (e.g., sidebar delete) |
| Portal-mounted select menus | [`frontend/src/components/Dropdown.tsx`](frontend/src/components/Dropdown.tsx) | Searchable/grouped themed select menu rendered into `.theme-dark, .theme-light` |
| Modal focus management | [`frontend/src/hooks/useModalFocus.ts`](frontend/src/hooks/useModalFocus.ts) | Topmost dialog stack, `Tab` trapping, `Escape` routing, focus restoration |
| Settings copy | [`frontend/src/features/settings/settingsCopy.ts`](frontend/src/features/settings/settingsCopy.ts) | Plain-English UI labels, blurbs, descriptions, and search keywords |
| Desktop window & theme bridge | [`frontend/electron/main.js`](frontend/electron/main.js) | Window bounds (`1200×800`, min `800×600`), native frame/title bar integration, `APP_THEME_COLORS` |

---

## 2. Design Principles

1. **Warm-Neutral Craft, Restrained Green Accent**
   - All surfaces live on one warm stone-neutral ramp (`#1a1917` → `#37352f` in dark mode; `#ecebe3` → `#ffffff` in light mode).
   - Green (`--st-accent` / `--st-accent-text`) is reserved for **primary actions**, **active selection**, **focus indicators**, and **positive status**. Never scatter decorative green or rainbow accents across ordinary cards.

2. **Theme-Agnostic Tokens Everywhere**
   - Components must style themselves via `--st-*` CSS custom properties or `.st-*` classes so both `.theme-dark` and `.theme-light` work identically with zero `isDark` branching in JSX.
   - **Never** use hardcoded dark-only Tailwind utilities (`bg-white/[0.03]`, `bg-black/20`, `border-white/[0.08]`, `text-neutral-200`) in new components. (Legacy remapping rules exist at the bottom of `globals.css` only for older chat components.)

3. **Progressive Disclosure & Visual Calm**
   - Keep primary views focused on what the user needs immediately.
   - Explanations longer than `90` characters (`INLINE_DESCRIPTION_MAX` in `Panel.tsx`) automatically move behind an `InfoTip` (`i`) button instead of bloating row height.
   - Secondary analytics, raw payloads, and diagnostics stay collapsed by default (for example, `Trends & breakdown` and `Tool IO` in `ObservabilityPanel`, or `Execution details` in chat).
   - Chrome like scrollbars stays subtle and low-opacity until hovered.

4. **Design for the Real Container, Not the Full Viewport**
   - Always check the actual container width before picking grid column counts:
     - Settings page body is `max-w-[46rem]` (`~696px` usable inner width inside the `960px` modal). Use **1–2 columns** for cards/charts and **at most 4 columns** for compact KPI strips—never `xl:grid-cols-5` inside Settings.
     - Chat transcript and composer are capped at `max-w-3xl` (`48rem` / `768px`).
     - Minimum Electron window size is `800×600`. Every modal, toolbar, and control row must remain usable and unclipped at `800×700`.

5. **Consistent Optical Alignment**
   - Compact settings rows center controls vertically; tall multi-line rows (`>= 72px`) automatically top-align controls via `Panel.tsx` + `.st-row[data-align='top']`.
   - Button groups share consistent control heights (`34px` default, `28px` compact/composer) and predictable left-to-right action ordering (`Cancel` / ghost on the left, primary or destructive action on the right).

6. **Desktop-Native Focus, Keyboard Isolation & Safe Destruction**
   - Every modal uses `useModalFocus` (or native `<dialog>.showModal()` via `MessageDialog`).
   - Nested popovers (`Dropdown`, `InfoTip`) stop `Escape` propagation so pressing `Escape` closes only the popover, not the parent dialog.
   - Destructive actions never default focus to the destructive button (`MessageDialog` focuses `Cancel` first; `InlineConfirm` focuses `Cancel` first).

7. **Accessibility by Default**
   - Maintain WCAG AA (`>= 4.5:1`) contrast for all body text (note how `.chat-interface` strengthens `--st-text*` and darkens `--st-accent` in dark mode).
   - Respect `prefers-reduced-motion: reduce`, `prefers-contrast: more`, and `forced-colors: active`.

---

## 3. Color System & Theme Tokens

All tokens are defined on `.theme-dark` and `.theme-light` in [`frontend/src/styles/globals.css`](frontend/src/styles/globals.css).

### 3.1 Core Surface, Border, Text & Semantic Tokens

| Token | `.theme-dark` | `.theme-light` | Role / Usage |
| --- | --- | --- | --- |
| `--st-bg` | `#262624` | `#faf9f5` | Main modal / app content background |
| `--st-bg-nav` | `#1e1d1b` | `#f0eee6` | Navigation sidebar, chat backdrop (`--chat-background`), app bar left cap |
| `--st-appbar` | `#1a1917` | `#ecebe3` | Top app bar background (must match title bar expectations) |
| `--st-surface` | `#2f2e2b` | `#ffffff` | Cards (`.st-card`), floating panels (`.panel`), active nav item, dropdown menus |
| `--st-surface-hover` | `#37352f` | `#f4f2ea` | Hover state for rows, menu items, ghost buttons |
| `--st-surface-muted` | `#232220` | `#f4f2ea` | Secondary surfaces (`.panel-muted`, `.live-surface`, `.app-bar-capsule`, usage cards) |
| `--st-surface-sunken` | `#1b1a18` | `#f2f0e8` | Inputs (`.st-input`, `.control`), code blocks (`.st-code`), segmented track (`.st-segment`), card footers, choice cards |
| `--st-border` | `rgba(255, 255, 255, 0.08)` | `rgba(30, 28, 24, 0.1)` | Hairline borders, card dividers, row separators |
| `--st-border-strong` | `rgba(255, 255, 255, 0.17)` | `rgba(30, 28, 24, 0.2)` | Input borders, secondary button borders, switches, dropdown menus |
| `--st-text` | `#f2f0ea` | `#1f1e1b` | Primary text, headings, labels, input values |
| `--st-text-muted` | `#b2afa5` | `#5f5d56` | Body descriptions (`.st-desc`), secondary metadata, ghost button text |
| `--st-text-faint` | `#86837b` | `#85827a` | Hints (`.st-hint`), group labels, placeholders, subtle icons, timestamps |
| `--st-accent` | `#648f36` | `#4f7a2b` | Primary button fill, active switch track, progress fill, selected row accent bar |
| `--st-accent-hover` | `#557c2c` | `#416523` | Hover fill for primary buttons |
| `--st-accent-text` | `#a7d879` | `#3a5f1e` | Accent text, active nav icons, selected menu text, links, focus outlines |
| `--st-accent-soft` | `rgba(140, 200, 90, 0.14)` | `rgba(79, 122, 43, 0.11)` | Selected choice/row/menu backgrounds, info notes |
| `--st-accent-border` | `rgba(140, 200, 90, 0.34)` | `rgba(79, 122, 43, 0.3)` | Focused inputs, active choice cards, accent badges, info note borders |
| `--st-on-accent` | `#ffffff` | `#ffffff` | Text/icons placed on solid `--st-accent` backgrounds |
| `--st-ok` | `#83cc6d` | `#2f6b1d` | Success / connected / ready status text & dots |
| `--st-ok-soft` | `rgba(110, 200, 100, 0.14)` | `rgba(79, 122, 43, 0.12)` | Success badge background |
| `--st-ok-border` | `rgba(110, 200, 100, 0.32)` | `rgba(79, 122, 43, 0.3)` | Success badge & status pill border |
| `--st-warn` | `#e6b665` | `#8a5a12` | Warning text, paused/degraded/tool-error indicators |
| `--st-warn-soft` | `rgba(220, 160, 70, 0.14)` | `rgba(190, 130, 30, 0.13)` | Warning note / badge / Full Access mode pill background |
| `--st-warn-border` | `rgba(220, 160, 70, 0.32)` | `rgba(190, 130, 30, 0.3)` | Warning borders |
| `--st-danger` | `#f0907e` | `#a83b28` | Error text, destructive action labels & borders |
| `--st-danger-soft` | `rgba(230, 105, 85, 0.14)` | `rgba(180, 60, 40, 0.1)` | Danger button hover, error notes/badges, window close hover |
| `--st-danger-border` | `rgba(230, 105, 85, 0.32)` | `rgba(180, 60, 40, 0.28)` | Danger button & alert borders |
| `--st-shadow` | `0 24px 64px rgba(0, 0, 0, 0.45)` | `0 24px 56px rgba(40, 36, 28, 0.16)` | Modal shells, `.panel`, `.st-menu`, `.st-info-tip` |
| `--st-focus` | `rgba(140, 200, 90, 0.4)` | `rgba(79, 122, 43, 0.32)` | Outer focus ring (`box-shadow: 0 0 0 3px var(--st-focus)`) |
| `--st-scrollbar` | `rgba(255, 255, 255, 0.08)` | `rgba(30, 28, 24, 0.1)` | Scrollbar thumb resting state |
| `--st-scrollbar-hover` | `rgba(255, 255, 255, 0.2)` | `rgba(30, 28, 24, 0.22)` | Scrollbar thumb when scrollable container is hovered |
| `--st-scrollbar-active` | `rgba(255, 255, 255, 0.34)` | `rgba(30, 28, 24, 0.36)` | Scrollbar thumb when thumb is hovered or dragged |

### 3.2 Chat Surface High-Contrast Overrides

Inside `.chat-interface`, text contrast is stepped up for long-form reading and user bubbles:

| Selector | Overridden Tokens | Reason |
| --- | --- | --- |
| `.theme-light .chat-interface` | `--st-text: #171717`, `--st-text-muted: #414141`, `--st-text-faint: #595959` | Stronger reading contrast on `#f0eee6` chat backdrop |
| `.theme-dark .chat-interface` | `--st-text: #f5f5f5`, `--st-text-muted: #d4d4d4`, `--st-text-faint: #b5b5b5`, `--st-accent: #4f7a2b` | Darker `--st-accent` ensures white user-bubble text meets `4.5:1` contrast |

### 3.3 Tailwind `accent` Extension & Overlays

- `tailwind.config.js` defines `accent` (`DEFAULT: #4a7a28`, `hover: #3d6620`, `light: #c8f09a`) used by `.bg-accent` / `text-accent-light` in chat bubbles and legacy utilities.
- Modal backdrop `.st-overlay` and native `dialog::backdrop`:
  - Dark: `rgba(12, 11, 10, 0.62)` + `backdrop-filter: blur(6px)`
  - Light: `rgba(40, 36, 28, 0.32)` + `backdrop-filter: blur(6px)`
- Isolated startup dialogs (`[data-theme='light'] .monaw-startup` in `message-dialog.css`) duplicate the light tokens so startup errors theme correctly without loading the main React bundle.
- `ContextUsageBar` ring stop colors: `safe: #22c55e` (`<60%`), `warm: #eab308` (`60–79%`), `near: #f97316` (`80–89%`), `danger: #ef4444` (`>=90%`).
- `StorageSettings` category chart palette (`CATEGORY_COLORS`): distinct muted hues readable on both dark and light surfaces (`chats: #6f9e3c`, `uploads: #3f8f9b`, `activity: #c08a2e`, `logs: #a8743f`, `outputs: #8a6fb8`, `workspace: #3f9b7a`, `other: #8b887f`, etc.).

---

## 4. Typography & Fonts

### 4.1 Font Families

Defined in [`frontend/tailwind.config.js`](frontend/tailwind.config.js) and loaded via Google Fonts in [`frontend/index.html`](frontend/index.html) (`Inter` weights `300`, `400`, `500`, `600`):

- **UI / Sans (`font-sans`)**: `'Geist Sans', 'Inter', system-ui, sans-serif` (applied globally on `body` with `antialiased`, `-webkit-font-smoothing: antialiased`, `-moz-osx-font-smoothing: grayscale`).
- **Code / Monospace (`font-mono`)**: `'Geist Mono', 'JetBrains Mono', monospace`.
- **Isolated native startup fallback**: `ui-sans-serif, system-ui, sans-serif` (`message-dialog.css`).

### 4.2 Type Scale & Text Roles

| Class / Role | Font Size | Weight | Line Height | Tracking / Measure | Color Token | Where Used |
| --- | --- | --- | --- | --- | --- | --- |
| `.st-page-title` | `0.875rem` (`14px`) | `600` | default | `-0.02em` | `--st-text` | Top `PageHeader` heading |
| `.st-title` | `0.875rem` (`14px`) | `600` | default | `-0.01em` | `--st-text` | Settings sidebar title |
| `.st-card-head h4.st-label` | `0.875rem` (`14px`) | `600` | `1.45` | default | `--st-text` | Card section headings |
| `.st-page-desc` | `0.8125rem` (`13px`) | `400` | `1.6` | `max-width: 62ch` | `--st-text-muted` | Subhead under `PageHeader` |
| `.app-bar-wordmark` / `.app-bar-title` | `13px` | `650` / `600` | default | `-0.01em` | `--st-text` | App bar brand & chat title |
| `.st-label` | `0.75rem` (`12px`) | `500` | `1.45` | default | `--st-text` | Row labels, button labels, choice titles |
| `.st-desc` | `0.75rem` (`12px`) | `400` | `1.6` | `max-width: 58ch` | `--st-text-muted` | Row descriptions, secondary status text |
| `.st-hint` | `0.75rem` (`12px`) | `400` | `1.6` | default | `--st-text-faint` | Footnotes, timestamps, minor captions |
| `.st-group-label` | `0.75rem` (`12px`) | `600` | default | `0.06em`, `uppercase` | `--st-text-faint` | Settings nav group headings |
| `.section-label` | `10px` | `600` | default | `0.12em`, `uppercase` | `--st-text-faint` | Compact uppercase section kickers |
| `.st-badge` / `.st-code` | `0.6875rem` (`11px`) | `500` / `400` | `1.6` (code) | `break-all` (code) | `--st-text-muted` | Badges, paths, JSON/log blocks |
| `.app-bar-status` / `.status-pill` | `10px` | `600` / `500` | default | `0.1em`, `uppercase` (status) | `--st-text-faint` / `--st-text-muted` | App bar status & compact pills |
| `.agent-prose` | `text-sm` (`14px`) | `400–600` | relaxed | `h1: 1.125rem`, `h2: 1rem`, `h3–h6: 0.875rem` | `--tw-prose-*` mapped to `--st-*` | Assistant markdown responses |

### 4.3 Numeric Formatting

- Use `tabular-nums` (`font-variant-numeric: tabular-nums`) for **all** counts, durations, token/cost metrics, version numbers, progress percentages, and list telemetry so columns align vertically and do not jitter during updates.

---

## 5. Layout, Spacing, Sizing & Alignment Rules

### 5.1 Shell & Container Dimensions

- **Desktop window** (`electron/main.js`): Default `1200×800`, minimum `800×600`. Native title bar is hidden (`hidden` on Windows/Linux, `hiddenInset` on macOS).
- **App bar** (`globals.css`): `--app-bar-height: 44px`; grid columns `var(--app-bar-cap) minmax(0, 1fr) auto`. Left cap width matches the sidebar (`16rem` expanded, `3.5rem` collapsed; `5rem` inset / `8.5rem` collapsed on macOS for traffic lights).
- **Chat & Composer** (`ChatWindow.tsx`, `InputBar.tsx`): Centered column `mx-auto max-w-3xl` (`48rem`) with `px-4` horizontal padding.
- **Settings Modal** (`SettingsModal.tsx`):
  - Shell: `h-[min(86vh,820px)] w-full max-w-[960px] rounded-2xl overflow-hidden` inside `.st-overlay.below-app-bar` (`p-4`).
  - Left nav (`<aside className="st-nav">`): `hidden md:flex w-52 shrink-0 p-3` (`208px`). Below `md`, collapses into a top `Dropdown` (`Settings section`).
  - Body scroller (`.st-scroll`): `px-5 py-4`, inner container `mx-auto w-full max-w-[46rem]` (`736px` max; `~696px` content width).
  - Top-right close button (`.st-settings-close`): `top: 0.75rem; right: 0.75rem;` (`PageHeader` reserves `padding-right: 2.25rem` inside `.settings-shell` so titles never collide with it).

### 5.2 Control Heights & Radius Scale

- **Standard control height**: `--control-height: 2.125rem` (`34px`) in `:root`.
- **Dense settings/composer height**: `--control-height-sm: 1.75rem` (`28px`); `.settings-shell` overrides `--control-height: var(--control-height-sm)` so all Settings inputs, selects, and buttons share a compact `28px` height.
- **Border radius scale**:
  - `rounded-2xl` (`1rem`): Modal shells (`.settings-shell`, `.monaw-message`), user/assistant chat bubbles.
  - `rounded-xl` (`0.75rem`): Cards (`.st-card`), execution timeline cards, image attachment previews, icon rail buttons.
  - `0.625rem` (`10px`): Choice cards (`.st-choice`), dropdown menus (`.st-menu`), notes (`.st-note`), segmented container (`.st-segment`).
  - `0.5rem` (`8px` / `rounded-lg`): Inputs (`.st-input`), selects (`.st-select`), buttons (`.st-btn`), code blocks (`.st-code`), composer icon buttons (`h-7 w-7`).
  - `999px` (`rounded-full`): Switches (`.st-switch`), badges (`.st-badge`), pills (`.app-bar-capsule`, `.app-bar-mode`), scrollbars, status dots.

### 5.3 Alignment Rules (Critical)

1. **Settings Rows (`.st-row`) — Height-Aware Center vs. Top Alignment**
   - Default alignment on `.st-row` is **`align-items: center`** (`settings.css`) with `justify-content: space-between; gap: 1rem; padding: 0.625rem 0.875rem;`.
   - When a row's rendered height is **`>= 72px`** (`ROW_TOP_ALIGN_HEIGHT_PX = 72` in `Panel.tsx` — i.e. 3+ lines of text, wrapped controls, or multi-paragraph status rows), it automatically receives `data-align="top"`, which switches it to **`align-items: flex-start`**.
   - **How it works**:
     - `SettingRow`, `SwitchRow`, and `NumberRow` attach `useRowAlignment` (`ResizeObserver` + `useLayoutEffect`).
     - `SettingsCard` runs `useCardRowAlignment` (`ResizeObserver` + `MutationObserver`) to manage any raw `<div className="st-row">` elements inside the card as well.
     - `SwitchRow` uses `.st-row-switch` on its control wrapper so switches stay dead-center on compact rows and gain `padding-top: 0.125rem` only when `[data-align='top']` is active.
   - **Rule for new code**: Always render rows inside `SettingsCard` using `SettingRow`, `SwitchRow`, `NumberRow`, or `.st-row`. **Never** hardcode `items-start` or `items-center` on a `.st-row` element—let the `72px` height rule govern alignment consistently.

2. **Headers (`PageHeader`, `SettingsCard`, `StackedRow`)**
   - `SettingsCard` header (`.st-card-head`) and `StackedRow` header row use **`items-center justify-between gap-4`** so card action buttons/badges align vertically with single-line headings.
   - `PageHeader` uses **`items-center`** when there is no `description` (e.g., `Browser` + `Reconnect`), and **`items-start`** when `description` is present so the action button aligns with the page title rather than floating mid-paragraph.

3. **Row Control Widths & Right-Edge Alignment**
   - Standard `SettingRow` controls sit in `.st-row-control` (`flex-shrink: 0; min-width: 11rem; max-width: 16rem;`) so dropdowns, short inputs, and read-only values share a clean right edge.
   - Wide inputs (paths, URLs, notes, image tags + action button) pass `wide` (`w-1/2 min-w-0`).
   - Switches, badges, and auto-sized buttons use `.st-row-control-auto` (`flex-shrink: 0`).
   - `NumberRow` pairs `.st-input-number` (`width: 6.5rem`, right-aligned text, spinner arrows hidden) with a fixed unit column (`min-w-[10ch] shrink-0 whitespace-nowrap`) so units align across stacked limit rows.

4. **Inline vs. Card-Corner `InfoTip`**
   - On `RowText` (rows and card heads), `InfoTip` renders `inline` beside the label (`18×18px`, `14px` icon) whenever `description.length > 90` or `descriptionInInfo` is set.
   - On `ChoiceCard`, `InfoTip` is pinned top-right (`absolute right-2 top-2`, `24×24px`) with `pr-10` on the card button so reading help never triggers selection.

5. **Composer Bottom Row (`InputBar.tsx`)**
   - Every control in the bottom toolbar shares a `28px` (`h-7`) baseline (`attach`, `voice`, `Permission mode`, `ModelPicker`, `ContextUsageBar` `28×28px`, `pause/continue`, `stop`, `queue`, `send`).
   - `.chat-composer-container` establishes `container: composer / inline-size`. Below `720px`, `.composer-model-control` collapses to a `28×28px` icon button so the toolbar stays on one clean row.

6. **Settings Card Spacing**
   - Stack multiple `SettingsCard` sections inside a page with `space-y-3` or `space-y-4`.
   - Separate rows inside a card exclusively with `.st-row + .st-row`, `.st-row-stacked + .st-row`, or `.st-divider-t` hairlines—never add nested outer margins inside cards.

---

## 6. Buttons & Controls

### 6.1 Settings & Dialog Button Classes (`settings.css`)

All `.st-btn` buttons share `height: var(--control-height)`, `border-radius: 0.5rem`, `font-size: 0.75rem` (`12px`), `font-weight: 500`, `gap: 0.375rem`, `padding: 0.25rem 0.625rem`, `white-space: nowrap`, `disabled:opacity-45`, and `focus-visible: box-shadow: 0 0 0 3px var(--st-focus)`.

| Class | Background | Border | Text Color | Hover | When to Use |
| --- | --- | --- | --- | --- | --- |
| `.st-btn.st-btn-primary` | `--st-accent` | `transparent` | `--st-on-accent` (`#fff`) | `--st-accent-hover` | Primary commitment (Save, Apply, Unlock, Prepare/Save report, ChatGPT sign-in) |
| `.st-btn.st-btn-secondary` | `--st-surface` | `--st-border-strong` | `--st-text` | `--st-surface-hover` | Standard secondary actions (Reconnect, Browse, Check for updates, Copy, Add) |
| `.st-btn.st-btn-ghost` | `transparent` | `transparent` | `--st-text-muted` | `--st-surface-hover` + `--st-text` | Low-emphasis actions (Cancel, Refresh, Replay, Bundle, Discard, Advanced) |
| `.st-btn.st-btn-danger` | `transparent` | `--st-danger-border` | `--st-danger` | `--st-danger-soft` | Destructive actions (Delete Data, Discard changes, Clear history) |
| `.st-btn-sm` | inherits variant | inherits variant | inherits variant | inherits variant | Compact `var(--control-height-sm)` (`28px`) buttons inside dense toolbars/card heads |
| `.st-btn-icon` | inherits variant | inherits variant | inherits variant | inherits variant | Square icon-only buttons (`width = height`, `padding: 0`), e.g. modal close, refresh icon |

### 6.2 App Shell & Chat Buttons

| Class / Component | Dimensions & Radius | Styling & Usage |
| --- | --- | --- |
| `.primary-button` (`globals.css`) | Caller sets size (e.g., `h-7 w-7 rounded-lg` in composer) | `--st-accent` fill, `--st-on-accent` text, `--st-accent-hover`, focus ring `--st-focus` |
| `.ghost-button` (`globals.css`) | Caller sets size (e.g., `h-9 w-9 rounded-xl` in collapsed sidebar) | `--st-text-muted`, hover `--st-surface-hover` + `--st-border` + `--st-text` |
| `.app-bar-icon-button` (`globals.css`) | `30×30px`, `border-radius: 9px` | Sidebar toggle & window controls (`.app-bar-window-close:hover` uses `--st-danger-soft` / `--st-danger`) |
| `.app-bar-mode` (`globals.css`) | `height: 22px`, `rounded-full`, `11px` `font-weight: 600` | Session capsule permission pill (`--default` uses `--st-ok*`, `--full_access` uses `--st-warn*`) |
| `.monaw-message-button` (`message-dialog.css`) | `min-height: 2rem`, `border-radius: 0.5rem`, `0.75rem` | Native modal buttons (`--primary`, `--danger`, default secondary) |

### 6.3 Button Ordering & Sizing Rules

- In footers and confirmation dialogs, place **Cancel / Ghost / Secondary** on the left and **Primary / Danger** on the right.
- Always use `type="button"` unless the button intentionally submits a `<form>`.
- Icon-only buttons **must** include `aria-label` (and `title` when helpful).

---

## 7. Component Library Catalog

### 7.1 Configuration & Panel Primitives ([`Panel.tsx`](frontend/src/components/ui/Panel.tsx))

| Component | Purpose | Key Props & Behavior |
| --- | --- | --- |
| `PageHeader` | Top title + optional blurb + optional right action | Uses `.st-page-title`, `.st-page-desc`. Reserves `pr-9` inside `.settings-shell` for the modal close button. |
| `SettingsCard` | Bordered container (`.st-card`) for related settings | `title`, `description`, `action`, `footnote`. Automatically manages alignment of raw `.st-row` descendants. |
| `SettingRow` | Label/description on left, control on right | `wide` (50% control width), `statusDescription` (keeps >90-char live status inline), `descriptionInInfo` (forces tooltip). |
| `StackedRow` | Label/description/action above a full-width control | Use for textareas, folder pickers, editable lists, or multi-control blocks. |
| `Switch` / `SwitchRow` | Accessible `<input type="checkbox" className="sr-only">` + `.st-switch` | Track `2.125rem × 1.25rem` (`34×20px`), thumb `0.75rem` (`12px`). `aria-label` carries the short label only. |
| `NumberRow` | Numeric input + aligned unit label | `.st-input-number` (`6.5rem` wide, right-aligned) + `min-w-[10ch]` unit. |
| `FlagToggle` | Compact label + `Switch` pair | Use inside dense flag grids, not as a replacement for `SwitchRow`. |
| `ChoiceCard` | Selectable radio-like card (`.st-choice`) | Sets `aria-pressed={selected}` and optional `data-tone="danger"`. Long explanation sits in top-right `InfoTip`. |
| `Badge` | Status chip (`.st-badge`) | `tone`: `'neutral' \| 'ok' \| 'warn' \| 'danger' \| 'accent'`. Always `rounded-full` at `11px`. |
| `Note` | Inline callout banner (`.st-note`) | `tone`: `'neutral' \| 'info' \| 'warn' \| 'danger'`, optional `icon`. Use **at most one per card**. Wrap in `<div role="alert">` for errors. |
| `ReadOnlyValue` | Label + `.st-code` value | Use for filesystem paths and non-editable identifiers. |

### 7.2 Form & Selection Controls

- **Text & Search Inputs**: `.st-input` (`--st-surface-sunken`, `1px solid var(--st-border-strong)`, `rounded-lg`, `0.75rem`, focus ring `--st-focus`). Add `.st-search` (`padding-left: 2rem`) when paired with a leading `13px` `Search` icon (`left-2.5 top-1/2 -translate-y-1/2`).
- **Steppers**: `.st-stepper` + `.st-stepper-input` (`3.5rem` centered).
- **File Inputs**: `.st-skill-file` styles `::file-selector-button` to match `.st-btn-secondary`.
- **`Dropdown` ([`Dropdown.tsx`](frontend/src/components/Dropdown.tsx))**:
  - **Never** use a raw `<select>` for UI pickers—use `Dropdown`.
  - Renders its `.st-menu` listbox into `.theme-dark, .theme-light` via `createPortal` so theme CSS variables apply while escaping `overflow: hidden` ancestors.
  - Supports `size="sm" | "md"`, `searchable`, `group` headers (`.st-menu-group`), `shortLabel` for tight triggers, full keyboard navigation (`ArrowUp`/`ArrowDown`/`Home`/`End`/typeahead), and `Escape` isolation.

### 7.3 Navigation & Segmented Controls

- **Settings Left Nav (`.st-nav-item`)**: Full-width button (`0.4375rem 0.625rem`, `0.75rem`, `font-weight: 500`), marks current page with `aria-current="page"`. Scroll container uses `.st-scroll-hidden`.
- **Segmented Control (`.st-segment` + `.st-segment-item`)**:
  - Track: `height: var(--control-height)`, `rounded-[0.625rem]`, `--st-surface-sunken`, `padding: 0.125rem`.
  - Active item: `--st-surface` background, `--st-text`, subtle `0 1px 2px rgba(0, 0, 0, 0.18)` shadow.
  - Accessibility: Use `role="tablist"` + `role="tab"` + `aria-selected` + Arrow-key switching for tab panels (like Tools: Skills/MCP/Plugins); use `role="group"` + `type="button"` + `aria-pressed` for view filters (like Activity log: Runs/Errors/Backend log/Support report).

### 7.4 Modals, Dialogs & Confirmations

- **Full-Window Modal Overlays**: `.st-overlay fixed inset-0 below-app-bar z-50 flex items-center justify-center p-4`. `.below-app-bar` (`top: var(--app-bar-height)`) keeps window controls and the drag region usable while a modal is open. Always attach `useModalFocus`.
- **Confirmation Dialogs (`MessageDialog`)**: Native `<dialog>` (`max-w: 28rem`, `1.25rem` padding, `1rem` radius). Focus starts on `Cancel` when `cancelLabel` is provided.
- **Inline Confirmations (`InlineConfirm`)**: Used in narrow lists (sidebar chats, scheduled tasks). Focus starts on `Cancel`; `Escape` cancels and stops propagation.

### 7.5 Activity Log & Telemetry Components (`ObservabilityPanel.tsx`, `settings.css`)

- **Overview KPI Strip**: `.st-obs-stat-grid` (2 columns mobile, 4 columns `>= 640px`) inside a `SettingsCard` with hairline cell borders and `tabular-nums`.
- **Collapsible Disclosures**: `<details className="st-obs-disclosure">` hides native marker (`::-webkit-details-marker { display: none }`) and rotates `.st-obs-disclosure-chevron` `180deg` when `[open]`.
- **Selectable List Rows**: `.st-obs-row` (`padding: 0.625rem 0.875rem`, hairline top border between rows, hover `--st-surface-hover`, `aria-selected="true"` uses `--st-accent-soft` + `inset 2px 0 0 var(--st-accent)`).
- **Vertical Event Timeline**: `.st-obs-timeline` + `.st-obs-timeline-item` (`data-tone="error"` highlights the timeline dot in `--st-warn`).
- **Virtualization Thresholds**: Virtualize lists above `40` messages in `ChatWindow` and `60` runs/errors in `ObservabilityPanel`.

---

## 8. Scrollbars

All scrollbars are defined centrally in [`frontend/electron/scrollbars.css`](frontend/electron/scrollbars.css) (imported by `globals.css` and `message-dialog.css`).

- **Default App Scrollbars**:
  - `8px × 8px` hit target with transparent track and corner, hidden scrollbar stepper buttons.
  - Thumb is a `999px` pill with `2px` transparent border + `background-clip: padding-box` (producing a crisp **4 px floating pill**).
  - Opacity progression: resting (`--st-scrollbar`: `0.08` dark / `0.10` light) → container hover (`--st-scrollbar-hover`: `0.20` / `0.22`) → thumb hover/active (`--st-scrollbar-active`: `0.34` / `0.36`).
  - Firefox compatibility (`scrollbar-width: thin`) is scoped inside `@supports (-moz-appearance: none)` so Chromium/Electron never overrides custom `::-webkit-scrollbar` pill styling with opaque OS bars.
- **Utility Classes**:
  - `.st-scroll` / `.settings-scroll-area`: Adds `scrollbar-gutter: stable` so switching between short and long pages does not shift horizontal layout.
  - `.st-scroll-hidden`: Completely hides the scrollbar (`display: none`, `scrollbar-width: none`) while preserving scrollability (used on the Settings left `<nav>`).
  - `.sidebar-scroll`: `6px` scrollbar with `--monaw-scrollbar-thumb: transparent` at rest, revealing only on container hover.
- **Accessibility**: `@media (prefers-contrast: more)` raises thumb contrast to `--st-text-muted` / `--st-text`; `@media (forced-colors: active)` restores `scrollbar-color: auto` and `ButtonText`.

---

## 9. Icons, Motion, Accessibility & Copy

### 9.1 Icons (`lucide-react`)

| Context | Size (`size` prop) | Notes |
| --- | --- | --- |
| App bar window & sidebar icons | `15` (Maximize/Restore `12`) | `aria-label` on buttons |
| Inline small indicators, chevrons, composer tools, `Note` / `InfoTip` / `NavItem` | `12–14` | Add `aria-hidden="true"` when decorative or next to text |
| Sidebar rail buttons | `15` (list item icons `13`, folder/chevron `11`) | — |
| Empty state / large card icon (e.g., Plugins placeholder) | `20` | `--st-text-faint` |

### 9.2 Motion & Reduced Motion

- Standard micro-transitions: `120ms–180ms` (`ease` or `ease-out`) on background, border, color, and transform.
- Sidebar collapse/expand: `220ms ease-in-out` on `.app-bar`; `180ms ease-out` on sidebar panels.
- **Reduced Motion**: Always provide `@media (prefers-reduced-motion: reduce)` fallbacks (`.app-bar` disables animations/transitions; `.startup-progress-fill` disables width transition).

### 9.3 Keyboard & Screen Reader Rules

- Focus ring standard: `box-shadow: 0 0 0 3px var(--st-focus)` (or `outline: 2px solid var(--st-accent-text); outline-offset: 2px–3px` on compact controls).
- Use `role="status"` + `aria-live="polite"` for non-blocking feedback (saved notices, update status, connection status) and `role="alert"` for errors.
- Avoid `window.alert` / `window.confirm`—always use `MessageDialog` or `InlineConfirm`.

### 9.4 Copy Conventions ([`settingsCopy.ts`](frontend/src/features/settings/settingsCopy.ts))

- Write in **plain, concrete English**—no internal architecture jargon (`durable`, `broker`, `harness`, `SQLite`) in user-facing labels.
- Sentence case for all headings, labels, buttons, tabs, and options (e.g., `Total runs`, `Delete Data`, `Check for updates`).
- Centralize non-trivial settings descriptions in `settingsCopy.ts` so every setting explains *what it does* and, if risky, *what happens when changed* in one concise sentence.

---

## 10. Pre-Ship UI Checklist

Before finalizing any UI change:

1. **Theme parity**: Verified visually/structurally in both `.theme-dark` and `.theme-light` using `--st-*` tokens (no hardcoded dark-only `bg-white/*` or `text-neutral-*` classes in new UI).
2. **Alignment**: Settings rows use `Panel.tsx` primitives or `.st-row` inside `SettingsCard` without manual `items-start`/`items-center` overrides; compact rows center, tall rows (`>= 72px`) top-align.
3. **Responsive bounds**: Layout fits cleanly inside `SettingsModal` (`max-w-[46rem]`) and at the minimum window width (`800px`) without horizontal overflow or clipped controls.
4. **Controls & portals**: Pickers use `Dropdown` (portal to theme root); modals use `useModalFocus` or `MessageDialog`; nested popovers stop `Escape` propagation.
5. **Accessibility**: Keyboard navigation, focus-visible rings, `aria-label` on icon buttons, `Cancel` focused by default in destructive confirms, and `tabular-nums` on dynamic metrics.
6. **Tests & build**: Add or update Vitest component tests (`*.test.tsx`) and run `npm run verify` inside `frontend/` (Vitest + Electron tests + `tsc --noEmit` + `vite build`) until everything passes cleanly.
