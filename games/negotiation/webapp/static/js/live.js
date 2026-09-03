// --- Live environment ---

import { escapeHtml, renderAlloc, renderApiMeta } from './utils.js';
import { idbSaveGame } from './storage.js';
import { buildGameConfig } from './config.js';

let currentGameId = null;
let currentGamePayload = null;
let currentLiveConfig = null;
let liveScoreA = 0, liveScoreB = 0;
let pollTimer = null;
let eventCursor = 0;
let _allLiveEvents = [];
let _earlyDecisions = {}; // Track early decisions by round: { roundNum: { agent_a: bool, agent_b: bool } }
let _roundStats = {}; // Track round stats: { roundNum: { agent_a_msg_count, agent_b_msg_count, agent_a_char_count, agent_b_char_count, num_turns_used } }
let _currentHumanAgent = null;
let _currentHumanPublicConfig = null;
let _thinkingTraceVisible = true;

export async function launchGame(payloadOverride = null) {
    const btn = payloadOverride ? null : document.getElementById('launchBtn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Starting...';
    }

    const payload = payloadOverride || buildGameConfig();
    currentGamePayload = payload;

    try {
        const resp = await fetch('/api/environment/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok || !data.episode_uid) {
            throw new Error(data.error || `server returned ${resp.status}`);
        }
        openLiveView(data.episode_uid, payload.mode);
    } catch (e) {
        alert('Failed to start environment: ' + e.message);
    }

    if (btn) {
        btn.disabled = false;
        btn.textContent = 'Launch Environment';
    }
}

export function openLiveView(gameId, mode) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="live"]').classList.add('active');
    document.getElementById('tab-live').classList.add('active');

    document.getElementById('liveEmpty').style.display = 'none';
    document.getElementById('liveView').style.display = 'block';
    document.getElementById('liveGameId').textContent = gameId;
    document.getElementById('liveMode').textContent = mode || '';
    document.getElementById('liveRounds').innerHTML = '';
    document.getElementById('eventLog').innerHTML = '';
    document.getElementById('liveStatus').innerHTML = '<span class="spinner"></span> Running...';
    liveScoreA = 0;
    liveScoreB = 0;
    currentLiveConfig = null;
    _earlyDecisions = {};
    _roundStats = {};
    _currentHumanAgent = null;
    _currentHumanPublicConfig = null;
    _thinkingTraceVisible = !isHumanVsLlmGame();
    document.getElementById('liveScoreA').textContent = '0';
    document.getElementById('liveScoreB').textContent = '0';
    document.getElementById('liveDotsA').innerHTML = '';
    document.getElementById('liveDotsB').innerHTML = '';
    document.getElementById('humanInputBar').style.display = 'none';
    document.getElementById('humanSystemPrompt').style.display = 'none';
    document.getElementById('humanProjectBrief').style.display = 'none';
    const stopBtn = document.getElementById('stopBtn');
    stopBtn.style.display = 'inline-block';
    stopBtn.disabled = false;
    stopBtn.textContent = 'Stop Environment';
    updateThinkingTraceUi();

    stopLiveConnection();

    currentGameId = gameId;
    eventCursor = 0;

    startPolling(gameId);
}

function stopLiveConnection() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    currentGameId = null;
}

export async function stopGame() {
    if (!currentGameId) return;
    const btn = document.getElementById('stopBtn');
    btn.disabled = true;
    btn.textContent = 'Stopping...';
    try {
        await fetch(`/api/environment/${currentGameId}/stop`, { method: 'POST' });
    } catch (e) {
        console.error('Stop failed:', e);
    }
}

function startPolling(gameId) {
    pollEvents(gameId);
    pollTimer = setInterval(() => pollEvents(gameId), 500);
}

let _pollInFlight = false;

async function pollEvents(gameId) {
    if (currentGameId !== gameId) return;
    // Guard against overlapping polls: on slow connections several interval
    // ticks can fire before the first response arrives, and all of them would
    // request the same `after=` cursor and render duplicate events.
    if (_pollInFlight) return;
    _pollInFlight = true;
    try {
        const resp = await fetch(`/api/environment/${gameId}/events?after=${eventCursor}`);
        const data = await resp.json();
        if (currentGameId !== gameId) return;
        for (const ev of data.events) {
            handleEvent(ev);
            _allLiveEvents.push(ev);
            eventCursor++;
        }
        if (data.done && pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
            saveCompletedGameToIDB(gameId);
        }
    } catch (e) {
        console.error('Poll failed:', e);
    } finally {
        _pollInFlight = false;
    }
}

async function saveCompletedGameToIDB(gameId) {
    try {
        const resp = await fetch(`/api/environment/${gameId}`);
        const result = await resp.json();
        const gameData = {
            episode_uid: gameId,
            game_config: currentGamePayload || {},
            result: result,
            events: _allLiveEvents.slice(),
            created_at: new Date().toISOString(),
        };
        await idbSaveGame(gameData);
    } catch (e) {
        console.error('Failed to save environment to IndexedDB:', e);
    }
}

// Exported so a replay surface can drive the same renderer from a recorded
// event stream instead of the live polling loop. The function is unchanged:
// replay and live must not diverge into two visualisations of one environment.
export function handleEvent(event) {
    const logEl = document.getElementById('eventLog');
    if (!logEl) return;
    const evDiv = document.createElement('div');
    evDiv.className = 'ev';

    switch (event.type) {
        case 'game_start':
            currentLiveConfig = event.data.config || null;
            evDiv.innerHTML = `<span class="ev-type">[start]</span> Environment ${event.data.episode_uid} — ${event.data.config?.mode || '?'} mode, ${event.data.config?.num_rounds || '?'} rounds`;
            break;

        case 'phase_start':
            evDiv.innerHTML = `<span class="ev-type">[round ${event.data.round}]</span> Phase: ${event.data.phase}`;
            // Initialize round block and stats when any phase starts
            if (event.data.phase === 'cheap_talk' || event.data.phase === 'decision') {
                ensureRoundBlock(event.data.round);
                // Initialize stats for this round (if not already done)
                if (!_roundStats[event.data.round]) {
                    _roundStats[event.data.round] = {
                        agent_a_msg_count: 0,
                        agent_b_msg_count: 0,
                        agent_a_char_count: 0,
                        agent_b_char_count: 0,
                        num_turns_used: 0,
                    };
                }
            }
            break;

        case 'cheap_talk':
            evDiv.innerHTML = `<span class="ev-type">[talk]</span> ${event.data.speaker}: ${(event.data.message || '').substring(0, 80)}...`;
            removeTypingIndicator(event.data.round, event.data.speaker);
            appendChatMsg(event.data.round, event.data.speaker, event.data.message);
            // Update stats for this round
            const stats = _roundStats[event.data.round];
            if (stats && event.data.speaker !== 'system') {
                const msgLen = (event.data.message || '').length;
                if (event.data.speaker === 'agent_a') {
                    stats.agent_a_msg_count++;
                    stats.agent_a_char_count += msgLen;
                } else if (event.data.speaker === 'agent_b') {
                    stats.agent_b_msg_count++;
                    stats.agent_b_char_count += msgLen;
                }
                if (event.data.turn != null) {
                    stats.num_turns_used = Math.max(stats.num_turns_used, event.data.turn + 1);
                }
            }
            break;

        case 'early_decision': {
            removeTypingIndicator(event.data.round, event.data.agent);
            evDiv.innerHTML = `<span class="ev-type">[early]</span> ${event.data.agent} locked in: ${JSON.stringify(event.data.allocation)}`;
            const round = event.data.round;
            if (!_earlyDecisions[round]) _earlyDecisions[round] = {};
            _earlyDecisions[round][event.data.agent] = true;
            break;
        }

        case 'round_complete': {
            evDiv.innerHTML = `<span class="ev-type">[result]</span> Round ${event.data.round_number}: ${event.data.overdrawn ? 'OVERDRAWN' : `A=${event.data.agent_a_reward}, B=${event.data.agent_b_reward}`}`;
            liveScoreA += event.data.agent_a_reward;
            liveScoreB += event.data.agent_b_reward;
            document.getElementById('liveScoreA').textContent = liveScoreA.toFixed(1);
            document.getElementById('liveScoreB').textContent = liveScoreB.toFixed(1);

            // Add round dots
            const roundNum = event.data.round_number;
            const overdrawn = event.data.overdrawn;
            const earlyA = _earlyDecisions[roundNum]?.agent_a || false;
            const earlyB = _earlyDecisions[roundNum]?.agent_b || false;

            addRoundDot('liveDotsA', roundNum, overdrawn, earlyA);
            addRoundDot('liveDotsB', roundNum, overdrawn, earlyB);

            finalizeRound(event.data);
            break;
        }

        case 'context_reset':
            evDiv.innerHTML = `<span class="ev-type">[shift]</span> ${event.data.agent} context reset for round ${event.data.round}`;
            break;

        case 'game_stopped':
            evDiv.innerHTML = `<span class="ev-type">[stopped]</span> Environment stopped by user after round ${event.data.after_round}`;
            document.getElementById('liveStatus').innerHTML = 'Stopped';
            document.getElementById('stopBtn').style.display = 'none';
            break;

        case 'game_complete':
            evDiv.innerHTML = `<span class="ev-type">[done]</span> Environment complete — A: ${Number(event.data.agent_a_cumulative_reward).toFixed(1)}, B: ${Number(event.data.agent_b_cumulative_reward).toFixed(1)}`;
            document.getElementById('liveStatus').innerHTML = event.data.stopped ? 'Stopped' : 'Environment Complete';
            document.getElementById('stopBtn').style.display = 'none';
            break;

        case 'waiting_for_human': {
            const bar = document.getElementById('humanInputBar');
            const prompt = document.getElementById('humanPrompt');
            bar.style.display = 'block';
            _currentHumanPhase = event.data.phase;
            _currentHumanAgent = event.data.agent;
            _currentHumanPublicConfig = event.data.public_config || _currentHumanPublicConfig;
            renderHumanProjectBrief(_currentHumanPublicConfig);
            if (event.data.phase === 'decision') {
                prompt.textContent = `Decision phase — select your purchase below`;
                showOrderPanel(true);
            } else {
                prompt.textContent = `Round ${event.data.round}, Turn ${(event.data.turn || 0) + 1} — type a message or make an early decision`;
                showChatMode();
            }
            evDiv.innerHTML = `<span class="ev-type">[human]</span> Waiting for your input...`;
            break;
        }

        case 'system_prompt': {
            evDiv.innerHTML = `<span class="ev-type">[prompt]</span> <strong>${event.data.agent}</strong>`;
            const pre = document.createElement('pre');
            pre.className = 'system-prompt-log';
            pre.textContent = event.data.prompt;
            evDiv.appendChild(pre);
            // Show human agent's system prompt as a collapsible panel in the live view
            if (isHumanRuntimeAgent(event.data.agent)) {
                showSystemPromptPanel(event.data.agent, event.data.prompt);
            }
            break;
        }

        case 'project_instructions': {
            evDiv.innerHTML = `<span class="ev-type">[projects]</span> <strong>${event.data.agent}</strong> round ${event.data.round}`;
            const projectPre = document.createElement('pre');
            projectPre.className = 'system-prompt-log';
            projectPre.textContent = event.data.prompt;
            evDiv.appendChild(projectPre);
            if (isHumanRuntimeAgent(event.data.agent)) {
                showProjectInstructionsPanel(event.data.agent, event.data.prompt);
            }
            break;
        }

        case 'llm_streaming': {
            evDiv.innerHTML = `<span class="ev-type">[streaming]</span> ${event.data.agent} generating (${event.data.phase})...`;
            showTypingIndicator(event.data.round, event.data.agent);
            break;
        }

        case 'thinking': {
            removeTypingIndicator(event.data.round, event.data.agent);
            evDiv.classList.add('thinking-event');
            const agentCls = event.data.agent === 'agent_a' ? 'thinking-a' : 'thinking-b';
            const label = event.data.agent === 'agent_a' ? 'A' : 'B';
            const metaStr = event.data.api_meta ? ` [${[
                event.data.api_meta.model,
                event.data.api_meta.duration_s != null ? event.data.api_meta.duration_s + 's' : null,
                event.data.api_meta.completion_tokens != null ? event.data.api_meta.completion_tokens + ' tok' : null,
            ].filter(Boolean).join(', ')}]` : '';
            evDiv.innerHTML = `<span class="ev-type">[think]</span> <strong>${label}</strong>${metaStr}`;
            const thinkingDetails = document.createElement('details');
            thinkingDetails.className = 'thinking-details';
            thinkingDetails.open = _thinkingTraceVisible;
            const thinkingSummary = document.createElement('summary');
            thinkingSummary.textContent = 'Thinking trace';
            const thinkPre = document.createElement('pre');
            thinkPre.className = `thinking-log ${agentCls}`;
            thinkPre.textContent = event.data.content;
            thinkingDetails.appendChild(thinkingSummary);
            thinkingDetails.appendChild(thinkPre);
            evDiv.appendChild(thinkingDetails);
            appendThinkingMsg(event.data.round, event.data.agent, event.data.content, event.data.api_meta);
            break;
        }

        case 'reasoning': {
            const agentCls = event.data.agent === 'agent_a' ? 'reasoning-a' : 'reasoning-b';
            const label = event.data.agent === 'agent_a' ? 'A' : 'B';
            evDiv.innerHTML = `<span class="ev-type">[reasoning]</span> <strong>${label}</strong>`;
            const details = document.createElement('details');
            const summary = document.createElement('summary');
            summary.textContent = 'View reasoning trace';
            summary.style.cursor = 'pointer';
            summary.style.fontWeight = 'bold';
            summary.style.marginBottom = '8px';
            const reasoningPre = document.createElement('pre');
            reasoningPre.className = `reasoning-log ${agentCls}`;
            reasoningPre.textContent = event.data.content;
            reasoningPre.style.marginTop = '4px';
            details.appendChild(summary);
            details.appendChild(reasoningPre);
            evDiv.appendChild(details);
            break;
        }

        case 'validation_error':
            evDiv.innerHTML = `<span class="ev-type">[error]</span> ${event.data.error}`;
            break;

        case 'api_failure': {
            const phase = event.data.phase === 'cheap_talk' ? 'chat' : 'decision';
            const turnInfo = event.data.turn >= 0 ? ` (turn ${event.data.turn})` : '';
            evDiv.innerHTML = `<span class="ev-type" style="color: #ff6b6b;">[API FAIL]</span> ` +
                `${event.data.agent} ${phase}${turnInfo}: ${event.data.error_type} ` +
                `(${event.data.retry_count} retries exhausted)`;
            evDiv.style.backgroundColor = '#ffe0e0';
            break;
        }

        case 'heuristic_fallback': {
            const phase = event.data.phase === 'cheap_talk' ? 'chat' : 'decision';
            const turnInfo = event.data.turn >= 0 ? ` (turn ${event.data.turn})` : '';
            evDiv.innerHTML = `<span class="ev-type" style="color: #ff8c00;">[FALLBACK]</span> ` +
                `${event.data.agent} ${phase}${turnInfo}: using heuristic agent`;
            evDiv.style.backgroundColor = '#fff3e0';
            break;
        }

        default:
            evDiv.innerHTML = `<span class="ev-type">[${event.type}]</span> ${JSON.stringify(event.data).substring(0, 100)}`;
    }

    logEl.appendChild(evDiv);
    logEl.scrollTop = logEl.scrollHeight;
}

function renderProjectRunsLive(projectRuns) {
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

function renderRoundStatsLive(stats, cheapTalkTurns) {
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
    const totalChars = stats.agent_a_char_count + stats.agent_b_char_count;
    const charTotal = formatChars(totalChars);
    parts.push(`Chars: A:${charA} B:${charB} (${charTotal})`);

    // Early submit indicator
    const earlySubmit = cheapTalkTurns > 0 && stats.num_turns_used < cheapTalkTurns;
    if (earlySubmit) {
        parts.push('<span class="early-indicator">⚡ Early</span>');
    }

    return `<span class="round-stats">${parts.join(' · ')}</span>`;
}

function ensureRoundBlock(roundNum) {
    const container = document.getElementById('liveRounds');
    let block = document.getElementById(`round-${roundNum}`);
    if (!block) {
        block = document.createElement('div');
        block.className = 'round-block open';
        block.id = `round-${roundNum}`;
        block.innerHTML = `
            <div class="round-header" onclick="this.parentElement.classList.toggle('open')">
                <div class="round-header-left">
                    <span class="round-label">Round ${roundNum}</span>
                    <span class="round-stats" id="round-stats-${roundNum}"></span>
                </div>
                <span class="round-status" id="round-status-${roundNum}">in progress</span>
            </div>
            <div class="round-body">
                <div class="chat-section" id="round-chat-${roundNum}">
                    <h4>Cheap Talk</h4>
                </div>
                <div class="allocations" id="round-alloc-${roundNum}" style="display:none"></div>
                <div class="supply-check" id="round-supply-${roundNum}" style="display:none"></div>
            </div>
        `;
        container.appendChild(block);
    }
}

function appendChatMsg(roundNum, speaker, message) {
    const chat = document.getElementById(`round-chat-${roundNum}`);
    if (!chat) return;
    const div = document.createElement('div');
    if (speaker === 'system') {
        div.className = 'chat-msg system';
        div.innerHTML = `<div class="bubble" style="color:var(--text-muted);font-style:italic;border-color:var(--border)">${escapeHtml(message)}</div>`;
    } else {
        const cls = speaker === 'agent_a' ? 'agent-a' : 'agent-b';
        const label = speaker === 'agent_a' ? 'A' : 'B';
        div.className = `chat-msg ${cls}`;
        div.innerHTML = `<div class="avatar">${label}</div><div class="bubble">${escapeHtml(message)}</div>`;
    }
    chat.appendChild(div);
}

function appendThinkingMsg(roundNum, speaker, content, apiMeta) {
    const chat = document.getElementById(`round-chat-${roundNum}`);
    if (!chat) return;
    const cls = speaker === 'agent_a' ? 'agent-a' : 'agent-b';
    const label = speaker === 'agent_a' ? 'A' : 'B';
    const metaBadge = renderApiMeta(apiMeta);
    const div = document.createElement('div');
    div.className = `chat-msg ${cls} thinking-msg`;
    div.innerHTML = `
        <div class="avatar">${label}</div>
        <div class="bubble thinking-bubble">
            <details class="thinking-details" ${_thinkingTraceVisible ? 'open' : ''}>
                <summary>Thinking trace${metaBadge}</summary>
                <div class="thinking-content">${escapeHtml(content)}</div>
            </details>
        </div>
    `;
    chat.appendChild(div);
}

function showTypingIndicator(roundNum, agent) {
    ensureRoundBlock(roundNum);
    removeTypingIndicator(roundNum, agent);
    const chat = document.getElementById(`round-chat-${roundNum}`);
    if (!chat) return;
    const cls = agent === 'agent_a' ? 'agent-a' : 'agent-b';
    const label = agent === 'agent_a' ? 'A' : 'B';
    const div = document.createElement('div');
    div.className = `chat-msg ${cls} typing-indicator-msg`;
    div.dataset.typingAgent = agent;
    div.innerHTML = `<div class="avatar">${label}</div><div class="bubble typing-bubble"><span class="typing-dots"><span></span><span></span><span></span></span></div>`;
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

function removeTypingIndicator(roundNum, agent) {
    const chat = document.getElementById(`round-chat-${roundNum}`);
    if (!chat) return;
    const indicators = chat.querySelectorAll(`.typing-indicator-msg[data-typing-agent="${agent}"]`);
    indicators.forEach(el => el.remove());
}

function finalizeRound(data) {
    const rn = data.round_number;
    const statusEl = document.getElementById(`round-status-${rn}`);
    if (statusEl) {
        if (data.overdrawn) {
            statusEl.className = 'round-status overdrawn';
            statusEl.textContent = 'overdrawn — both get 0';
        } else {
            statusEl.className = 'round-status success';
            statusEl.textContent = `A: +${data.agent_a_reward.toFixed(1)} · B: +${data.agent_b_reward.toFixed(1)}`;
        }
    }

    // Update round stats display
    const statsEl = document.getElementById(`round-stats-${rn}`);
    if (statsEl && _roundStats[rn]) {
        const cfg = currentGamePayload || {};
        const cheapTalkTurns = cfg.cheap_talk_turns || 0;
        const statsHtml = renderRoundStatsLive(_roundStats[rn], cheapTalkTurns);
        statsEl.outerHTML = statsHtml;
    }

    const allocDiv = document.getElementById(`round-alloc-${rn}`);
    if (allocDiv) {
        allocDiv.style.display = 'grid';
        allocDiv.innerHTML = `
            <div class="alloc-card agent-a">
                <h5>Agent A Purchased</h5>
                ${renderAlloc(data.agent_a_allocation)}
                ${renderProjectRunsLive(data.agent_a_project_runs)}
                <div class="reward-line">Reward: ${data.agent_a_reward.toFixed(1)}</div>
            </div>
            <div class="alloc-card agent-b">
                <h5>Agent B Purchased</h5>
                ${renderAlloc(data.agent_b_allocation)}
                ${renderProjectRunsLive(data.agent_b_project_runs)}
                <div class="reward-line">Reward: ${data.agent_b_reward.toFixed(1)}</div>
            </div>
        `;
    }

    const supDiv = document.getElementById(`round-supply-${rn}`);
    if (supDiv) {
        supDiv.style.display = 'flex';
        const resources = Object.keys(data.resource_supply);
        supDiv.innerHTML = resources.map(r => {
            const demanded = data.total_demanded[r] || 0;
            const supply = data.resource_supply[r];
            const pct = Math.min((demanded / supply) * 100, 100);
            const over = demanded > supply;
            return `<div class="supply-item">
                <span>${r}</span>
                <div class="supply-bar-bg"><div class="supply-bar ${over ? 'over' : 'ok'}" style="width:${pct}%"></div></div>
                <span>${demanded}/${supply}</span>
            </div>`;
        }).join('');
    }

    if (rn > 1) {
        const prev = document.getElementById(`round-${rn - 1}`);
        if (prev) prev.classList.remove('open');
    }
}

let _currentHumanPhase = null;

export function sendHumanInput() {
    const input = document.getElementById('humanInput');
    const text = input.value.trim();
    if (!text) return;
    _postHumanInput(text);
    input.value = '';
}

function _postHumanInput(text) {
    if (!currentGameId) return;
    fetch(`/api/environment/${currentGameId}/input`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent: _currentHumanAgent, text }),
    }).catch(e => console.error('Input POST failed:', e));
    document.getElementById('humanInputBar').style.display = 'none';
}

function showChatMode() {
    document.getElementById('humanChatSection').style.display = '';
    document.getElementById('humanOrderPanel').style.display = 'none';
    document.getElementById('orderToggleRow').style.display = '';
    document.getElementById('orderBackRow').style.display = 'none';
    document.getElementById('humanInput').focus();
}

function showOrderPanel(decisionPhase) {
    buildOrderSliders();
    document.getElementById('humanOrderPanel').style.display = '';
    document.getElementById('humanChatSection').style.display = decisionPhase ? 'none' : 'none';
    document.getElementById('orderToggleRow').style.display = 'none';
    document.getElementById('orderBackRow').style.display = decisionPhase ? 'none' : '';
}

window.toggleOrderMode = function(toOrder) {
    if (toOrder) {
        showOrderPanel(false);
    } else {
        showChatMode();
    }
};

function getGameConfig() {
    return _currentHumanPublicConfig || currentLiveConfig || currentGamePayload || {};
}

function getConfiguredAgents() {
    const payload = currentGamePayload || {};
    return payload.agents || [payload.agent_a, payload.agent_b].filter(Boolean);
}

function isHumanVsLlmGame() {
    const types = getConfiguredAgents().map(agent => agent?.type);
    return types.includes('human') && types.includes('llm');
}

function thinkingModeEnabled() {
    return Boolean(document.getElementById('thinkingMode')?.checked);
}

function updateThinkingTraceUi() {
    const liveView = document.getElementById('liveView');
    const notice = document.getElementById('thinkingNotice');
    if (!liveView || !notice) return;

    const showNotice = thinkingModeEnabled();
    notice.style.display = showNotice ? 'flex' : 'none';
    liveView.classList.toggle('thinking-hidden', !_thinkingTraceVisible);

    const humanVsLlm = isHumanVsLlmGame();
    if (humanVsLlm) {
        const label = _thinkingTraceVisible ? 'Collapse All' : 'Expand All';
        notice.innerHTML = `
            <span>LLM private reasoning is shown as collapsed trace sections. Expand one when you want to inspect it.</span>
            <button class="thinking-toggle-btn" type="button" onclick="window.toggleThinkingTrace()">${label}</button>
        `;
    } else {
        notice.innerHTML = `Dashed italic bubbles show each agent's private reasoning — this text is <strong>not</strong> visible to the other agent.`;
    }
}

window.toggleThinkingTrace = function() {
    _thinkingTraceVisible = !_thinkingTraceVisible;
    document.querySelectorAll('#liveView .thinking-details').forEach(details => {
        details.open = _thinkingTraceVisible;
    });
    updateThinkingTraceUi();
};

function getRuntimeAgentConfig(agent) {
    const payload = currentGamePayload || {};
    const agents = payload.agents || [payload.agent_a, payload.agent_b];
    const firstSpeaker = payload.first_speaker || 0;
    const runtimeIndex = agent === 'agent_a' ? firstSpeaker : 1 - firstSpeaker;
    return agents[runtimeIndex] || {};
}

function isHumanRuntimeAgent(agent) {
    return getRuntimeAgentConfig(agent)?.type === 'human';
}

function getOwnProjects(cfg) {
    if (cfg.own_projects) return cfg.own_projects;
    const projects = cfg.agent_projects || currentLiveConfig?.agent_projects || currentGamePayload?.agent_projects;
    if (!projects) return [];
    const idx = _currentHumanAgent === 'agent_b' ? 1 : 0;
    return projects[idx] || [];
}

function formatProjectRequirement(project) {
    return Object.entries(project.requirements || {})
        .map(([resource, quantity]) => `${escapeHtml(resource)} x${quantity}`)
        .join(', ');
}

function formatMoney(amount) {
    // Trim trailing zeros so 3.0 -> "3" and 1.5 -> "1.5".
    return Number.parseFloat(Number(amount || 0).toFixed(2)).toString();
}

function projectCost(project, costs) {
    return Object.entries(project.requirements || {})
        .reduce((sum, [resource, quantity]) => sum + quantity * (costs[resource] || 0), 0);
}

function maxRunsForProject(project, cfg) {
    const costs = cfg.resource_costs || {};
    const supply = cfg.resource_supply || {};
    const budget = cfg.agent_budget || 15;
    const requirements = Object.entries(project.requirements || {});
    if (requirements.length === 0) return 0;

    const bySupply = requirements.map(([resource, quantity]) => {
        if (quantity <= 0) return 0;
        return Math.floor((supply[resource] || 0) / quantity);
    });
    const cost = projectCost(project, costs);
    const byBudget = cost > 0 ? Math.floor(budget / cost) : 0;
    return Math.max(0, Math.min(20, byBudget, ...bySupply));
}

function getSelectedProjectRuns() {
    const runs = {};
    document.querySelectorAll('#projectRunControls .project-run-input').forEach(input => {
        const count = parseInt(input.value);
        if (count > 0) runs[input.dataset.projectName] = count;
    });
    return runs;
}

function getSelectedOrder() {
    const order = {};
    document.querySelectorAll('#orderSliders .order-slider').forEach(slider => {
        const qty = parseInt(slider.value);
        if (qty > 0) order[slider.dataset.resource] = qty;
    });
    return order;
}

function resourcesRequiredForRuns(projects, runs) {
    const required = {};
    projects.forEach(project => {
        const count = runs[project.name] || 0;
        if (count <= 0) return;
        Object.entries(project.requirements || {}).forEach(([resource, quantity]) => {
            required[resource] = (required[resource] || 0) + quantity * count;
        });
    });
    return required;
}

function syncOrderFromProjectRuns() {
    const cfg = getGameConfig();
    const projects = getOwnProjects(cfg);
    const required = resourcesRequiredForRuns(projects, getSelectedProjectRuns());
    document.querySelectorAll('#orderSliders .order-slider').forEach(slider => {
        const value = required[slider.dataset.resource] || 0;
        slider.value = Math.min(parseInt(slider.max), value);
        document.getElementById(`orderQty_${slider.dataset.resource}`).textContent = slider.value;
    });
    updateOrderValidation();
}

function renderHumanProjectBrief(cfg) {
    const el = document.getElementById('humanProjectBrief');
    if (!el || !cfg) return;
    const projects = getOwnProjects(cfg);
    if (!projects.length) {
        el.style.display = 'none';
        return;
    }
    const synergy = cfg.scenario_synergy
        ? `<div class="human-synergy">Synergy: both buy ${escapeHtml(cfg.scenario_synergy.resource)}, combined >= ${cfg.scenario_synergy.threshold}, bonus +${cfg.scenario_synergy.bonus}</div>`
        : '';
    const costs = cfg.resource_costs || {};
    const budget = cfg.agent_budget || 15;
    el.style.display = '';
    el.innerHTML = `
        <div class="human-project-brief-header">
            <div class="human-project-brief-title">Your projects this round</div>
            <div class="human-project-brief-budget" title="Cash available to buy resources this round">
                Budget <strong>$${formatMoney(budget)}</strong>
            </div>
        </div>
        <div class="human-project-brief-grid">
            ${projects.map(project => `
                <div class="human-project-pill">
                    <strong>${escapeHtml(project.name)}</strong>
                    <span>${formatProjectRequirement(project)}</span>
                    <span class="human-project-cost">$${formatMoney(projectCost(project, costs))} to run once</span>
                    <em>${project.reward}/run</em>
                </div>
            `).join('')}
        </div>
        ${synergy}
    `;
}

function buildOrderSliders() {
    const cfg = getGameConfig();
    const resources = cfg.resource_types || Object.keys(cfg.resource_costs || {});
    const costs = cfg.resource_costs || {};
    const supply = cfg.resource_supply || {};
    const budget = cfg.agent_budget || 15;
    const maxTypes = cfg.max_resource_types_per_turn || 2;
    const projects = getOwnProjects(cfg);

    const projectContainer = document.getElementById('projectRunControls');
    if (projects.length > 0) {
        projectContainer.innerHTML = `
            <div class="project-run-title">Project runs</div>
            ${projects.map((project, idx) => {
                const maxRuns = maxRunsForProject(project, cfg);
                const req = formatProjectRequirement(project);
                return `<div class="project-run-row" data-project-index="${idx}">
                    <div class="project-run-info">
                        <strong>${escapeHtml(project.name)}</strong>
                        <span>${req} -> ${project.reward}/run</span>
                    </div>
                    <input type="range" min="0" max="${maxRuns}" value="0" class="project-run-slider" data-project-name="${escapeHtml(project.name)}" data-project-index="${idx}" oninput="window._updateProjectRun(this)" />
                    <input type="number" min="0" max="${maxRuns}" value="0" class="project-run-input" data-project-name="${escapeHtml(project.name)}" data-project-index="${idx}" oninput="window._updateProjectRun(this)" />
                </div>`;
            }).join('')}
        `;
    } else {
        projectContainer.innerHTML = '';
    }

    const container = document.getElementById('orderSliders');
    container.innerHTML = resources.map(r => {
        const cost = costs[r] || 1;
        const maxByBudget = Math.floor(budget / cost);
        const maxQty = Math.min(maxByBudget, supply[r] || 10);
        return `<div class="order-slider-row" data-resource="${r}">
            <label class="order-resource-label">${r} <span class="order-cost-hint">$${cost}/ea</span></label>
            <input type="range" min="0" max="${maxQty}" value="0" class="order-slider" data-resource="${r}" oninput="window._updateOrderSlider(this)" />
            <span class="order-qty" id="orderQty_${r}">0</span>
        </div>`;
    }).join('');

    const summary = document.getElementById('orderSummary');
    summary.innerHTML = `
        <span class="order-cost-line">Cost: $<span id="orderCostValue">0.0</span> / $${budget}</span>
        <span class="order-types-line">Types: <span id="orderTypesValue">0</span> / ${maxTypes}</span>
    `;

    updateOrderValidation();
}

window._updateProjectRun = function(el) {
    const row = el.closest('.project-run-row');
    if (!row) return;
    const slider = row.querySelector('.project-run-slider');
    const input = row.querySelector('.project-run-input');
    const max = parseInt(input.max);
    const value = Math.max(0, Math.min(max, parseInt(el.value) || 0));
    slider.value = value;
    input.value = value;
    syncOrderFromProjectRuns();
};

window._updateOrderSlider = function(el) {
    const r = el.dataset.resource;
    document.getElementById(`orderQty_${r}`).textContent = el.value;
    updateOrderValidation();
};

function updateOrderValidation() {
    const cfg = getGameConfig();
    const costs = cfg.resource_costs || {};
    const budget = cfg.agent_budget || 15;
    const maxTypes = cfg.max_resource_types_per_turn || 2;
    const projects = getOwnProjects(cfg);
    const order = getSelectedOrder();
    const projectRuns = getSelectedProjectRuns();
    const required = resourcesRequiredForRuns(projects, projectRuns);

    const sliders = document.querySelectorAll('#orderSliders .order-slider');
    let totalCost = 0;
    let numTypes = 0;
    sliders.forEach(s => {
        const qty = parseInt(s.value);
        if (qty > 0) {
            totalCost += qty * (costs[s.dataset.resource] || 1);
            numTypes++;
        }
    });

    document.getElementById('orderCostValue').textContent = totalCost.toFixed(1);
    document.getElementById('orderTypesValue').textContent = numTypes;

    const errors = [];
    if (totalCost > budget + 0.001) errors.push(`Over budget by $${(totalCost - budget).toFixed(1)}`);
    if (numTypes > maxTypes) errors.push(`Max ${maxTypes} resource type${maxTypes > 1 ? 's' : ''} allowed`);
    Object.entries(required).forEach(([resource, quantity]) => {
        if ((order[resource] || 0) < quantity) {
            errors.push(`${resource} needs ${quantity} for selected project runs`);
        }
    });

    const errEl = document.getElementById('orderError');
    const submitBtn = document.getElementById('orderSubmitBtn');
    if (errors.length > 0) {
        errEl.textContent = errors.join(' · ');
        errEl.style.display = '';
        submitBtn.disabled = true;
    } else {
        errEl.style.display = 'none';
        submitBtn.disabled = false;
    }

    // Color the cost/types indicators
    const costEl = document.getElementById('orderCostValue');
    costEl.className = totalCost > budget + 0.001 ? 'over' : '';
    const typesEl = document.getElementById('orderTypesValue');
    typesEl.className = numTypes > maxTypes ? 'over' : '';
}

window.submitOrder = function() {
    const order = getSelectedOrder();
    const projectRuns = getSelectedProjectRuns();
    if (Object.keys(projectRuns).length > 0) {
        order.projects = projectRuns;
    }

    _postHumanInput(JSON.stringify(order));
};

function addRoundDot(containerId, roundNum, overdrawn, madeEarlyDecision) {
    const container = document.getElementById(containerId);
    const dot = document.createElement('div');
    dot.className = 'round-dot';
    dot.title = `Round ${roundNum}`;

    if (overdrawn && madeEarlyDecision) {
        dot.classList.add('early-overdrawn');
        dot.title += ' (Early decision → Overdrawn)';
    } else if (overdrawn) {
        dot.classList.add('overdrawn');
        dot.title += ' (Overdrawn)';
    } else if (madeEarlyDecision) {
        dot.classList.add('early-success');
        dot.title += ' (Early decision)';
    } else {
        dot.classList.add('normal');
    }

    container.appendChild(dot);
}

function showSystemPromptPanel(agent, prompt) {
    const container = document.getElementById('humanSystemPrompt');
    const label = agent === 'agent_a' ? 'A' : 'B';
    container.style.display = '';
    container.innerHTML = `
        <details class="system-prompt-panel" open>
            <summary class="system-prompt-summary">
                <svg class="summary-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
                Your Instructions (Agent ${label})
            </summary>
            <pre class="system-prompt-content">${escapeHtml(prompt)}</pre>
        </details>
    `;
}

function showProjectInstructionsPanel(agent, prompt) {
    const container = document.getElementById('humanSystemPrompt');
    const label = agent === 'agent_a' ? 'A' : 'B';
    container.style.display = '';
    container.innerHTML = `
        <details class="system-prompt-panel" open>
            <summary class="system-prompt-summary">
                <svg class="summary-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
                New Project Instructions (Agent ${label})
            </summary>
            <pre class="system-prompt-content">${escapeHtml(prompt)}</pre>
        </details>
    `;
}

export function exportGameLog() {
    if (!currentGameId || _allLiveEvents.length === 0) return;
    const lines = [
        JSON.stringify({ type: 'game_config', data: { episode_uid: currentGameId, config: currentGamePayload } }),
        ..._allLiveEvents.map(e => JSON.stringify(e)),
    ];
    const blob = new Blob([lines.join('\n') + '\n'], { type: 'application/x-ndjson' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `game_${currentGameId}.jsonl`;
    a.click();
    URL.revokeObjectURL(url);
}

export function getCurrentGameId() {
    return currentGameId;
}
