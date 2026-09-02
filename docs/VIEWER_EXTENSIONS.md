# Viewer extensions

The local viewer works for every `GameTraceBase` without game code: message
events render as a conversation and all other event payloads have a safe JSON
fallback. It also shows trace-derived metric artifacts and OTel spans when a
trace is opened from the local stack.

Add a renderer only when a game benefits from a compact domain-specific view.
Renderers are browser modules registered by event type; they receive the full
event and return a DOM node. They are a presentation layer only: analysis must
continue to work from the durable event payload.

```javascript
// a2a-viewer/js/renderers/my_game.js
import { registerRenderer } from "./index.js";

registerRenderer("proposal", (event) => {
  const node = document.createElement("article");
  node.className = "event";
  node.textContent = `${event.data.speaker}: ${event.data.choice}`;
  return node;
});
```

Import that module once from `a2a-viewer/js/main.js`:

```javascript
import "./renderers/my_game.js";
```

Use namespaced event types such as `my_game.proposal` when a generic word could
collide with another game. The renderer registry selects an exact event-type
match, otherwise it uses the default JSON renderer. This intentionally keeps
the release viewer static and local; a future packaged viewer-adapter boundary
can load extensions without editing the shared entrypoint.

Run the stack and open a stored trace to verify the rendering path:

```bash
docker compose up --build
# http://localhost:8080
```
