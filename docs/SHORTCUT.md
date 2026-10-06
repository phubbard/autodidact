# "Send to Autodidact" — the share-sheet Shortcut

A Shortcut that appears in the share sheet on iPhone, iPad and Mac and POSTs the shared link to `POST /ingest-url`. The server fetches the page, extracts the text and stores it like any other capture. This is the path for anything that is not a browser tab you dwelled on: **Apple News stories** (saved or otherwise), links from Mail and Messages, Reader-mode pages on the phone, and anything else with a Share button.

Build it once in the Shortcuts app and it syncs to all devices via iCloud. There is no `.shortcut` file in the repo because exported shortcuts are signed and tied to the exporting account; the recipe below takes about three minutes.

## The endpoint

```
POST https://autodidact.phfactor.net/ingest-url
Authorization: Bearer <token>
Content-Type: application/json

{"url": "https://apple.news/A…", "client": "iPhone"}
```

Optional fields: `title` (overrides the page's), `note` (prepended to the stored text, so a word you add is searchable later), `source` (`news` or `shared`; defaults to `news` for `apple.news` links and `shared` otherwise).

Reply: `{"id", "created", "new_visit", "url", "title", "source", "via", "chars"}`. `url` is where the story was actually fetched from (the publisher, after unwrapping an Apple News link), `via` is the apple.news link when one was unwrapped, `chars` is how much text was stored. Errors: `400` bad URL or source, `502` the page could not be fetched or was not HTML (a PDF, say), `401` wrong token.

Quick check from a terminal before building the shortcut:

```sh
curl -sS -X POST https://autodidact.phfactor.net/ingest-url \
  -H "Authorization: Bearer $AUTODIDACT_TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://apple.news/<a saved story link>","client":"curl"}'
```

## Building the Shortcut

Shortcuts app → `+` → name it **Send to Autodidact**. Open the shortcut's info sheet (ⓘ) and turn on **Show in Share Sheet**; under "Share Sheet Types" keep **URLs**, **Safari web pages**, **Articles** and **Rich text** ticked, untick the rest.

Actions, in order:

1. **Receive** `URLs, Safari web pages, Articles, Rich text` **input from** `Share Sheet`. In "If there's no input": `Get Clipboard` (so running it from the Shortcuts app with a copied link also works).
2. **Get URLs from** `Shortcut Input`. (For a Safari page this yields the page URL; for Apple News it yields the `apple.news` link; for text it finds any URL inside.)
3. **Get Device Details** → `Device Name`. (Shows up as the "client" on the server's `/debug` page.)
4. **Get Contents of URL** with:
   - URL: `https://autodidact.phfactor.net/ingest-url`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <token from /srv/autodidact/env>`
   - Request Body: `JSON`
     - `url` → the **URLs** variable from step 2 (if it is a list, Shortcuts sends the first; wrap steps 4–5 in **Repeat with Each** over URLs if you want every link from a text selection)
     - `client` → the **Device Name** variable from step 3
5. **Get Dictionary Value** `title` **in** `Contents of URL`.
6. **Show Notification**: `Saved: ` + the Dictionary Value, with "Play Sound" off.

Optionally insert an **Ask for Input** (Text, prompt "Note?", default empty) before step 4 and send it as `note`; that is how you tag a story with the word you'll remember it by. It costs a tap per share, so most people leave it out and rely on the title and text.

## Using it

- **Apple News** (iPhone or Mac): on a story, Share → scroll the action row → **Send to Autodidact**. Saving a story in News and sending it to Autodidact are separate gestures; News has no hook that fires on save. For stories with a web version the publisher's text is stored; for News+ exclusives the server stores the title, the blurb, and the apple.news link, and says in the text that nothing more could be fetched.
- **Safari on iPhone**: Share → Send to Autodidact. Only needed for pages the Safari extension did not capture (short dwell, or a page you want tagged with a note).
- **Mail, Messages, anywhere with a link**: long-press the link → Share, or share the message and the shortcut finds the URL in the text.
- **Mac**: Share menu in Safari, News and Notes shows Shortcuts share-sheet entries once the shortcut has synced. Or select a link and run it from the Shortcuts menu bar item.

Away from home it works over Tailscale (the Caddy rule admits `100.64.0.0/10`); without Tailscale the shortcut fails with a connection error and nothing is stored — unlike the extension, there is no queue, so re-share later.

## Caveats

- The server fetches as `Autodidact/0.1` with no cookies, so **paywalled publishers return the teaser**. What you read inside Apple News+ is not retrievable; what is stored is whatever the publisher's public page shows.
- `apple.news` links forward non-Apple clients to the publisher via a meta-refresh or script redirect. The server follows one hop. If Apple changes that page format, stories start landing as "No article text could be fetched" with the apple.news link; `extract.unwrap_apple_news()` is the one function to update.
- Dedup is by normalised URL and text: a story you also read in the browser extension or via FreshRSS becomes one page with an extra visit, not a duplicate.
