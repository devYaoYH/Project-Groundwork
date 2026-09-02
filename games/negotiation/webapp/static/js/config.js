import { generateScenario as solveScenario } from './optimizer.js';

// --- Config ---

export const PROVIDERS = {
    openai:     { api_base: 'https://api.openai.com/v1',                                    model: 'gpt-4o-mini',                     api_format: 'openai',    placeholder: 'sk-...' },
    anthropic:  { api_base: 'https://api.anthropic.com/v1',                                 model: 'claude-sonnet-4-5-20250929',      api_format: 'anthropic', placeholder: 'sk-ant-...' },
    gemini:     { api_base: 'https://generativelanguage.googleapis.com/v1beta/openai',      model: 'gemini-2.0-flash',                api_format: 'openai',    placeholder: 'AIza...' },
    ollama:     { api_base: 'http://localhost:11434/v1',                                    model: 'llama3',                          api_format: 'openai',    placeholder: 'ollama' },
    openrouter: { api_base: 'https://openrouter.ai/api/v1',                                 model: 'meta-llama/llama-3.3-70b-instruct', api_format: 'openai',  placeholder: 'sk-or-v1-...' },
    custom:     { api_base: '',                                                              model: '',                                api_format: 'openai',    placeholder: '' },
};

export function applyProvider(x) {
    const provider = document.getElementById(`agent${x}Provider`).value;
    const preset = PROVIDERS[provider];
    if (!preset) return;
    document.getElementById(`agent${x}Base`).value = preset.api_base;
    document.getElementById(`agent${x}Key`).placeholder = preset.placeholder;

    const modelSelect = document.getElementById(`agent${x}ModelSelect`);
    const modelInput = document.getElementById(`agent${x}Model`);
    const modelOption = Array.from(modelSelect.options).find(opt => opt.value === preset.model);

    if (modelOption) {
        modelSelect.value = preset.model;
        modelInput.style.display = 'none';
        modelInput.value = preset.model;
    } else {
        modelSelect.value = 'custom';
        modelInput.style.display = '';
        modelInput.value = preset.model;
    }
}

function detectProviderFromModel(modelName) {
    const model = modelName.toLowerCase();

    // OpenRouter: Check FIRST (before substring checks to avoid false positives)
    // OpenRouter model IDs use provider/model-name format (e.g., meta-llama/llama-3.3-70b-instruct)
    if (model.includes('/')) return 'openrouter';

    if (model.includes('claude') || model.includes('anthropic')) return 'anthropic';
    if (model.includes('gpt') || model.includes('openai') || model.startsWith('o1-') || model.startsWith('o3-') || model.startsWith('o4-')) return 'openai';
    if (model.includes('gemini')) return 'gemini';
    if (model.includes('llama') || model.includes('mistral') || model.includes('qwen')) return 'ollama';
    return null;
}

export function handleModelSelect(agent) {
    const select = document.getElementById(`agent${agent}ModelSelect`);
    const customInput = document.getElementById(`agent${agent}Model`);

    if (select.value === 'custom') {
        customInput.style.display = '';
        customInput.focus();
    } else {
        customInput.style.display = 'none';
        customInput.value = select.value;

        // Auto-update api_base when model changes
        const detectedProvider = detectProviderFromModel(select.value);
        if (detectedProvider && PROVIDERS[detectedProvider]) {
            const providerPreset = PROVIDERS[detectedProvider];
            document.getElementById(`agent${agent}Base`).value = providerPreset.api_base;
            document.getElementById(`agent${agent}Provider`).value = detectedProvider;
        }
    }
}

export function getAgentConfig(x) {
    const type = document.getElementById(`agent${x}Type`).value;
    const cfg = { type };
    if (type === 'llm') {
        const provider = document.getElementById(`agent${x}Provider`).value;
        cfg.api_base = document.getElementById(`agent${x}Base`).value;
        cfg.api_key = document.getElementById(`agent${x}Key`).value;

        // Get model from select or custom input
        const select = document.getElementById(`agent${x}ModelSelect`);
        const customInput = document.getElementById(`agent${x}Model`);
        cfg.model = select.value === 'custom' ? customInput.value : select.value;

        cfg.temperature = parseFloat(document.getElementById(`agent${x}Temp`).value);
        cfg.api_format = PROVIDERS[provider]?.api_format || 'openai';
    }
    return cfg;
}

export function hasConsent() {
    return window._firestoreAvailable && localStorage.getItem('data_consent') === 'accepted';
}

// --- Project mode state ---
let _generatedProjects = null; // { agent_projects, oracle_stats }

export function setGeneratedProjects(data) { _generatedProjects = data; }
export function getGeneratedProjects() { return _generatedProjects; }

export function buildGameConfig() {
    const config = {
        mode: document.getElementById('gameMode').value,
        num_rounds: parseInt(document.getElementById('numRounds').value),
        cheap_talk_turns: parseInt(document.getElementById('cheapTalkTurns').value),
        agent_budget: parseFloat(document.getElementById('agentBudget').value),
        max_resource_types_per_turn: parseInt(document.getElementById('maxTypes').value),
        resource_types: ["wood", "stone", "gold"],
        resource_supply: {
            wood: parseInt(document.getElementById('supWood').value),
            stone: parseInt(document.getElementById('supStone').value),
            gold: parseInt(document.getElementById('supGold').value),
        },
        resource_costs: {
            wood: parseFloat(document.getElementById('costWood').value),
            stone: parseFloat(document.getElementById('costStone').value),
            gold: parseFloat(document.getElementById('costGold').value),
        },
        agent_a: getAgentConfig('A'),
        agent_b: getAgentConfig('B'),
        consent: hasConsent(),
        goal: document.getElementById('gameGoal').value || null,
        thinking: document.getElementById('thinkingMode').checked,
        visible_utilities: document.getElementById('visibleUtilities').value === 'true',
        visible_opponent_reward: document.getElementById('visibleOpponentReward').value === 'true',
        enable_cheap_talk: document.getElementById('enableCheapTalk').checked,
        share_projects: document.getElementById('shareProjects').checked,
        think_about_opponent: document.getElementById('thinkAboutOpponent').checked,
        maximize_joint: document.getElementById('maximizeJoint').checked,
        full_transparency: document.getElementById('fullTransparency').checked,
    };

    if (_generatedProjects) {
        config.agent_projects = _generatedProjects.agent_projects;
        config.oracle_stats = _generatedProjects.oracle_stats;
        if (_generatedProjects.scenario_synergy) {
            config.scenario_synergy = _generatedProjects.scenario_synergy;
        }
    }

    return config;
}


export async function generateScenario() {
    const btn = document.getElementById('generateScenarioBtn');
    const status = document.getElementById('scenarioStatus');
    const solverBar = document.getElementById('solverBar');
    const solverStats = document.getElementById('solverStats');
    btn.disabled = true;
    status.textContent = 'Generating scenario...';
    status.className = 'solver-status';
    solverBar.style.transition = 'none';
    solverBar.style.width = '0%';
    solverBar.offsetHeight; // reflow
    solverBar.style.transition = 'width 0.15s linear';

    const targetMC = parseFloat(document.getElementById('mcRatio').value) / 100;
    const resourceTypes = ["wood", "stone", "gold"];
    const costs = {
        wood: parseFloat(document.getElementById('costWood').value),
        stone: parseFloat(document.getElementById('costStone').value),
        gold: parseFloat(document.getElementById('costGold').value),
    };
    const supply = {
        wood: parseInt(document.getElementById('supWood').value),
        stone: parseInt(document.getElementById('supStone').value),
        gold: parseInt(document.getElementById('supGold').value),
    };
    const cashPerPlayer = parseFloat(document.getElementById('agentBudget').value);
    const maxTypes = parseInt(document.getElementById('maxTypes').value);

    try {
        const data = await solveScenario(targetMC, resourceTypes, costs, supply, cashPerPlayer, (pct, bestState) => {
            solverBar.style.width = pct + '%';
            if (pct < 100) {
                solverStats.textContent = `score ${bestState.score.toFixed(1)} · M/C ${bestState.ev.mcRatio.toFixed(3)}`;
            }
        }, maxTypes);

        _generatedProjects = data;
        displayProjects(data);

        // Try to get creative project names from server (if toggle is on)
        if (document.getElementById('nameProjects').checked) {
            status.textContent = 'Naming projects...';
            try {
                const nameResp = await fetch('/api/scenario/name-projects', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ agent_projects: data.agent_projects }),
                });
                if (nameResp.ok) {
                    const nameData = await nameResp.json();
                    // Build old→new name mapping and update collab_detail
                    const oldNames = _generatedProjects.agent_projects.flat().map(p => p.name);
                    _generatedProjects.agent_projects = nameData.agent_projects;
                    const newNames = nameData.agent_projects.flat().map(p => p.name);
                    let detail = _generatedProjects.oracle_stats.collab_detail || '';
                    oldNames.forEach((old, i) => { detail = detail.replaceAll(old, newNames[i]); });
                    _generatedProjects.oracle_stats.collab_detail = detail;
                    displayProjects(_generatedProjects);
                }
            } catch (_) { /* keep generic names */ }
        }

        const os = data.oracle_stats;
        status.textContent = `M/C = ${os.mc_ratio.toFixed(3)}  |  C = ${os.combined}  |  M = ${os.collab_max}`;
        status.className = 'solver-status ok';

        if (data.solver_stats) {
            solverStats.textContent = `${data.solver_stats.candidates} candidates evaluated, ${data.solver_stats.improvements} improvements`;
        }

        document.getElementById('launchBtn').disabled = false;
        document.getElementById('oracleSolveBtn').disabled = false;
        document.getElementById('saveScenarioBtn').disabled = false;
    } catch (e) {
        status.textContent = `Error: ${e.message}`;
        status.className = 'solver-status err';
        solverBar.style.width = '0%';
    }
    btn.disabled = false;
}

function displayProjects(data) {
    const os = data.oracle_stats;

    // Collab stats bar
    const collabView = document.getElementById('collabView');
    collabView.style.display = '';

    const mcRatio = os.mc_ratio;
    const gap = Math.abs(os.v1 - os.v2);
    const fairness = os.combined > 0 ? (1 - gap / os.combined) : 1;
    const mcClass = mcRatio >= 1.0 ? 'gg' : mcRatio >= 0.8 ? 'gok' : 'gb';

    document.getElementById('collabStats').innerHTML = `
        <div class="ci"><div class="clbl">C (individual sum)</div><div class="cval">${os.combined}</div><div class="csub">V1=${os.v1} + V2=${os.v2}</div></div>
        <div class="ci"><div class="clbl">M (collab max)</div><div class="cval">${os.collab_max}</div><div class="csub">A=${os.base_score_a} + B=${os.base_score_b}${os.synergy_bonus ? ` + syn=${os.synergy_bonus}` : ''}</div></div>
        <div class="ci"><div class="clbl">M/C</div><div class="cval"><span class="gpill ${mcClass}">${mcRatio.toFixed(3)}</span></div></div>
        <div class="ci"><div class="clbl">Fairness</div><div class="cval">${(fairness * 100).toFixed(0)}%</div><div class="csub">gap=${gap}</div></div>
        <div class="ci"><div class="clbl">Swap Fair</div><div class="cval">${os.swap_fairness != null ? (os.swap_fairness * 100).toFixed(0) + '%' : '—'}</div><div class="csub">${os.best_p1_favor != null ? `P1≥P2: ${os.best_p1_favor} · P2≥P1: ${os.best_p2_favor}` : ''}</div></div>
        <div class="ci"><div class="clbl">Optimal Paths</div><div class="cval">${os.optimal_paths != null ? os.optimal_paths : '—'}</div><div class="csub">${os.optimal_paths >= 2 ? 'strategic ambiguity' : 'unique optimum'}</div></div>
        <div class="ci"><div class="clbl">Neg. Pressure</div><div class="cval">${os.negotiation_pressure != null ? (os.negotiation_pressure * 100).toFixed(0) + '%' : '—'}</div><div class="csub">overlap=${os.solo_overlap != null ? (os.solo_overlap * 100).toFixed(0) + '%' : '?'} · diverge=${os.collab_divergence != null ? (os.collab_divergence * 100).toFixed(0) + '%' : '?'}</div></div>
        <div class="ci"><div class="clbl">Collab Plan</div><div class="cval" style="font-size:0.8em">${os.collab_detail || ''}</div></div>
    `;

    // Resource bars
    const resUsed = os.res_used || {};
    const supply = {
        wood: parseInt(document.getElementById('supWood').value),
        stone: parseInt(document.getElementById('supStone').value),
        gold: parseInt(document.getElementById('supGold').value),
    };
    const barsEl = document.getElementById('collabResBars');
    barsEl.innerHTML = Object.keys(supply).map(r => {
        const used = resUsed[r] || 0;
        const cap = supply[r] || 1;
        const pct = Math.min(100, (used / cap) * 100);
        return `<div class="res-bar-row">
            <span class="res-bar-label">${r}</span>
            <div class="res-bar-track"><div class="res-bar-fill" style="width:${pct}%"></div></div>
            <span class="res-bar-val">${used} / ${cap}</span>
        </div>`;
    }).join('');

    // Project cards
    const display = document.getElementById('projectsDisplay');
    display.style.display = '';

    for (const [idx, containerId] of [[0, 'projectsA'], [1, 'projectsB']]) {
        const el = document.getElementById(containerId);
        const projects = data.agent_projects[idx] || [];
        const asc = idx === 0 ? os.asc1 : os.asc2;
        const ascLabel = asc != null ? `<div class="project-card" style="font-size:0.85em;color:var(--text-dim)">Action space: ${asc} strategies</div>` : '';
        el.innerHTML = projects.map(p => {
            const req = Object.entries(p.requirements).map(([r, q]) => `${r}x${q}`).join(', ');
            return `<div class="project-card"><strong>${p.name}</strong>: [${req}] = ${p.reward}/run</div>`;
        }).join('') + ascLabel;
        // Show scenario-level synergy below projects
        if (data.scenario_synergy) {
            const syn = data.scenario_synergy;
            el.innerHTML += `<div class="project-card" style="color:var(--accent-a)">Synergy: +${syn.bonus} flat if combined ${syn.resource} ≥ ${syn.threshold}</div>`;
        }
    }
}

function clearGeneratedState() {
    _generatedProjects = null;
    document.getElementById('launchBtn').disabled = true;
    document.getElementById('oracleSolveBtn').disabled = true;
    document.getElementById('saveScenarioBtn').disabled = true;
    document.getElementById('oracleResult').style.display = 'none';
    document.getElementById('collabView').style.display = 'none';
    document.getElementById('projectsDisplay').style.display = 'none';
    document.getElementById('solverStats').textContent = '';
    document.getElementById('scenarioStatus').textContent = 'Set M/C ratio and generate a scenario to start.';
    document.getElementById('scenarioStatus').className = 'solver-status';
    document.getElementById('solverBar').style.width = '0%';
}

// --- Scenario Save/Load ---

const SCENARIOS_KEY = 'saved_scenarios';

function getSavedScenarios() {
    try {
        return JSON.parse(localStorage.getItem(SCENARIOS_KEY) || '[]');
    } catch { return []; }
}

function putSavedScenarios(scenarios) {
    localStorage.setItem(SCENARIOS_KEY, JSON.stringify(scenarios));
}

export function saveScenario() {
    if (!_generatedProjects) return;
    const mcRatio = _generatedProjects.oracle_stats?.mc_ratio;
    const defaultName = `M/C ${mcRatio?.toFixed(2) ?? '?'} — ${new Date().toLocaleDateString()}`;
    const name = prompt('Scenario name:', defaultName);
    if (!name) return;

    const scenario = {
        name,
        saved_at: new Date().toISOString(),
        resource_supply: {
            wood: parseInt(document.getElementById('supWood').value),
            stone: parseInt(document.getElementById('supStone').value),
            gold: parseInt(document.getElementById('supGold').value),
        },
        resource_costs: {
            wood: parseFloat(document.getElementById('costWood').value),
            stone: parseFloat(document.getElementById('costStone').value),
            gold: parseFloat(document.getElementById('costGold').value),
        },
        agent_budget: parseFloat(document.getElementById('agentBudget').value),
        max_resource_types_per_turn: parseInt(document.getElementById('maxTypes').value),
        generated: {
            agent_projects: _generatedProjects.agent_projects,
            oracle_stats: _generatedProjects.oracle_stats,
            scenario_synergy: _generatedProjects.scenario_synergy || null,
            solver_stats: _generatedProjects.solver_stats || null,
        },
    };

    const scenarios = getSavedScenarios();
    scenarios.unshift(scenario);
    putSavedScenarios(scenarios);
    renderSavedScenarios();
}

export function loadScenario(idx) {
    const scenarios = getSavedScenarios();
    const s = scenarios[idx];
    if (!s) return;

    // Set resource fields
    document.getElementById('supWood').value = s.resource_supply.wood;
    document.getElementById('supStone').value = s.resource_supply.stone;
    document.getElementById('supGold').value = s.resource_supply.gold;
    document.getElementById('costWood').value = s.resource_costs.wood;
    document.getElementById('costStone').value = s.resource_costs.stone;
    document.getElementById('costGold').value = s.resource_costs.gold;
    document.getElementById('agentBudget').value = s.agent_budget;
    document.getElementById('maxTypes').value = s.max_resource_types_per_turn;

    // Restore generated projects
    _generatedProjects = s.generated;
    displayProjects(_generatedProjects);

    // Enable buttons
    document.getElementById('launchBtn').disabled = false;
    document.getElementById('oracleSolveBtn').disabled = false;
    document.getElementById('saveScenarioBtn').disabled = false;

    // Update status
    const os = s.generated.oracle_stats;
    const status = document.getElementById('scenarioStatus');
    status.textContent = `Loaded: M/C = ${os.mc_ratio.toFixed(3)}  |  C = ${os.combined}  |  M = ${os.collab_max}`;
    status.className = 'solver-status ok';

    // Update M/C slider to match
    const mcPct = Math.round(os.mc_ratio * 100);
    const slider = document.getElementById('mcRatio');
    if (mcPct >= parseInt(slider.min) && mcPct <= parseInt(slider.max)) {
        slider.value = mcPct;
        document.getElementById('mcDisplay').textContent = (mcPct / 100).toFixed(2);
    }
}

export function deleteScenario(idx) {
    const scenarios = getSavedScenarios();
    scenarios.splice(idx, 1);
    putSavedScenarios(scenarios);
    renderSavedScenarios();
}

export function renderSavedScenarios() {
    const container = document.getElementById('savedScenariosSection');
    const scenarios = getSavedScenarios();

    if (!scenarios.length) {
        container.innerHTML = '';
        return;
    }

    const rows = scenarios.map((s, i) => {
        const mc = s.generated?.oracle_stats?.mc_ratio;
        const date = new Date(s.saved_at).toLocaleDateString();
        return `<div class="saved-scenario-row">
            <span class="saved-scenario-name" title="${s.name}">${s.name}</span>
            <span class="saved-scenario-meta">${mc ? mc.toFixed(2) : '?'} · ${date}</span>
            <button class="saved-scenario-load" onclick="window.loadScenario(${i})">Load</button>
            <button class="saved-scenario-del" onclick="window.deleteScenario(${i})">Del</button>
        </div>`;
    }).join('');

    container.innerHTML = `
        <details class="saved-scenarios-details">
            <summary class="saved-scenarios-summary">Saved Scenarios (${scenarios.length})</summary>
            <div class="saved-scenarios-list">${rows}</div>
        </details>`;
}

export function importScenarioJSON() {
    const text = document.getElementById('importScenarioText').value.trim();
    if (!text) return;
    const status = document.getElementById('scenarioStatus');
    try {
        const data = JSON.parse(text);
        if (!data.agent_projects || !data.oracle_stats) {
            status.textContent = 'Invalid JSON: must have agent_projects and oracle_stats';
            status.className = 'solver-status err';
            return;
        }
        // Map placeholder resource names (r1/r2/r3) to frontend names (wood/stone/gold)
        const RESOURCE_MAP = {r1: 'wood', r2: 'stone', r3: 'gold'};
        const needsMapping = data.agent_projects.some(player =>
            player.some(p => Object.keys(p.requirements).some(k => k in RESOURCE_MAP))
        );
        if (needsMapping) {
            for (const player of data.agent_projects) {
                for (const proj of player) {
                    const mapped = {};
                    for (const [k, v] of Object.entries(proj.requirements)) {
                        mapped[RESOURCE_MAP[k] || k] = v;
                    }
                    proj.requirements = mapped;
                }
            }
            if (data.oracle_stats?.res_used) {
                const mapped = {};
                for (const [k, v] of Object.entries(data.oracle_stats.res_used)) {
                    mapped[RESOURCE_MAP[k] || k] = v;
                }
                data.oracle_stats.res_used = mapped;
            }
        }
        _generatedProjects = data;
        displayProjects(data);

        const os = data.oracle_stats;
        status.textContent = `Imported: M/C = ${os.mc_ratio.toFixed(3)}  |  C = ${os.combined}  |  M = ${os.collab_max}`;
        status.className = 'solver-status ok';

        document.getElementById('launchBtn').disabled = false;
        document.getElementById('oracleSolveBtn').disabled = false;
        document.getElementById('saveScenarioBtn').disabled = false;
        document.getElementById('importScenarioWrap').style.display = 'none';
    } catch (e) {
        status.textContent = `JSON parse error: ${e.message}`;
        status.className = 'solver-status err';
    }
}

export function swapAgentConfigs() {
    const fields = ['Type', 'Provider', 'Base', 'Key', 'ModelSelect', 'Model', 'Temp'];
    const valsA = {};
    const valsB = {};
    for (const f of fields) {
        const elA = document.getElementById(`agentA${f}`);
        const elB = document.getElementById(`agentB${f}`);
        valsA[f] = elA.value;
        valsB[f] = elB.value;
    }
    for (const f of fields) {
        document.getElementById(`agentA${f}`).value = valsB[f];
        document.getElementById(`agentB${f}`).value = valsA[f];
    }
    // Sync visibility of LLM config panels and custom model inputs
    for (const x of ['A', 'B']) {
        const type = document.getElementById(`agent${x}Type`).value;
        document.getElementById(`agent${x}LLM`).style.display = type === 'llm' ? 'block' : 'none';
        const select = document.getElementById(`agent${x}ModelSelect`);
        const input = document.getElementById(`agent${x}Model`);
        input.style.display = select.value === 'custom' ? '' : 'none';
    }
}

export function initConfigListeners() {
    // Initialize model inputs with default selected values
    ['A', 'B'].forEach(x => {
        const select = document.getElementById(`agent${x}ModelSelect`);
        const input = document.getElementById(`agent${x}Model`);
        input.value = select.value;
    });

    // Toggle LLM config visibility
    ['A', 'B'].forEach(x => {
        document.getElementById(`agent${x}Type`).addEventListener('change', e => {
            document.getElementById(`agent${x}LLM`).style.display = e.target.value === 'llm' ? 'block' : 'none';
        });
    });

    // Invalidate generated scenario when resource/cost/budget fields change
    const invalidatingIds = ['supWood', 'supStone', 'supGold', 'costWood', 'costStone', 'costGold', 'agentBudget', 'maxTypes'];
    invalidatingIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', clearGeneratedState);
    });

    // Wire up oracle solve
    window.__oracleSolve = oracleSolve;

    // Wire up scenario save/load and render existing
    window.saveScenario = saveScenario;
    window.loadScenario = loadScenario;
    window.deleteScenario = deleteScenario;
    window.importScenarioJSON = importScenarioJSON;
    renderSavedScenarios();
}

async function oracleSolve() {
    if (!_generatedProjects) return;
    const btn = document.getElementById('oracleSolveBtn');
    const resultDiv = document.getElementById('oracleResult');
    const agentCfg = getAgentConfig('A');

    if (agentCfg.type !== 'llm') {
        resultDiv.style.display = '';
        resultDiv.innerHTML = '<b style="color:var(--error)">Agent A must be an LLM agent to use Oracle Solve.</b>';
        return;
    }

    btn.disabled = true;
    btn.textContent = 'Solving...';
    resultDiv.style.display = '';
    resultDiv.innerHTML = 'Asking ' + (agentCfg.model || 'LLM') + ' to find the joint optimum...';

    try {
        const resp = await fetch('/api/scenario/oracle-solve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                agent_projects: _generatedProjects.agent_projects,
                resource_types: ["wood", "stone", "gold"],
                resource_supply: {
                    wood: parseInt(document.getElementById('supWood').value),
                    stone: parseInt(document.getElementById('supStone').value),
                    gold: parseInt(document.getElementById('supGold').value),
                },
                resource_costs: {
                    wood: parseFloat(document.getElementById('costWood').value),
                    stone: parseFloat(document.getElementById('costStone').value),
                    gold: parseFloat(document.getElementById('costGold').value),
                },
                agent_budget: parseFloat(document.getElementById('agentBudget').value),
                max_resource_types_per_turn: parseInt(document.getElementById('maxTypes').value),
                scenario_synergy: _generatedProjects.scenario_synergy || null,
                agent_config: agentCfg,
            }),
        });
        const data = await resp.json();

        if (data.error) {
            resultDiv.innerHTML = `<b style="color:var(--error)">Error:</b> ${data.error}${data.raw ? `<details style="margin-top:6px"><summary style="cursor:pointer;font-size:11px;color:var(--text-dim)">Raw LLM response</summary><pre style="white-space:pre-wrap;font-size:11px;margin-top:4px">${data.raw}</pre></details>` : ''}`;
            btn.disabled = false;
            btn.textContent = 'Solve with Oracle';
            return;
        }

        const os = _generatedProjects.oracle_stats;
        const warns = data.warnings?.length
            ? `<div style="color:var(--error);font-weight:600;margin-bottom:6px">Violations: ${data.warnings.join(', ')}</div>`
            : '';

        const fmtPurchases = (p) => Object.entries(p).filter(([,v]) => v > 0).map(([r,v]) => `${r}: ${v}`).join(', ') || 'none';
        const fmtRuns = (r) => r && Object.keys(r).length ? Object.entries(r).filter(([,v]) => v > 0).map(([name,n]) => `${name} x${n}`).join(', ') || 'none' : '';

        const promptSection = data.prompt
            ? `<details style="margin-top:8px"><summary style="cursor:pointer;font-size:11px;color:var(--text-dim)">System Prompt</summary><pre style="white-space:pre-wrap;font-size:11px;margin-top:4px;padding:8px;background:var(--surface);border:1px solid var(--border);border-radius:4px;max-height:300px;overflow-y:auto">${data.prompt}</pre></details>`
            : '';

        resultDiv.innerHTML = `
            ${warns}
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:8px">
                <div><b>Player 1</b><br>
                    Purchases: ${fmtPurchases(data.player1.purchases)} ($${data.player1.spent})<br>
                    ${fmtRuns(data.player1.project_runs) ? 'Runs: ' + fmtRuns(data.player1.project_runs) + '<br>' : ''}
                    ${data.player1.details.join('<br>')}
                    ${data.player1.synergy_bonus ? '<br>Synergy +' + data.player1.synergy_bonus : ''}
                    <br><b>Score: ${data.player1.reward}</b></div>
                <div><b>Player 2</b><br>
                    Purchases: ${fmtPurchases(data.player2.purchases)} ($${data.player2.spent})<br>
                    ${fmtRuns(data.player2.project_runs) ? 'Runs: ' + fmtRuns(data.player2.project_runs) + '<br>' : ''}
                    ${data.player2.details.join('<br>')}
                    ${data.player2.synergy_bonus ? '<br>Synergy +' + data.player2.synergy_bonus : ''}
                    <br><b>Score: ${data.player2.reward}</b></div>
            </div>
            <b>LLM combined:</b> ${data.combined} &nbsp;|&nbsp;
            <b>True collab max:</b> ${os.collab_max} &nbsp;|&nbsp;
            <b>Efficiency:</b> ${Math.round(100 * data.combined / Math.max(os.collab_max, 1))}%<br>
            <b>Reasoning:</b> ${data.reasoning || '—'}<br>
            <span style="font-size:11px;color:var(--text-dim)">Answered by ${agentCfg.model}</span>
            ${promptSection}
        `;
    } catch (e) {
        resultDiv.innerHTML = `<b style="color:var(--error)">Error:</b> ${e.message}`;
    }
    btn.disabled = false;
    btn.textContent = 'Solve with Oracle';
}
