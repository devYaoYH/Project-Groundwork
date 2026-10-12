# Recorded player inputs in episode replay

All environments use the same cursor-linked chat renderer. Dyads use opposing
bubbles; groups use participant names and avatars. Every event remains in the
transcript, including committed placements, drawings, private reports and final
outcomes. The synthetic-event filter changes presentation, never event indices.

Recorded `observation` and `seat_observation` events show the seat's input image
before expandable input text and the following response. Private visibility is
labeled explicitly. Committed output images are displayed on their actor's lane
when the environment records them. Historical payloads remain expandable even
when no output image was originally recorded. Scripted seat configurations carry
a deterministic-policy notice, so browser fixtures cannot be mistaken for model
conversations.

This is researcher inspection of recorded evidence, not delivery of private
input to another seat. No observations or historical output images are invented.

SQLite and local JSON stores retain recorded image bytes in content-addressed
`assets/` directories beside the store. Hosted workers publish the same bytes
with final evidence. Episode endpoints verify image references, digest and MIME;
hosted requests also pass episode ownership checks. Bank previews use their
separate release-scoped endpoint.

The agent pool includes the exact OpenRouter `deepseek/deepseek-v4.1-flash` binding
with verified text/image capabilities. Keep its key in private configuration.
