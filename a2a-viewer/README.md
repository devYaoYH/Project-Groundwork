# a2a Trace Viewer

Static, dependency-free viewer for `GameTraceBase` JSON files written by
`a2a_engine.tracing.write_trace`. Plain HTML + ES modules + CSS — no build
step.

## Run

```sh
cd a2a-viewer
python -m http.server 8000
# open http://localhost:8000
```

Or just open `index.html` directly in a modern browser (some browsers block
ES module loads from `file://` — use the http server in that case).

Load a trace via the **Open trace JSON** button or by dragging a `.json`
file onto the page. A sample trace is in `examples/sample_trace.json`.

## Layout

```
index.html
css/styles.css
js/
  main.js              bootstrap, file/drag-drop, render orchestration
  header.js            header panel (ids, agents, metrics, final_state)
  timeline.js          event-list rendering
  renderers/
    index.js           registry + default (collapsible JSON) renderer
    message.js         chat-bubble renderer for "message" events
    game_lifecycle.js  renderers for "game_start" and "game_end"
```

## Adding a custom renderer for a downstream game

1. Create `js/renderers/<game_name>.js`:

   ```js
   import { registerRenderer } from "./index.js";

   registerRenderer("my_event_type", (event) => {
     const div = document.createElement("div");
     div.className = "event";
     div.textContent = `my_event_type: ${JSON.stringify(event.data)}`;
     return div;
   });
   ```

2. Import it from `js/main.js` so registration runs at load time:

   ```js
   import "./renderers/my_game.js";
   ```

Any event whose `type` is not registered falls through to the default
renderer (collapsible pretty-printed JSON), so missing renderers are
non-fatal.

## Serving it

The viewer is served by the local stack alongside the control plane; see
`docs/LOCAL_STACK.md`. Because it is static files with no build step, any
static host can serve it against a compatible trace API.
