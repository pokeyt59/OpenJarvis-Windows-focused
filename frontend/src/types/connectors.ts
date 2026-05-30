export interface SetupStep {
  label: string;
  url?: string;
  urlLabel?: string;
}

export interface ConnectorMeta {
  connector_id: string;
  display_name: string;
  auth_type: 'oauth' | 'local' | 'bridge' | 'filesystem';
  category: 'communication' | 'documents' | 'pim' | 'other';
  icon: string;
  color: string;
  description: string;
  unitLabel?: string;  // "emails", "messages", "meeting notes", "pages", "notes", etc.
  steps?: SetupStep[];
  troubleshooting?: string[];
  inputFields?: Array<{
    name: string;
    placeholder: string;
    type?: 'text' | 'password';
    /**
     * When true (and on a Tauri build), the form renders a "Browse..."
     * button next to this field that opens the OS-native folder
     * picker via ``@tauri-apps/plugin-dialog``. Falls back to the
     * webkitdirectory file input on web. Only meaningful for
     * filesystem-path fields (typically ``name: 'path'``).
     */
    browseFolder?: boolean;
    /**
     * When true (and on a Tauri build), the form renders a "My location"
     * button that fills this field with the device's coordinates from the OS
     * location service. Used by the weather connector.
     */
    useLocation?: boolean;
  }>;
}

export interface ConnectorInfo {
  connector_id: string;
  display_name: string;
  auth_type: "oauth" | "local" | "bridge" | "filesystem";
  connected: boolean;
  auth_url?: string;
  mcp_tools?: string[];
  chunks?: number;
}

export interface SyncStatus {
  state: "idle" | "syncing" | "paused" | "error";
  items_synced: number;
  items_total: number;
  /** Items processed in the current (or most recent) run only. `null`
   *  when no sync has been triggered through this server session yet. */
  new_items_synced?: number | null;
  /** ISO 8601 timestamp of the oldest indexed item, used to label how far
   *  back the corpus reaches ("past 3 months", "past 5 years"). `null`
   *  before anything is indexed. */
  oldest_item_date?: string | null;
  last_sync: string | null;
  error: string | null;
}

export interface ConnectRequest {
  path?: string;
  token?: string;
  code?: string;
  email?: string;
  password?: string;
}

export type WizardStep = "pick" | "connect" | "ingest" | "ready";

// Backward-compatible alias
export type SourceCard = ConnectorMeta;

export type ConnectorCategory = ConnectorMeta['category'];

export const SOURCE_CATALOG: ConnectorMeta[] = [
  // ── Upload / Paste ─────────────────────────────────────────────────
  {
    connector_id: 'upload',
    display_name: 'Upload / Paste',
    auth_type: 'filesystem',
    category: 'other',
    icon: 'FileUp',
    color: 'text-blue-400',
    description: 'Paste text or upload documents',
    unitLabel: 'documents',
    steps: [
      { label: 'Paste text or upload files (.txt, .md, .pdf, .docx, .csv) to add them to your knowledge base.' },
    ],
    inputFields: [],
  },
  // ── Communication ──────────────────────────────────────────────────
  {
    // Unified Gmail card. Defaults to the IMAP (app-password) flow because
    // it needs no Google Cloud setup; the OAuth path is offered as an
    // "Advanced" disclosure rendered in DataSourcesPage.
    connector_id: 'gmail_imap',
    display_name: 'Gmail',
    auth_type: 'oauth',
    category: 'communication',
    icon: 'Mail',
    color: 'text-red-400',
    description: 'Email messages and threads',
    unitLabel: 'emails',
    steps: [
      {
        label: 'Make sure 2-Step Verification is enabled, then generate a 16-character App Password (Mail / Other / "OpenJarvis"). Paste it below \u2014 spaces are fine, and use the app password, not your regular Gmail password.',
        url: 'https://myaccount.google.com/apppasswords',
        urlLabel: 'How to get an app password \u2192',
      },
    ],
    troubleshooting: [
      "Don't see App Passwords? Make sure 2-Step Verification is enabled first.",
      "Google Workspace user? Your admin may need to enable App Passwords for your organization.",
    ],
    inputFields: [
      { name: 'email', placeholder: 'you@gmail.com', type: 'text' },
      { name: 'password', placeholder: 'App password (xxxx xxxx xxxx xxxx)', type: 'password' },
    ],
  },
  {
    connector_id: 'slack',
    display_name: 'Slack',
    auth_type: 'oauth',
    category: 'communication',
    icon: 'Hash',
    color: 'text-purple-400',
    description: 'Read messages from every channel, private channel, DM, and group DM you have access to',
    unitLabel: 'messages',
    steps: [
      {
        label: 'Go to api.slack.com/apps and click "Create New App" → choose "From scratch". Name it "OpenJarvis" and pick your workspace',
        url: 'https://api.slack.com/apps',
        urlLabel: 'Open Slack Apps',
      },
      {
        label: 'In the left sidebar, click "OAuth & Permissions". Scroll down to "User Token Scopes" (NOT "Bot Token Scopes"). Click "Add an OAuth Scope" and add EACH of these scopes one by one:',
      },
      {
        label: 'channels:history • channels:read • groups:history • groups:read • im:history • im:read • mpim:history • mpim:read • users:read',
      },
      {
        label: 'In the left sidebar, click "Install App" → click "Install to Workspace" → click "Allow". After installing, copy the "User OAuth Token" that appears (starts with xoxp-, NOT xoxb-)',
      },
      {
        label: 'Paste the user token below. Sync indexes every channel, private channel, DM, and group DM you have access to — no need to invite anything to channels',
      },
      {
        label: '(Optional) Set the app icon: in the left sidebar click "Basic Information" → scroll to "Display Information" → upload the OpenJarvis logo',
        url: 'https://github.com/open-jarvis/OpenJarvis/blob/main/assets/openjarvis-slack-icon.jpg',
        urlLabel: 'Download icon',
      },
    ],
    inputFields: [
      { name: 'token', placeholder: 'xoxp-...', type: 'password' },
    ],
  },
  {
    connector_id: 'notion',
    display_name: 'Notion',
    auth_type: 'oauth',
    category: 'documents',
    icon: 'FileText',
    color: 'text-gray-300',
    description: 'Pages and databases',
    unitLabel: 'pages',
    steps: [
      {
        label: 'Go to notion.so/profile/integrations → click "+ New integration". Name it "OpenJarvis", select your workspace, and click Submit',
        url: 'https://www.notion.so/profile/integrations',
        urlLabel: 'Open Notion Integrations',
      },
      {
        label: 'Copy the "Internal Integration Secret" (starts with ntn_) and paste it below',
      },
      {
        label: 'To share ALL your pages at once: open any top-level page → click "..." (top right) → "Connections" → "Add connections" → search "OpenJarvis" → click it. This shares the page and all its sub-pages. Repeat for each top-level page, or share your entire workspace by doing this on every root page',
      },
      {
        label: 'Tip: if you have a single top-level page that contains everything, sharing just that one page will share all nested sub-pages automatically',
      },
    ],
    inputFields: [
      { name: 'token', placeholder: 'ntn_...', type: 'password' },
    ],
  },
  {
    connector_id: 'granola',
    display_name: 'Granola',
    auth_type: 'oauth',
    category: 'documents',
    icon: 'Mic',
    color: 'text-amber-400',
    description: 'AI meeting notes',
    unitLabel: 'meeting notes',
    steps: [
      { label: 'Open the Granola desktop app. Click the gear icon (Settings) in the bottom-left corner, then click "API"' },
      { label: 'Click "Generate API Key" (or copy your existing key). Paste the key below' },
    ],
    inputFields: [
      { name: 'token', placeholder: 'grn_...', type: 'password' },
    ],
  },
  // ── Documents ──────────────────────────────────────────────────────
  {
    connector_id: 'obsidian',
    display_name: 'Obsidian',
    auth_type: 'filesystem',
    category: 'documents',
    icon: 'FolderOpen',
    color: 'text-purple-300',
    description: 'Markdown vault',
    unitLabel: 'notes',
    steps: [
      {
        label: 'Find your vault path: open Obsidian → click the vault name in the bottom-left corner → "Manage Vaults" → look at the path shown under your vault name. On Windows this is usually C:\\Users\\YOU\\Documents\\MyVault (or wherever you saved it)',
      },
      {
        label: 'Alternatively, open File Explorer → navigate to your vault folder (it contains a hidden .obsidian directory). Click in the address bar to copy the full path',
      },
      {
        label: 'Paste the full path below. OpenJarvis will index all .md files in the vault',
      },
    ],
    inputFields: [
      { name: 'path', placeholder: 'C:\\Users\\you\\Documents\\MyVault', type: 'text', browseFolder: true },
    ],
  },
  {
    connector_id: 'gdrive',
    display_name: 'Google Drive',
    auth_type: 'oauth',
    category: 'documents',
    icon: 'FolderOpen',
    color: 'text-blue-400',
    description: 'Docs, Sheets, and files',
    unitLabel: 'files',
    steps: [
      {
        label: 'Go to Google Cloud Console → create a new project (or select an existing one). Give it any name (e.g. "OpenJarvis")',
        url: 'https://console.cloud.google.com/projectcreate',
        urlLabel: 'Create Project',
      },
      {
        label: 'Enable the Google Drive API: click the link below, make sure your project is selected at the top, then click "Enable"',
        url: 'https://console.cloud.google.com/apis/library/drive.googleapis.com',
        urlLabel: 'Enable Drive API',
      },
      {
        label: 'Create OAuth credentials: go to Credentials (link below) → click "+ Create Credentials" → choose "OAuth client ID" → Application type: "Desktop app" → click "Create"',
        url: 'https://console.cloud.google.com/apis/credentials',
        urlLabel: 'Open Credentials',
      },
      {
        label: 'A dialog will show your Client ID and Client Secret. Copy both and paste them below. (If you miss it, click the download icon next to your OAuth client to see them again)',
      },
    ],
    inputFields: [
      { name: 'email', placeholder: 'Client ID (e.g. 123456-abc.apps.googleusercontent.com)', type: 'text' },
      { name: 'password', placeholder: 'Client Secret', type: 'password' },
    ],
  },
  // ── PIM (Calendar, Contacts) ───────────────────────────────────────
  {
    connector_id: 'gcalendar',
    display_name: 'Google Calendar',
    auth_type: 'oauth',
    category: 'pim',
    icon: 'Calendar',
    color: 'text-blue-400',
    description: 'Events and meetings',
    unitLabel: 'events',
    steps: [
      {
        label: 'Go to Google Cloud Console → use the same project as Google Drive (or create a new one)',
        url: 'https://console.cloud.google.com/projectcreate',
        urlLabel: 'Open Console',
      },
      {
        label: 'Enable the Google Calendar API: click the link below, select your project, then click "Enable"',
        url: 'https://console.cloud.google.com/apis/library/calendar-json.googleapis.com',
        urlLabel: 'Enable Calendar API',
      },
      {
        label: 'Go to Credentials → "+ Create Credentials" → "OAuth client ID" → Application type: "Desktop app" → "Create". Copy the Client ID and Client Secret',
        url: 'https://console.cloud.google.com/apis/credentials',
        urlLabel: 'Open Credentials',
      },
      {
        label: 'Paste the Client ID and Client Secret below (you can reuse the same OAuth client as Google Drive if you enabled both APIs in the same project)',
      },
    ],
    inputFields: [
      { name: 'email', placeholder: 'Client ID', type: 'text' },
      { name: 'password', placeholder: 'Client Secret', type: 'password' },
    ],
  },
  {
    connector_id: 'gcontacts',
    display_name: 'Google Contacts',
    auth_type: 'oauth',
    category: 'pim',
    icon: 'Users',
    color: 'text-blue-400',
    description: 'People and contact info',
    unitLabel: 'contacts',
    steps: [
      {
        label: 'Go to Google Cloud Console → use the same project as Google Drive (or create a new one)',
        url: 'https://console.cloud.google.com/projectcreate',
        urlLabel: 'Open Console',
      },
      {
        label: 'Enable the People API: click the link below, select your project, then click "Enable"',
        url: 'https://console.cloud.google.com/apis/library/people.googleapis.com',
        urlLabel: 'Enable People API',
      },
      {
        label: 'Go to Credentials → "+ Create Credentials" → "OAuth client ID" → Application type: "Desktop app" → "Create". Copy the Client ID and Client Secret',
        url: 'https://console.cloud.google.com/apis/credentials',
        urlLabel: 'Open Credentials',
      },
      {
        label: 'Paste the Client ID and Client Secret below',
      },
    ],
    inputFields: [
      { name: 'email', placeholder: 'Client ID', type: 'text' },
      { name: 'password', placeholder: 'Client Secret', type: 'password' },
    ],
  },
  {
    connector_id: 'outlook',
    display_name: 'Outlook',
    auth_type: 'oauth',
    category: 'communication',
    icon: 'Mail',
    color: 'text-blue-400',
    description: 'Microsoft email and calendar',
    unitLabel: 'emails',
    steps: [
      {
        label: 'Go to the Azure Portal → App Registrations → click "+ New registration". Name it "OpenJarvis", select "Accounts in this organizational directory only", and click Register',
        url: 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
        urlLabel: 'Open Azure App Registrations',
      },
      {
        label: 'In the left sidebar, click "API Permissions" → "Add a permission" → "Microsoft Graph" → "Delegated permissions" → search and check "Mail.Read" → click "Add permissions"',
      },
      {
        label: 'In the left sidebar, click "Certificates & secrets" → "New client secret" → set a description and expiry → click "Add" → immediately copy the "Value" (you won\'t see it again)',
      },
      {
        label: 'Go to "Overview" in the left sidebar and copy the "Application (client) ID". Paste both the Client ID and the Client Secret below',
      },
    ],
    inputFields: [
      { name: 'email', placeholder: 'Application (client) ID', type: 'text' },
      { name: 'password', placeholder: 'Client Secret Value', type: 'password' },
    ],
  },
  {
    connector_id: 'dropbox',
    display_name: 'Dropbox',
    auth_type: 'oauth',
    category: 'documents',
    icon: 'FolderOpen',
    color: 'text-blue-300',
    description: 'Cloud file storage',
    unitLabel: 'files',
    steps: [
      {
        label: 'Go to the Dropbox App Console and click "Create app". Choose "Scoped access" → "Full Dropbox" → give it a name (e.g. "OpenJarvis") → click "Create app"',
        url: 'https://www.dropbox.com/developers/apps/create',
        urlLabel: 'Open Dropbox App Console',
      },
      {
        label: 'Click the "Permissions" tab at the top. Check "files.metadata.read" and "files.content.read" → click "Submit" at the bottom to save',
      },
      {
        label: 'Go back to the "Settings" tab. Under "OAuth 2", find "Generated access token" and click "Generate". Copy the token and paste it below',
      },
    ],
    inputFields: [
      { name: 'token', placeholder: 'Access token (sl.u...)', type: 'password' },
    ],
  },
  {
    connector_id: 'whatsapp',
    display_name: 'WhatsApp',
    auth_type: 'oauth',
    category: 'communication',
    icon: 'MessageSquare',
    color: 'text-green-400',
    description: 'WhatsApp messages (Meta Cloud API)',
    unitLabel: 'messages',
    steps: [
      {
        label: 'Go to Meta for Developers → click "Create App" → choose "Business" type → fill in your app details and click "Create App"',
        url: 'https://developers.facebook.com/apps/',
        urlLabel: 'Open Meta Developer Portal',
      },
      {
        label: 'On the app dashboard, find "WhatsApp" and click "Set up". Follow the prompts to add a WhatsApp test number. Go to "API Setup" and copy the temporary access token',
      },
      {
        label: 'Copy your "Phone Number ID" (shown on the API Setup page) and the access token. Paste them below separated by a colon — e.g. 123456789:EAABx...',
      },
    ],
    inputFields: [
      { name: 'token', placeholder: 'Phone Number ID:Access Token', type: 'password' },
    ],
  },
  // ── Microsoft Graph trio (share one Azure app) ─────────────────────
  {
    connector_id: 'onenote',
    display_name: 'OneNote',
    auth_type: 'oauth',
    category: 'documents',
    icon: 'StickyNote',
    color: 'text-purple-400',
    description: 'Microsoft OneNote notebooks',
    unitLabel: 'pages',
    steps: [
      {
        label: 'Go to the Azure Portal → App Registrations → click "+ New registration". Name it "OpenJarvis", choose "Accounts in any organizational directory and personal Microsoft accounts", set Redirect URI to "Public client/native" with value http://localhost:8790/callback, click Register',
        url: 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
        urlLabel: 'Open Azure App Registrations',
      },
      {
        label: 'In the left sidebar, click "API Permissions" → "Add a permission" → "Microsoft Graph" → "Delegated permissions" → search and check "offline_access", "Notes.Read", and "Notes.Read.All" → click "Add permissions"',
      },
      {
        label: 'In the left sidebar, click "Certificates & secrets" → "+ New client secret" → set a description and expiry → click "Add" → immediately copy the "Value" column (you will not see it again)',
      },
      {
        label: 'Go to "Overview" and copy the "Application (client) ID". Paste both the Client ID and the Client Secret below separated by a colon — e.g. 11111111-...:secret-value. A browser window opens for consent; finish there and OneNote will start syncing',
      },
    ],
    troubleshooting: [
      'If the consent screen says "AADSTS50011 redirect URI mismatch", go back to your app registration → Authentication → add http://localhost:8790/callback as a redirect URI',
      'If you also enable OneDrive or Microsoft To Do, you can reuse the same Azure app — just add their scopes to API Permissions and use the same client_id:secret',
    ],
    inputFields: [
      { name: 'email', placeholder: 'Application (client) ID', type: 'text' },
      { name: 'password', placeholder: 'Client Secret Value', type: 'password' },
    ],
  },
  {
    connector_id: 'onedrive',
    display_name: 'OneDrive',
    auth_type: 'oauth',
    category: 'documents',
    icon: 'Cloud',
    color: 'text-sky-400',
    description: 'OneDrive files (text content indexed; Office docs as metadata)',
    unitLabel: 'files',
    steps: [
      {
        label: 'Reuse the same Azure App Registration you created for OneNote, or create a fresh one (same redirect URI: http://localhost:8790/callback)',
        url: 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
        urlLabel: 'Open Azure App Registrations',
      },
      {
        label: 'In API Permissions, add Microsoft Graph delegated permissions: "offline_access", "Files.Read", "Files.Read.All" → click "Add permissions"',
      },
      {
        label: 'If you skipped the OneNote setup: in "Certificates & secrets" generate a new client secret and copy the Value column immediately',
      },
      {
        label: 'Paste the Application (client) ID and the Client Secret below, separated by a colon. A browser will pop up to grant consent — once you finish, OneDrive starts syncing',
      },
    ],
    inputFields: [
      { name: 'email', placeholder: 'Application (client) ID', type: 'text' },
      { name: 'password', placeholder: 'Client Secret Value', type: 'password' },
    ],
  },
  {
    connector_id: 'mstodo',
    display_name: 'Microsoft To Do',
    auth_type: 'oauth',
    category: 'pim',
    icon: 'CheckSquare',
    color: 'text-blue-500',
    description: 'Tasks from every Microsoft To Do list',
    unitLabel: 'tasks',
    steps: [
      {
        label: 'Reuse the same Azure App Registration you set up for OneNote / OneDrive (same redirect URI: http://localhost:8790/callback)',
        url: 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
        urlLabel: 'Open Azure App Registrations',
      },
      {
        label: 'In API Permissions add Microsoft Graph delegated permissions: "offline_access" and "Tasks.Read" → click "Add permissions"',
      },
      {
        label: 'Paste the Application (client) ID and Client Secret Value below, separated by a colon — same format as OneNote. The consent browser pop-up will appear once',
      },
    ],
    inputFields: [
      { name: 'email', placeholder: 'Application (client) ID', type: 'text' },
      { name: 'password', placeholder: 'Client Secret Value', type: 'password' },
    ],
  },
  // ── Local Windows sources ─────────────────────────────────────────
  {
    connector_id: 'local_folder',
    display_name: 'Local Folder',
    auth_type: 'filesystem',
    category: 'documents',
    icon: 'FolderOpen',
    color: 'text-zinc-400',
    description: 'Index any folder on disk (Documents, Desktop, a project dir)',
    unitLabel: 'files',
    steps: [
      {
        label: 'Open File Explorer → navigate to the folder you want to index. Click in the address bar and copy the full path (e.g. C:\\Users\\you\\Documents)',
      },
      {
        label: 'Paste the folder path below. OpenJarvis recursively indexes .txt, .md, .csv, .json, source-code files and similar text formats. Binary files (images, PDFs, Office docs) are skipped, and junk directories (.git, node_modules, $Recycle.Bin, etc.) are pruned automatically',
      },
      {
        label: 'Files larger than 2 MB are skipped to keep the first sync sane. You can add more folders later by connecting this source again with a different path',
      },
    ],
    inputFields: [
      { name: 'path', placeholder: 'C:\\Users\\you\\Documents', type: 'text', browseFolder: true },
    ],
  },
  {
    connector_id: 'edge',
    display_name: 'Microsoft Edge',
    auth_type: 'filesystem',
    category: 'other',
    icon: 'Globe',
    color: 'text-cyan-400',
    description: 'Browser history and bookmarks (local read)',
    unitLabel: 'visits & bookmarks',
    steps: [
      {
        label: 'Leave the path blank to auto-detect the default Edge profile at %LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\Default',
      },
      {
        label: 'For a non-default profile, paste the profile directory (it contains the files named "History" and "Bookmarks"). Edge keeps these files locked while running — OpenJarvis copies them to a temp file before reading, so you don\'t need to close the browser',
      },
      {
        label: 'OpenJarvis indexes the most recent 5000 history entries plus every bookmark. Nothing is uploaded — the sync is purely local',
      },
    ],
    inputFields: [
      { name: 'path', placeholder: '(optional) profile dir, leave blank to auto-detect', type: 'text', browseFolder: true },
    ],
  },
  {
    connector_id: 'discord',
    display_name: 'Discord',
    auth_type: 'oauth',
    category: 'communication',
    icon: 'MessageCircle',
    color: 'text-indigo-400',
    description: 'Direct messages and group DMs',
    unitLabel: 'messages',
    steps: [
      {
        label: 'Open Discord in your browser and sign in. Press F12 to open DevTools → switch to the "Network" tab → click any channel to load messages',
      },
      {
        label: 'In the Network tab, click any request to "/api/" → look at the request headers and copy the full value of the "authorization" header (it does NOT start with "Bearer")',
      },
      {
        label: 'Paste the token below. The sync walks every DM and group DM you can see — server channels are skipped for this first version because most are too noisy to index',
      },
    ],
    troubleshooting: [
      'Discord may invalidate a user token if it detects unusual activity. If sync stops working, refresh the token using the same DevTools steps',
      'If you do not see authorization headers in DevTools, make sure the filter is set to "All" or "Fetch/XHR", not "WS" or "JS"',
    ],
    inputFields: [
      { name: 'token', placeholder: 'Discord user token', type: 'password' },
    ],
  },
  // ── Local Outlook (Desktop) was here ──────────────────────────────
  // Removed: the cloud `outlook` card (Microsoft Graph OAuth) already
  // covers everyone's mailbox. The local variant read PST/OST files
  // through MAPI via pywin32, which only adds value for users wanting
  // to index OFFLINE archive .pst files without going through OAuth —
  // a niche we judged not worth the extra friction (pywin32 dep, Outlook
  // desktop installed, no auto-sync, schema differs across Outlook
  // versions). Originally added during the Windows-native connector
  // sweep when "as much local Windows surface as possible" was the
  // explicit design goal; in hindsight it duplicated the cloud card.
  // Backend module + tests deleted in the same change.
  {
    connector_id: 'phone_link',
    display_name: 'Phone Link (Android SMS)',
    auth_type: 'filesystem',
    category: 'communication',
    icon: 'Smartphone',
    color: 'text-emerald-400',
    description: 'SMS bridged from your Android via the Windows Phone Link app',
    unitLabel: 'messages',
    steps: [
      {
        label: 'Set up Microsoft Phone Link on this PC and pair your Android phone. Make sure SMS sync is enabled in Phone Link settings',
        url: 'https://www.microsoft.com/en-us/windows/sync-across-your-devices',
        urlLabel: 'Phone Link setup guide',
      },
      {
        label: 'Leave the path blank to auto-detect the LocalState dir at %LOCALAPPDATA%\\Packages\\Microsoft.YourPhone_8wekyb3d8bbwe\\LocalState',
      },
      {
        label: 'BEST-EFFORT WARNING: Microsoft has never documented this storage format and the schema changes across versions. The connector probes for common shapes; if your version stores messages differently the sync may yield nothing. Nothing crashes — it just stays empty',
      },
    ],
    troubleshooting: [
      'iPhone users: Phone Link\'s iPhone integration does not expose message text locally — only Android SMS gets cached',
      'If sync yields zero messages, your Phone Link version may be storing messages only in memory. Check Phone Link → Settings → Messages → ensure "Sync messages" is on, send/receive a few SMS, then re-sync',
    ],
    inputFields: [
      { name: 'path', placeholder: '(optional) LocalState dir, leave blank to auto-detect', type: 'text', browseFolder: true },
    ],
  },
  {
    connector_id: 'ide_workspaces',
    display_name: 'IDE Workspaces (VS Code + JetBrains)',
    auth_type: 'filesystem',
    category: 'other',
    icon: 'Code2',
    color: 'text-orange-400',
    description: 'Recent projects from VS Code, Insiders, VSCodium, and every JetBrains IDE',
    unitLabel: 'workspaces',
    steps: [
      {
        label: 'No setup needed. OpenJarvis auto-detects the standard config locations: %APPDATA%\\Code\\User\\workspaceStorage (and the Insiders / VSCodium equivalents) plus every JetBrains product dir under %APPDATA%\\JetBrains\\ (IntelliJIdea, PyCharm, WebStorm, RustRover, GoLand, RubyMine, CLion, PhpStorm, DataGrip, Rider, AndroidStudio — all picked up automatically)',
      },
      {
        label: 'Each yielded entry is a Document carrying the project folder name as title, full filesystem path in the body, plus the IDE flavour and last-opened timestamp in metadata so the agent can answer "what was I working on last week"',
      },
      {
        label: 'Nothing is indexed beyond the recent-project list itself — the connector does not crawl project files. Pair this with the "Local Folder" connector if you want the actual code indexed too',
      },
    ],
    troubleshooting: [
      'Portable VS Code installs keep their config inside the install dir rather than %APPDATA%. The auto-detection will miss them — they currently aren\'t supported by the UI; let us know if you need this',
      'JetBrains Toolbox installs use the standard %APPDATA%\\JetBrains location, so they are picked up automatically',
    ],
    inputFields: [],
  },
  // ── External services (token + OAuth) ──────────────────────────────
  {
    connector_id: 'github_notifications',
    display_name: 'GitHub Notifications',
    auth_type: 'oauth',
    category: 'communication',
    icon: 'Bell',
    color: 'text-zinc-300',
    description: 'Your unread GitHub notifications (issues, PRs, mentions)',
    unitLabel: 'notifications',
    steps: [
      {
        label: 'Go to GitHub → Settings → Developer settings → Personal access tokens → "Tokens (classic)". Click "Generate new token (classic)"',
        url: 'https://github.com/settings/tokens',
        urlLabel: 'Open GitHub token settings',
      },
      {
        label: 'Note: name it "OpenJarvis", set an expiry that fits your habits (90 days is a reasonable balance), check ONLY the "notifications" scope — nothing else is needed',
      },
      {
        label: 'Click "Generate token" at the bottom. Copy the value that appears (starts with ghp_, gho_, ghs_, ghu_, or github_pat_ for fine-grained tokens) — you will not see it again',
      },
      {
        label: 'Paste the token below. OpenJarvis validates it against the GitHub API before saving, so if the wrong scope is checked or the token is malformed you will get an immediate error',
      },
    ],
    troubleshooting: [
      'If GitHub says the token lacks scope, regenerate with the "notifications" box checked. Read-only scopes are intentional — we never need to write anything',
      'Fine-grained tokens also work; give them "Notifications" repository permission set to Read-only',
    ],
    inputFields: [
      { name: 'token', placeholder: 'ghp_... or github_pat_...', type: 'password' },
    ],
  },
  {
    connector_id: 'google_tasks',
    display_name: 'Google Tasks',
    auth_type: 'oauth',
    category: 'pim',
    icon: 'CheckSquare',
    color: 'text-blue-400',
    description: 'Tasks from Google Tasks lists',
    unitLabel: 'tasks',
    steps: [
      {
        label: 'Use the same Google Cloud project you set up for Google Drive / Calendar / Contacts. If this is your first Google connector, follow the Drive setup first — Google Tasks shares its credentials',
        url: 'https://console.cloud.google.com/apis/library/tasks.googleapis.com',
        urlLabel: 'Enable Tasks API',
      },
      {
        label: 'Enable the Google Tasks API for your project (link above). The OAuth consent flow we already use covers Tasks via the tasks.readonly scope',
      },
      {
        label: 'Paste the same Client ID and Client Secret you used for Drive / Calendar separated by a colon (e.g. 123456-abc.apps.googleusercontent.com:client-secret-value). If you have not done a Google OAuth yet, a browser window opens for consent on first save',
      },
    ],
    troubleshooting: [
      'If consent says "Tasks API has not been used" — that means you skipped step 2; enable the API and try again',
      'Already connected another Google connector? Google Tasks may auto-connect on first sync since all Google connectors share the same OAuth tokens — you can still re-paste here to force the consent flow',
    ],
    inputFields: [
      { name: 'email', placeholder: 'Client ID (e.g. 123-abc.apps.googleusercontent.com)', type: 'text' },
      { name: 'password', placeholder: 'Client Secret', type: 'password' },
    ],
  },
  {
    connector_id: 'news_rss',
    display_name: 'News / RSS',
    auth_type: 'oauth',
    category: 'documents',
    icon: 'Rss',
    color: 'text-orange-300',
    description: 'Headlines from RSS and Atom feeds you follow',
    unitLabel: 'articles',
    steps: [
      {
        label: 'Find the feed URL for a site you want to follow. Most blogs and news sites publish at /rss, /feed, /atom.xml, or /rss.xml. The browser address bar should end in that path — paste the FULL URL, not the homepage',
      },
      {
        label: 'Example feeds you can try: https://news.ycombinator.com/rss (Hacker News), https://feeds.bbci.co.uk/news/rss.xml (BBC), https://stackoverflow.blog/feed/ (Stack Overflow blog)',
      },
      {
        label: 'Paste a single feed URL below. OpenJarvis fetches it once to confirm it parses as RSS/Atom, then saves it. To add MORE feeds, expand this card again with a different URL — additions stack, dupes are ignored',
      },
      {
        label: 'Sync pulls the 5 most recent items per feed. Fully local: no auth, no third-party API, just plain HTTP fetches to each feed URL',
      },
    ],
    troubleshooting: [
      'If you get "URL didn\'t return valid XML": you probably pasted a homepage instead of a feed link. Look for an orange RSS icon on the page, or check the site\'s footer for "RSS / Subscribe"',
      'A few sites block the default httpx user-agent — most don\'t. If a known-good feed fails, please file an issue with the URL',
    ],
    inputFields: [
      { name: 'token', placeholder: 'https://example.com/rss.xml', type: 'text' },
    ],
  },
  {
    connector_id: 'oura',
    display_name: 'Oura Ring',
    auth_type: 'oauth',
    category: 'other',
    icon: 'Activity',
    color: 'text-cyan-300',
    description: 'Sleep, readiness, and daily activity from your Oura Ring',
    unitLabel: 'days',
    steps: [
      {
        label: 'Sign in at cloud.ouraring.com → click your profile (top right) → Personal Access Tokens → "Create New Personal Access Token"',
        url: 'https://cloud.ouraring.com/personal-access-tokens',
        urlLabel: 'Open Oura Personal Tokens',
      },
      {
        label: 'Name it "OpenJarvis", click Create. Copy the token value that appears — Oura shows it once, then only the last 4 characters',
      },
      {
        label: 'Paste below. OpenJarvis validates the token against Oura\'s API before saving, so an expired or revoked token gets caught immediately',
      },
    ],
    troubleshooting: [
      'If Oura says "401 Unauthorized": the token was revoked or pasted incorrectly. Generate a fresh one and try again',
      'The connector pulls 7 days of sleep, readiness, and daily activity per sync. Older data isn\'t fetched to keep the corpus tight — if you want historical data, let us know',
    ],
    inputFields: [
      { name: 'token', placeholder: 'Personal Access Token', type: 'password' },
    ],
  },
  {
    connector_id: 'spotify',
    display_name: 'Spotify',
    auth_type: 'oauth',
    category: 'other',
    icon: 'Music',
    color: 'text-green-400',
    description: 'Recently played tracks from your Spotify account',
    unitLabel: 'tracks',
    steps: [
      {
        label: 'Go to the Spotify Developer Dashboard and click "Create app". Name it "OpenJarvis" (or whatever) — the name only shows on the consent screen',
        url: 'https://developer.spotify.com/dashboard',
        urlLabel: 'Open Spotify Developer Dashboard',
      },
      {
        label: 'Important: set Redirect URI to EXACTLY http://127.0.0.1:8888/callback (NOT localhost — Spotify treats them as different). Tick the Web API checkbox. Save',
      },
      {
        label: 'Open the app you just created → Settings → copy the "Client ID" and click "View client secret" to copy that too',
      },
      {
        label: 'Paste both below separated by a colon — e.g. abc123:xyz456. OpenJarvis will save the credentials and a browser window opens for the one-time consent. After approving, tracks start syncing',
      },
    ],
    troubleshooting: [
      'If Spotify says "INVALID_CLIENT: Invalid redirect URI" after consent: re-check step 2 — the redirect URI must be exactly http://127.0.0.1:8888/callback, with no trailing slash and 127.0.0.1 not localhost',
      'Free Spotify accounts work — premium is not required for recently-played data',
      'The connector reads recently-played only (the API doesn\'t expose listening history beyond ~50 most recent tracks). To capture more, sync frequently',
    ],
    inputFields: [
      { name: 'email', placeholder: 'Client ID', type: 'text' },
      { name: 'password', placeholder: 'Client Secret', type: 'password' },
    ],
  },
  {
    connector_id: 'strava',
    display_name: 'Strava',
    auth_type: 'oauth',
    category: 'other',
    icon: 'Bike',
    color: 'text-orange-500',
    description: 'Recent activities (runs, rides, swims, etc.) from Strava',
    unitLabel: 'activities',
    steps: [
      {
        label: 'Go to your Strava API Settings and click "Create your application" (or edit the existing one if you have one)',
        url: 'https://www.strava.com/settings/api',
        urlLabel: 'Open Strava API settings',
      },
      {
        label: 'Fill in: Application Name "OpenJarvis", Category (any), Website (any, e.g. http://localhost), Authorization Callback Domain "127.0.0.1". Upload any image. Click Create',
      },
      {
        label: 'On the app page, copy the "Client ID" (a number) and the "Client Secret" (long hex string). Strava shows the secret only after you click "Show"',
      },
      {
        label: 'Paste both below separated by a colon — e.g. 123456:hex-secret-here. OpenJarvis saves the credentials and a browser window opens for one-time authorization with the activity:read_all scope',
      },
    ],
    troubleshooting: [
      'If Strava says "Bad Request" after consent: the Authorization Callback Domain in step 2 must be exactly "127.0.0.1" (no http://, no port, no path)',
      'The activity:read_all scope is needed to see private activities. If you only want public activities, you can edit the connector to use activity:read instead',
    ],
    inputFields: [
      { name: 'email', placeholder: 'Client ID (numeric)', type: 'text' },
      { name: 'password', placeholder: 'Client Secret', type: 'password' },
    ],
  },
  {
    connector_id: 'weather',
    display_name: 'Weather',
    auth_type: 'oauth',
    category: 'other',
    icon: 'Cloud',
    color: 'text-sky-300',
    description: 'Current conditions and forecast for a location you set',
    unitLabel: 'observations',
    steps: [
      {
        label: 'Sign up for a free OpenWeatherMap account. The free tier covers 1000 API calls/day — far more than this connector uses',
        url: 'https://home.openweathermap.org/users/sign_up',
        urlLabel: 'Sign up at OpenWeatherMap',
      },
      {
        label: 'Once signed in, go to API keys. A default key is created for you — copy it. Heads up: new keys take up to 10 minutes to activate, so if the first attempt 401s, wait and retry',
        url: 'https://home.openweathermap.org/api_keys',
        urlLabel: 'Open API keys',
      },
      {
        label: 'Enter your location below using City,Country code (e.g. "San Francisco,US", "London,GB", "Tokyo,JP"). Add a state code in between for US cities to disambiguate (e.g. "Portland,OR,US"). Then paste your API key',
      },
      {
        label: 'OpenJarvis validates both fields against OpenWeatherMap before saving, so you\'ll see immediately if the key is bad or the location string isn\'t recognized',
      },
    ],
    troubleshooting: [
      'New API keys take ~10 min to activate after creation. If a 401 hits immediately after signup, wait and re-paste',
      'Location format matters: "Springfield" alone is ambiguous; "Springfield,IL,US" is not',
      'Each sync emits the current weather plus a 12-hour forecast in 3-hour chunks — 5 documents total per refresh',
    ],
    inputFields: [
      { name: 'email', placeholder: 'Location (e.g. San Francisco,US — or use My location)', type: 'text', useLocation: true },
      { name: 'password', placeholder: 'OpenWeatherMap API key', type: 'password' },
    ],
  },
];
