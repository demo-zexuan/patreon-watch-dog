# Patreon Watch Dog (AstrBot plugin)

An [AstrBot](https://github.com/AstrBotDevs/AstrBot) plugin that tracks Patreon
creator updates and sends notifications to one or more Telegram groups on a
configurable schedule.

I. Features

    1. Track multiple Patreon creators (campaigns) with a friendly name each
    2. Poll the Patreon API v2 on a configurable interval
    3. Send customizable messages to multiple Telegram chats
    4. One-click test report: collect post titles into a Markdown table and
       send it to the configured Telegram chats
    5. WebUI page with a "One-click test" button (AstrBot Plugin Pages)
    6. Admin commands for status, manual scan, campaign discovery and tests
    7. Persistent last-seen state so only new posts are notified

## Installation

1. Clone this repository into the AstrBot plugin folder:

   ```bash
   cd /path/to/AstrBot
   mkdir -p data/plugins
   cd data/plugins
   git clone https://github.com/zexuan.peng/patreon-watch-dog
   ```

2. Restart AstrBot (or reload the plugin from the WebUI).
3. Open the plugin configuration in the WebUI and fill in the settings below.

## Configuration

| Key | Type | Description |
| --- | --- | --- |
| `patreon_access_token` | string | Patreon OAuth2 access token with the `campaigns` scope. |
| `scan_interval_minutes` | int | Poll interval in minutes (default 30). |
| `scan_enabled` | bool | Enable the automatic scheduled scan (default true). |
| `notify_on_first_scan` | bool | Notify about posts found on the first scan (default false). |
| `max_posts_per_check` | int | Max new posts notified per creator per scan (default 5). |
| `creators` | template list | One entry per creator: `campaign_id` (required) and `display_name`. |
| `telegram_bot_token` | string | Bot token from @BotFather. |
| `telegram_chat_ids` | list | Telegram group/channel IDs, e.g. `-1001234567890`. |
| `message_template` | text | Notification template (see placeholders below). |
| `telegram_parse_mode` | string | `HTML`, `Markdown`, `MarkdownV2` or empty for plain text. |
| `request_timeout_seconds` | int | HTTP timeout for Patreon/Telegram requests (default 30). |

### Template placeholders

`{creator_name}`, `{post_title}`, `{post_url}`, `{published_at}`,
`{post_content}` (truncated to 500 chars).

Default template:

```
🔔 {creator_name} posted a new update!
📄 {post_title}
🔗 {post_url}
```

## Getting your credentials

1. **Patreon token** — register an **API v2 client** on the
   [Clients & API Keys](https://www.patreon.com/portal/registration/register-clients)
   page (a Patreon creator account is required; you do not need to launch
   your creator page). Then copy the **Creator's Access Token** from the
   client page. A v2 client token automatically gets all v2 scopes,
   including `campaigns.posts`, which is needed to read posts.
   > The old `patreon.com/portal/api/access-token` page has been retired;
   > the Clients & API Keys page above is the current entry point.
2. **Campaign ID** — run `/patreon campaigns` after configuring the token, or
   read the numeric ID from the creator's Patreon URL.
3. **Telegram bot token** — talk to @BotFather, create a bot and copy the token.
4. **Chat ID** — forward a message from the target group to @userinfobot, or
   call `getUpdates` on the bot API.

## Commands

All commands require admin permissions (see the AstrBot permission settings):

- `/patreon status` — show the current status and the last scan result
- `/patreon scan` — run a scan immediately
- `/patreon report` — collect the latest post titles of every configured
  creator, build a Markdown table, **show it in the chat** and send it to the
  configured Telegram chats
- `/patreon campaigns` — list campaigns accessible to your token
- `/patreon test` — send a test notification to every configured chat
- `/patreon help` — show command help

## One-click test in the WebUI

Open the plugin detail page in the AstrBot WebUI and select the **test-report**
page. It provides a **One-click test** button that:

1. Fetches the latest posts of every configured creator
2. Builds a Markdown table (`Creator | Title | Published`)
3. Sends the table (wrapped in a code block) to every configured Telegram chat
4. Shows the result summary and a preview on the page

The page requires a recent AstrBot that supports Plugin Pages; the plugin
itself still works on older versions (the button is simply unavailable).

## Notes

- The first scan records the current latest posts without sending
  notifications. Set `notify_on_first_scan` to change this behavior.
- When `telegram_parse_mode` is set, escape reserved characters in your
  template (see the Telegram Bot API documentation).
- Follow the Patreon API terms of service and respect rate limits.

## License

Add a license of your choice before publishing this plugin.
