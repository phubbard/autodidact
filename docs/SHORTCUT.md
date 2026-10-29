# "Send to Autodidact" — the share-sheet Shortcut

A Shortcut that appears in the share sheet on iPhone, iPad and Mac and POSTs the shared link to `POST /ingest-url`. The server fetches the page, extracts the text and stores it like any other capture. This is the path for anything that is not a browser tab you dwelled on: **Apple News stories** (saved or otherwise), links from Mail and Messages, Reader-mode pages on the phone, and anything else with a Share button.

Build it once and it syncs to all devices via iCloud. There is no `.shortcut` file in the repo because signed shortcuts are tied to the account that signed them, but `docs/make_shortcut.py` generates one on a Mac:

```sh
python3 docs/make_shortcut.py
shortcuts sign --mode anyone --input "Send to Autodidact (unsigned).shortcut" --output "Send to Autodidact.shortcut"
open "Send to Autodidact.shortcut"      # Shortcuts → Add Shortcut
```

The token is not embedded: after importing, expand "Get contents of" and paste it over `PASTE-TOKEN-HERE` in the Authorization header. The manual recipe below builds the same thing by hand.

## The endpoint

```
POST https://autodidact.phfactor.net/ingest-url
Authorization: Bearer <token>
Content-Type: application/json

{"url": "https://apple.news/A…", "client": "iPhone"}
```

Optional fields: `shared` (the raw shared content as text; the server takes the first link from it when `url` has none, which is how Apple News shares arrive), `input_type` (for error messages), `title` (overrides the page's), `note` (prepended to the stored text, so a word you add is searchable later), `source` (`news` or `shared`; defaults to `news` for `apple.news` links and `shared` otherwise).

Reply: `{"ok", "message", "id", "created", "new_visit", "url", "title", "source", "via", "chars"}`. `message` is ready to show ("Saved: <title>", "Already saved: <title>", or with "(title and link only; no article text)" for a News+ exclusive). Every failure is JSON too, `{"ok": false, "error", "message": "Not saved: <reason>"}`, including a wrong token and unexpected server errors, so the Shortcut only ever displays `message`. `url` is where the story was actually fetched from (the publisher, after unwrapping an Apple News link), `via` is the apple.news link when one was unwrapped, `chars` is how much text was stored. Errors: `400` bad URL or source, `502` the page could not be fetched or was not HTML (a PDF, say), `401` wrong token.

Quick check from a terminal before building the shortcut:

```sh
curl -sS -X POST https://autodidact.phfactor.net/ingest-url \
  -H "Authorization: Bearer $AUTODIDACT_TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://apple.news/<a saved story link>","client":"curl"}'
```

## Building the Shortcut

Shortcuts app → `+` → name it **Send to Autodidact**. Open the shortcut's info sheet (ⓘ) and turn on **Show in Share Sheet**; under "Share Sheet Types" keep only **URLs** and **Safari web pages** ticked. Do not add Text, Rich text or Articles: Apple News then shares the story as text with no link in it, "Get URLs" finds nothing, and the shortcut stops without a word (2026-10-28).

Actions, in order:

1. **Receive** `URLs, Safari web pages` **input from** `Share Sheet`. In "If there's no input": `Get Clipboard` (so running it from the Shortcuts app with a copied link also works).
2. **Get URLs from** `Shortcut Input`. (For a Safari page this yields the page URL; for Apple News it yields the `apple.news` link; for text it finds any URL inside.)
3. **Get Device Details** → `Device Name`. (Shows up as the "client" on the server's `/debug` page.)
4. **Get Contents of URL** with:
   - URL: `https://autodidact.phfactor.net/ingest-url`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <token from /srv/autodidact/env>`
   - Request Body: `JSON`
     - `url` → the **URLs** variable from step 2. Do not put **Get Item from List** in front of it: with no link found, that action ends the shortcut silently. The server takes the first link itself.
     - `shared` → **Shortcut Input** (the server looks for a link here when `url` is empty, and quotes it in the error otherwise)
     - `client` → the **Device Name** variable from step 3
5. **Get Dictionary Value** `message` **in** `Contents of URL`.
6. **Show Notification**: the Dictionary Value, with "Play Sound" off. Errors arrive as "Not saved: <reason>".

Optionally insert an **Ask for Input** (Text, prompt "Note?", default empty) before step 4 and send it as `note`; that is how you tag a story with the word you'll remember it by. It costs a tap per share, so most people leave it out and rely on the title and text.

## Using it

- **Apple News** (iPhone or Mac): on a story, Share → scroll the action row → **Send to Autodidact**. Saving a story in News and sending it to Autodidact are separate gestures; News has no hook that fires on save. For stories with a web version the publisher's text is stored; for News+ exclusives the server stores the title, the blurb, and the apple.news link, and says in the text that nothing more could be fetched.
- **Safari on iPhone**: Share → Send to Autodidact. Only needed for pages the Safari extension did not capture (short dwell, or a page you want tagged with a note).
- **Mail, Messages, anywhere with a link**: long-press the link → Share. Sharing a block of text does not offer the shortcut (it takes only URLs); copy the text and run the shortcut from the Shortcuts app or widget instead, and it finds the link on the clipboard.
- **Mac**: Share menu in Safari, News and Notes shows Shortcuts share-sheet entries once the shortcut has synced. Or select a link and run it from the Shortcuts menu bar item.

Away from home it works over Tailscale (the Caddy rule admits `100.64.0.0/10`); without Tailscale the shortcut fails with a connection error and nothing is stored — unlike the extension, there is no queue, so re-share later.

## Caveats

- The server fetches as `Autodidact/0.1` with no cookies, so **paywalled publishers return the teaser**. What you read inside Apple News+ is not retrievable; what is stored is whatever the publisher's public page shows.
- `apple.news` links forward non-Apple clients to the publisher via a meta-refresh or script redirect. The server follows one hop. If Apple changes that page format, stories start landing as "No article text could be fetched" with the apple.news link; `extract.unwrap_apple_news()` is the one function to update.
- Dedup is by normalised URL *and* text. The server's extraction rarely matches a feed excerpt or the browser extension's text exactly, so sharing a story you also got via FreshRSS or the extension usually adds a second row for the same URL (with the fuller text) rather than a visit on the first. Search shows both. Sharing the same link twice does dedup.
