/**
 * Plain-English copy for the settings surface.
 *
 * Every control that is not self-evident gets one sentence saying what it does
 * and, where it matters, what happens if you get it wrong. Keeping the strings
 * here (rather than inline) makes it easy to review the wording as a whole and
 * to keep the tone consistent across panels.
 */

export interface SettingsPageCopy {
  label: string
  /** One line under the page title. */
  blurb: string
  /** Extra words that should match this page when searching. */
  keywords: string
}

export const PAGE_COPY = {
  model: {
    label: 'Voice',
    blurb: 'Configure voice input.',
    keywords: 'speech voice whisper transcription',
  },
  apiKeys: {
    label: 'Connections',
    blurb: 'Connect a ChatGPT account or save API keys and tokens for outside services.',
    keywords: 'api key token secret credential openai google tavily telegram search',
  },
  identity: {
    label: 'Identity',
    blurb: 'Tell the agent what to call itself, what to call you, and how to write.',
    keywords: 'name nickname tone style persona about me agents.md custom instructions',
  },
  tools: {
    label: 'Tools',
    blurb: 'Add your own skills, MCP connections, and plugins. Built-in tools are always on.',
    keywords: 'skills capabilities tools browser computer use memory scheduling web search mcp model context protocol stdio http server integration plugins extensions',
  },
  browser: {
    label: 'Browser',
    blurb: 'Choose the Chrome profile the agent drives, which sites it may open, and where it saves screenshots.',
    keywords: 'chrome devtools profile connection allow prompt screenshots automation domains reconnect',
  },
  observability: {
    label: 'Activity log',
    blurb: 'Inspect what the agent actually did: traces, token usage, and errors.',
    keywords: 'observability traces logs tokens usage errors debugging replay',
  },
  permissions: {
    label: 'Permissions',
    blurb: 'Control when the agent must stop and ask you before it changes something.',
    keywords: 'approval confirm safety allow deny folders apps delete risk',
  },
  sandbox: {
    label: 'Sandbox',
    blurb: 'Decide how isolated commands are when the agent runs them on this machine.',
    keywords: 'isolation docker container network cpu memory timeout shell exec',
  },
} as const satisfies Record<string, SettingsPageCopy>

export type SettingsPageId = keyof typeof PAGE_COPY

/* ------------------------------------------------------------------ model --- */

export const MODEL_COPY = {
  provider: 'Choose OpenAI account, OpenAI API, or Google. Connect the selected provider under Connections.',
  model: 'Bigger models reason better and cost more per message. Smaller "mini"/"flash" models are faster and cheaper.',
  reasoningEffort: 'How long the model is allowed to think before it answers. Higher settings give better results on hard problems but are slower and cost more.',
  vision: 'Screenshots and image attachments go straight to the model you picked above, which reads them natively. No separate vision model is needed.',
  speechEngine: 'Local needs a one-time download. Cloud sends audio to Google and requires an API key.',
  speechLocalIntro: 'Voice input needs the Whisper model downloaded once (about 142 MB). It then runs entirely on this machine.',
  runtimeLimits: 'Safety brakes for a single request. If the agent gets stuck in a loop, these stop it instead of letting it run forever.',
  maxIterations: 'How many tool calls the agent may make while answering one message.',
  maxTurnSeconds: 'The wall-clock budget for answering one message, across all tool calls.',
  maxLlmCallSeconds: 'How long to wait for a single reply from the model before giving up.',
} as const

/* --------------------------------------------------------------- identity --- */

export const IDENTITY_COPY = {
  agentName: 'What the agent calls itself in replies and in the sidebar.',
  userName: 'What the agent should call you.',
  userIdentity: 'Background the agent should keep in mind every conversation: your role, what you are working on, tools you use.',
  communicationStyle: 'How you want replies written: length, tone, language, formatting, how blunt to be.',
  customInstructions: 'Applied to every conversation and saved in your workspace.',
  customInstructionsLimit: 'Permission checks still apply.',
  memory: 'Let the agent remember your preferences and project context between conversations.',
} as const

/* ----------------------------------------------------------------- skills --- */

export const SKILLS_COPY = {
  intro: 'Built-in skills such as browser use, computer use, web search and scheduling are always on, so they are not listed here.',
  title: 'Your skills',
  empty: 'No skills added yet. Ask Monaw to create one, and it will appear here.',
  unavailable: 'This skill cannot be turned on until the missing backend dependency is installed.',
} as const

/* ---------------------------------------------------------------- browser --- */

export const BROWSER_COPY = {
  usesYourProfile:
    'Monaw drives your own Chrome profile, with your saved logins and tabs, through Chrome DevTools. It opens Chrome when a browser tool first needs it.',
  chromeProfile: 'The profile Monaw opens when it launches your Chrome.',
  autoApprove: 'On Windows, clicks Allow on Chrome’s “Allow remote debugging?” prompt while Monaw connects.',
  allowedDomains: 'Leave empty to allow all sites. Applies to pages the browser opens and to page fetches.',
  screenshots: 'Where browser screenshots are saved. Your Chrome profile stays in Chrome’s own folder.',
  chromeMissing: 'Google Chrome was not found. Install Chrome (version 144 or later) to use browser automation.',
  reconnect: 'Restart Monaw’s connection to Chrome. Your Chrome and its tabs stay open.',
} as const

/* -------------------------------------------------------------------- mcp --- */

export const MCP_COPY = {
  intro: 'Optional connections to external tools. Monaw’s built-in skills work without adding a server.',
  bridge: 'Master switch. When off, no MCP server is contacted and none of their tools are available.',
  transportStdio: 'Monaw starts the server as a local program and talks to it over its input and output.',
  transportHttp: 'Monaw connects to a server that is already running at a URL.',
  command: 'The program to run, for example npx. Arguments go in the list below.',
  url: 'The address of the running MCP server.',
  workingDir: 'Folder the command runs in. Leave empty to use the default.',
  startupTimeout: 'How long to wait for the server to come up before treating it as failed.',
  callTimeout: 'How long to wait for one tool call to finish.',
  reconnect: 'Automatically restart the connection if the server stops responding.',
  allowList: 'Leave empty to expose all server tools.',
  trustedTools: 'These tools run without an approval prompt.',
  riskOverrides: 'Changes risk labels; approval rules still apply.',
  plaintextWarning: 'Headers and environment variables are saved as plain text.',
} as const

/* ------------------------------------------------------------ permissions --- */

export const PERMISSION_MODE_COPY = {
  default: {
    title: 'Default',
    summary: 'Ask for changes.',
    description: 'Reading, browsing, clicks, typing and opening apps run automatically. File changes and commands ask for approval.',
    recommendation: 'Recommended',
    rules: ['Automatic: read files, browse, click, type and open apps.', 'Ask you: create, edit or delete files, run commands or stop processes.', 'Protected targets stay blocked.'],
  },
  full_access: {
    title: 'Full Access',
    summary: 'Run automatically.',
    description: 'File changes and deletion, browser actions, apps, integrations and commands run without approval prompts. Protected targets remain blocked.',
    recommendation: 'Higher risk',
    rules: ['Automatic: file changes and deletion, browser actions, apps, integrations and commands.', 'No action approval prompts.', 'Protected targets stay blocked.'],
  },
  auto_review: {
    title: 'Auto Review',
    summary: 'Review changes for me.',
    description: 'Routine actions run automatically. A separate AI review checks changes and commands against your conversation, approves authorized actions, denies clear violations, and asks you when uncertain or unavailable.',
    recommendation: 'AI review',
    rules: ['Automatic: read files, browse, click, type and open apps.', 'AI reviews: file changes and deletion, commands and stopping processes.', 'Denied actions cannot be retried unchanged in the same turn; repeated denials pause it.', 'Ask you: review is uncertain or unavailable.'],
  },
  custom: {
    title: 'Custom',
    summary: 'Use my config file.',
    description: 'Edit permissions.custom_profile in the config file for action approvals, folder rules and app rules. There are no setup switches here.',
    recommendation: 'Advanced',
    rules: ['Uses the rules in permissions.custom_profile.', 'Edit approvals, folder access and app access in the config file.', 'Save the file; subsequent tool calls use the new rules.'],
  },
} as const

export const CONFIRMATION_COPY: Record<string, { label: string; description: string }> = {
  mutate: {
    label: 'Creating or editing files',
    description: 'Ask before the agent writes to a file or changes its contents.',
  },
  delete: {
    label: 'Deleting files',
    description: 'Ask before the agent removes anything. Deletion is not undoable.',
  },
  launch_app: {
    label: 'Opening applications',
    description: 'Ask before the agent starts a program on your computer.',
  },
  click: {
    label: 'Clicking on screen',
    description: 'Ask before the agent clicks buttons in other applications.',
  },
  type: {
    label: 'Typing on screen',
    description: 'Ask before the agent types into other applications.',
  },
}

export const RISK_COPY = {
  allowDelete: {
    label: 'Allow deleting files at all',
    description: 'When off, all deletion is blocked, even with approval.',
  },
  dangerous: {
    label: 'Always confirm high-risk actions',
    description: 'Overrides other rules for actions such as wiping folders or changing system settings.',
  },
  screenFallback: {
    label: 'Allow screen-pixel control',
    description: 'Used when app controls cannot be read. Coordinates can be less reliable.',
  },
} as const

export const OVERRIDE_COPY = {
  paths: 'Overrides the approval profile for these folders.',
  blockedRoots: 'Always blocked, regardless of other rules.',
  apps: 'Matches apps by executable path.',
  pathFlags: {
    read: 'Read files in this folder',
    write: 'Create and edit files here',
    delete: 'Delete files here',
    launch: 'Run programs from here',
    require_confirmation: 'Ask me first, every time',
    enabled: 'Rule is active',
  },
  appFlags: {
    launch_allowed: 'Start this app',
    uia_allowed: 'Read and control its window',
    screen_fallback_allowed: 'Click by pixel if needed',
    require_confirmation: 'Ask me first, every time',
    enabled: 'Rule is active',
  },
} as const

/* --------------------------------------------------------------- sandbox --- */

export const SANDBOX_COPY = {
  intro: 'When the agent runs a shell command, the sandbox decides how much of your machine that command can see and touch.',
  enabled: 'Master switch. When off, shell commands are blocked.',
  mode: 'How strictly commands are isolated.',
  modeHelp: {
    off: 'The agent cannot run shell commands at all.',
    auto: 'Uses Docker for supported commands; otherwise runs on your machine. Approvals follow your permission mode.',
    enforce: 'Requires Docker. Commands that need your machine directly are blocked.',
    host: 'Runs on your machine. Approvals follow your permission mode; files and network are not isolated.',
    docker: 'Requires Docker. Unsupported commands are blocked.',
    local_restricted: 'Run on your machine with limits that are best-effort only.',
  },
  network: 'Applies to Docker containers. Host commands use your machine’s network.',
  networkHelp: {
    deny: 'Docker containers cannot access the network. Host commands are not isolated.',
    allow_with_approval: 'Allows container network when the command is permitted by your approval mode.',
    allow: 'Docker containers can access the network.',
  },
  writeStrategy: 'What happens to files a sandboxed command creates.',
  writeStrategyHelp: {
    discard: 'Throw the files away when the command finishes.',
    copy_out: 'Copy new files back into your workspace afterwards.',
    direct_rw: 'Write straight into your real folders. Least isolated.',
  },
  backends: 'Isolation engines detected on this computer. Docker gives the strongest guarantees.',
  dockerImage: 'Pin a digest to keep the image version fixed.',
  dockerReadOnly: 'The workspace remains writable.',
  resources: 'Docker enforces these limits. Host execution enforces timeout and output limits; CPU, memory and process limits require Docker.',
  resourceHelp: {
    timeout_seconds: 'Kill the command after this many seconds.',
    memory_mb: 'Maximum memory the command may use.',
    cpus: 'How many CPU cores the command may use.',
    pids: 'Maximum number of processes it may spawn, which blocks fork bombs.',
  },
  enforceNeedsDocker: 'Enforce mode needs Docker, and Docker was not detected. Commands will be blocked until it is available.',
} as const

/* ---------------------------------------------------------------- footer --- */

export const FOOTER_COPY = {
  clean: 'No unsaved changes.',
  dirty: 'You have unsaved changes.',
  saved: 'Settings saved.',
  duplicateMcp: 'Two MCP servers share a name. Rename one before saving.',
  discardConfirm: 'Discard your unsaved changes?',
} as const
