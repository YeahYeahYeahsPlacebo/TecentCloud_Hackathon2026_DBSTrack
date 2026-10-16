# Public submission preview

The live gateway requires private server-side LLM configuration. No secure backend
hosting account was available during the submission deployment, so the public
GitHub Pages build is explicitly labelled **Interactive preview — live AI validation unavailable**.
It uses the current merged frontend, not the separate simplified interface.

Available: browser-dependent Tap to talk, editable final text, and explicitly
labelled fixture cards. Speech recognition depends on the browser's speech service
and microphone permission. There is no claim of verified physical microphone or
speaker behavior on the public origin.

Unavailable: live Send, two-transfer parsing, server-backed clarification, live
LLM validation, registration, signing and execution. The public preview is not the
full video demo. Samples do not become approved transactions, and no server keys
are shipped. The normal gateway frontend and its security checks are unchanged.

Build from a reviewed source commit into a new directory outside the repository:

```sh
python3 scripts/build_public_preview.py /tmp/confirm-plus-chop-public-build
```

Publish only that output directory on the `gh-pages` branch, using GitHub Pages
with branch source `gh-pages` and path `/`. `deployment.json` records the source
commit and explicitly reports the absence of a backend. `/api/health` is not a live
endpoint on this static host and must not be represented as a healthy gateway.
Never include `.env`, repository metadata, local logs, or private configuration.

Public URL: https://yeahyeahyeahsplacebo.github.io/TecentCloud_Hackathon2026_DBSTrack/

Deployment verification (16 October 2026): HTTPS page and deployment metadata
returned 200. A fresh Chrome 155 profile loaded the page without JavaScript errors;
editable text and all four sample cards worked. Send and registration stayed
disabled, including after fixture loads; no authorization action was enabled.
Seven JavaScript files passed Chrome syntax compilation. Physical microphone
recognition, audible playback and passkey ceremonies were not verified.

The static host returned 404 for `api/health` and 405 for POST `api/message` with
either the single known-payee sentence or the two-transfer John/Jane sentence.
These are unavailable backend endpoints, not live parser or validator results.
