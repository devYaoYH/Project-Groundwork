# Text and image inputs

Seat observations reserve `content` for a string or an ordered list of typed
parts. Existing dictionaries without `content` keep the default text rendering.
`TextPart`, `ImagePart`, `SeatObservation`, and `ContentPart` live in
`a2a_engine.content`. Part types form a discriminated union; future modalities
require their own representation, adapter mapping, and admission support.

```python
from a2a_engine.content import AssetStore, TextPart

store = AssetStore("release/items/assets")
image = store.put(png_bytes, "image/png")
observation = {
    "content": [
        TextPart(text="Describe the shape visible to you.").model_dump(),
        image.model_dump(exclude_none=True),
    ]
}
```

`LLMAgent.build_messages` preserves these parts. When overriding it, preserve
all required modalities: `LLMAgent.act` rejects an override that discards an
image. `observation_content(observation, prompt)` combines an environment prompt
with structured content for custom strategies. It uses only the reserved
seat-projected content when that field exists, excluding incidental observation
metadata from the default prompt. Agents must receive the environment's private
projection; never give this helper a full bank row or shared evaluator state.

## Capability admission

Environment releases and individual roles declare `required_modalities`:

```yaml
required_modalities: [text]
roles:
  - id: viewer
    count: 1
    required_modalities: [text, image]
```

Concrete agent configurations, pool bindings, and design roster entries declare
verified model/deployment capabilities:

```yaml
model: your-verified-model
api_format: openai
capabilities:
  input: [text, image]
  evidence: https://your-provider.example/model-deployment-contract
```

A design roster uses `binding` in place of `model`. Capability evidence is an
explicit researcher assertion based on documentation or a deployment contract;
it is pinned alongside the concrete model. The framework does not infer image
support from the model name, contact a provider to discover support, or assume
that an OpenAI-compatible endpoint has a vision-capable model. Text is the
backward-compatible default. Missing/unknown capabilities and unknown modality
adapters fail before provider I/O. Environment and role requirements are combined
at compilation and copied into seat configs. Preflight verifies those configs
and image bytes again. `make_llm_client` checks declared requirements; both raw
streaming and nonstreaming entry points also check the actual content.

The researcher agent screen shows declared inputs; release detail shows role
requirements. Design validation reports incompatible seats at their roster path.
Provider credentials remain separate from capability evidence.

## Assets and release inputs

Each image part carries only `type`, `asset: asset://<sha256>`, `sha256`, and
`mime_type`. Supported inputs are PNG, JPEG, and WebP, up to 4 MiB per image.
Bytes are checked against both their digest and MIME signature. Arbitrary URLs,
absolute file references, SVG, audio/video, image generation, and provider-native
image blocks are outside this contract and are rejected.

An image bank stores digest-named files in `assets/` beside its JSON/JSONL file.
Images may appear anywhere in a bank row, including separate seat views. Bank
loading verifies every reference. For text banks the fingerprint remains the
raw bank-file SHA-256. For image banks it is SHA-256 of canonical JSON containing
`rows_sha256` and sorted unique image metadata. Thus rows, bytes, and MIME metadata
are all pinned; `ItemBank.load(..., expected_sha256=...)` rejects drift.
Use the loaded `item_bank_sha256` in `ItemPolicy`, rather than hashing only rows.
Declared `InputConfig.sha256` continues to mean the raw bytes of that input file.

`ItemBank.package(destination)` copies the rows and all verified referenced images
and verifies the resulting fingerprint. Include that directory in the environment
wheel's package data and container build input alongside the existing release
inputs. Keep `ItemPolicy.bank_path` relative to the release YAML for typed YAML
releases, and relative to the workspace for registered catalogue releases,
following the existing release contract. Compiled catalogue configs use a
relative `asset_root` beside that bank, so the same plan resolves in a relocated
workspace/container. Typed YAML loading resolves against the release file.
Explicit clients can set `asset_root` to an installed package directory or a
relocated store. This location is excluded from config identity; asset digests
remain part of identity. A missing asset fails clearly rather than fetching a
mutable remote URL or guessing a filesystem path.

## Local and remote seats

Local LLM requests resolve image references immediately before provider mapping.
Remote seat dispatch resolves only the already-projected observation and sends
verified inline bytes through authenticated, signed `/1` pushes or authenticated
`/2` MCP invocations. No asset directory is mounted into the agent service.
Registration and join metadata contain capabilities, not image stimuli, bank row
IDs, filenames, partner views, or evaluator answers. Private visual content
belongs in subsequent seat observations; registration rejects image content. Remote inbox images are explicitly unsupported;
put permitted visual inputs in the private observation content.

Join requests advertise `input_modalities`, defaulting to `[text]` for older
agents. An image seat rejects a text/unknown runtime before consuming its ticket.
The framework's `StructuredOutputStrategy` preserves image content; prompt-only
custom strategies advertise only text unless they explicitly implement
`supported_input_modalities` and declare verified model capabilities. This avoids
silently admitting an adapter that drops images. The existing set of supported
environment runtime strategies and design runtime restrictions still applies;
adding an environment requires implementing its runtime strategy.

Ordinary wire messages retain the 256 KiB limit. Image-bearing invocations allow
up to 8 MiB, including base64, while retaining the 256 KiB limit on text/metadata.
Image completion/telemetry and ordinary MCP responses keep their old limits.

## Provider payloads and inspection

The mappings use current official provider contracts:

- [OpenAI Chat Completions vision](https://developers.openai.com/api/docs/guides/images-vision?api-mode=chat): text plus `image_url` data URLs. This also serves the existing Gemini/OpenRouter/Ollama OpenAI-compatible transports, conditional on the explicitly verified deployment capability.
- [Anthropic vision](https://platform.claude.com/docs/en/build-with-claude/vision): text plus base64 image sources, also for Claude on Vertex. Streaming cache control is added to the last real content block without wrapping the list as text.
- [Google image understanding](https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/image-understanding): Gemini `Part.from_bytes` with MIME metadata, in both streaming and nonstreaming calls.

Images are supported in user messages. System/assistant image inputs and unknown
adapter formats fail explicitly. Provider call histories remain structured and
are not mutated by payload conversion.

Traces, manifests, configs, SQLite/cloud persistence and replay preserve image
references, MIME metadata, and digests. Inline `data` is removed from persisted
schemas and events; provider telemetry records media metadata, never base64.
`A2A_CAPTURE_CONTENT=false` continues to suppress provider input/output content.
Provider HTTP error bodies are not logged because they may echo image bytes.
Replay resolves the recorded digest against the packaged release asset store;
retain that store with the release/trace bundle. It never substitutes a caption.

Item-bank and episode payload views show researcher image previews through a
release-scoped asset endpoint. Hosted access requires the existing researcher
session. Only assets referenced by the verified bank are served. This endpoint
is not an agent MCP tool. The researcher can inspect all views; a seat sees only
its own recorded observation. Shared communication transcripts do not implicitly
inherit observation images. Preserve that boundary in environment-owned routers.

Offline coverage uses deterministic tiny PNG fixtures, provider payload fakes,
real loopback `/1` and `/2` transports, and packaged/reloaded banks. No paid model
run is needed to validate the framework contract.
