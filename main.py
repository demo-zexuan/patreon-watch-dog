"""
Patreon Watch Dog (AstrBot plugin)

Track Patreon creator updates and notify configured Telegram groups
on a configurable schedule.

I. Features
    1. Track multiple Patreon creators (campaigns) through templates
    2. Poll the Patreon API v2 on a configurable interval
    3. Send customisable notifications to multiple Telegram chats
    4. Provide admin commands for status, manual scan and tests

Requirements: AstrBot >= 4.10.4 (template_list config schema support)

@author zexuan.peng
@created 2026-09-01
"""

import asyncio
import json
import re
import string
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# Plugin Pages Web API helpers. Pages support needs a recent AstrBot,
# so keep the import optional to stay compatible with older versions.
try:
    from astrbot.api.web import error_response, json_response

    _WEB_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the AstrBot version
    _WEB_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_NAME = "astrbot_plugin_patreon_watch_dog"

PATREON_API_BASE = "https://www.patreon.com/api/oauth2/v2"
TELEGRAM_API_BASE = "https://api.telegram.org"

DEFAULT_MESSAGE_TEMPLATE = (
    "🔔 {creator_name} posted a new update!\n"
    "📄 {post_title}\n"
    "🔗 {post_url}"
)

STARTUP_DELAY_SECONDS = 10
MAX_TRACKED_POSTS = 20
MAX_CONTENT_CHARS = 500
SCAN_LIMIT = 10
REPORT_POSTS_LIMIT = 5
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_KEYBOARD_BLOCK_LIMIT = 3800

KV_KEY_LAST_SEEN = "last_seen_posts:"
KV_KEY_LAST_SCAN_TIME = "last_scan_time"
KV_KEY_LAST_SCAN_RESULT = "last_scan_result"

_COMMAND_PREFIX_RE = re.compile(r"^[/\\!]?patreon\b", re.IGNORECASE)


class PatreonApiError(Exception):
    """Raised when the Patreon API returns an unexpected response."""


class PatreonClient:
    """Minimal async client for the Patreon API v2."""

    def __init__(self, access_token: str, timeout_seconds: int) -> None:
        self._access_token = access_token
        self._timeout_seconds = max(5, timeout_seconds)

    def _headers(self) -> dict[str, str]:
        # Patreon drops requests without an informative User-Agent header.
        return {
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": f"{PLUGIN_NAME} (AstrBot plugin)",
        }

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: dict[str, str],
        action: str,
    ) -> dict[str, Any]:
        """Perform an authorised GET request and return the JSON payload.

        I. Send the request
            1. Attach the bearer token
            2. Apply the per-request timeout
        II. Validate the response
            1. Non-200 responses raise PatreonApiError
            2. Malformed JSON raises PatreonApiError
        """
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        try:
            async with session.get(
                url, params=params, headers=self._headers(), timeout=timeout
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise PatreonApiError(
                        f"Patreon API failed to {action}: HTTP {resp.status} ({body})"
                    )
                return await resp.json()
        except PatreonApiError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise PatreonApiError(f"Patreon API request failed to {action}: {exc}") from exc

    async def get_campaigns(
        self, session: aiohttp.ClientSession
    ) -> list[dict[str, str]]:
        """List campaigns the token can access.

        Args:
            session: Shared aiohttp session.

        Returns:
            A list of {"id", "name", "url"} dictionaries.
        """
        payload = await self._get_json(
            session,
            f"{PATREON_API_BASE}/campaigns",
            {"fields[campaign]": "name,url,creation_name"},
            "list campaigns",
        )
        campaigns: list[dict[str, str]] = []
        for item in payload.get("data", []):
            attributes = item.get("attributes", {}) or {}
            campaigns.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(
                        attributes.get("creation_name")
                        or attributes.get("name")
                        or ""
                    ),
                    "url": str(attributes.get("url") or ""),
                }
            )
        return campaigns

    async def get_latest_posts(
        self, session: aiohttp.ClientSession, campaign_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """Fetch the most recent posts of a campaign.

        I. Call the posts endpoint
            1. Request the fields used by the template system
            2. Sort newest first and limit the page size
        II. Normalise each post into a plain dictionary

        Args:
            session: Shared aiohttp session.
            campaign_id: Numeric Patreon campaign ID.
            limit: Maximum number of posts to fetch.

        Returns:
            A list of post dicts with id/title/url/content/published_at.
        """
        page_size = max(1, min(int(limit), 50))
        payload = await self._get_json(
            session,
            f"{PATREON_API_BASE}/campaigns/{campaign_id}/posts",
            {
                "fields[post]": "title,url,content,published_at,is_public",
                "sort": "-published_at",
                "page[count]": str(page_size),
            },
            f"fetch posts of campaign {campaign_id}",
        )
        posts: list[dict[str, Any]] = []
        for item in payload.get("data", []):
            attributes = item.get("attributes", {}) or {}
            posts.append(
                {
                    "id": str(item.get("id") or ""),
                    "title": str(attributes.get("title") or "(no title)"),
                    "url": str(attributes.get("url") or ""),
                    "content": str(attributes.get("content") or ""),
                    "published_at": str(attributes.get("published_at") or ""),
                }
            )
        return posts


class RssFeedError(Exception):
    """Raised when an RSS feed cannot be fetched or parsed."""


def _rss_parse_datetime(value: str) -> str:
    """Convert an RSS/Atom timestamp into an ISO UTC string.

    I. Try ISO 8601 first (Atom feeds use updated)
    II. Fall back to RFC 2822 (RSS 2.0 uses pubDate)
    """
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return value


def parse_rss_feed(xml_text: str, limit: int) -> list[dict[str, Any]]:
    """Parse an RSS 2.0 (or Atom) feed into normalised posts.

    I. Parse the XML document
        1. RSS 2.0 items live under channel/item
        2. Atom entries live under feed/entry
    II. Normalise each item
        1. id falls back to the item link
        2. published_at is converted from RFC 2822 to ISO UTC
        3. description is kept as the post content

    Args:
        xml_text: Raw feed content.
        limit: Maximum number of items to return.

    Returns:
        A list of post dicts with id/title/url/content/published_at.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RssFeedError(f"Invalid RSS XML: {exc}") from exc

    items: list[ET.Element] = []
    if root.tag.endswith("rss"):
        for channel in root.findall("channel"):
            items.extend(channel.findall("item"))
    elif root.tag.endswith("feed"):
        for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
            items.append(entry)

    posts: list[dict[str, Any]] = []
    atom_ns = "{http://www.w3.org/2005/Atom}"
    for item in items[:max(1, min(int(limit), 100))]:
        title = (item.findtext("title") or "").strip()
        if not title:
            title = (item.findtext(f"{atom_ns}title") or "").strip()
        link_el = item.find("link")
        if link_el is None:
            link_el = item.find(f"{atom_ns}link")
        link = (link_el.text or "").strip() if link_el is not None else ""
        # Atom namespace link may carry an href attribute instead.
        if not link:
            link = (link_el.get("href") or "").strip() if link_el is not None else ""
        guid_el = item.find("guid")
        if guid_el is None:
            guid_el = item.find(f"{atom_ns}id")
        guid = (guid_el.text or "").strip() if guid_el is not None else ""
        pub_date = (
            item.findtext("pubDate")
            or item.findtext("published")
            or item.findtext(f"{atom_ns}published")
            or item.findtext("updated")
            or item.findtext(f"{atom_ns}updated")
            or ""
        ).strip()
        description = (
            item.findtext("description")
            or item.findtext("content")
            or item.findtext(f"{atom_ns}content")
            or ""
        ).strip()

        posts.append(
            {
                "id": guid or link or title,
                "title": title or "(no title)",
                "url": link,
                "content": description,
                "published_at": _rss_parse_datetime(pub_date),
            }
        )
    return posts


class RssClient:
    """Minimal async client that turns an RSS feed into post dicts."""

    def __init__(self, timeout_seconds: int) -> None:
        self._timeout_seconds = max(5, timeout_seconds)

    async def fetch_latest_posts(
        self, session: aiohttp.ClientSession, rss_url: str, limit: int
    ) -> list[dict[str, Any]]:
        """Fetch and parse the latest items of an RSS feed.

        Args:
            session: Shared aiohttp session.
            rss_url: RSS feed URL (may contain auth parameters).
            limit: Maximum number of posts to fetch.

        Returns:
            A list of post dicts in feed order.
        """
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        try:
            async with session.get(rss_url, timeout=timeout) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise RssFeedError(
                        f"RSS feed returned HTTP {resp.status} ({body})"
                    )
                xml_text = await resp.text()
        except RssFeedError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise RssFeedError(f"RSS feed request failed: {exc}") from exc
        return parse_rss_feed(xml_text, limit)


class TelegramNotifier:
    """Send text messages through the Telegram Bot API."""

    def __init__(self, bot_token: str, parse_mode: str, timeout_seconds: int) -> None:
        self._bot_token = bot_token
        self._parse_mode = parse_mode
        self._timeout_seconds = max(5, timeout_seconds)

    async def send_text(
        self,
        session: aiohttp.ClientSession,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
    ) -> bool:
        """Send a text message to a chat.

        I. Build the request
            1. chat_id accepts an integer ID or a channel username
            2. parse_mode is only included when configured
            3. An explicit parse_mode overrides the configured default
        II. Handle the response
            1. A successful response returns True
            2. HTTP 429 retries once after retry_after seconds
            3. Any other failure returns False

        Returns:
            True when Telegram confirms the message was sent.
        """
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        effective_parse_mode = self._parse_mode if parse_mode is None else parse_mode
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if effective_parse_mode:
            payload["parse_mode"] = effective_parse_mode

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        for attempt in range(2):
            try:
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    data = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Telegram sendMessage request failed: %s", exc)
                return False

            if resp.status == 200 and data.get("ok"):
                return True

            if resp.status == 429:
                retry_after = int(
                    (data.get("parameters") or {}).get("retry_after", 3)
                )
                if retry_after <= 30:
                    await asyncio.sleep(retry_after)
                    continue

            logger.error(
                "Telegram sendMessage failed: HTTP %s (%s)",
                resp.status,
                str(data)[:300],
            )
            return False
        return False


class _SafeFormatter(string.Formatter):
    """Formatter that renders missing template fields as empty strings."""

    def get_field(self, field_name: str, args: tuple, kwargs: dict) -> tuple:
        try:
            return super().get_field(field_name, args, kwargs)
        except (KeyError, AttributeError):
            return "", None


def render_template(template: str, values: dict[str, str]) -> str:
    """Render a notification template with the given values.

    Missing placeholders render as empty strings so a broken
    template never crashes the scanner.

    Args:
        template: User-defined message template.
        values: Placeholder name to value mapping.

    Returns:
        The rendered message text.
    """
    try:
        return _SafeFormatter().vformat(template, (), values)
    except Exception:
        return values.get("post_url", template)


def escape_md_cell(value: str) -> str:
    """Escape a value for a Markdown table cell.

    I. Flatten the value
        1. Collapse newlines into spaces to keep one row per post
    II. Escape the pipe character so the table structure stays valid
    """
    text = (value or "").strip().replace("\n", " ").replace("|", "\\|")
    return text or "-"


def _display_width(text: str) -> int:
    """Return the terminal display width of a string.

    East-Asian wide characters (CJK, full width, many emoji) occupy two
    columns in monospace clients, so they must count as width 2 when
    padding the table cells.
    """
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad_display(text: str, width: int) -> str:
    """Pad a string with spaces up to a target display width."""
    return text + " " * max(0, width - _display_width(text))


def build_markdown_table(rows: list[dict[str, str]]) -> str:
    """Build a well-aligned Markdown table with Creator/Title/Published.

    I. Normalise the cells
        1. Apply Markdown escaping to every cell
        2. Missing values render as "-"
    II. Align the columns
        1. Pad every cell to the longest display width in its column
        2. Wide CJK/emoji characters are counted as two columns so the
           table lines up in monospace clients (Telegram code blocks,
           terminal, Markdown preview)
    III. Assemble the table
        1. Header row
        2. Separator row widened to match the column widths
        3. One row per post

    Args:
        rows: One dict per post with creator_name/post_title/published_at.

    Returns:
        The aligned Markdown table text without a code-block wrapper.
    """
    headers = ["Creator", "Title", "Published"]
    if not rows:
        # Render a placeholder row so an empty report still looks tidy
        # instead of showing a bare table skeleton.
        return (
            "| " + " | ".join(headers) + " |\n"
            + "| " + " | ".join(["---"] * len(headers)) + " |\n"
            + "| " + " | ".join(["—"] * len(headers)) + " |"
        )

    cells = [headers]
    for row in rows:
        cells.append(
            [
                escape_md_cell(row.get("creator_name", "")),
                escape_md_cell(row.get("post_title", "")),
                escape_md_cell(row.get("published_at", "")),
            ]
        )

    widths = [
        max(_display_width(cell) for cell in column) for column in zip(*cells)
    ]
    lines = [
        "| "
        + " | ".join(_pad_display(cell, width) for cell, width in zip(headers, widths))
        + " |",
        "| " + " | ".join("-" * width for width in widths) + " |",
    ]
    for row_cells in cells[1:]:
        lines.append(
            "| "
            + " | ".join(
                _pad_display(cell, width) for cell, width in zip(row_cells, widths)
            )
            + " |"
        )
    return "\n".join(lines)


def build_report_markdown(rows: list[dict[str, str]], generated_at: str = "") -> str:
    """Build the complete one-click test report document.

    I. Heading
        1. Report title
        2. Generation timestamp
    II. Body
        1. The aligned Markdown table
        2. A summary line with the total post count

    Args:
        rows: One dict per post with creator_name/post_title/published_at.
        generated_at: ISO timestamp (UTC) of the report generation.

    Returns:
        The full report document used by the chat command, the Telegram
        notification and the WebUI page preview.
    """
    lines = ["📊 Patreon Posts Test Report"]
    if generated_at:
        lines.append(f"Generated: {generated_at}")
    lines.append("")
    lines.append(build_markdown_table(rows))
    lines.append("")
    lines.append(f"Total: {len(rows)} posts from tracked creators.")
    if not rows:
        lines.append(
            "No posts returned. The Patreon API returns an empty list for "
            "campaigns the token cannot read (e.g. a third-party creator); "
            "the token must own the campaign or be explicitly authorized."
        )
    return "\n".join(lines)


def split_markdown_messages(table: str) -> list[str]:
    """Split a Markdown table into Telegram-safe code-block messages.

    I. Group table lines into chunks
        1. Keep every message below the Telegram length limit
        2. Never split a single table line
    II. Wrap each chunk in a MarkdownV2 code block

    Args:
        table: The Markdown table text.

    Returns:
        A list of messages, each wrapped in triple backticks.
    """
    if not table.strip():
        return []
    lines = table.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        line_size = len(line) + 1
        if current and current_size + line_size > TELEGRAM_KEYBOARD_BLOCK_LIMIT:
            chunks.append("\n".join(current))
            current = []
            current_size = 0
        current.append(line)
        current_size += line_size
    if current:
        chunks.append("\n".join(current))
    return [f"```\n{chunk}\n```" for chunk in chunks]


@register(
    "astrbot_plugin_patreon_watch_dog",
    "zexuan.peng",
    "Track Patreon creator updates and notify Telegram groups.",
    "1.4.0",
)
class PatreonWatchDog(Star):
    """AstrBot plugin that watches Patreon creators for updates."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._http_session: aiohttp.ClientSession | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._patreon: PatreonClient | None = None
        self._rss: RssClient | None = None
        self._notifier: TelegramNotifier | None = None

        # Register the backend endpoint used by the "One-click test report"
        # page in the AstrBot WebUI (requires a recent AstrBot).
        if _WEB_AVAILABLE:
            context.register_web_api(
                f"/{PLUGIN_NAME}/report",
                self.page_run_report,
                ["POST"],
                "Run one-click test report",
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Set up HTTP clients and start the scheduler when enabled."""
        timeout_seconds = self._cfg_int("request_timeout_seconds", 30)
        self._http_session = aiohttp.ClientSession()
        self._patreon = PatreonClient(
            self._cfg_str("patreon_access_token"), timeout_seconds
        )
        self._rss = RssClient(timeout_seconds)
        self._notifier = TelegramNotifier(
            self._cfg_str("telegram_bot_token"),
            self._cfg_str("telegram_parse_mode", ""),
            timeout_seconds,
        )

        if self._cfg_bool("scan_enabled", True):
            self._scheduler_task = asyncio.create_task(
                self._scheduler_loop(), name="patreon-watch-dog-scheduler"
            )
            logger.info(
                "Patreon Watch Dog scheduler started (interval: %s min).",
                self._cfg_int("scan_interval_minutes", 30),
            )
        else:
            logger.info("Patreon Watch Dog scheduler is disabled.")

    async def terminate(self) -> None:
        """Cancel the scheduler task and close the HTTP session."""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Patreon Watch Dog scheduler stopped with errors.")
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _cfg_str(self, key: str, default: str = "") -> str:
        try:
            value = self.config.get(key, default)
            return str(value) if value is not None else default
        except Exception:
            return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return max(1, int(self.config.get(key, default) or default))
        except Exception:
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        try:
            return bool(self.config.get(key, default))
        except Exception:
            return default

    def _cfg_list(self, key: str) -> list[Any]:
        try:
            value = self.config.get(key, [])
            if isinstance(value, list):
                return value
            return [value] if value is not None else []
        except Exception:
            return []

    def _get_creators(self) -> list[dict[str, str]]:
        """Normalise the configured creator templates.

        I. Collect entries that carry a valid campaign ID
        II. Fall back to the campaign ID when no display name is set
        III. Keep an optional RSS URL that overrides the API source
        """
        creators: list[dict[str, str]] = []
        for item in self._cfg_list("creators"):
            if not isinstance(item, dict):
                continue
            campaign_id = str(item.get("campaign_id") or "").strip()
            if not campaign_id:
                continue
            display_name = str(item.get("display_name") or "").strip()
            rss_url = str(item.get("rss_url") or "").strip()
            creators.append(
                {
                    "campaign_id": campaign_id,
                    "display_name": display_name or campaign_id,
                    "rss_url": rss_url,
                }
            )
        return creators

    def _get_chat_ids(self) -> list[str]:
        """Normalise the configured Telegram chat IDs."""
        chat_ids: list[str] = []
        for item in self._cfg_list("telegram_chat_ids"):
            value = str(item).strip()
            if value:
                chat_ids.append(value)
        return chat_ids

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        """Poll Patreon periodically until the plugin is stopped.

        I. Wait for the platform to be fully ready
        II. Poll forever
            1. Run one scan and protect it against unexpected errors
            2. Sleep for the configured interval (read every cycle)
        """
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                await self._run_scan("scheduled")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Patreon Watch Dog scheduled scan failed.")

            interval_seconds = self._cfg_int("scan_interval_minutes", 30) * 60
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # Scan pipeline
    # ------------------------------------------------------------------

    async def _run_scan(self, trigger: str = "manual") -> dict[str, Any]:
        """Scan every configured creator and notify about new posts.

        I. Validate configuration
            1. Creators and Telegram destinations must be configured
        II. Scan each creator
            1. Fetch the latest posts
            2. Pick posts that have not been seen before
            3. Notify every configured chat for each new post
            4. Remember the newest post IDs
        III. Persist and return the scan summary

        Args:
            trigger: "scheduled", "manual" or another source label.

        Returns:
            The scan summary used for logging and status display.
        """
        summary: dict[str, Any] = {
            "trigger": trigger,
            "time": self._now_iso(),
            "creators": 0,
            "new_posts": 0,
            "sent": 0,
            "failed": 0,
            "errors": [],
        }

        creators = self._get_creators()
        chat_ids = self._get_chat_ids()
        if not creators:
            summary["errors"].append("No creators configured.")
            await self._persist_scan_result(summary)
            return summary
        if not chat_ids:
            summary["errors"].append("No Telegram chat IDs configured.")
            await self._persist_scan_result(summary)
            return summary

        session = self._http_session
        if (
            session is None
            or self._notifier is None
            or (self._patreon is None and self._rss is None)
        ):
            summary["errors"].append("Plugin clients are not initialised.")
            await self._persist_scan_result(summary)
            return summary

        template = self._cfg_str("message_template", DEFAULT_MESSAGE_TEMPLATE)
        summary["creators"] = len(creators)

        for creator in creators:
            try:
                posts = await self._fetch_posts_for_creator(
                    session, creator, SCAN_LIMIT
                )
                new_posts = await self._select_new_posts(creator, posts)
                if new_posts:
                    summary["new_posts"] += len(new_posts)
                    for post in new_posts:
                        text = render_template(
                            template, self._build_template_values(creator, post)
                        )
                        for chat_id in chat_ids:
                            ok = await self._notifier.send_text(session, chat_id, text)
                            if ok:
                                summary["sent"] += 1
                            else:
                                summary["failed"] += 1
                await self._remember_last_seen(creator["campaign_id"], posts)
            except Exception as exc:
                logger.warning(
                    "Failed to scan creator %s: %s", creator["campaign_id"], exc
                )
                summary["errors"].append(f"{creator['campaign_id']}: {exc}")

        await self._persist_scan_result(summary)
        return summary

    async def _select_new_posts(
        self, creator: dict[str, str], posts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Determine posts that have not been notified yet.

        I. Load the previously seen post IDs
        II. Decide the candidate posts
            1. The first scan only records unless notify_on_first_scan is true
            2. Later scans use unseen posts only
        III. Sort chronologically and cap the batch size
        """
        campaign_id = creator["campaign_id"]
        seen_key = f"{KV_KEY_LAST_SEEN}{campaign_id}"
        seen_raw = await self.get_kv_data(seen_key, None)
        is_first_scan = seen_raw is None

        seen_ids: set[str] = set()
        if seen_raw:
            try:
                parsed = json.loads(seen_raw)
                if isinstance(parsed, list):
                    seen_ids = {str(item) for item in parsed}
            except (TypeError, ValueError):
                seen_ids = set()

        if is_first_scan:
            if not self._cfg_bool("notify_on_first_scan", False):
                return []
            candidates = list(posts)
        else:
            candidates = [post for post in posts if post["id"] not in seen_ids]

        # Publish older posts first so updates arrive in chronological order.
        candidates.sort(key=lambda post: post.get("published_at") or "")
        limit = self._cfg_int("max_posts_per_check", 5)
        return candidates[:limit]

    async def _remember_last_seen(
        self, campaign_id: str, posts: list[dict[str, Any]]
    ) -> None:
        """Persist the newest post IDs so the next scan can diff them."""
        ids = [post["id"] for post in posts[:MAX_TRACKED_POSTS]]
        await self.put_kv_data(f"{KV_KEY_LAST_SEEN}{campaign_id}", json.dumps(ids))

    async def _fetch_posts_for_creator(
        self,
        session: aiohttp.ClientSession,
        creator: dict[str, str],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch posts from the configured source for one creator.

        I. RSS source wins when rss_url is configured
        II. Otherwise fall back to the Patreon API
        """
        if creator.get("rss_url"):
            if self._rss is None:
                raise RuntimeError("RSS client is not initialised.")
            return await self._rss.fetch_latest_posts(
                session, creator["rss_url"], limit
            )
        if self._patreon is None:
            raise RuntimeError("Patreon client is not initialised.")
        return await self._patreon.get_latest_posts(
            session, creator["campaign_id"], limit
        )

    def _build_template_values(
        self, creator: dict[str, str], post: dict[str, Any]
    ) -> dict[str, str]:
        """Build the placeholder values exposed to the message template."""
        return {
            "creator_name": creator["display_name"],
            "post_title": post.get("title", ""),
            "post_url": post.get("url", ""),
            "published_at": self._format_published_at(post.get("published_at", "")),
            "post_content": self._truncate(post.get("content", ""), MAX_CONTENT_CHARS),
        }

    @staticmethod
    def _format_published_at(value: str) -> str:
        """Convert an ISO timestamp into a human-readable UTC string."""
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            return value

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Trim long post content so Telegram messages stay readable."""
        text = text or ""
        return text if len(text) <= limit else text[:limit] + "…"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _persist_scan_result(self, summary: dict[str, Any]) -> None:
        """Store the latest scan time and summary in the plugin KV store."""
        try:
            await self.put_kv_data(KV_KEY_LAST_SCAN_TIME, summary.get("time", ""))
            await self.put_kv_data(
                KV_KEY_LAST_SCAN_RESULT, json.dumps(summary, ensure_ascii=False)
            )
        except Exception as exc:
            logger.warning("Failed to persist scan summary: %s", exc)

    # ------------------------------------------------------------------
    # One-click test report
    # ------------------------------------------------------------------

    async def _run_report(self) -> dict[str, Any]:
        """Fetch latest posts of every creator and send a Markdown table.

        I. Validate configuration
            1. Creators and Telegram destinations must be configured
        II. Collect posts
            1. Fetch the latest posts of every configured creator
            2. Add one row per post to the report table
        III. Send the report
            1. Split the table into Telegram-safe code-block messages
            2. Send every message to every configured chat

        Returns:
            Report payload used by the WebUI page and the chat command.
        """
        report: dict[str, Any] = {
            "ok": False,
            "creators": 0,
            "posts": 0,
            "sent": 0,
            "failed": 0,
            "markdown": "",
            "errors": [],
            "notices": [],
        }
        creators = self._get_creators()
        chat_ids = self._get_chat_ids()
        if not creators:
            report["errors"].append("No creators configured.")
            return report
        if not chat_ids:
            report["errors"].append("No Telegram chat IDs configured.")
            return report
        if (
            self._notifier is None
            or (self._patreon is None and self._rss is None)
        ):
            report["errors"].append("Plugin clients are not initialised.")
            return report

        session = self._http_session or object()
        rows: list[dict[str, str]] = []
        report["creators"] = len(creators)
        for creator in creators:
            try:
                posts = await self._fetch_posts_for_creator(
                    session, creator, REPORT_POSTS_LIMIT
                )
                if not posts:
                    if creator.get("rss_url"):
                        note = (
                            f"{creator['display_name']} ({creator['campaign_id']}): "
                            "RSS feed returned no items. Check that the rss_url "
                            "is valid and publicly reachable."
                        )
                    else:
                        note = (
                            f"{creator['display_name']} ({creator['campaign_id']}): "
                            "Patreon API returned 0 posts. This is expected when the "
                            "token cannot read that campaign (it must own the campaign "
                            "or be explicitly authorized by its creator)."
                        )
                    report["notices"].append(note)
                for post in posts:
                    rows.append(
                        {
                            "creator_name": creator["display_name"],
                            "post_title": post.get("title", ""),
                            "published_at": self._format_published_at(
                                post.get("published_at", "")
                            ),
                        }
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch posts for %s: %s", creator["campaign_id"], exc
                )
                report["errors"].append(f"{creator['campaign_id']}: {exc}")

        report["posts"] = len(rows)
        markdown = build_report_markdown(rows, self._now_iso())
        report["markdown"] = markdown
        for message in split_markdown_messages(markdown):
            for chat_id in chat_ids:
                ok = await self._notifier.send_text(
                    session, chat_id, message, parse_mode="MarkdownV2"
                )
                if ok:
                    report["sent"] += 1
                else:
                    report["failed"] += 1
        report["ok"] = report["failed"] == 0
        return report

    async def page_run_report(self):
        """WebUI handler: run the one-click test report."""
        try:
            return json_response(await self._run_report())
        except Exception as exc:
            logger.exception("Plugin page report failed.")
            return error_response(str(exc))

    @staticmethod
    def _format_report_result(report: dict[str, Any]) -> str:
        """Render a report payload for the chat command reply."""
        errors = report.get("errors", [])
        notices = report.get("notices", [])
        lines = [
            "Patreon test report finished:",
            f"- Creators: {report.get('creators', 0)}",
            f"- Posts collected: {report.get('posts', 0)}",
            f"- Messages sent: {report.get('sent', 0)}",
            f"- Messages failed: {report.get('failed', 0)}",
        ]
        if notices:
            lines.append("- Note:")
            for note in notices:
                lines.append(f"  • {note}")
        if errors:
            lines.append(f"- Errors: {errors}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Commands (admin only)
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("patreon")
    async def patreon_command(self, event: AstrMessageEvent):
        """Patreon Watch Dog admin commands.

        Usage: /patreon <subcommand>
        Subcommands: help, status, scan, report, campaigns, test
        """
        text = (event.message_str or "").strip()
        subcommand = _COMMAND_PREFIX_RE.sub("", text).strip().split(" ", 1)[0].lower()
        args = _COMMAND_PREFIX_RE.sub("", text).strip().split(" ", 1)

        if not subcommand or subcommand == "help":
            yield event.plain_result(self._help_text())
            return
        if subcommand == "status":
            yield event.plain_result(await self._status_text())
            return
        if subcommand == "config":
            yield event.plain_result(await self._config_text())
            return
        if subcommand == "scan":
            summary = await self._run_scan("manual")
            yield event.plain_result(self._format_summary(summary))
            return
        if subcommand == "report":
            report = await self._run_report()
            yield event.plain_result(self._format_report_result(report))
            # Show the Markdown table directly in the chat as code blocks
            # (the table is also sent to the configured Telegram chats).
            for message in split_markdown_messages(report.get("markdown", "")):
                yield event.plain_result(message)
            return
        if subcommand == "campaigns":
            yield event.plain_result(await self._campaigns_text())
            return
        if subcommand == "test":
            yield event.plain_result(await self._test_notification(args))
            return
        yield event.plain_result(self._help_text())

    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------

    def _help_text(self) -> str:
        return (
            "Patreon Watch Dog commands (admin only):\n"
            "/patreon status - show plugin status\n"
            "/patreon config - show the current configuration (secrets masked)\n"
            "/patreon scan - run a scan now\n"
            "/patreon report - show and send a Markdown table test report\n"
            "/patreon campaigns - list Patreon campaigns the token can access\n"
            "/patreon test - send a test notification to the configured chats\n"
            "/patreon help - show this help"
        )

    async def _status_text(self) -> str:
        """Build a human-readable status snapshot from config and KV data."""
        creators = self._get_creators()
        chat_ids = self._get_chat_ids()
        token_ok = bool(self._cfg_str("patreon_access_token"))
        tg_ok = bool(self._cfg_str("telegram_bot_token"))

        last_time = ""
        last_result = ""
        try:
            last_time = str(await self.get_kv_data(KV_KEY_LAST_SCAN_TIME, ""))
        except Exception:
            pass
        try:
            last_result = str(await self.get_kv_data(KV_KEY_LAST_SCAN_RESULT, ""))
        except Exception:
            pass

        lines = [
            "Patreon Watch Dog status:",
            f"- Scan enabled: {self._cfg_bool('scan_enabled', True)}",
            f"- Interval: {self._cfg_int('scan_interval_minutes', 30)} min",
            f"- Creators tracked: {len(creators)}",
            f"- Telegram bot token set: {tg_ok}",
            f"- Telegram chat IDs configured: {len(chat_ids)}",
            f"- Patreon API token set: {token_ok}",
            f"- Last scan time: {last_time or 'never'}",
        ]
        if last_result:
            try:
                parsed = json.loads(last_result)
                lines.append(
                    "- Last scan result: "
                    f"new_posts={parsed.get('new_posts', 0)}, "
                    f"sent={parsed.get('sent', 0)}, "
                    f"failed={parsed.get('failed', 0)}"
                )
            except (TypeError, ValueError):
                lines.append(f"- Last scan result: {last_result}")
        return "\n".join(lines)

    @staticmethod
    def _mask_secret(value: str) -> str:
        """Mask a sensitive value for display in the config command."""
        value = (value or "").strip()
        if not value:
            return "(not set)"
        if len(value) <= 8:
            return "••••"
        return f"{value[:4]}…{value[-4:]}"

    async def _config_text(self) -> str:
        """Render the current configuration with all secrets masked.

        I. Patreon side
            1. Access token status (masked)
            2. Tracked creators
        II. Scheduling
            1. Interval, enabled flag and batch limits
        III. Telegram side
            1. Bot token status (masked)
            2. Chat IDs and parse mode
            3. Message template (truncated)
        """
        creators = self._get_creators()
        chat_ids = self._get_chat_ids()
        template = self._cfg_str("message_template", DEFAULT_MESSAGE_TEMPLATE)
        template_snippet = template.replace("\n", " ").strip()

        lines = [
            "Patreon Watch Dog configuration:",
            "- Patreon API token: "
            f"{self._mask_secret(self._cfg_str('patreon_access_token'))}",
            f"- Scan enabled: {self._cfg_bool('scan_enabled', True)}",
            f"- Scan interval: {self._cfg_int('scan_interval_minutes', 30)} min",
            f"- Notify on first scan: {self._cfg_bool('notify_on_first_scan', False)}",
            f"- Max posts per check: {self._cfg_int('max_posts_per_check', 5)}",
            f"- Creators ({len(creators)}):",
        ]
        for creator in creators:
            lines.append(f"  • {creator['campaign_id']} ({creator['display_name']})")
        lines.append(
            "- Telegram bot token: "
            f"{self._mask_secret(self._cfg_str('telegram_bot_token'))}"
        )
        lines.append(
            f"- Telegram chat IDs ({len(chat_ids)}): "
            f"{', '.join(chat_ids) if chat_ids else '(none)'}"
        )
        lines.append(
            "- Telegram parse mode: "
            f"{self._cfg_str('telegram_parse_mode', '') or '(plain text)'}"
        )
        lines.append(f"- Message template: {template_snippet[:120]}")
        lines.append(
            f"- Request timeout: {self._cfg_int('request_timeout_seconds', 30)} s"
        )
        return "\n".join(lines)

    async def _campaigns_text(self) -> str:
        """List campaigns available to the configured Patreon token."""
        if not self._cfg_str("patreon_access_token"):
            return "Patreon API token is not configured. Set patreon_access_token first."
        session = self._http_session
        if session is None or self._patreon is None:
            return "Plugin clients are not initialised."
        try:
            campaigns = await self._patreon.get_campaigns(session)
        except Exception as exc:
            return f"Failed to list campaigns: {exc}"
        if not campaigns:
            return "No campaigns found. Check that your token has the 'campaigns' scope."
        lines = ["Accessible Patreon campaigns:"]
        for campaign in campaigns:
            name = campaign["name"] or "(unnamed)"
            lines.append(f"- {campaign['id']}: {name} ({campaign['url']})")
        return "\n".join(lines)

    async def _test_notification(self, args: list[str]) -> str:
        """Send a test notification to every configured chat."""
        chat_ids = self._get_chat_ids()
        if not chat_ids:
            return "No Telegram chat IDs configured."
        session = self._http_session
        if session is None or self._notifier is None:
            return "Plugin clients are not initialised."

        message = (
            "✅ Patreon Watch Dog test notification.\n"
            "If you can read this, Telegram notifications are configured correctly."
        )
        sent = 0
        failed = 0
        for chat_id in chat_ids:
            ok = await self._notifier.send_text(session, chat_id, message)
            if ok:
                sent += 1
            else:
                failed += 1
        return f"Test notification sent: {sent} ok, {failed} failed."

    @staticmethod
    def _format_summary(summary: dict[str, Any]) -> str:
        """Render a scan summary for the command reply."""
        trigger = summary.get("trigger", "manual")
        errors = summary.get("errors", [])
        return (
            f"Patreon scan ({trigger}) finished:\n"
            f"- Creators: {summary.get('creators', 0)}\n"
            f"- New posts: {summary.get('new_posts', 0)}\n"
            f"- Messages sent: {summary.get('sent', 0)}\n"
            f"- Messages failed: {summary.get('failed', 0)}\n"
            + (f"- Errors: {errors}" if errors else "")
        )
