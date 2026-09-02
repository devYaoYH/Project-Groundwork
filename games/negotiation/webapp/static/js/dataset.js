// --- Dataset tab ---

import { escapeHtml, renderRoundBlock, renderGameMetadata, formatDate, renderRoundDots } from './utils.js';
import { exportGameAsJSONL } from './export.js';

let _datasetTraces = [];
let _currentPage = 1;
let _totalTraces = 0;
let _pageSize = 50;
let _currentTraceData = null;
let _selectedGameIds = new Set();
let _judgeListenerAbort = null;  // cancels stale judge-version change listeners

export async function loadDataset() {
    if (!window._firestoreAvailable) {
        document.getElementById('datasetUnavailable').style.display = 'block';
        document.getElementById('datasetTable').innerHTML = '';
        document.getElementById('datasetPagination').style.display = 'none';
        return;
    }
    document.getElementById('datasetUnavailable').style.display = 'none';
    _currentPage = 1;
    _datasetTraces = [];
    document.getElementById('datasetDetail').style.display = 'none';
    await fetchTracesPage(1);
}

async function fetchTracesPage(page) {
    try {
        _currentPage = page;
        const offset = (page - 1) * _pageSize;
        const url = `/api/traces?limit=${_pageSize}&offset=${offset}`;
        const resp = await fetch(url);
        const data = await resp.json();
        _datasetTraces = data.traces || [];
        _totalTraces = data.total || _datasetTraces.length;
        renderDatasetTable();
        renderPagination();
        document.getElementById('datasetEmpty').style.display = _datasetTraces.length === 0 ? 'block' : 'none';
    } catch (e) {
        console.error('Failed to load traces:', e);
    }
}

function renderPagination() {
    const totalPages = Math.ceil(_totalTraces / _pageSize);
    if (totalPages <= 1) {
        document.getElementById('datasetPagination').style.display = 'none';
        return;
    }
    document.getElementById('datasetPagination').style.display = '';

    const maxButtons = 7;
    let startPage = Math.max(1, _currentPage - Math.floor(maxButtons / 2));
    let endPage = Math.min(totalPages, startPage + maxButtons - 1);
    if (endPage - startPage < maxButtons - 1) {
        startPage = Math.max(1, endPage - maxButtons + 1);
    }

    let html = `
        <button class="page-btn" ${_currentPage === 1 ? 'disabled' : ''} onclick="goToPage(${_currentPage - 1})">‹ Prev</button>
    `;

    if (startPage > 1) {
        html += `<button class="page-btn" onclick="goToPage(1)">1</button>`;
        if (startPage > 2) html += `<span class="page-ellipsis">...</span>`;
    }

    for (let i = startPage; i <= endPage; i++) {
        html += `<button class="page-btn ${i === _currentPage ? 'active' : ''}" onclick="goToPage(${i})">${i}</button>`;
    }

    if (endPage < totalPages) {
        if (endPage < totalPages - 1) html += `<span class="page-ellipsis">...</span>`;
        html += `<button class="page-btn" onclick="goToPage(${totalPages})">${totalPages}</button>`;
    }

    html += `
        <button class="page-btn" ${_currentPage === totalPages ? 'disabled' : ''} onclick="goToPage(${_currentPage + 1})">Next ›</button>
        <span class="page-info">${_totalTraces} total traces</span>
    `;

    document.getElementById('datasetPagination').innerHTML = html;
}

window.goToPage = function(page) {
    fetchTracesPage(page);
};

function renderDatasetTable() {
    const filtered = filterTraces(_datasetTraces);
    updateDatasetStats(filtered);

    const container = document.getElementById('datasetTable');
    if (filtered.length === 0) {
        container.innerHTML = '';
        return;
    }

    container.innerHTML = `
        <div class="dataset-table-wrap">
            <table class="dataset-table">
                <colgroup>
                    <col style="width:40px">
                    <col style="width:80px">
                    <col style="width:200px">
                    <col style="width:100px">
                    <col style="width:120px">
                    <col style="width:60px">
                    <col style="width:70px">
                    <col style="width:70px">
                    <col style="width:150px">
                </colgroup>
                <thead>
                    <tr>
                        <th><input type="checkbox" id="selectAllCheckbox" title="Select all"></th>
                        <th>Game ID<span class="col-resize"></span></th>
                        <th>Experiment<span class="col-resize"></span></th>
                        <th>Mode<span class="col-resize"></span></th>
                        <th>Agents<span class="col-resize"></span></th>
                        <th>Rounds<span class="col-resize"></span></th>
                        <th>Agent A<span class="col-resize"></span></th>
                        <th>Agent B<span class="col-resize"></span></th>
                        <th>Date</th>
                    </tr>
                </thead>
                <tbody>
                    ${filtered.map(t => `
                        <tr data-game-id="${t.game_id}">
                            <td class="checkbox-cell" onclick="event.stopPropagation()">
                                <input type="checkbox" class="game-checkbox" data-game-id="${t.game_id}" ${_selectedGameIds.has(t.game_id) ? 'checked' : ''}>
                            </td>
                            <td class="gid">${t.game_id.substring(0, 8)}</td>
                            <td class="dataset-cell-ellipsis" title="${escapeHtml(t.experiment_label || '-')}">${escapeHtml(t.experiment_label || '-')}</td>
                            <td><span class="mode-badge small">${t.mode}</span>${t.swapped ? ' <span class="swap-badge">swapped</span>' : ''}</td>
                            <td>${(t.agents || []).map(a => a.type).join(' vs ')}</td>
                            <td>${t.num_rounds}</td>
                            <td>${Number(t.agent_a_reward).toFixed(1)}</td>
                            <td>${Number(t.agent_b_reward).toFixed(1)}</td>
                            <td class="meta">${formatDate(t.created_at)}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;

    // Attach row click handlers
    container.querySelectorAll('tbody tr').forEach(row => {
        row.addEventListener('click', () => loadTraceDetail(row.dataset.gameId));
    });

    // Attach checkbox handlers
    const selectAllCheckbox = container.querySelector('#selectAllCheckbox');
    const gameCheckboxes = container.querySelectorAll('.game-checkbox');

    selectAllCheckbox.addEventListener('change', (e) => {
        const isChecked = e.target.checked;
        gameCheckboxes.forEach(cb => {
            cb.checked = isChecked;
            const gameId = cb.dataset.gameId;
            if (isChecked) {
                _selectedGameIds.add(gameId);
            } else {
                _selectedGameIds.delete(gameId);
            }
        });
    });

    gameCheckboxes.forEach(cb => {
        cb.addEventListener('change', (e) => {
            const gameId = e.target.dataset.gameId;
            if (e.target.checked) {
                _selectedGameIds.add(gameId);
            } else {
                _selectedGameIds.delete(gameId);
            }
            updateSelectAllCheckboxState();
        });
    });

    function updateSelectAllCheckboxState() {
        const allVisibleIds = new Set(filtered.map(t => t.game_id));
        const allSelected = Array.from(allVisibleIds).every(id => _selectedGameIds.has(id));
        selectAllCheckbox.checked = allSelected && allVisibleIds.size > 0;
    }

    updateSelectAllCheckboxState();

    // Attach column resize handlers
    initColumnResize(container.querySelector('.dataset-table'));
}

function initColumnResize(table) {
    const handles = table.querySelectorAll('.col-resize');
    const cols = table.querySelectorAll('colgroup col');

    handles.forEach((handle, i) => {
        let startX, startWidth;

        handle.addEventListener('mousedown', (e) => {
            e.preventDefault();
            e.stopPropagation();
            startX = e.pageX;
            startWidth = cols[i].offsetWidth || parseInt(cols[i].style.width);

            const onMouseMove = (e) => {
                const diff = e.pageX - startX;
                const newWidth = Math.max(40, startWidth + diff);
                cols[i].style.width = newWidth + 'px';
            };

            const onMouseUp = () => {
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            };

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
        });
    });
}

function filterTraces(traces) {
    const modeFilter = document.getElementById('datasetFilterMode').value;
    const agentFilter = document.getElementById('datasetFilterAgent').value;
    const expFilter = (document.getElementById('datasetFilterExperiment')?.value || '').trim().toLowerCase();
    return traces.filter(t => {
        if (modeFilter && t.mode !== modeFilter) return false;
        if (agentFilter) {
            const types = (t.agents || []).map(a => a.type);
            if (!types.includes(agentFilter)) return false;
        }
        if (expFilter && !(t.experiment_label || '').toLowerCase().includes(expFilter) && !(t.game_id || '').toLowerCase().includes(expFilter)) return false;
        return true;
    });
}

function updateDatasetStats(traces) {
    const stats = document.getElementById('datasetStats');
    if (traces.length === 0) {
        stats.innerHTML = '';
        return;
    }
    const totalA = traces.reduce((s, t) => s + Number(t.agent_a_reward), 0);
    const totalB = traces.reduce((s, t) => s + Number(t.agent_b_reward), 0);
    stats.innerHTML = `
        <span>${traces.length} traces</span>
        <span>Avg A: ${(totalA / traces.length).toFixed(1)}</span>
        <span>Avg B: ${(totalB / traces.length).toFixed(1)}</span>
    `;
}

export function exportCurrentTrace() {
    if (_currentTraceData) exportGameAsJSONL(_currentTraceData);
}

function renderReflections(reflections, agentAReward, agentBReward, perRoundScenarios) {
    if (!reflections || Object.keys(reflections).length === 0) {
        return '';
    }

    // Calculate theoretical joint maximum if oracle stats available
    let theoreticalJointMax = null;
    let efficiency = null;
    if (perRoundScenarios && Array.isArray(perRoundScenarios)) {
        let sum = 0;
        for (const scenario of perRoundScenarios) {
            const oracle = scenario.oracle_stats;
            if (oracle && oracle.collab_max) {
                sum += oracle.collab_max;
            }
        }
        if (sum > 0) {
            theoreticalJointMax = sum;
            const actualJoint = (agentAReward || 0) + (agentBReward || 0);
            efficiency = theoreticalJointMax > 0 ? (actualJoint / theoreticalJointMax * 100) : 0;
        }
    }

    let html = '<div class="reflections-section" style="margin-top:24px;padding:16px;background:var(--surface-2);border-radius:6px">';
    html += '<h3 style="margin-top:0;font-size:1.1em;color:var(--text)">🤔 Post-Game Reflections</h3>';

    if (theoreticalJointMax !== null) {
        const jointActual = (agentAReward || 0) + (agentBReward || 0);
        const expectedIndividual = theoreticalJointMax / 2.0;
        const warningColor = efficiency >= 60 ? 'var(--accent-a)' : 'var(--danger)';
        html += `<div style="margin-bottom:12px;padding:8px;background:var(--surface);border-radius:4px;font-size:0.9em">
            <strong>Performance:</strong> Joint total ${jointActual.toFixed(1)} / Theoretical max ${theoreticalJointMax.toFixed(1)}
            <span style="color:${efficiency >= 80 ? 'var(--success)' : warningColor};font-weight:bold">
                (${efficiency.toFixed(1)}% joint efficiency)
            </span>
            <br>
            <span style="color:var(--text-dim);font-size:0.95em">
                Expected individual: ${expectedIndividual.toFixed(1)}
                (A: ${agentAReward.toFixed(1)} = ${((agentAReward / expectedIndividual) * 100).toFixed(1)}%,
                B: ${agentBReward.toFixed(1)} = ${((agentBReward / expectedIndividual) * 100).toFixed(1)}%)
            </span>
        </div>`;
    }

    if (reflections.agent_a) {
        html += `<div style="margin-bottom:12px">
            <div style="font-weight:600;margin-bottom:4px;color:var(--accent-a)">Agent A:</div>
            <div style="padding:8px;background:var(--surface);border-left:3px solid var(--accent-a);border-radius:4px;font-style:italic">
                ${escapeHtml(reflections.agent_a)}
            </div>
        </div>`;
    }

    if (reflections.agent_b) {
        html += `<div style="margin-bottom:12px">
            <div style="font-weight:600;margin-bottom:4px;color:var(--accent-b)">Agent B:</div>
            <div style="padding:8px;background:var(--surface);border-left:3px solid var(--accent-b);border-radius:4px;font-style:italic">
                ${escapeHtml(reflections.agent_b)}
            </div>
        </div>`;
    }

    html += '</div>';
    return html;
}

function _formatVersionLabel(v) {
    const date = v.judged_at ? v.judged_at.slice(0, 10) : '';
    return `${v.judge_model || 'unknown'} @ ${v.prompt_version || '?'}${date ? ' (' + date + ')' : ''}`;
}

function renderJudgment(judgment, versions) {
    if (!judgment || !judgment.rounds) return '';

    const effIcon = { positive: '✅', negative: '❌', neutral: '➖' };
    const intentColor = { cooperative: 'var(--accent-a)', 'self-interested': 'var(--danger)', ambiguous: 'var(--text-dim)' };

    let html = '<div class="judge-section" id="judge-section" style="margin-top:24px;padding:16px;background:var(--surface-2);border-radius:6px">';
    html += `<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">`;
    html += `<h3 style="margin:0;font-size:1.1em;color:var(--text)">⚖️ LLM Judge Analysis</h3>`;

    // Version selector — only shown when multiple versions exist
    if (versions && versions.length > 1) {
        html += `<select id="judge-version-select" style="font-size:0.8em;padding:3px 6px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px;cursor:pointer">`;
        for (const v of versions) {
            const selected = v._doc_id === judgment._doc_id ? ' selected' : '';
            html += `<option value="${escapeHtml(v._doc_id)}"${selected}>${escapeHtml(_formatVersionLabel(v))}</option>`;
        }
        html += `</select>`;
    } else {
        html += `<span style="font-size:0.75em;color:var(--text-dim)">${escapeHtml(_formatVersionLabel(judgment))}</span>`;
    }
    html += `</div>`;

    if (judgment.game_attribution) {
        html += `<div style="margin-bottom:16px;padding:10px;background:var(--surface);border-left:3px solid var(--accent);border-radius:4px;font-style:italic;font-size:0.95em">
            ${escapeHtml(judgment.game_attribution)}
        </div>`;
    }

    if (judgment.input_transcript) {
        html += `<details style="margin-bottom:16px">
            <summary style="cursor:pointer;font-size:0.85em;color:var(--text-dim);padding:4px 0;user-select:none">
                📋 Judge input transcript
            </summary>
            <pre style="margin:8px 0 0 0;padding:10px;background:var(--surface);border-radius:4px;font-size:0.75em;white-space:pre-wrap;word-break:break-word;overflow-x:auto;max-height:400px;overflow-y:auto;color:var(--text-dim)">${escapeHtml(judgment.input_transcript)}</pre>
        </details>`;
    }

    for (const rnd of judgment.rounds) {
        const outcomeColor = rnd.round_outcome === 'optimal' ? 'var(--success)' : rnd.round_outcome === 'overdrawn' ? 'var(--danger)' : 'var(--warning)';
        html += `<details style="margin-bottom:10px" open>
            <summary style="cursor:pointer;font-weight:600;padding:6px 0">
                Round ${rnd.round_number}
                <span style="color:${outcomeColor};margin-left:6px">${rnd.round_outcome}</span>
                <span style="color:var(--text-dim);font-size:0.85em;margin-left:6px">${(rnd.joint_efficiency * 100).toFixed(0)}% efficiency</span>
                <span style="color:var(--text-dim);font-size:0.85em;margin-left:6px">${(rnd.patterns || []).length} patterns</span>
            </summary>
            <div style="padding:8px 0 0 12px">`;

        if (rnd.round_attribution) {
            html += `<div style="margin-bottom:10px;font-size:0.9em;color:var(--text-dim);font-style:italic">${escapeHtml(rnd.round_attribution)}</div>`;
        }
        if (rnd.prior_round_influence) {
            html += `<div style="margin-bottom:10px;font-size:0.85em;padding:4px 8px;background:var(--surface);border-radius:4px;color:var(--text-dim)">
                🔗 ${escapeHtml(rnd.prior_round_influence)}</div>`;
        }

        for (const p of (rnd.patterns || [])) {
            const coherenceIcon = p.speech_allocation_coherent ? '🤝' : '⚠️';
            html += `<div style="margin-bottom:10px;padding:8px;background:var(--surface);border-radius:4px;border-left:3px solid ${intentColor[p.intent] || 'var(--border)'}">
                <div style="font-weight:600;margin-bottom:4px">
                    ${effIcon[p.effectiveness] || '?'} ${escapeHtml(p.name)}
                    <span style="font-size:0.8em;font-weight:normal;color:${intentColor[p.intent] || 'var(--text-dim)'};margin-left:6px">${p.intent}</span>
                    <span style="font-size:0.8em;margin-left:6px" title="${p.speech_allocation_coherent ? 'speech matches allocation' : 'speech/allocation mismatch'}">${coherenceIcon}</span>
                    ${p.canonical_id ? `<span style="font-size:0.75em;font-weight:normal;color:var(--text-dim);margin-left:6px">[${escapeHtml(p.canonical_id)}]</span>` : ''}
                </div>
                <div style="font-size:0.88em;margin-bottom:6px">${escapeHtml(p.description)}</div>
                ${p.coherence_note ? `<div style="font-size:0.82em;color:var(--warning);margin-bottom:6px">⚠ ${escapeHtml(p.coherence_note)}</div>` : ''}
                ${(p.evidence || []).map(ev => `
                    <div style="font-size:0.8em;color:var(--text-dim);border-left:2px solid var(--border);padding-left:6px;margin-top:4px">
                        <em>${escapeHtml(ev.speaker)}/${escapeHtml(ev.type)}:</em> "${escapeHtml(ev.quote)}"
                    </div>`).join('')}
            </div>`;
        }

        html += '</div></details>';
    }

    html += '</div>';
    return html;
}

async function loadTraceDetail(gameId) {
    try {
        // Check sessionStorage cache first
        const cacheKey = `trace_${gameId}`;
        let traceData;
        const cached = sessionStorage.getItem(cacheKey);
        if (cached) {
            traceData = JSON.parse(cached);
        } else {
            const resp = await fetch(`/api/traces/${gameId}`);
            if (!resp.ok) {
                alert('Failed to load trace');
                return;
            }
            traceData = await resp.json();
            try { sessionStorage.setItem(cacheKey, JSON.stringify(traceData)); } catch (_) { /* quota exceeded */ }
        }
        _currentTraceData = traceData;
        const data = traceData.result || {};
        const config = traceData.game_config || {};
        const cheapTalkTurns = config.cheap_talk_turns || 0;

        const detail = document.getElementById('datasetDetail');
        detail.style.display = 'block';

        let html = `
            <div class="game-header">
                <h2>Trace ${data.game_id || gameId}</h2>
                <span class="mode-badge">${data.mode || '?'}</span>
            </div>
            <div class="scoreboard">
                <div class="score-card agent-a">
                    <div class="label">Agent A — Total</div>
                    <div class="value">${Number(data.agent_a_cumulative_reward || 0).toFixed(1)}</div>
                    <div class="round-dots">${renderRoundDots(data.rounds || [], 'agent_a', cheapTalkTurns)}</div>
                </div>
                <div class="score-card agent-b">
                    <div class="label">Agent B — Total</div>
                    <div class="value">${Number(data.agent_b_cumulative_reward || 0).toFixed(1)}</div>
                    <div class="round-dots">${renderRoundDots(data.rounds || [], 'agent_b', cheapTalkTurns)}</div>
                </div>
            </div>
            ${renderGameMetadata(traceData.game_config, data, { showDownload: true, downloadId: gameId })}
            ${renderPromptEvents(traceData.prompt_events)}
        `;

        for (const round of (data.rounds || [])) {
            html += renderRoundBlock(round);
        }

        html += renderReflections(data.reflections, data.agent_a_cumulative_reward, data.agent_b_cumulative_reward, traceData.per_round_scenarios);

        detail.innerHTML = html;

        // Attach handlers
        detail.querySelector('[data-action="downloadTrace"]')?.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            exportCurrentTrace();
        });

        detail.scrollIntoView({ behavior: 'smooth' });

        // Async: fetch latest judgment + all versions concurrently (non-blocking)
        Promise.all([
            fetch(`/api/judge/${gameId}`).then(r => r.ok ? r.json() : null).catch(() => null),
            fetch(`/api/judge/${gameId}/versions`).then(r => r.ok ? r.json() : null).catch(() => null),
        ]).then(([judgment, versionsResp]) => {
            if (!judgment || judgment.error) return;
            const versions = versionsResp?.versions || [];
            const judgeHtml = renderJudgment(judgment, versions);
            if (!judgeHtml) return;
            detail.insertAdjacentHTML('beforeend', judgeHtml);

            // Cancel any listener from a previously selected game, then attach a
            // fresh one scoped to this gameId. Without this, selecting multiple
            // games accumulates listeners that all fire on a single dropdown change,
            // replacing the view with a different game's judgment.
            if (_judgeListenerAbort) _judgeListenerAbort.abort();
            _judgeListenerAbort = new AbortController();
            detail.addEventListener('change', async (e) => {
                if (e.target.id !== 'judge-version-select') return;
                const docId = e.target.value;
                const section = detail.querySelector('#judge-section');
                if (section) section.style.opacity = '0.5';
                try {
                    const resp = await fetch(`/api/judge/${gameId}/version/${docId}`);
                    if (!resp.ok) { if (section) section.style.opacity = '1'; return; }
                    const newJudgment = await resp.json();
                    const newHtml = renderJudgment(newJudgment, versions);
                    if (section && newHtml) {
                        section.insertAdjacentHTML('afterend', newHtml);
                        section.remove();
                        // Restore selected value in the new dropdown
                        const newSelect = detail.querySelector('#judge-version-select');
                        if (newSelect) newSelect.value = docId;
                    }
                } catch (_) {
                    if (section) section.style.opacity = '1';
                }
            }, { signal: _judgeListenerAbort.signal });
        });
    } catch (e) {
        console.error('Failed to load trace detail:', e);
    }
}

export async function exportDatasetJSONL() {
    const filtered = filterTraces(_datasetTraces);
    if (filtered.length === 0) {
        alert('No traces to export');
        return;
    }

    const lines = [];
    for (const t of filtered) {
        try {
            const resp = await fetch(`/api/traces/${t.game_id}`);
            if (resp.ok) {
                const full = await resp.json();
                lines.push(JSON.stringify(full));
            }
        } catch (e) {
            console.error(`Failed to fetch trace ${t.game_id}:`, e);
        }
    }

    const blob = new Blob([lines.join('\n') + '\n'], { type: 'application/jsonl' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `dataset_${new Date().toISOString().slice(0, 10)}.jsonl`;
    a.click();
    URL.revokeObjectURL(url);
}

function showDeleteModal() {
    if (_selectedGameIds.size === 0) {
        alert('No games selected to delete.');
        return;
    }

    const collection = window._appConfig?.firestore_collection || 'game_traces';
    const gameIds = Array.from(_selectedGameIds);
    const idsJson = JSON.stringify(gameIds).replace(/"/g, "'");
    const command = `python -c "import firebase_admin; from firebase_admin import firestore; firebase_admin.initialize_app() if not len(firebase_admin._apps) else None; db = firestore.client(); [db.collection('${collection}').document(id).delete() for id in ${idsJson}]; print('Deleted ${gameIds.length} document(s)')"`;
    const modal = document.createElement('div');
    modal.className = 'command-modal';
    modal.innerHTML = `
        <div class="command-modal-content">
            <span class="command-modal-close">&times;</span>
            <h3>Firestore Delete Command</h3>
            <p>Run the following command in your shell to delete the selected game traces:</p>
            <pre id="deleteCommand">${command}</pre>
            <button id="copyCommandBtn">Copy to Clipboard</button>
        </div>
    `;
    document.body.appendChild(modal);

    modal.querySelector('.command-modal-close').onclick = () => {
        document.body.removeChild(modal);
    };
    modal.querySelector('#copyCommandBtn').onclick = () => {
        navigator.clipboard.writeText(command).then(() => {
            alert('Command copied to clipboard!');
        }, () => {
            alert('Failed to copy command.');
        });
    };
    window.onclick = (event) => {
        if (event.target == modal) {
            document.body.removeChild(modal);
        }
    }
}

function renderPromptEvents(promptEvents) {
    if (!promptEvents || promptEvents.length === 0) return '';

    // Group by type then by round/agent
    const systemPrompts = promptEvents.filter(e => e.type === 'system_prompt');
    const ctInstructions = promptEvents.filter(e => e.type === 'project_instructions');

    let inner = '';

    // System prompts (one per agent, emitted at game start)
    for (const ev of systemPrompts) {
        const d = ev.data || {};
        inner += `<div style="margin-bottom:8px">
            <strong>${escapeHtml(d.agent || '?')} — System Prompt</strong>
            <pre class="system-prompt-log" style="white-space:pre-wrap;max-height:300px;overflow-y:auto;font-size:0.8em;margin-top:4px">${escapeHtml(d.prompt || '')}</pre>
        </div>`;
    }

    // Cheap talk instructions (per round, per agent)
    if (ctInstructions.length > 0) {
        inner += '<div style="margin-top:8px"><strong>Project Instructions</strong></div>';
        for (const ev of ctInstructions) {
            const d = ev.data || {};
            inner += `<div style="margin-bottom:6px;margin-left:8px">
                <em>Round ${d.round || '?'} — ${escapeHtml(d.agent || '?')}</em>
                <pre class="system-prompt-log" style="white-space:pre-wrap;max-height:200px;overflow-y:auto;font-size:0.8em;margin-top:2px">${escapeHtml(d.prompt || '')}</pre>
            </div>`;
        }
    }

    return `<details style="margin-top:8px;margin-bottom:8px">
        <summary style="cursor:pointer;font-size:0.9em;color:var(--text-muted)">Agent Prompts (${promptEvents.length} events)</summary>
        <div style="padding:8px 0">${inner}</div>
    </details>`;
}

async function jumpToGameId() {
    const input = document.getElementById('datasetJumpId');
    const gameId = (input?.value || '').trim();
    if (!gameId) return;
    await loadTraceDetail(gameId);
    input.value = '';
}

export function initDatasetListeners() {
    document.getElementById('datasetFilterMode')?.addEventListener('change', () => renderDatasetTable());
    document.getElementById('datasetFilterAgent')?.addEventListener('change', () => renderDatasetTable());
    document.getElementById('datasetFilterExperiment')?.addEventListener('input', () => renderDatasetTable());
    document.getElementById('datasetDeleteBtn')?.addEventListener('click', showDeleteModal);
    document.getElementById('datasetJumpBtn')?.addEventListener('click', jumpToGameId);
    document.getElementById('datasetJumpId')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') jumpToGameId();
    });
}
