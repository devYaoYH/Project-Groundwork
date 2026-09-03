"""Generate a small human labeler for rubric calibration.

The generated HTML is self-contained and exports a wide CSV with the same label
columns used by the LLM rater, which makes inter-rater agreement a direct CSV
comparison.

Usage:
    uv run python -m judge.build_calibration_labeler \
      --taxonomy judge/output/taxonomy_v3.json \
      --sample judge/output/calibration_rounds_v3.json
"""

import argparse
import json
from pathlib import Path

from negotiation_game.backend.defaults import REPO_ROOT

DEFAULT_TAXONOMY = REPO_ROOT / "judge" / "output" / "taxonomy_v3.json"
FALLBACK_TAXONOMY = REPO_ROOT / "judge" / "output" / "taxonomy_v2.json"
DEFAULT_SAMPLE = REPO_ROOT / "judge" / "output" / "calibration_rounds_v3.json"
DEFAULT_OUTPUT = REPO_ROOT / "judge" / "output" / "labeling" / "calibration_labeler_v3.html"


def resolve_taxonomy(path: Path) -> Path:
    if path.exists():
        return path
    if path == DEFAULT_TAXONOMY and FALLBACK_TAXONOMY.exists():
        print(f"{path} not found; using {FALLBACK_TAXONOMY} for now.")
        return FALLBACK_TAXONOMY
    raise FileNotFoundError(path)


def load_rubric(path: Path) -> tuple[list[dict], list[dict]]:
    with open(path) as f:
        taxonomy = json.load(f)

    patterns: list[dict] = []
    for polarity in ("negative_patterns", "positive_patterns"):
        for pat in taxonomy.get(polarity, []):
            patterns.append({
                "id": pat["id"],
                "label": pat["label"],
                "polarity": "negative" if polarity == "negative_patterns" else "positive",
                "definition": pat.get("definition") or pat.get("description", ""),
                "inclusion_criteria": pat.get("inclusion_criteria", []),
                "exclusion_criteria": pat.get("exclusion_criteria", []),
            })
    auxiliary_tags: list[dict] = []
    for tag in taxonomy.get("auxiliary_tags", []):
        auxiliary_tags.append({
            "id": tag["id"],
            "label": tag["label"],
            "definition": tag.get("definition") or tag.get("description", ""),
            "inclusion_criteria": tag.get("inclusion_criteria", []),
            "exclusion_criteria": tag.get("exclusion_criteria", []),
        })
    return patterns, auxiliary_tags


def build_html(sample: dict, patterns: list[dict], auxiliary_tags: list[dict], taxonomy_name: str) -> str:
    data_blob = json.dumps(
        {"sample": sample, "patterns": patterns, "auxiliaryTags": auxiliary_tags, "taxonomyName": taxonomy_name},
        separators=(",", ":"),
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Rubric Calibration Labeler</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #1f2933; }}
#app {{ max-width: 1160px; margin: 0 auto; padding: 20px; }}
.topbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 12px; }}
h1 {{ font-size: 18px; margin: 0; }}
.muted {{ color: #667085; font-size: 12px; }}
.layout {{ display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(360px, .95fr); gap: 14px; align-items: start; }}
.panel {{ background: white; border: 1px solid #d9dee8; border-radius: 8px; padding: 14px; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.tag {{ font-size: 12px; background: #eef2f7; border: 1px solid #d9dee8; border-radius: 6px; padding: 3px 7px; }}
.tag.good {{ background: #e8f7ef; border-color: #b8e6cb; }}
.tag.warn {{ background: #fff5db; border-color: #f0d48a; }}
.tag.bad {{ background: #fdecec; border-color: #f2b7b7; }}
.transcript {{ max-height: 68vh; overflow: auto; padding-right: 4px; }}
.turn {{ border-left: 3px solid #c7d7ee; padding: 8px 10px; background: #f9fafb; border-radius: 0 6px 6px 0; margin: 8px 0; }}
.turn.b {{ border-left-color: #d0baf2; }}
.speaker {{ font-size: 11px; font-weight: 700; color: #475467; text-transform: uppercase; margin-bottom: 4px; }}
.speech {{ white-space: pre-wrap; font-size: 13px; }}
details {{ margin-top: 5px; }}
summary {{ cursor: pointer; color: #667085; font-size: 12px; }}
.thinking {{ white-space: pre-wrap; color: #667085; font-size: 12px; background: #fff; border: 1px solid #eaecf0; border-radius: 6px; padding: 8px; margin-top: 5px; }}
.pattern {{ display: grid; grid-template-columns: 20px 1fr; gap: 8px; padding: 9px 8px; border-radius: 7px; border: 1px solid transparent; }}
.pattern:hover {{ background: #f8fafc; }}
.pattern.checked {{ background: #eef6ff; border-color: #b7d8ff; }}
.pattern input {{ width: 16px; height: 16px; margin-top: 2px; }}
.pattern-title {{ font-weight: 700; font-size: 13px; }}
.pattern-id {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #475467; font-size: 11px; margin-left: 4px; }}
.pattern-def {{ font-size: 12px; color: #475467; margin-top: 2px; }}
.agent-row {{ display: flex; gap: 12px; margin-top: 6px; font-size: 12px; color: #475467; }}
.agent-row label {{ display: flex; gap: 4px; align-items: center; }}
.agent-row input {{ width: 14px; height: 14px; margin: 0; }}
.criteria {{ margin: 6px 0 0; padding-left: 18px; color: #667085; font-size: 12px; }}
.criteria li {{ margin: 2px 0; }}
.group-title {{ font-size: 12px; font-weight: 800; letter-spacing: .05em; text-transform: uppercase; margin: 12px 0 6px; }}
.group-title.negative {{ color: #b42318; }}
.group-title.positive {{ color: #047857; }}
.actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
button {{ border: 1px solid #cfd6e3; background: #fff; border-radius: 7px; padding: 8px 12px; font-weight: 700; cursor: pointer; }}
button.primary {{ background: #2563eb; border-color: #2563eb; color: #fff; }}
button.green {{ background: #059669; border-color: #059669; color: #fff; }}
button:disabled {{ opacity: .45; cursor: default; }}
textarea {{ width: 100%; min-height: 58px; border: 1px solid #cfd6e3; border-radius: 7px; padding: 8px; margin-top: 8px; }}
.progress {{ height: 8px; background: #e5eaf2; border-radius: 99px; overflow: hidden; margin-top: 6px; }}
.bar {{ height: 100%; background: #2563eb; width: 0%; }}
@media (max-width: 900px) {{ .layout {{ grid-template-columns: 1fr; }} .transcript {{ max-height: none; }} }}
</style>
</head>
<body>
<div id="app"></div>
<script>
const DATA = {data_blob};
const ROUNDS = DATA.sample.rounds || [];
const PATTERNS = DATA.patterns || [];
const AUX_TAGS = DATA.auxiliaryTags || [];
const LABEL_IDS = PATTERNS.map(p => p.id);
const AUX_IDS = AUX_TAGS.map(t => t.id);
const STORE_KEY = 'calibration_labels_' + DATA.taxonomyName.replace(/\\W+/g, '_');
let idx = 0;
let labels = {{}};

function esc(s) {{
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function keyFor(item) {{ return item.episode_uid + ':' + item.round_number; }}

function entryFor(item) {{
  const key = keyFor(item);
  if (!labels[key]) labels[key] = {{checked: {{}}, aux: {{}}, notes: '', submitted: false}};
  if (!labels[key].aux) labels[key].aux = {{}};
  return labels[key];
}}

function load() {{
  try {{
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) {{
      const saved = JSON.parse(raw);
      labels = saved.labels || {{}};
      idx = saved.idx || 0;
    }}
  }} catch (e) {{ console.warn(e); }}
}}

function save() {{
  localStorage.setItem(STORE_KEY, JSON.stringify({{idx, labels}}));
}}

function outcomeClass(outcome) {{
  if (outcome === 'optimal') return 'good';
  if (outcome === 'overdrawn') return 'bad';
  return 'warn';
}}

function allocText(obj) {{
  const pairs = Object.entries(obj || {{}}).map(([k, v]) => `${{k}}=${{v}}`);
  return pairs.length ? pairs.join(', ') : 'none';
}}

function renderTranscript(rnd) {{
  const turns = (rnd.cheap_talk || []).filter(t => t.speaker !== 'system' && (t.speech || t.thinking));
  if (!turns.length) return '<div class="muted">No agent transcript for this round.</div>';
  return turns.map(t => {{
    const cls = t.speaker === 'agent_b' ? ' b' : '';
    const thinking = t.thinking ? `<details><summary>thinking</summary><div class="thinking">${{esc(t.thinking)}}</div></details>` : '';
    const speech = t.speech ? `<div class="speech">${{esc(t.speech)}}</div>` : '';
    const decision = t.is_decision ? ' decision' : '';
    return `<div class="turn${{cls}}"><div class="speaker">${{esc(t.speaker)}} turn ${{esc(t.turn)}}${{decision}}</div>${{thinking}}${{speech}}</div>`;
  }}).join('');
}}

function renderPattern(p, entry) {{
  const checked = !!entry.checked[p.id];
  const inc = (p.inclusion_criteria || []).map(c => `<li>${{esc(c)}}</li>`).join('');
  const exc = (p.exclusion_criteria || []).map(c => `<li>${{esc(c)}}</li>`).join('');
  return `<label class="pattern ${{checked ? 'checked' : ''}}">
    <input type="checkbox" data-id="${{esc(p.id)}}" ${{checked ? 'checked' : ''}}>
    <div>
      <div class="pattern-title">${{esc(p.label)}}<span class="pattern-id">${{esc(p.id)}}</span></div>
      <div class="pattern-def">${{esc(p.definition)}}</div>
      <details>
        <summary>criteria</summary>
        <ul class="criteria">${{inc}}</ul>
        ${{exc ? `<div class="muted" style="margin-top:6px;">Exclusions</div><ul class="criteria">${{exc}}</ul>` : ''}}
      </details>
    </div>
  </label>`;
}}

function renderAuxTag(tag, entry) {{
  const aux = entry.aux[tag.id] || {{present: false, agent_a: false, agent_b: false}};
  const inc = (tag.inclusion_criteria || []).map(c => `<li>${{esc(c)}}</li>`).join('');
  const exc = (tag.exclusion_criteria || []).map(c => `<li>${{esc(c)}}</li>`).join('');
  return `<div class="pattern ${{aux.present ? 'checked' : ''}}">
    <input type="checkbox" data-aux-present="${{esc(tag.id)}}" ${{aux.present ? 'checked' : ''}}>
    <div>
      <div class="pattern-title">${{esc(tag.label)}}<span class="pattern-id">${{esc(tag.id)}}</span></div>
      <div class="pattern-def">${{esc(tag.definition)}}</div>
      <div class="agent-row">
        <label><input type="checkbox" data-aux-agent="${{esc(tag.id)}}" data-agent="agent_a" ${{aux.agent_a ? 'checked' : ''}} ${{aux.present ? '' : 'disabled'}}> agent_a</label>
        <label><input type="checkbox" data-aux-agent="${{esc(tag.id)}}" data-agent="agent_b" ${{aux.agent_b ? 'checked' : ''}} ${{aux.present ? '' : 'disabled'}}> agent_b</label>
      </div>
      <details>
        <summary>criteria</summary>
        <ul class="criteria">${{inc}}</ul>
        ${{exc ? `<div class="muted" style="margin-top:6px;">Exclusions</div><ul class="criteria">${{exc}}</ul>` : ''}}
      </details>
    </div>
  </div>`;
}}

function render() {{
  const app = document.getElementById('app');
  const item = ROUNDS[idx];
  const done = Object.values(labels).filter(e => e.submitted).length;
  const pct = ROUNDS.length ? (done / ROUNDS.length * 100) : 0;
  if (!item) {{
    app.innerHTML = `<div class="panel"><h1>Calibration complete</h1><p class="muted">${{done}} / ${{ROUNDS.length}} rounds submitted.</p><div class="actions"><button class="green" onclick="downloadWide()">Download wide CSV</button><button onclick="downloadLong()">Download long CSV</button></div></div>`;
    return;
  }}
  const rnd = item.round;
  const entry = entryFor(item);
  const neg = PATTERNS.filter(p => p.polarity === 'negative').map(p => renderPattern(p, entry)).join('');
  const pos = PATTERNS.filter(p => p.polarity === 'positive').map(p => renderPattern(p, entry)).join('');
  const aux = AUX_TAGS.map(t => renderAuxTag(t, entry)).join('');

  app.innerHTML = `
    <div class="topbar">
      <div>
        <h1>Rubric Calibration Labeler</h1>
        <div class="muted">${{esc(DATA.taxonomyName)}} · round ${{idx + 1}} / ${{ROUNDS.length}}</div>
        <div class="progress"><div class="bar" style="width:${{pct}}%"></div></div>
      </div>
      <div class="actions">
        <button onclick="prev()" ${{idx === 0 ? 'disabled' : ''}}>Prev</button>
        <button class="primary" onclick="submitNext()">Submit & next</button>
        <button class="green" onclick="downloadWide()">Download CSV</button>
      </div>
    </div>
    <div class="layout">
      <section class="panel">
        <h1>${{esc(item.episode_uid)}} · Round ${{esc(item.round_number)}}</h1>
        <div class="meta">
          <span class="tag ${{outcomeClass(rnd.round_outcome)}}">${{esc(rnd.round_outcome)}}</span>
          <span class="tag">eff ${{((rnd.joint_efficiency || 0) * 100).toFixed(1)}}%</span>
          <span class="tag">${{esc(item.mode)}}</span>
          <span class="tag">mc ${{esc(item.mc_ratio)}}</span>
          <span class="tag">A ${{esc(item.model_a)}}</span>
          <span class="tag">B ${{esc(item.model_b)}}</span>
        </div>
        <div class="meta">
          <span class="tag">A submitted: ${{esc(allocText(rnd.allocation_a))}}</span>
          <span class="tag">B submitted: ${{esc(allocText(rnd.allocation_b))}}</span>
          <span class="tag">reward A/B: ${{esc(rnd.reward_a)}} / ${{esc(rnd.reward_b)}}</span>
        </div>
        <div class="transcript">${{renderTranscript(rnd)}}</div>
      </section>
      <aside class="panel">
        <div class="group-title negative">Negative patterns</div>
        ${{neg}}
        <div class="group-title positive">Positive patterns</div>
        ${{pos}}
        <div class="group-title">Auxiliary tags</div>
        <div class="muted">Mark round-level presence, then attribute to agent_a, agent_b, or both.</div>
        ${{aux}}
        <textarea id="notes" placeholder="Optional note for disagreement, ambiguity, or rubric edits...">${{esc(entry.notes)}}</textarea>
        <div class="actions">
          <button onclick="clearRound()">None apply</button>
          <button class="primary" onclick="submitNext()">Submit & next</button>
        </div>
      </aside>
    </div>`;

  document.querySelectorAll('input[type=checkbox]').forEach(cb => {{
    cb.addEventListener('change', e => {{
      if (e.target.dataset.auxPresent || e.target.dataset.auxAgent) return;
      const id = e.target.dataset.id;
      const ent = entryFor(item);
      ent.checked[id] = e.target.checked;
      save();
      render();
    }});
  }});
  document.querySelectorAll('input[data-aux-present]').forEach(cb => {{
    cb.addEventListener('change', e => {{
      const id = e.target.dataset.auxPresent;
      const ent = entryFor(item);
      if (!ent.aux[id]) ent.aux[id] = {{present: false, agent_a: false, agent_b: false}};
      ent.aux[id].present = e.target.checked;
      if (!e.target.checked) {{
        ent.aux[id].agent_a = false;
        ent.aux[id].agent_b = false;
      }}
      save();
      render();
    }});
  }});
  document.querySelectorAll('input[data-aux-agent]').forEach(cb => {{
    cb.addEventListener('change', e => {{
      const id = e.target.dataset.auxAgent;
      const agent = e.target.dataset.agent;
      const ent = entryFor(item);
      if (!ent.aux[id]) ent.aux[id] = {{present: true, agent_a: false, agent_b: false}};
      ent.aux[id].present = true;
      ent.aux[id][agent] = e.target.checked;
      save();
      render();
    }});
  }});
  document.getElementById('notes').addEventListener('input', e => {{
    entryFor(item).notes = e.target.value;
    save();
  }});
}}

function submitNext() {{
  const item = ROUNDS[idx];
  entryFor(item).submitted = true;
  save();
  idx++;
  save();
  render();
}}

function prev() {{
  idx = Math.max(0, idx - 1);
  save();
  render();
}}

function clearRound() {{
  const item = ROUNDS[idx];
  entryFor(item).checked = {{}};
  entryFor(item).aux = {{}};
  entryFor(item).submitted = true;
  save();
  submitNext();
}}

function csvEscape(v) {{ return '"' + String(v ?? '').replace(/"/g, '""') + '"'; }}

function wideRows() {{
  return ROUNDS.map(item => {{
    const e = labels[keyFor(item)] || {{checked: {{}}, aux: {{}}, notes: '', submitted: false}};
    const row = [
      item.episode_uid,
      item.round_number,
      item.round.round_outcome || '',
      item.round.joint_efficiency ?? '',
      e.submitted ? 1 : 0,
      e.notes || ''
    ];
    LABEL_IDS.forEach(id => row.push(e.checked && e.checked[id] ? 1 : 0));
    AUX_IDS.forEach(id => {{
      const aux = e.aux?.[id] || {{present: false, agent_a: false, agent_b: false}};
      row.push(aux.present ? 1 : 0, aux.agent_a ? 1 : 0, aux.agent_b ? 1 : 0);
    }});
    return row;
  }});
}}

function download(filename, text) {{
  const blob = new Blob([text], {{type: 'text/csv'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}}

function downloadWide() {{
  const auxHeader = AUX_IDS.flatMap(id => [id, id + '_agent_a', id + '_agent_b']);
  const header = ['episode_uid', 'round_number', 'round_outcome', 'joint_efficiency', 'human_submitted', 'human_notes', ...LABEL_IDS, ...auxHeader];
  const csv = [header, ...wideRows()].map(row => row.map(csvEscape).join(',')).join('\\n');
  download('human_taxonomy_labels_v3_calibration.csv', csv);
}}

function downloadLong() {{
  const rows = [['episode_uid', 'round_number', 'item_id', 'item_type', 'answer', 'agent_a', 'agent_b', 'notes']];
  ROUNDS.forEach(item => {{
    const e = labels[keyFor(item)] || {{checked: {{}}, aux: {{}}, notes: ''}};
    LABEL_IDS.forEach(id => rows.push([item.episode_uid, item.round_number, id, 'core_label', e.checked && e.checked[id] ? 'yes' : 'no', '', '', e.notes || '']));
    AUX_IDS.forEach(id => {{
      const aux = e.aux?.[id] || {{present: false, agent_a: false, agent_b: false}};
      rows.push([item.episode_uid, item.round_number, id, 'auxiliary_tag', aux.present ? 'yes' : 'no', aux.agent_a ? 'yes' : 'no', aux.agent_b ? 'yes' : 'no', e.notes || '']);
    }});
  }});
  download('human_taxonomy_labels_v3_calibration_long.csv', rows.map(row => row.map(csvEscape).join(',')).join('\\n'));
}}

load();
render();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a human calibration labeler.")
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    taxonomy_path = resolve_taxonomy(args.taxonomy)
    with open(args.sample) as f:
        sample = json.load(f)
    patterns, auxiliary_tags = load_rubric(taxonomy_path)

    html = build_html(sample, patterns, auxiliary_tags, taxonomy_path.name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")

    print(f"Wrote {args.output}")
    print(f"Taxonomy: {taxonomy_path}")
    print(f"Patterns: {len(patterns)}")
    print(f"Auxiliary tags: {len(auxiliary_tags)}")
    print(f"Rounds: {len(sample.get('rounds', []))}")


if __name__ == "__main__":
    main()
