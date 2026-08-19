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
    label: 'Model',
    blurb: 'Pick the AI model that powers the agent, and cap how long a single request may run.',
    keywords: 'provider openai gpt google gemini reasoning effort vision images speech voice whisper timeout limits',
  },
  apiKeys: {
    label: 'Connections',
    blurb: 'Store the API keys and tokens the agent needs to reach outside services.',
    keywords: 'api key token secret credential openai google tavily telegram search',
  },
  identity: {
    label: 'Identity',
    blurb: 'Tell the agent what to call itself, what to call you, and how to write.',
    keywords: 'name nickname tone style persona about me agents.md custom instructions',
  },
  memory: {
    label: 'Memory',
    blurb: 'Decide what the agent is allowed to remember between conversations, and review what it already knows.',
    keywords: 'remember recall facts preferences learning retrieval curation',
  },
  skills: {
    label: 'Skills',
    blurb: 'Turn the agent’s capabilities on or off. A skill it cannot use is a thing it cannot do.',
    keywords: 'capabilities tools browser computer use memory scheduling background check',
  },
  browser: {
    label: 'Browser',
    blurb: 'Choose which browser the agent drives and where it saves screenshots and downloads.',
    keywords: 'chrome chromium headless cdp profile downloads screenshots automation domains',
  },
  mcp: {
    label: 'MCP servers',
    blurb: 'Connect external tool servers that speak the Model Context Protocol.',
    keywords: 'model context protocol stdio http server tools integration',
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
    keywords: 'isolation docker wsl container network cpu memory timeout shell exec',
  },
} as const satisfies Record<string, SettingsPageCopy>

export type SettingsPageId = keyof typeof PAGE_COPY

/* ------------------------------------------------------------------ model --- */

export const MODEL_COPY = {
  provider: 'The company whose models the agent uses. Each provider needs its own API key, set up under Connections.',
  model: 'Bigger models reason better and cost more per message. Smaller "mini"/"flash" models are faster and cheaper.',
  reasoningEffort: 'How long the model is allowed to think before it answers. Higher settings give better results on hard problems but are slower and cost more.',
  vision: 'Screenshots and image attachments go straight to the model you picked above, which reads them natively. No separate vision model is needed.',
  speechEngine: 'Local runs on this computer and works offline after a one-time download. Cloud sends your audio to Google and needs a Google API key.',
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
  customInstructions: 'Standing instructions applied to every conversation, stored as a file in your workspace. Good for project conventions and things you keep repeating.',
  customInstructionsLimit: 'These guide behaviour. They cannot override safety rules or permission checks.',
} as const

/* ----------------------------------------------------------------- skills --- */

export const SKILLS_COPY = {
  intro: 'A skill is a group of tools. Turning one off removes those tools from the agent entirely, so it cannot use them even if you ask.',
  unavailable: 'This skill cannot be turned on until the missing backend dependency is installed.',
} as const

/* ---------------------------------------------------------------- browser --- */

export const BROWSER_COPY = {
  mode: 'Managed uses a clean browser Monaw controls — safest, but not signed in to anything. System uses your own Chrome, so the agent inherits your logins.',
  systemConnection: 'How to reach your own Chrome. Attach only connects to a Chrome you already started with remote debugging enabled.',
  cdpUrl: 'The debugging address of your Chrome. Only change this if you started Chrome on a non-default port.',
  chromeProfile: 'Which of your Chrome profiles to use when driving your own browser.',
  allowedDomains: 'If set, the agent may only visit these domains. Leave empty to allow any site.',
  systemFallback: 'If the managed browser fails to start, fall back to your own Chrome instead of giving up.',
  headless: 'Run the browser invisibly. Faster, but you cannot watch what the agent is doing.',
  keepAlive: 'Leave the browser open between requests so logins and tabs survive. Turn off to start clean every time.',
  advanced: 'These affect how the agent reads a page. The defaults are right for almost everyone — change them only if pages are being misread.',
  domEngine: 'How the page structure is analysed. Auto picks the best available engine.',
  paintOrder: 'Ignore elements that are visually covered by something else, so the agent does not click hidden buttons.',
  crossOrigin: 'Let the agent look inside embedded frames from other sites, such as payment or sign-in widgets.',
  maxIframes: 'How many embedded frames to read per page. Higher is slower.',
  frameDepth: 'How deep to follow frames nested inside other frames.',
  outputWorkspace: 'Where files the agent produces end up, so you can find them in Explorer.',
  managedProfile: 'Internal storage for the managed browser. Read-only.',
  traces: 'Internal recordings of browser sessions, used for diagnostics. Read-only.',
  resetSession: 'Close the current browser session and start fresh. Use this if the browser is stuck.',
} as const

/* -------------------------------------------------------------------- mcp --- */

export const MCP_COPY = {
  intro: 'MCP servers are small programs that expose extra tools to the agent — a file system, a database, a browser, your own scripts.',
  bridge: 'Master switch. When off, no MCP server is contacted and none of their tools are available.',
  transportStdio: 'Monaw starts the server as a local program and talks to it over its input and output.',
  transportHttp: 'Monaw connects to a server that is already running at a URL.',
  command: 'The program to run, for example npx. Arguments go in the list below.',
  url: 'The address of the running MCP server.',
  workingDir: 'Folder the command runs in. Leave empty to use the default.',
  startupTimeout: 'How long to wait for the server to come up before treating it as failed.',
  callTimeout: 'How long to wait for one tool call to finish.',
  reconnect: 'Automatically restart the connection if the server stops responding.',
  allowList: 'Only expose these tools to the agent. Leave empty to expose everything the server offers.',
  plaintextWarning: 'Environment variables and HTTP headers for MCP servers are stored as plain text in the settings file. Do not put high-value secrets here.',
} as const

/* ------------------------------------------------------------ permissions --- */

export const PERMISSION_MODE_COPY = {
  default: {
    title: 'Ask before changing things',
    summary: 'The agent can read freely, but stops for your approval before it writes, deletes, or launches anything.',
    recommendation: 'Recommended',
  },
  full_access: {
    title: 'Act without asking',
    summary: 'The agent reads and writes without stopping. It will not interrupt you, and it will not warn you before changing files.',
    recommendation: 'Higher risk',
  },
  custom: {
    title: 'Custom rules',
    summary: 'You decide exactly which actions need approval, and set per-folder and per-app exceptions below.',
    recommendation: 'Advanced',
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
    description: 'When off, the agent refuses every delete, even one you approve. Leave off unless you genuinely need it.',
  },
  dangerous: {
    label: 'Always confirm high-risk actions',
    description: 'Keeps a confirmation prompt on things like wiping a folder or changing system settings, whatever the other switches say.',
  },
  screenFallback: {
    label: 'Allow screen-pixel control',
    description: 'Lets the agent click by looking at raw pixels when it cannot read an app’s controls properly. More capable, but it can click the wrong thing.',
  },
} as const

export const OVERRIDE_COPY = {
  paths: 'Grant or restrict the agent on specific folders. Rules here override the profile above.',
  blockedRoots: 'Folders the agent may never touch, no matter what any other rule says.',
  apps: 'Grant or restrict the agent on specific applications, matched by their executable path.',
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
  enabled: 'Master switch. When off, commands run directly on your machine with no isolation.',
  mode: 'How strictly commands are isolated.',
  modeHelp: {
    off: 'The agent cannot run shell commands at all.',
    auto: 'Use the strongest isolation available, and fall back if it is not installed.',
    enforce: 'Refuse to run anything unless strong isolation (Docker) is available.',
    host: 'Run directly on your machine, asking for approval first.',
    docker: 'Always run inside a Docker container.',
    local_restricted: 'Run on your machine with limits that are best-effort only.',
  },
  network: 'Whether commands running in the sandbox can reach the internet.',
  networkHelp: {
    deny: 'No network access. Safest default.',
    allow_with_approval: 'Network access only after you approve it.',
    allow: 'Unrestricted network access.',
  },
  writeStrategy: 'What happens to files a sandboxed command creates.',
  writeStrategyHelp: {
    discard: 'Throw the files away when the command finishes.',
    copy_out: 'Copy new files back into your workspace afterwards.',
    direct_rw: 'Write straight into your real folders. Least isolated.',
  },
  backends: 'Isolation engines detected on this computer. Docker gives the strongest guarantees.',
  dockerImage: 'The container image commands run in. Pin it with a digest for reproducible results.',
  dockerReadOnly: 'Make the container file system read-only except for the workspace, so a command cannot alter the image.',
  resources: 'Hard ceilings for a single sandboxed command, so a runaway process cannot exhaust your machine.',
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
