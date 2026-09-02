// --- History tab ---

import { renderRoundBlock, renderGameMetadata, renderRoundDots } from './utils.js';
import { idbGetAllGames, idbGetGame } from './storage.js';
import { exportGameAsJSONL } from './export.js';
import { openLiveView } from './live.js';

export async function loadHistory() {
    try {
        const localGames = await idbGetAllGames();

        let inProgress = [];
        try {
            const resp = await fetch('/api/games');
            const serverGames = await resp.json();
            inProgress = serverGames.filter(g => g.mode === 'in_progress');
        } catch (e) {
            console.error('Failed to fetch server games:', e);
        }

        const list = document.getElementById('historyList');
        const empty = document.getElementById('historyEmpty');

        const allItems = [];

        for (const g of inProgress) {
            allItems.push(`
                <div class="game-list-item" data-action="loadGameDetail" data-game-id="${g.game_id}">
                    <div>
                        <span class="gid">${g.game_id}</span>
                        <span class="meta" style="margin-left:12px">in progress</span>
                    </div>
                    <div class="meta"><span class="spinner"></span> Running</div>
                </div>
            `);
        }

        for (const g of localGames.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))) {
            const r = g.result || {};
            allItems.push(`
                <div class="game-list-item" data-action="loadLocalGameDetail" data-game-id="${g.game_id}">
                    <div>
                        <span class="gid">${g.game_id}</span>
                        <span class="meta" style="margin-left:12px">${r.mode || '?'} · ${r.num_rounds || '?'} rounds</span>
                    </div>
                    <div class="meta">A: ${Number(r.agent_a_cumulative_reward || 0).toFixed(1)} · B: ${Number(r.agent_b_cumulative_reward || 0).toFixed(1)}</div>
                </div>
            `);
        }

        if (allItems.length === 0) {
            empty.style.display = 'block';
            list.innerHTML = '';
            return;
        }
        empty.style.display = 'none';
        list.innerHTML = allItems.join('');

        // Attach click handlers
        list.querySelectorAll('[data-action="loadGameDetail"]').forEach(el => {
            el.addEventListener('click', () => loadGameDetail(el.dataset.gameId));
        });
        list.querySelectorAll('[data-action="loadLocalGameDetail"]').forEach(el => {
            el.addEventListener('click', () => loadLocalGameDetail(el.dataset.gameId));
        });
    } catch (e) {
        console.error(e);
    }
}

export async function loadLocalGameDetail(gameId) {
    try {
        const gameData = await idbGetGame(gameId);
        if (!gameData) return;
        const data = gameData.result;
        const config = gameData.game_config || {};
        const cheapTalkTurns = config.cheap_talk_turns || 0;

        const detail = document.getElementById('historyDetail');
        detail.style.display = 'block';

        let html = `
            <div class="game-header">
                <h2>Game ${data.game_id}</h2>
                <span class="mode-badge">${data.mode}</span>
            </div>
            <div class="scoreboard">
                <div class="score-card agent-a">
                    <div class="label">Agent A — Total</div>
                    <div class="value">${data.agent_a_cumulative_reward.toFixed(1)}</div>
                    <div class="round-dots">${renderRoundDots(data.rounds, 'agent_a', cheapTalkTurns)}</div>
                </div>
                <div class="score-card agent-b">
                    <div class="label">Agent B — Total</div>
                    <div class="value">${data.agent_b_cumulative_reward.toFixed(1)}</div>
                    <div class="round-dots">${renderRoundDots(data.rounds, 'agent_b', cheapTalkTurns)}</div>
                </div>
            </div>
            ${renderGameMetadata(gameData.game_config, data, { showDownload: true, downloadId: gameId })}
        `;

        for (const round of data.rounds) {
            html += renderRoundBlock(round);
        }

        detail.innerHTML = html;

        // Attach handlers
        detail.querySelector('[data-action="downloadTrace"]')?.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            exportLocalGame(gameId);
        });
    } catch (e) {
        console.error(e);
    }
}

export async function exportLocalGame(gameId) {
    const gameData = await idbGetGame(gameId);
    if (gameData) exportGameAsJSONL(gameData);
}

export async function loadGameDetail(gameId) {
    try {
        const resp = await fetch(`/api/game/${gameId}`);
        const data = await resp.json();

        if (data.status === 'in_progress') {
            openLiveView(gameId, '');
            return;
        }

        const localData = await idbGetGame(gameId);
        if (localData) {
            loadLocalGameDetail(gameId);
            return;
        }

        const detail = document.getElementById('historyDetail');
        detail.style.display = 'block';

        // Note: config not available for in-memory games, default to 0
        const cheapTalkTurns = 0;

        let html = `
            <div class="game-header">
                <h2>Game ${data.game_id}</h2>
                <span class="mode-badge">${data.mode}</span>
            </div>
            <div class="scoreboard">
                <div class="score-card agent-a">
                    <div class="label">Agent A — Total</div>
                    <div class="value">${data.agent_a_cumulative_reward.toFixed(1)}</div>
                    <div class="round-dots">${renderRoundDots(data.rounds, 'agent_a', cheapTalkTurns)}</div>
                </div>
                <div class="score-card agent-b">
                    <div class="label">Agent B — Total</div>
                    <div class="value">${data.agent_b_cumulative_reward.toFixed(1)}</div>
                    <div class="round-dots">${renderRoundDots(data.rounds, 'agent_b', cheapTalkTurns)}</div>
                </div>
            </div>
            ${renderGameMetadata(null, data)}
        `;

        for (const round of data.rounds) {
            html += renderRoundBlock(round);
        }

        detail.innerHTML = html;
    } catch (e) {
        console.error(e);
    }
}
