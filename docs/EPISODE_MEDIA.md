# Recorded player inputs in episode replay

Episodes with recorded `observation` or `seat_observation` events open as player
turns in the shared chat viewer. Each private input appears before its acting
seat's response. Images are shown immediately; long input text can be expanded.
Private reports carry a private-response marker. Round boundaries and the common
event cursor remain available. **Show all events** opens the complete chat event
stream, including actions, reasoning, payloads, and the existing synthetic-event
filter. Older traces keep the existing cursor-linked chat view.

This is researcher inspection of recorded evidence. It does not deliver another
seat's private input to an agent or construct missing observations.

SQLite and local JSON stores retain generated visual observations in a
content-addressed `assets/` directory beside the store. Hosted workers publish
those bytes with final evidence. Episode image endpoints require a reference in
the selected trace, verify digest and MIME type, and use the episode ownership
gate for hosted requests. Release-bank previews use their separate endpoint.

The agent pool includes `ds-v4.1-flash`, the exact OpenRouter model
`deepseek/deepseek-v4.1-flash`, with explicit text/image capability evidence.
Keep `OPENROUTER_API_KEY` in private environment configuration. Visual environments
must declare image requirements on their seats; text-only or unknown capability
bindings cannot satisfy those requirements.
