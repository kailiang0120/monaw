"""Reviewed Composio actions, independent of the upstream dynamic catalog.

Protocol source and license attribution: docs/composio.md.
Adding a provider requires an explicit review of its actions and account selector.
"""

from .models import Capability, ProviderManifest


def text_field(description: str, maximum: int = 2048) -> dict:
    """Bound opaque identifiers and query strings before sending them upstream."""
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": maximum,
        "description": description,
    }


def read_action(
    identifier: str, description: str, properties=None, required=()
) -> Capability:
    """Define a closed input schema for one reviewed, read-only action."""
    return Capability(
        id=identifier,
        description=description,
        action="read",
        required_scopes={"composio:bound"},
        parameters={
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    )


PAGE_SIZE = {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}
MESSAGE_ID = text_field("Message ID returned by this account")
NEXT_LINK = text_field(
    "Opaque pagination token returned by the previous Outlook page", 8192
)

MANIFESTS = (
    ProviderManifest(
        id="gmail",
        name="Gmail",
        # Composio support confirmed this scope is not approved on 2026-11-27.
        setup_notice="Gmail sign-in is unavailable: Composio's managed app is not approved for read-only Gmail access. A custom Google OAuth app is required; Monaw does not yet support that setup.",
        connection_method="composio",
        category="email",
        description="Find and read Gmail messages.",
        version="composio-1",
        auth_methods={"api_key"},
        capabilities=(
            read_action("get_profile", "Read the connected Gmail mailbox profile."),
            read_action(
                "fetch_emails",
                "Search Gmail messages using Gmail query syntax; returns summaries.",
                {
                    "query": text_field(
                        "Gmail search, for example is:unread newer_than:7d"
                    ),
                    "maxResults": PAGE_SIZE,
                    "pageToken": text_field(
                        "nextPageToken from the previous page", 8192
                    ),
                },
            ),
            read_action(
                "get_message",
                "Read one Gmail message and its body.",
                {"messageId": MESSAGE_ID},
                ("messageId",),
            ),
        ),
    ),
    ProviderManifest(
        id="outlook",
        name="Outlook",
        connection_method="composio",
        category="email",
        description="Read Outlook mail and browse folders.",
        version="composio-1",
        auth_methods={"api_key"},
        capabilities=(
            read_action("get_profile", "Read the connected Outlook account profile."),
            read_action(
                "list_mail_folders",
                "List Outlook mail folders, one bounded page at a time.",
                {
                    "top": PAGE_SIZE,
                    "nextLink": NEXT_LINK,
                },
            ),
            read_action(
                "list_messages",
                "List or filter Outlook messages using Microsoft Graph OData filters.",
                {
                    "mailFolderId": text_field(
                        "Mail folder ID or well-known name such as inbox"
                    ),
                    "top": PAGE_SIZE,
                    "filter": text_field("OData filter, for example isRead eq false"),
                    "nextLink": NEXT_LINK,
                },
            ),
            read_action(
                "get_message",
                "Read one Outlook message with its plain-text body.",
                {"messageId": MESSAGE_ID},
                ("messageId",),
            ),
        ),
    ),
)

# These are code-reviewed actions, not a live import of Composio's catalog.
TOOLS = {
    "gmail": {
        "get_profile": "GMAIL_GET_PROFILE",
        "fetch_emails": "GMAIL_FETCH_EMAILS",
        "get_message": "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
    },
    "outlook": {
        "get_profile": "OUTLOOK_GET_PROFILE",
        "list_mail_folders": "OUTLOOK_LIST_MAIL_FOLDERS",
        "list_messages": "OUTLOOK_LIST_MESSAGES",
        "get_message": "OUTLOOK_GET_MESSAGE",
    },
}
SCOPES = {
    "gmail": [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/gmail.readonly",
    ],
    "outlook": ["User.Read", "offline_access", "Mail.Read"],
}
FIXED_ARGUMENTS: dict[tuple[str, str], dict] = {}
CURSOR = text_field("Continuation token from the previous page", 8192)
RESOURCE_ID = text_field("Resource ID returned by this connected account")
QUERY = text_field("Search terms or the provider's supported query syntax")


def app(identifier, name, category, description, scopes, actions):
    """Register a provider with closed native schemas and immutable execution defaults."""
    TOOLS[identifier] = {}
    SCOPES[identifier] = scopes
    capabilities = []
    for capability, slug, fixed in actions:
        TOOLS[identifier][capability.id] = slug
        FIXED_ARGUMENTS[identifier, capability.id] = fixed
        capabilities.append(capability)
    return ProviderManifest(
        id=identifier,
        name=name,
        connection_method="composio",
        category=category,
        description=description,
        version="composio-1",
        auth_methods={"api_key"},
        capabilities=tuple(capabilities),
    )


def operation(identifier, slug, description, properties=None, required=(), fixed=None):
    """Pair one public capability with its reviewed upstream tool and fixed parameters."""
    return read_action(identifier, description, properties, required), slug, fixed or {}


MANIFESTS += (
    app(
        "googlecalendar",
        "Google Calendar",
        "calendar",
        "Read calendars and upcoming events.",
        ["https://www.googleapis.com/auth/calendar.readonly"],
        (
            operation(
                "get_profile",
                "GOOGLECALENDAR_GET_CURRENT_USER",
                "Read the connected calendar identity.",
            ),
            operation(
                "list_calendars",
                "GOOGLECALENDAR_LIST_CALENDARS",
                "List accessible calendars.",
                {"max_results": PAGE_SIZE, "page_token": CURSOR},
            ),
            operation(
                "list_events",
                "GOOGLECALENDAR_EVENTS_LIST",
                "Read events in a calendar; use RFC3339 times with timezone offsets.",
                {
                    "calendarId": RESOURCE_ID,
                    "query": QUERY,
                    "timeMin": text_field("RFC3339 start with timezone", 64),
                    "timeMax": text_field("RFC3339 end with timezone", 64),
                    "maxResults": PAGE_SIZE,
                    "pageToken": CURSOR,
                },
                fixed={"singleEvents": True},
            ),
        ),
    ),
    app(
        "googledrive",
        "Google Drive",
        "files",
        "Find files and read Google Docs and Sheets.",
        ["https://www.googleapis.com/auth/drive.readonly"],
        (
            operation(
                "get_profile",
                "GOOGLEDRIVE_GET_ABOUT",
                "Read the connected Drive identity.",
                fixed={"fields": "user"},
            ),
            operation(
                "find_files",
                "GOOGLEDRIVE_FIND_FILE",
                "Find files, documents and spreadsheets using Drive search.",
                {"q": QUERY, "pageSize": PAGE_SIZE, "pageToken": CURSOR},
            ),
            operation(
                "get_file",
                "GOOGLEDRIVE_GET_FILE_METADATA",
                "Read file metadata by ID.",
                {"fileId": RESOURCE_ID},
                ("fileId",),
            ),
            operation(
                "get_document",
                "GOOGLEDRIVE_GET_DOCUMENT",
                "Read a Google document by ID.",
                {"document_id": RESOURCE_ID},
                ("document_id",),
            ),
            operation(
                "get_sheet_values",
                "GOOGLEDRIVE_GET_SPREADSHEET_VALUES",
                "Read a bounded A1 range of spreadsheet cells; request small ranges.",
                {
                    "spreadsheet_id": RESOURCE_ID,
                    "range": text_field("A1 cell range, e.g. Sheet1!A1:D50", 256),
                },
                ("spreadsheet_id", "range"),
            ),
        ),
    ),
    app(
        "one_drive",
        "OneDrive",
        "files",
        "Find files and browse folders.",
        ["User.Read", "offline_access", "Files.Read"],
        (
            operation(
                "get_profile",
                "ONE_DRIVE_GET_USER",
                "Read the connected OneDrive identity.",
                fixed={"user_id": "me"},
            ),
            operation(
                "list_files",
                "ONE_DRIVE_LIST_FOLDER_CHILDREN",
                "List files in your Drive root or a selected folder.",
                {"folder_item_id": RESOURCE_ID, "top": PAGE_SIZE, "page_token": CURSOR},
                fixed={"use_me_drive": True},
            ),
            operation(
                "search_files",
                "ONE_DRIVE_SEARCH_ITEMS",
                "Search your OneDrive files.",
                {"q": QUERY, "top": PAGE_SIZE, "page_token": CURSOR},
                ("q",),
            ),
            operation(
                "get_file",
                "ONE_DRIVE_GET_ITEM",
                "Read file metadata by ID.",
                {"item_id": RESOURCE_ID},
                ("item_id",),
            ),
        ),
    ),
    app(
        "notion",
        "Notion",
        "knowledge",
        "Search shared pages and read their content.",
        [],  # Notion grants are configured on the OAuth integration, not per-request scopes.
        (
            operation(
                "get_profile",
                "NOTION_GET_ABOUT_USER",
                "Read the authenticated Notion integration identity.",
                fixed={"user_id": "me"},
            ),
            operation(
                "search_pages",
                "NOTION_SEARCH_NOTION_PAGE",
                "Search pages shared with this Notion connection.",
                {"query": QUERY, "page_size": PAGE_SIZE, "start_cursor": CURSOR},
            ),
            operation(
                "get_page",
                "NOTION_RETRIEVE_PAGE",
                "Read page metadata and properties.",
                {"page_id": RESOURCE_ID},
                ("page_id",),
            ),
            operation(
                "get_blocks",
                "NOTION_FETCH_BLOCK_CONTENTS",
                "Read page or block content, one page at a time.",
                {
                    "block_id": RESOURCE_ID,
                    "page_size": PAGE_SIZE,
                    "start_cursor": CURSOR,
                },
                ("block_id",),
            ),
        ),
    ),
    app(
        "slack",
        "Slack",
        "messaging",
        "Search messages and read conversations.",
        [
            "search:read",
            "channels:read",
            "channels:history",
            "groups:read",
            "groups:history",
            "im:read",
            "im:history",
            "mpim:read",
            "mpim:history",
        ],
        (
            operation(
                "get_profile", "SLACK_TEST_AUTH", "Verify the Slack user and workspace."
            ),
            operation(
                "list_conversations",
                "SLACK_LIST_CONVERSATIONS",
                "List conversations accessible to the connected user.",
                {"limit": PAGE_SIZE, "cursor": CURSOR},
                fixed={"types": "public_channel,private_channel,im,mpim"},
            ),
            operation(
                "search_messages",
                "SLACK_SEARCH_MESSAGES",
                "Search Slack messages using Slack search syntax.",
                {
                    "query": QUERY,
                    "count": PAGE_SIZE,
                    "page": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ("query",),
                fixed={"auto_paginate": False},
            ),
            operation(
                "get_history",
                "SLACK_FETCH_CONVERSATION_HISTORY",
                "Read a bounded page of conversation messages.",
                {"channel": RESOURCE_ID, "limit": PAGE_SIZE, "cursor": CURSOR},
                ("channel",),
            ),
        ),
    ),
    app(
        "microsoft_teams",
        "Microsoft Teams",
        "messaging",
        "Browse teams, channels, and messages.",
        [
            "User.Read",
            "offline_access",
            "Team.ReadBasic.All",
            "Channel.ReadBasic.All",
            "ChannelMessage.Read.All",
        ],
        (
            operation(
                "get_profile",
                "MICROSOFT_TEAMS_GET_MY_PROFILE",
                "Read the connected Teams identity.",
                fixed={"user_id": "me"},
            ),
            operation(
                "list_teams",
                "MICROSOFT_TEAMS_TEAMS_LIST",
                "List teams accessible to this account.",
                {"top": PAGE_SIZE, "page_token": CURSOR},
            ),
            operation(
                "list_channels",
                "MICROSOFT_TEAMS_TEAMS_LIST_CHANNELS",
                "List channels in a team.",
                {"team_id": RESOURCE_ID, "page_token": CURSOR},
                ("team_id",),
            ),
            operation(
                "list_messages",
                "MICROSOFT_TEAMS_TEAMS_LIST_CHANNEL_MESSAGES",
                "Read a page of channel messages.",
                {
                    "team_id": RESOURCE_ID,
                    "channel_id": RESOURCE_ID,
                    "top": PAGE_SIZE,
                    "page_token": CURSOR,
                },
                ("team_id", "channel_id"),
            ),
        ),
    ),
    app(
        "todoist",
        "Todoist",
        "tasks",
        "Read tasks, due dates, and projects.",
        ["data:read"],
        (
            operation(
                "get_profile",
                "TODOIST_GET_USER",
                "Read the connected Todoist identity.",
            ),
            operation(
                "list_tasks",
                "TODOIST_GET_ALL_TASKS",
                "List or filter active Todoist tasks.",
                {
                    "project_id": RESOURCE_ID,
                    "filter": QUERY,
                    "limit": PAGE_SIZE,
                    "cursor": CURSOR,
                },
            ),
            operation(
                "list_projects",
                "TODOIST_GET_ALL_PROJECTS",
                "List Todoist projects.",
                {"limit": PAGE_SIZE, "cursor": CURSOR},
            ),
        ),
    ),
    app(
        "linear",
        "Linear",
        "tasks",
        "Search issues and browse projects.",
        ["read"],
        (
            operation(
                "get_profile",
                "LINEAR_GET_CURRENT_USER",
                "Read the connected Linear identity.",
            ),
            operation(
                "search_issues",
                "LINEAR_SEARCH_ISSUES",
                "Search issue titles, descriptions, or identifiers.",
                {"query": QUERY, "first": PAGE_SIZE, "after": CURSOR},
                ("query",),
            ),
            operation(
                "list_projects",
                "LINEAR_LIST_LINEAR_PROJECTS",
                "List Linear projects.",
                {"first": PAGE_SIZE, "after": CURSOR},
            ),
        ),
    ),
)
# Google Calendar/Drive sign-in is also blocked in the current project, as reported
# by the user. Gmail's exact scope limitation was separately confirmed by support.
GOOGLE_SETUP_NOTICE = (
    "Google sign-in is currently unavailable with Monaw's Composio setup. "
    "This connector remains listed, but connecting is disabled until Google "
    "authorization is resolved."
)
MANIFESTS = tuple(
    manifest.model_copy(update={"setup_notice": GOOGLE_SETUP_NOTICE})
    if manifest.id in {"googlecalendar", "googledrive"}
    else manifest
    for manifest in MANIFESTS
)
MANIFEST_BY_ID = {manifest.id: manifest for manifest in MANIFESTS}
