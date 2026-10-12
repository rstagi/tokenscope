# Session canvas (web build)

Source of the `tokenscope live` session page (`/s/<id>`): a React Flow canvas of the session's intents, with
subagents as their own branches. The built files are committed into the Python package, so running tokenscope never
needs node or a network:

- `tokenscope/live/static/canvas.js`, `canvas.css`, `canvas.js.LEGAL.txt` (bundled licence comments)
- `THIRD_PARTY_NOTICES.md` at the repo root (licences of the bundled packages, generated)

Rebuild after editing `src/`:

    cd web/canvas
    npm ci
    npm run build

Libraries: `@xyflow/react` (MIT) with React 18 (MIT); its d3 helpers are ISC / BSD-3-Clause. No layout library: the
layout (`place()` in `src/main.jsx`) is a small incremental top-down flow so nodes never move once placed. Data comes
from `/api/session/<id>/flow` (with `?replay=1`: a server-side replay, see `tokenscope/live/replay.py`).
