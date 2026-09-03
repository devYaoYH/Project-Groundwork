// --- Utility functions ---

export function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}

export function renderApiMeta(meta) {
    if (!meta) return '';
    const parts = [];
    if (meta.model) parts.push(meta.model);
    if (meta.duration_s != null) parts.push(`${meta.duration_s}s`);
    if (meta.completion_tokens != null) parts.push(`${meta.completion_tokens} out tok`);
    if (meta.prompt_tokens != null) parts.push(`${meta.prompt_tokens} in tok`);
    if (parts.length === 0) return '';
    return `<div class="api-meta-badge">${parts.join(' · ')}</div>`;
}

export function renderRoundDots(rounds, agent, cheapTalkTurns) {
    if (!rounds || rounds.length === 0) return '';

    return rounds.map(r => {
        const roundNum = r.round_number;
        const overdrawn = r.overdrawn || false;

        // Compute early termination: did round end before using all cheap talk turns?
        let roundEndedEarly = false;

        if (r.stats && r.stats.early_submit !== undefined) {
            // Use precomputed stats if available
            roundEndedEarly = r.stats.early_submit;
        } else if (cheapTalkTurns > 0) {
            // Compute from transcript: count speech messages (not thinking)
            const transcript = r.cheap_talk_transcript || [];
            const speechMessages = transcript.filter(entry =>
                entry.type !== 'thinking' && entry.speaker !== 'system'
            );
            // Find max turn number used
            const maxTurn = speechMessages.length > 0
                ? Math.max(...speechMessages.map(e => e.turn || 0))
                : -1;
            const turnsUsed = maxTurn + 1;
            roundEndedEarly = turnsUsed < cheapTalkTurns;
        }

        let className = 'round-dot';
        let title = `Round ${roundNum}`;

        if (overdrawn && roundEndedEarly) {
            className += ' early-overdrawn';
            title += ' (Early termination → Overdrawn)';
        } else if (overdrawn) {
            className += ' overdrawn';
            title += ' (Overdrawn)';
        } else if (roundEndedEarly) {
            className += ' early-success';
            title += ' (Early termination)';
        } else {
            className += ' normal';
        }

        return `<div class="${className}" title="${title}"></div>`;
    }).join('');
}

function renderProjectRuns(projectRuns) {
    if (!projectRuns) return '';
    const runs = projectRuns.runs || {};
    const runStr = Object.entries(runs).filter(([, v]) => v > 0).map(([k, v]) => `${k} x${v}`).join(', ');
    if (!runStr) return '<div class="project-runs-line">No projects run</div>';
    let detail = `<div class="project-runs-line">Projects: ${runStr}`;
    if (projectRuns.synergy_bonus > 0) {
        detail += ` (base: ${projectRuns.base_reward.toFixed(1)}, synergy: +${projectRuns.synergy_bonus.toFixed(1)})`;
    }
    detail += '</div>';
    return detail;
}

export function renderAlloc(alloc) {
    if (!alloc || Object.keys(alloc).length === 0) return '<div class="alloc-item"><span class="resource">Nothing purchased</span></div>';
    return Object.entries(alloc).map(([r, q]) =>
        `<div class="alloc-item"><span class="resource">${r}</span><span class="qty">${q}</span></div>`
    ).join('');
}

function renderRoundStats(stats) {
    if (!stats) return '';

    const parts = [];

    // Turns used
    parts.push(`Turns: ${stats.num_turns_used}`);

    // Message counts
    parts.push(`Msgs: A:${stats.agent_a_msg_count} B:${stats.agent_b_msg_count}`);

    // Character counts split by agent + total
    const formatChars = (count) => count >= 1000 ? `${(count / 1000).toFixed(1)}k` : count.toString();
    const charA = formatChars(stats.agent_a_char_count);
    const charB = formatChars(stats.agent_b_char_count);
    const charTotal = formatChars(stats.total_char_count);
    parts.push(`Chars: A:${charA} B:${charB} (${charTotal})`);

    // Early submit indicator
    if (stats.early_submit) {
        parts.push('<span class="early-indicator">⚡ Early</span>');
    }

    return `<span class="round-stats">${parts.join(' · ')}</span>`;
}

export function renderRoundBlock(round) {
    const overdrawn = round.overdrawn;
    const statsHtml = renderRoundStats(round.stats);

    return `
        <div class="round-block open">
            <div class="round-header" onclick="this.parentElement.classList.toggle('open')">
                <div class="round-header-left">
                    <span class="round-label">Round ${round.round_number}</span>
                    ${statsHtml}
                </div>
                <span class="round-status ${overdrawn ? 'overdrawn' : 'success'}">
                    ${overdrawn ? 'overdrawn' : `A: +${round.agent_a_reward.toFixed(1)} · B: +${round.agent_b_reward.toFixed(1)}`}
                </span>
            </div>
            <div class="round-body">
                <div class="chat-section">
                    <h4>Cheap Talk</h4>
                    ${[...(round.cheap_talk_transcript || [])].sort((a, b) => {
                        const ta = (a.turn != null && a.turn >= 0) ? a.turn : Infinity;
                        const tb = (b.turn != null && b.turn >= 0) ? b.turn : Infinity;
                        return ta - tb;
                    }).map(m => {
                        if (m.speaker === 'system') {
                            return `<div class="chat-msg system"><div class="bubble" style="color:var(--text-muted);font-style:italic;border-color:var(--border)">${escapeHtml(m.message)}</div></div>`;
                        }
                        const cls = m.speaker === 'agent_a' ? 'agent-a' : 'agent-b';
                        const label = m.speaker === 'agent_a' ? 'A' : 'B';
                        const metaBadge = renderApiMeta(m.api_meta);
                        if (m.type === 'thinking') {
                            return `<div class="chat-msg ${cls} thinking-msg"><div class="avatar">${label}</div><div class="bubble thinking-bubble">${escapeHtml(m.message)}${metaBadge}</div></div>`;
                        }
                        return `<div class="chat-msg ${cls}"><div class="avatar">${label}</div><div class="bubble">${escapeHtml(m.message)}${metaBadge}</div></div>`;
                    }).join('')}
                </div>
                <div class="allocations" style="display:grid">
                    <div class="alloc-card agent-a">
                        <h5>Agent A</h5>
                        ${renderAlloc(round.agent_a_allocation)}
                        ${renderProjectRuns(round.agent_a_project_runs)}
                        <div class="reward-line">Reward: ${round.agent_a_reward.toFixed(1)}</div>
                    </div>
                    <div class="alloc-card agent-b">
                        <h5>Agent B</h5>
                        ${renderAlloc(round.agent_b_allocation)}
                        ${renderProjectRuns(round.agent_b_project_runs)}
                        <div class="reward-line">Reward: ${round.agent_b_reward.toFixed(1)}</div>
                    </div>
                </div>
                <div class="supply-check" style="display:flex">
                    ${Object.keys(round.resource_supply || {}).map(r => {
                        const demanded = (round.total_demanded || {})[r] || 0;
                        const supply = round.resource_supply[r];
                        const pct = Math.min((demanded / supply) * 100, 100);
                        const over = demanded > supply;
                        return `<div class="supply-item"><span>${r}</span><div class="supply-bar-bg"><div class="supply-bar ${over ? 'over' : 'ok'}" style="width:${pct}%"></div></div><span>${demanded}/${supply}</span></div>`;
                    }).join('')}
                </div>
            </div>
        </div>
    `;
}

function renderProjectInfo(projects, label) {
    if (!projects || projects.length === 0) return '-';
    return projects.map(p => {
        const req = Object.entries(p.requirements).map(([r, q]) => `${r}x${q}`).join(', ');
        let s = `${p.name}: [${req}] = ${p.reward}/run`;
        if (p.synergy) s += ` (+${p.synergy.bonus} if ${p.synergy.resource}>=${p.synergy.threshold})`;
        return s;
    }).join('; ');
}

function formatOracleStats(os) {
    if (!os) return '';
    let s = `V1=${os.v1?.toFixed(1)}, V2=${os.v2?.toFixed(1)}, C=${os.combined?.toFixed(1)}, M=${os.collab_max?.toFixed(1)}, M/C=${os.mc_ratio?.toFixed(3)}`;
    if (os.collab_detail && os.collab_detail !== '—') {
        s += ` | Optimal: ${os.collab_detail}`;
    }
    return s;
}

function renderRewardMeta(cfg, res) {
    const scenarios = res.per_round_scenarios;
    const agentProjects = cfg.agent_projects || res.agent_projects;
    if (!agentProjects) return '';

    // Check if scenarios vary across rounds (rotating projects)
    const hasRotation = scenarios && scenarios.length > 1 && scenarios.some((s, i) =>
        i > 0 && JSON.stringify(s.agent_projects) !== JSON.stringify(scenarios[0].agent_projects)
    );

    // For non-rotating games, show single oracle stats row; for rotating, fold into per-round
    let oracleHtml = '';
    if (!hasRotation) {
        const os = scenarios?.[0]?.oracle_stats || cfg.oracle_stats;
        if (os) {
            oracleHtml = `<div class="meta-row"><div class="meta-group wide"><div class="meta-label">Oracle Stats</div><div class="meta-value">${formatOracleStats(os)}</div></div></div>`;
        }
    }

    if (hasRotation) {
        // Rotating: skip top-level A/B projects, show per-round details with oracle stats
        return renderPerRoundScenarios(scenarios);
    }

    return `
        <div class="meta-row">
            <div class="meta-group wide">
                <div class="meta-label"><span class="dot dot-a"></span> A Projects</div>
                <div class="meta-value">${renderProjectInfo(agentProjects[0])}</div>
            </div>
        </div>
        <div class="meta-row">
            <div class="meta-group wide">
                <div class="meta-label"><span class="dot dot-b"></span> B Projects</div>
                <div class="meta-value">${renderProjectInfo(agentProjects[1])}</div>
            </div>
        </div>
        ${oracleHtml}`;
}

function renderPerRoundScenarios(perRoundScenarios) {
    if (!perRoundScenarios || perRoundScenarios.length === 0) return '';
    const rows = perRoundScenarios.map((s, i) => {
        const roundNum = i + 1;
        const aProj = renderProjectInfo(s.agent_projects?.[0]);
        const bProj = renderProjectInfo(s.agent_projects?.[1]);
        const os = s.oracle_stats;
        const oracleStr = formatOracleStats(os) || '-';
        return `<div class="meta-row" style="font-size:0.85em">
            <div class="meta-group"><div class="meta-label">Round ${roundNum}</div></div>
            <div class="meta-group wide"><div class="meta-label">Oracle</div><div class="meta-value">${oracleStr}</div></div>
        </div>
        <div class="meta-row" style="font-size:0.85em">
            <div class="meta-group wide"><div class="meta-label"><span class="dot dot-a"></span> A</div><div class="meta-value">${aProj}</div></div>
        </div>
        <div class="meta-row" style="font-size:0.85em">
            <div class="meta-group wide"><div class="meta-label"><span class="dot dot-b"></span> B</div><div class="meta-value">${bProj}</div></div>
        </div>`;
    });
    return `<details class="per-round-details" style="margin-top:4px">
        <summary style="cursor:pointer;font-size:0.85em;color:var(--text-muted)">Per-Round Projects (${perRoundScenarios.length} rounds)</summary>
        ${rows.join('')}
    </details>`;
}

export function renderGameMetadata(config, result, options = {}) {
    if (!config && !result) return '';
    const cfg = config || {};
    const res = result || {};
    const { showDownload = false, downloadId = null } = options;

    const agentA = cfg.agent_a || (cfg.agents && cfg.agents[0]) || {};
    const agentB = cfg.agent_b || (cfg.agents && cfg.agents[1]) || {};
    const agentALabel = agentA.type === 'llm' ? `LLM (${agentA.model || '?'})` : (agentA.type || '?');
    const agentBLabel = agentB.type === 'llm' ? `LLM (${agentB.model || '?'})` : (agentB.type || '?');

    const supply = cfg.resource_supply || {};
    const costs = cfg.resource_costs || {};
    const resources = cfg.resource_types || Object.keys(supply).length > 0 ? Object.keys(supply) : ['wood', 'stone', 'gold'];

    const mode = cfg.mode || res.mode || '?';
    const rounds = cfg.num_rounds || res.num_rounds || '?';
    const ctTurns = cfg.cheap_talk_turns || '?';
    const budget = cfg.agent_budget != null ? cfg.agent_budget : '?';
    const seed = cfg.seed != null ? cfg.seed : (res.seed != null ? res.seed : '-');
    const swapped = cfg.swapped || res.swapped || false;
    const rotateProjects = cfg.rotate_projects || res.per_round_scenarios?.length > 0;
    const thinking = cfg.thinking != null ? (cfg.thinking ? 'On' : 'Off') : '-';
    const visUtil = cfg.visible_utilities ? 'Visible' : 'Hidden';
    const visOutcome = cfg.visible_outcome === false ? 'Hidden' : 'Visible';
    const expLabel = cfg.experiment_label || '';
    const expRunId = cfg.episode_id || '';
    const goal = cfg.goal || '';
    const goalLabel = goal || 'Maximize Own Reward';

    const downloadBtn = showDownload && downloadId
        ? `<button class="metadata-download-btn" data-action="downloadTrace" data-id="${downloadId}" title="Download JSONL">
             <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
               <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
             </svg>
           </button>`
        : '';

    return `
        <details class="environment-metadata-details">
            <summary class="environment-metadata-summary">
                <span class="summary-text">
                    <svg class="summary-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="6 9 12 15 18 9"></polyline>
                    </svg>
                    Environment Configuration
                </span>
                ${downloadBtn}
            </summary>
            <div class="environment-metadata">
                ${expLabel || expRunId ? `<div class="meta-row">${expLabel ? `<div class="meta-group wide"><div class="meta-label">Experiment</div><div class="meta-value experiment-label">${escapeHtml(expLabel)}</div></div>` : ''}${expRunId ? `<div class="meta-group wide"><div class="meta-label">Run ID</div><div class="meta-value" style="font-family:monospace;font-size:0.85em" title="${escapeHtml(expRunId)}">${escapeHtml(expRunId.substring(0, 8))}</div></div>` : ''}</div>` : ''}
                <div class="meta-row">
                    <div class="meta-group">
                        <div class="meta-label">Mode</div>
                        <div class="meta-value">${mode}${swapped ? ' (swapped)' : ''}${rotateProjects ? ' (rotate)' : ''}</div>
                    </div>
                    <div class="meta-group">
                        <div class="meta-label">Rounds</div>
                        <div class="meta-value">${rounds}</div>
                    </div>
                    <div class="meta-group">
                        <div class="meta-label">Cheap Talk</div>
                        <div class="meta-value">${ctTurns} turns</div>
                    </div>
                    <div class="meta-group">
                        <div class="meta-label">Budget</div>
                        <div class="meta-value">$${budget}</div>
                    </div>
                    <div class="meta-group">
                        <div class="meta-label">Thinking</div>
                        <div class="meta-value">${thinking}</div>
                    </div>
                    <div class="meta-group">
                        <div class="meta-label">Utilities</div>
                        <div class="meta-value">${visUtil}</div>
                    </div>
                    <div class="meta-group">
                        <div class="meta-label">Outcome</div>
                        <div class="meta-value">${visOutcome}</div>
                    </div>
                    <div class="meta-group">
                        <div class="meta-label">Seed</div>
                        <div class="meta-value">${seed}</div>
                    </div>
                    <div class="meta-group">
                        <div class="meta-label">Goal</div>
                        <div class="meta-value">${goalLabel}</div>
                    </div>
                </div>
                <div class="meta-row">
                    <div class="meta-group">
                        <div class="meta-label"><span class="dot dot-a"></span> Agent A</div>
                        <div class="meta-value">${agentALabel}</div>
                    </div>
                    <div class="meta-group">
                        <div class="meta-label"><span class="dot dot-b"></span> Agent B</div>
                        <div class="meta-value">${agentBLabel}</div>
                    </div>
                </div>
                ${renderRewardMeta(cfg, res)}
                <div class="meta-row">
                    <div class="meta-group wide">
                        <div class="meta-label">Resources</div>
                        <div class="meta-value">${resources.map(r =>
                            `${r}: ${supply[r] != null ? supply[r] : '?'} avail @ $${costs[r] != null ? costs[r] : '?'}`
                        ).join(' · ')}</div>
                    </div>
                </div>
            </div>
        </details>
    `;
}

export function formatDate(dateStr) {
    if (!dateStr) return '-';
    try {
        const d = new Date(dateStr);
        return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
        return dateStr;
    }
}
