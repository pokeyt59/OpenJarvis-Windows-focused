"""Data source connectors for Deep Research."""

from openjarvis.connectors._stubs import (
    Attachment,
    BaseConnector,
    Document,
    SyncStatus,
)
from openjarvis.connectors.store import KnowledgeStore

__all__ = ["Attachment", "BaseConnector", "Document", "KnowledgeStore", "SyncStatus"]

# Auto-register built-in connectors
import openjarvis.connectors.obsidian  # noqa: F401

try:
    import openjarvis.connectors.gmail  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.gmail_imap  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.gdrive  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import openjarvis.connectors.notion  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.granola  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.gcontacts  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.slack_connector  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.outlook  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.gcalendar  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.dropbox  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import openjarvis.connectors.whatsapp  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.oura  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.strava  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.spotify  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.google_tasks  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.weather  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.github_notifications  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.hackernews  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.news_rss  # noqa: F401
except ImportError:
    pass

# ── Windows-native additions ─────────────────────────────────────────────
# Microsoft Graph trio share ms_graph_auth; each is independent so import
# failures are isolated.
try:
    import openjarvis.connectors.onenote  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.onedrive  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.mstodo  # noqa: F401
except ImportError:
    pass

# Local-file connectors: no external deps so import shouldn't fail, but
# wrap defensively to match the rest of the file.
try:
    import openjarvis.connectors.local_folder  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.edge  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.discord  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.phone_link  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.ide_workspaces  # noqa: F401
except ImportError:
    pass
