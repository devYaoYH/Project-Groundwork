// --- Batch launch ---

import { buildGameConfig } from './config.js';

let _batchPollTimer = null;

export async function launchBatch() {
    const btn = document.getElementById('batchLaunchBtn');
    btn.disabled = true;
    btn.textContent = 'Starting...';

    const count = parseInt(document.getElementById('batchCount').value);
    const seedInput = document.getElementById('batchSeed').value;
    const balance = document.getElementById('batchBalance').checked;
    const experimentLabel = document.getElementById('batchLabel').value.trim();

    const payload = {
        count,
        balance_turn_order: balance,
        config: buildGameConfig(),
    };
    if (seedInput) payload.seed = parseInt(seedInput);
    if (experimentLabel) payload.experiment_label = experimentLabel;

    try {
        const resp = await fetch('/api/batch/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.error) {
            alert('Batch error: ' + data.error);
            btn.disabled = false;
            btn.textContent = 'Launch Batch';
            return;
        }
        startBatchPolling(data.game_ids, data.swapped);
    } catch (e) {
        alert('Failed to start batch: ' + e.message);
    }

    btn.disabled = false;
    btn.textContent = 'Launch Batch';
}

function startBatchPolling(gameIds, swappedFlags) {
    const progress = document.getElementById('batchProgress');
    progress.style.display = 'block';

    const listEl = document.getElementById('batchGameList');
    listEl.innerHTML = gameIds.map((gid, i) =>
        `<div class="batch-game-row" id="batch-row-${gid}">
            <span class="batch-game-id">${gid.substring(0, 8)}</span>
            <span class="batch-game-swap">${swappedFlags[i] ? 'swapped' : ''}</span>
            <span class="batch-game-status" id="batch-status-${gid}"><span class="spinner"></span></span>
            <span class="batch-game-rewards" id="batch-rewards-${gid}"></span>
        </div>`
    ).join('');

    updateBatchProgress(0, gameIds.length);

    if (_batchPollTimer) clearInterval(_batchPollTimer);
    _batchPollTimer = setInterval(() => pollBatchStatus(gameIds), 2000);
    pollBatchStatus(gameIds);
}

async function pollBatchStatus(gameIds) {
    try {
        const resp = await fetch(`/api/batch/status?ids=${gameIds.join(',')}`);
        const data = await resp.json();

        let doneCount = 0;
        for (const g of data.games) {
            const statusEl = document.getElementById(`batch-status-${g.game_id}`);
            const rewardsEl = document.getElementById(`batch-rewards-${g.game_id}`);
            if (g.done) {
                doneCount++;
                if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">done</span>';
                if (rewardsEl) rewardsEl.textContent = `A: ${Number(g.agent_a_reward).toFixed(1)} · B: ${Number(g.agent_b_reward).toFixed(1)}`;
            }
        }

        updateBatchProgress(doneCount, gameIds.length);

        if (data.all_done && _batchPollTimer) {
            clearInterval(_batchPollTimer);
            _batchPollTimer = null;
        }
    } catch (e) {
        console.error('Batch poll failed:', e);
    }
}

function updateBatchProgress(done, total) {
    document.getElementById('batchProgressText').textContent = `${done} / ${total} complete`;
    const pct = total > 0 ? (done / total) * 100 : 0;
    document.getElementById('batchProgressFill').style.width = `${pct}%`;
}
