# Phone screenshots to add

The terminal screenshots in this repo are real captures. These four have to come from your
phone, because claude.ai sits behind a bot check that automation shouldn't click through.
Save them in `docs/images/` with these names. Then, in `ARTICLE.md`, replace each
`📱 [Screenshot X: …]` blockquote with the image line shown here.

| # | File | How to get it | Line for ARTICLE.md |
|---|------|---------------|---------------------|
| A | `phone-a-session-list.png` | On the Mac: `cd ~/Dev/my-app && cc`. On the phone: Claude app → **Code**. Capture the list with `my-app` and its green dot. | `![The Claude app's Code tab with my-app online](docs/images/phone-a-session-list.png)` |
| B | `phone-b-session.png` | Tap `my-app`, send *"what files are in this project?"*, and capture once Claude answers. | `![The my-app session on the phone, in sync with the Mac](docs/images/phone-b-session.png)` |
| C | `phone-c-environment.png` | On the Mac: `ccserve`. In the app, start a new Code session and capture the environment picker with your Mac's hostname in it. | `![Starting a new session on the Mac from the Claude app](docs/images/phone-c-environment.png)` |
| D | `phone-d-termius.png` | Termius → SSH to the Mac → pick `my-app` in the `cpick` menu. Capture with the green tmux bar visible. | `![Termius attached to the same tmux session](docs/images/phone-d-termius.png)` |

Before you publish, check every shot:

- Hide or crop **session URLs, QR codes and IDs** (`session_…`, `env_…`).
- Hide other sessions whose names give away client or private projects. Archive them or crop them out.
- Crop out notification banners, the carrier name and your email or avatar.
