# Evolution Media Send Research

Wayfinder ticket: [Find out whether the Evolution transport can send an image](https://github.com/ivzc07/bienwabot/issues/52), part of the [X-timeline news source map](https://github.com/ivzc07/bienwabot/issues/50).

The map wants a worthy tweet to reach the bien.mx group as **a screenshot plus Rebe's Spanish line, and no URL**.
Today `rebe_agent/evolution.py` has exactly one send path, `POST /message/sendText/{instance}`, so a screenshot needs a second one.
This document settles what that second path costs, against the build `bien-evo` is actually running rather than against whatever the docs happen to describe.

Research date: 2026-07.
Every claim below is either read off the live instance or off the source at the exact tag that instance runs.

---

## 0. The version is not a guess

The Coolify service `bien-evo` pins `evoapicloud/evolution-api:v2.3.7` in its compose file, and the running container agrees when asked.
An unauthenticated `GET /` on the instance answers:

```json
{"status":200,"message":"Welcome to the Evolution API, it is working!","version":"2.3.7",
 "clientName":"evolution_v2","whatsappWebVersion":"2.3000.1035194821"}
```

That matters more than it looks.
Evolution's hosted documentation has moved twice, the old `doc.evolution-api.com/v2/api-reference/...` paths now 404, and the current site at [docs.evolutionfoundation.com.br](https://docs.evolutionfoundation.com.br/en/evolution-api/send-media-message) documents a moving target rather than a pinned one.
So the primary source for everything below is the source tree at [tag 2.3.7](https://github.com/EvolutionAPI/evolution-api/tree/2.3.7), with the docs used only to corroborate the endpoint's public shape.
Where the two disagree, this document follows the source, and says so.

---

## 1. The endpoint exists, and it is one POST

`POST /message/sendMedia/{instance}` is registered in [`sendMessage.router.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/routes/sendMessage.router.ts), behind the same `apikey` header guard as `sendText`.
It is the same authentication, the same base URL, and the same `{instance}` path segment the existing client already builds.

The route is declared as `upload.single('file')`, so it accepts **either** a `multipart/form-data` body with a real file part **or** a plain JSON body.
The [official docs page](https://docs.evolutionfoundation.com.br/en/evolution-api/send-media-message) only shows the multipart form, which is misleading: the JSON path is fully supported and is the one this codebase wants, because `httpx` posting JSON is what `EvolutionClient._post` already does.

The JSON body, from [`SendMediaDto`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/dto/sendMessage.dto.ts):

```json
{
  "number": "1203...@g.us",
  "mediatype": "image",
  "media": "<raw base64, or an http(s) URL>",
  "caption": "Rebe's line goes here",
  "fileName": "tweet.png",
  "delay": 2400
}
```

`mediatype` is one of `image`, `document`, `video`, `audio`, `ptv`, and the JSON-schema validator in [`message.schema.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/validate/message.schema.ts) requires only `number` and `mediatype`.
`media` is *not* required by the schema, which is a trap worth knowing about: a body missing `media` sails past validation and is rejected one layer later by the controller with a 400 reading `Owned media must be a url or base64`.

`Content-Type: application/json` and `apikey` are the only headers needed, which is exactly the header pair `EvolutionClient` already sets.

---

## 2. Base64 and URL both work, and base64 is the right one here

The controller in [`sendMessage.controller.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/controllers/sendMessage.controller.ts) accepts the send if any of three things is true: a multipart file was attached, `media` passes `isURL`, or `media` passes `isBase64`.
Anything else is a 400.
Downstream in [`prepareMediaMessage`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/integrations/channel/whatsapp/whatsapp.baileys.service.ts#L2746) the URL branch does `axios.get(..., { responseType: 'arraybuffer' })` and the non-URL branch does `Buffer.from(media, 'base64')`.

Base64 is the correct choice for Rebe, and the reason is architectural rather than aesthetic.
On the URL branch it is **Evolution's container** that fetches the image, not the agent, and section 2.2 of [the deployment spec](./deployment-architecture-spec.md) is explicit that `rebe-agent` has no public FQDN.
Serving a screenshot by URL would therefore mean giving the agent an HTTP surface that exists purely so another container can pull one file from it, plus a lifetime question about when that file stops being served.
Base64 keeps the screenshot inside the request that sends it, so there is no second artifact to host, expire, or leak, and no window in which a URL is live but the message has not gone out.

Two concrete gotchas on the base64 branch.
`isBase64` is class-validator's, so a **data URI prefix** (`data:image/png;base64,...`) fails the check and earns a 400 - the value must be the raw base64 payload alone.
And `fileName` is only mandatory for `mediatype: "document"`; for an image the controller does not demand it, and the service defaults it to `image.jpg` anyway.

---

## 3. Size and format: Evolution re-encodes every image to JPEG

This is the finding with the most consequence for a screenshot of a tweet, and it is not in any documentation.

For `mediatype: "image"` specifically, [line 2775](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/integrations/channel/whatsapp/whatsapp.baileys.service.ts#L2775) runs `await sharp(imageBuffer).jpeg().toBuffer()` unconditionally, then forces `fileName` to `image.jpg` and `mimetype` to `image/jpeg`.
Whatever is sent in - PNG, WebP, anything [sharp](https://sharp.pixelplumbing.com/api-constructor/) can decode - leaves as JPEG, and a `mimetype` supplied by the caller is overwritten rather than honoured.
Sharp's `.jpeg()` defaults are [quality 80 with 4:2:0 chroma subsampling](https://sharp.pixelplumbing.com/api-output/), which is a lossy, chroma-halving pass over an image whose entire content is small text on a flat background.
A tweet screenshot is close to the worst case for that codec, so legibility is a real risk and the screenshot ticket should size its render generously rather than assume the bytes arrive untouched.
It also means there is no point sending PNG for crispness: the crispness is discarded server-side.

On size, the honest answer is that there are three separate ceilings and only two of them are knowable from here.

Evolution's own Express body limit is **136 MB**, set as `json({ limit: '136mb' })` in [`main.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/main.ts), which caps the base64 string and so caps the real image at roughly three quarters of that.
Sharp's constructor default refuses input above **268402689 pixels** (`limitInputPixels`), and Evolution calls `sharp()` with no options, so that default applies.
WhatsApp's own media ceiling sits downstream of both, inside Baileys (`baileys@7.0.0-rc.9`), and Evolution neither documents nor enforces it - so this document does not put a number on it.
None of this binds in practice: a tweet screenshot is orders of magnitude under every one of those limits, and the limits are recorded here only so nobody later assumes a limit that is not there.

Accepted input formats are therefore "whatever sharp decodes" rather than a list Evolution publishes: JPEG, PNG, WebP, AVIF, GIF, SVG and TIFF, per the [sharp constructor docs](https://sharp.pixelplumbing.com/api-constructor/).

---

## 4. `delay` yes, `presence` no - and `presence` was never real on `sendText` either

`SendMediaDto` extends the same `Metadata` base class as `SendTextDto`, and that base carries `delay`, `quoted`, `linkPreview`, `mentionsEveryOne` and `mentioned`.
The media JSON schema mirrors the text schema field for field, `delay` included, described identically as "Enter a value in milliseconds".
So **`delay` behaves on media exactly as it behaves on text**, and it drives the same code: [`sendMessageWithTyping`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/integrations/channel/whatsapp/whatsapp.baileys.service.ts#L2289) subscribes to presence, raises `composing`, sleeps `delay` milliseconds, drops to `paused`, and only then sends - with a re-assert loop for holds over twenty seconds.

`presence` is the interesting half.
Neither `Metadata` nor either JSON schema declares a `presence` field, and both [`textMessage`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/integrations/channel/whatsapp/whatsapp.baileys.service.ts#L2629) and [`mediaMessage`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/integrations/channel/whatsapp/whatsapp.baileys.service.ts#L2981) hard-code `presence: 'composing'` when they call into the typing helper.
A `presence` sent by a caller is simply dropped on the floor - on `sendMedia` and on `sendText` alike.

That corrects the module docstring in `rebe_agent/evolution.py`, which says `sendText` "can carry the same `presence` and `delay`".
On v2.3.7 it carries `delay`; the presence is Evolution's decision, and it is always `composing`.

**The cost to the anti-ban pacer contract is zero.**
The pacer never uses either field: it holds the typing indicator through a separate `POST /chat/sendPresence/{instance}` call and then sends with no `delay` at all, precisely so the send is recorded before it goes on the wire.
Because `sendMessageWithTyping` skips its whole typing block when `delay` is absent (`if (options?.delay)`), a media send with no `delay` behaves identically to today's text send.
Section 2 of [the anti-ban playbook](./anti-ban-ops-spec.md) - `composing` before every send, ~30 ms per character, Gaussian jitter, clamped 1500-5000 ms - therefore transfers to images untouched, and `TypingProfile` keeps the hold under the ten seconds Baileys allows a presence to live.

One wrinkle the builder should size for.
`prepareMediaMessage` runs *before* `sendMessageWithTyping`, and it does the fetch, the sharp re-encode, and the encrypted upload to WhatsApp's media servers.
So a media send spends real wall-clock time before the typing indicator appears, where a text send does not.
The pacer's twenty-second HTTP timeout in `REQUEST_TIMEOUT_SECONDS` covers the whole of that, and a screenshot-sized upload should sit far inside it - but it is the one place where "same contract" is a statement about the fields, not about the latency.

An open question this document deliberately leaves for the pacer: what a *typing* indicator means in front of an image.
A human sending a screenshot does not type for four seconds first, and `_draw_typing_seconds` scales the pause off `len(text)`.
Feeding it the caption length is defensible and is the smallest change; nothing in the transport forces the answer either way.

---

## 5. The caption rides in the same message - one notification, one pacer slot

`prepareMediaMessage` sets `prepareMedia[mediaType].caption = mediaMessage?.caption` at [line 2877](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/integrations/channel/whatsapp/whatsapp.baileys.service.ts#L2877), on the same `imageMessage` protobuf that carries the picture.
That is WhatsApp's native image-with-caption, not two messages stitched together.

So the group gets **one** notification, and the pacer counts **one** send against the four-a-minute, three-an-hour and twelve-a-day ceilings.
This is the answer the map needed: "a screenshot of the tweet plus Rebe's Spanish line" is a single message, and neither the cadence spec nor the envelope has to grow a notion of a two-part post.

The caption is a plain string with no length constraint in the schema, and it is not run through any of Evolution's link handling - which suits a message shape that is explicitly meant to carry no URL.

---

## 6. The message id is at the same JSON path the client already reads

`mediaMessage` returns whatever `sendMessageWithTyping` returns, and that is `messageRaw` from [`prepareMessage`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/integrations/channel/whatsapp/whatsapp.baileys.service.ts#L4652), which sets `key: message.key` verbatim from Baileys.
The response body is a JSON object shaped like:

```json
{
  "key": { "remoteJid": "1203...@g.us", "fromMe": true, "id": "3EB0..." },
  "message": { "imageMessage": { "caption": "...", "mimetype": "image/jpeg", ... } },
  "messageType": "imageMessage",
  "messageTimestamp": 1753...,
  "status": "PENDING"
}
```

The message id is **`key.id`** - the identical path `EvolutionClient.send_text` already reads, and the docs' documented response (`{ "key": {}, "message": {}, "status": "" }`) agrees on the envelope.
The HTTP status on success is **201 Created**, not 200, because the router returns `HttpStatus.CREATED`; the existing `_post` treats anything under 400 as success, so that is already handled.

Practically this means the posted store and the pacer need no new key concept.
A media send returns a message id from the same place, with the same best-effort caveat the current code already encodes: if the id is missing, the message still went out and an empty string is the honest answer.

`messageType` is a useful bonus - it reads `imageMessage` on a successful picture, which is a cheap post-hoc assertion that WhatsApp accepted a picture and not, say, a document.

---

## 7. Failure modes, and whether a half-post is possible

Evolution's failure surface here is coarser than the new docs suggest.
The docs page advertises a structured envelope with `success`, `error.code` and `meta`, but the running 2.3.7 error handler in `main.ts` emits `{ "status": ..., "error": ..., "response": { "message": ... } }`.
Treat the documented envelope as boilerplate and the source as truth; `EvolutionError` already keeps the raw body text rather than parsing it, which turns out to be the right call.

The failures that matter, in the order they can happen:

**Rejected before anything is attempted (400).**
A `media` that is neither a URL nor valid base64 - a data-URI prefix being the likely own-goal - is refused by the controller with `Owned media must be a url or base64`.
A base64 `document` with no `fileName` is refused the same way.
Nothing has been fetched, uploaded, or shown to the group.

**Rejected during preparation (500).**
`prepareMediaMessage` wraps its whole body in a try/catch that rethrows as `InternalServerErrorException`.
An image sharp cannot decode, an image above `limitInputPixels`, a URL that 404s or times out, an out-of-memory on a huge buffer - all of these surface as a 500 with the underlying error stringified into the body.
There is no distinct status code for "oversized" versus "corrupt": both are a 500 and both must be read out of the message text.
A payload over the 136 MB Express limit is different again, and is refused by body-parser before Evolution's own code runs.

**Rejected during send (400).**
`sendMessageWithTyping` catches everything from its own body and rethrows as `BadRequestException(error.toString())`.
A group that cannot be resolved is a 404 `Group not found`; a WhatsApp-side refusal arrives here as a 400.
This is also where a 463 reach-out time-lock would surface, and `RATE_LIMIT_STATUSES` in the existing client already separates that case.

**Can a failed media send leave a partial post?**
From the group's point of view, no.
The encrypted upload to WhatsApp's media servers happens inside `prepareMediaMessage`, strictly before any message is sent, so a failure after the upload leaves an orphaned blob on WhatsApp's servers that no message references and nobody in the group can see.
There is no state in which the image lands and the caption does not, because they are one protobuf.

There are two smaller partial artifacts worth naming honestly, though.
If `delay` were ever used and the send then failed, the group would have watched Rebe type and then seen nothing - which is why the pacer's existing habit of holding presence separately and sending with no `delay` is worth keeping for media too.
And the pacer records the send in `SendLog` *before* the wire call, so a failed media send still consumes one of the twelve daily slots.
That is deliberate and correct - a retry storm against a failing endpoint is exactly what the ceilings exist to stop - but it means a screenshot pipeline that fails repeatedly burns the day's allowance silently, and the news leg should drop rather than retry.

---

## Verdict

**Image sending is safe to build on.**

Every contract the transport has to satisfy is already satisfied, and none of them needed a workaround.
The endpoint is one POST with the same auth and the same base URL.
The caption rides inside the image message, so the group sees one notification and the pacer counts one send.
The message id comes back at `key.id`, the same path the text path reads, so the posted store and the send log need no new concept.
`delay` works identically to text, and the fact that `presence` is ignored costs nothing because the pacer never used it - it holds presence through `/chat/sendPresence` and sends with no `delay`, and a media send with no `delay` skips Evolution's typing block exactly as a text send does.

The one genuine constraint is not about sending at all: **Evolution re-encodes every image to JPEG at sharp's default quality 80 with 4:2:0 chroma subsampling**, so a screenshot of small text will be degraded no matter what format is sent in.
That is a sizing and legibility requirement on the screenshot ticket, not a blocker here.

### What `evolution.py` would need to grow

One method, and one line of protocol.

- **`send_media(chat, image_base64, caption) -> str`** on `EvolutionClient`, posting `{"number": chat, "mediatype": "image", "media": <raw base64>, "caption": caption, "fileName": ...}` to `/message/sendMedia/{instance}`, reading `key.id` back exactly as `send_text` does.
No `presence`, no `delay` - the pacer owns both, and Evolution ignores the former anyway.
- **The same method on the `EvolutionSender` protocol**, so the pacer can send an image without knowing which concrete client it holds.
- **No change to `_post`**, `EvolutionError`, `RATE_LIMIT_STATUSES`, or the 201-vs-200 handling: media reuses all of it unmodified.
- **A correction to the module docstring**, which currently claims `sendText` can carry `presence`; on v2.3.7 it carries `delay` only, and the presence is always `composing`.

Two things sit just outside this module and belong to the tickets that own them.
`Pacer.send` is typed around `text` - it fingerprints it, refuses empties, and scales the typing pause off its length - so a media send needs the pacer to grow a caption-plus-image shape, which is the cadence and pacer ticket's call rather than the transport's.
And the base64 decision means the screenshot never becomes a hosted artifact, which simplifies the "where does a screenshot live" fog on the map: it lives in memory, for the length of one request.

---

## Sources

Live instance, read-only, 2026-07-27: `GET http://evo-i856ku5lxcr2o1v64eveeahq.45.132.242.102.sslip.io/` reporting `"version":"2.3.7"`.
Coolify service `bien-evo`, compose pin `evoapicloud/evolution-api:v2.3.7`.

Source at [tag 2.3.7](https://github.com/EvolutionAPI/evolution-api/tree/2.3.7):
[`sendMessage.dto.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/dto/sendMessage.dto.ts),
[`message.schema.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/validate/message.schema.ts),
[`sendMessage.router.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/routes/sendMessage.router.ts),
[`sendMessage.controller.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/controllers/sendMessage.controller.ts),
[`whatsapp.baileys.service.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/api/integrations/channel/whatsapp/whatsapp.baileys.service.ts),
[`main.ts`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/src/main.ts),
[`package.json`](https://github.com/EvolutionAPI/evolution-api/blob/2.3.7/package.json).

Official documentation: [Send Media Message](https://docs.evolutionfoundation.com.br/en/evolution-api/send-media-message) (corroborates endpoint, auth and response envelope; its multipart-only examples and its error envelope do not match 2.3.7).

Image pipeline: [sharp constructor](https://sharp.pixelplumbing.com/api-constructor/) for `limitInputPixels` and accepted inputs, [sharp output](https://sharp.pixelplumbing.com/api-output/) for the `.jpeg()` defaults.
