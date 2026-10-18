// mcp__Chrome-dev-tools__ is the name older saved MCP entries used.
const CHROME_DEVTOOLS_PREFIXES = ['mcp__chrome-devtools__', 'mcp__Chrome-dev-tools__']

// Chrome DevTools MCP tool names, in the words a user would use.
const BROWSER_ACTIONS: Record<string, string> = {
  navigate_page: 'open page',
  new_page: 'open new tab',
  select_page: 'switch tab',
  list_pages: 'list tabs',
  close_page: 'close tab',
  take_snapshot: 'read page',
  take_screenshot: 'take screenshot',
  click: 'click',
  hover: 'hover',
  drag: 'drag',
  fill: 'type',
  fill_form: 'fill form',
  press_key: 'press key',
  upload_file: 'upload file',
  handle_dialog: 'answer dialog',
  wait_for: 'wait for page',
  evaluate_script: 'run page script',
  list_console_messages: 'read console',
  get_console_message: 'read console',
  list_network_requests: 'read network requests',
  get_network_request: 'read network request',
  emulate: 'emulate device',
  resize_page: 'resize window',
}

function humanize(name: string): string {
  return name.replace(/_+/g, ' ').trim()
}

export function chromeDevToolsAction(tool: string): string | null {
  const prefix = CHROME_DEVTOOLS_PREFIXES.find((candidate) => tool.startsWith(candidate))
  if (!prefix) return null
  const name = tool.slice(prefix.length)
  return BROWSER_ACTIONS[name] ?? humanize(name)
}

/** browser_ also covers browser_fetch and the removed browser_* tools in older saved conversations. */
export function isBrowserTool(tool: string): boolean {
  return tool.startsWith('browser_') || chromeDevToolsAction(tool) !== null
}

/** The tool name shown in chat; raw names such as mcp__chrome-devtools__click stay internal. */
export function toolDisplayName(tool: string): string {
  const browserAction = chromeDevToolsAction(tool)
  if (browserAction) return `browser · ${browserAction}`
  const mcp = /^mcp__(.+?)__(.+)$/.exec(tool)
  if (mcp) return `${mcp[1]} · ${humanize(mcp[2])}`
  return tool
}
