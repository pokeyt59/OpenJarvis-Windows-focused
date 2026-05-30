"""News/RSS connector — aggregate headlines from RSS and Atom feeds.

Uses stdlib xml.etree.ElementTree for parsing (no extra dependencies).
Config file lists feeds to follow. All HTTP calls are in module-level
functions for easy mocking in tests.
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlparse

import httpx

from openjarvis.connectors._stubs import BaseConnector, Document, SyncStatus
from openjarvis.core.config import DEFAULT_CONFIG_DIR
from openjarvis.core.registry import ConnectorRegistry

_DEFAULT_CONFIG_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "news_rss.json")


def _fetch_feed(url: str) -> str:
    """Download raw XML from a feed URL."""
    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _parse_rss_items(xml_text: str, max_items: int = 5) -> List[Dict[str, str]]:
    """Parse RSS or Atom XML and return up to *max_items* entries."""
    root = ET.fromstring(xml_text)
    items: List[Dict[str, str]] = []

    # RSS 2.0: <rss><channel><item>
    for item_el in root.iter("item"):
        if len(items) >= max_items:
            break
        items.append(
            {
                "title": (item_el.findtext("title") or "").strip(),
                "description": (item_el.findtext("description") or "").strip()[:200],
                "link": (item_el.findtext("link") or "").strip(),
                "pubDate": (item_el.findtext("pubDate") or "").strip(),
            }
        )

    # Atom: <feed><entry>
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry_el in root.iter("{http://www.w3.org/2005/Atom}entry"):
            if len(items) >= max_items:
                break
            link_el = entry_el.find("atom:link", ns)
            link_href = link_el.get("href", "") if link_el is not None else ""
            summary = entry_el.findtext("{http://www.w3.org/2005/Atom}summary") or ""
            updated = entry_el.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            items.append(
                {
                    "title": (
                        entry_el.findtext("{http://www.w3.org/2005/Atom}title") or ""
                    ).strip(),
                    "description": summary.strip()[:200],
                    "link": link_href.strip(),
                    "pubDate": updated.strip(),
                }
            )

    return items


def _parse_pub_date(date_str: str) -> Optional[datetime]:
    """Best-effort parse of an RSS pubDate or Atom updated timestamp."""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@ConnectorRegistry.register("news_rss")
class NewsRSSConnector(BaseConnector):
    """Aggregate headlines from configured RSS/Atom feeds."""

    connector_id = "news_rss"
    # ``auth_type = "oauth"`` is what /connect uses to route the pasted
    # feed URL through ``handle_callback``. The connector itself has
    # no auth at all — RSS is public — but reusing the "paste a string"
    # path gives us the validation flow for free.
    auth_type = "oauth"
    display_name = "News / RSS"

    def __init__(self, *, config_path: str = _DEFAULT_CONFIG_PATH) -> None:
        self._config_path = Path(config_path)
        self._status = SyncStatus()

    def _load_config(self) -> List[Dict[str, str]]:
        """Load feed list from disk."""
        data = json.loads(self._config_path.read_text(encoding="utf-8"))
        return data.get("feeds", [])

    def is_connected(self) -> bool:
        if not self._config_path.exists():
            return False
        try:
            feeds = self._load_config()
            return len(feeds) > 0
        except (json.JSONDecodeError, OSError):
            return False

    def disconnect(self) -> None:
        if self._config_path.exists():
            self._config_path.unlink()

    def handle_callback(self, code: str) -> None:
        """Validate the pasted feed URL and add it to the feed list.

        First-time call creates the config file with a one-entry feed
        list; subsequent calls *append* a new feed if its URL isn't
        already present. The feed name is derived from the URL's host
        so the per-Document ``feed_name`` metadata is still useful for
        filtering in retrieval.

        Validation: must be a well-formed http(s) URL and the response
        must parse as RSS or Atom (we don't insist on items existing —
        a brand-new feed could be empty — but the XML root must look
        like a feed).
        """
        url = (code or "").strip()
        if not url:
            raise ValueError("Empty feed URL")

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Feed URL must start with http:// or https://")
        if not parsed.netloc:
            raise ValueError("Feed URL is missing a host")

        # Fetch + parse to confirm this really is an RSS/Atom feed.
        # Saves the user a hour of "why does sync find nothing" when
        # they pasted a homepage URL by mistake.
        try:
            xml_text = _fetch_feed(url)
        except httpx.HTTPError as exc:
            raise ValueError(f"Couldn't fetch feed: {exc}") from exc
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(
                "URL didn't return valid XML. Make sure it's a direct "
                "link to an RSS or Atom feed (not the website's homepage)."
            ) from exc

        # Sniff: RSS 2.0 has <rss>/<channel>; Atom has <feed> in the
        # Atom namespace. Either is acceptable.
        is_rss = root.tag in ("rss", "channel")
        is_atom = root.tag.endswith("}feed") or root.tag == "feed"
        if not (is_rss or is_atom):
            raise ValueError(
                "URL responded but doesn't look like an RSS or Atom feed."
            )

        # Append to existing config or create fresh. Dedup by URL so
        # repeated paste-the-same-thing is a no-op.
        existing: Dict[str, List[Dict[str, str]]] = {"feeds": []}
        if self._config_path.exists():
            try:
                existing = json.loads(self._config_path.read_text(encoding="utf-8"))
                if "feeds" not in existing or not isinstance(existing["feeds"], list):
                    existing = {"feeds": []}
            except (json.JSONDecodeError, OSError):
                existing = {"feeds": []}

        already_present = any(
            f.get("url") == url for f in existing["feeds"]
        )
        if not already_present:
            existing["feeds"].append(
                {"name": parsed.netloc, "url": url}
            )

        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(existing, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(self._config_path, 0o600)
        except OSError:
            pass

    def sync(
        self, *, since: Optional[datetime] = None, cursor: Optional[str] = None
    ) -> Iterator[Document]:
        """Yield Documents for recent items across all configured feeds."""
        feeds = self._load_config()

        for feed in feeds:
            feed_name = feed.get("name", "Unknown Feed")
            feed_url = feed.get("url", "")
            if not feed_url:
                continue

            try:
                xml_text = _fetch_feed(feed_url)
            except httpx.HTTPError:
                continue

            items = _parse_rss_items(xml_text)
            for item in items:
                pub_dt = _parse_pub_date(item["pubDate"])

                # Filter by since if the date is parseable
                if since and pub_dt and pub_dt.replace(tzinfo=None) < since:
                    continue

                title = item["title"] or "Untitled"
                doc_id = f"rss-{feed_name}-{title[:40]}"

                yield Document(
                    doc_id=doc_id,
                    source="news_rss",
                    doc_type="article",
                    content=item["description"],
                    title=title,
                    timestamp=pub_dt or datetime.now(),
                    url=item["link"] or None,
                    metadata={"feed_name": feed_name},
                )

        self._status.state = "idle"
        self._status.last_sync = datetime.now()

    def sync_status(self) -> SyncStatus:
        return self._status
