// --- Cell launch ---

import { buildGameConfig } from './config.js';

let _cellPollTimer = null;

export async function launchCell() {
    const btn = document.getElementById('cellLaunchBtn');
    btn.disabled = true;
    btn.textContent = 'Starting...';

    const count = parseInt(document.getElementById('cellCount').value);
    const seedInput = document.getElementById('cellSeed').value;
    const balance = document.getElementById('cellBalance').checked;
    const experimentLabel = document.getElementById('cellLabel').value.trim();

    const payload = {
        count,
        balance_turn_order: balance,
        config: buildGameConfig(),
    };
    if (seedInput) payload.seed = parseInt(seedInput);
    if (experimentLabel) payload.experiment_label = experimentLabel;

    try {
        const resp = await fetch('/api/cell/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.error) {
            alert('Cell error: ' + data.error);
            btn.disabled = false;
            btn.textContent = 'Launch Cell';
            return;
        }
        startCellPolling(data.episode_uids, data.swapped);
    } catch (e) {
        alert('Failed to start cell: ' + e.message);
    }

    btn.disabled = false;
    btn.textContent = 'Launch Cell';
}

function startCellPolling(gameIds, swappedFlags) {
    const progress = document.getElementById('cellProgress');
    progress.style.display = 'block';

    const listEl = document.getElementById('cellGameList');
    listEl.innerHTML = gameIds.map((gid, i) =>
        `<div class="cell-environment-row" id="cell-row-${gid}">
            <span class="cell-environment-id">${gid.substring(0, 8)}</span>
            <span class="cell-environment-swap">${swappedFlags[i] ? 'swapped' : ''}</span>
            <span class="cell-environment-status" id="cell-status-${gid}"><span class="spinner"></span></span>
            <span class="cell-environment-rewards" id="cell-rewards-${gid}"></span>
        </div>`
    ).join('');

    updateCellProgress(0, gameIds.length);

    if (_cellPollTimer) clearInterval(_cellPollTimer);
    _cellPollTimer = setInterval(() => pollCellStatus(gameIds), 2000);
    pollCellStatus(gameIds);
}

async function pollCellStatus(gameIds) {
    try {
        const resp = await fetch(`/api/cell/status?ids=${gameIds.join(',')}`);
        const data = await resp.json();

        let doneCount = 0;
        for (const g of data.games) {
            const statusEl = document.getElementById(`cell-status-${g.episode_uid}`);
            const rewardsEl = document.getElementById(`cell-rewards-${g.episode_uid}`);
            if (g.done) {
                doneCount++;
                if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">done</span>';
                if (rewardsEl) rewardsEl.textContent = `A: ${Number(g.agent_a_reward).toFixed(1)} · B: ${Number(g.agent_b_reward).toFixed(1)}`;
            }
        }

        updateCellProgress(doneCount, gameIds.length);

        if (data.all_done && _cellPollTimer) {
            clearInterval(_cellPollTimer);
            _cellPollTimer = null;
        }
    } catch (e) {
        console.error('Cell poll failed:', e);
    }
}

function updateCellProgress(done, total) {
    document.getElementById('cellProgressText').textContent = `${done} / ${total} complete`;
    const pct = total > 0 ? (done / total) * 100 : 0;
    document.getElementById('cellProgressFill').style.width = `${pct}%`;
}
