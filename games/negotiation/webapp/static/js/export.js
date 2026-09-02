// --- Export/Import ---

import { idbSaveGame } from './storage.js';

export function exportGameAsJSONL(gameData) {
    const lines = [];
    lines.push(JSON.stringify({ type: 'game_config', data: gameData.game_config }));
    for (const ev of (gameData.events || [])) {
        lines.push(JSON.stringify(ev));
    }
    lines.push(JSON.stringify({ type: 'result', data: gameData.result }));
    const blob = new Blob([lines.join('\n') + '\n'], { type: 'application/jsonl' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `game_${gameData.game_id}.jsonl`;
    a.click();
    URL.revokeObjectURL(url);
}

export async function handleJSONLImport(event) {
    const files = event.target.files;
    if (!files.length) return;
    let imported = 0;
    for (const file of files) {
        try {
            const text = await file.text();
            const lines = text.trim().split('\n').filter(l => l.trim());
            const parsed = lines.map(l => JSON.parse(l));

            let game_config = {};
            let result = null;
            const events = [];

            for (const line of parsed) {
                if (line.type === 'game_config') {
                    game_config = line.data || {};
                } else if (line.type === 'result') {
                    result = line.data || {};
                } else {
                    events.push(line);
                }
            }

            if (!result) {
                console.warn(`Skipping ${file.name}: no result line found`);
                continue;
            }

            const game_id = result.game_id || file.name.replace(/\.jsonl$/, '').replace(/^game_/, '');
            const gameData = {
                game_id,
                game_config,
                result,
                events,
                created_at: new Date().toISOString(),
            };
            await idbSaveGame(gameData);
            imported++;
        } catch (e) {
            console.error(`Failed to import ${file.name}:`, e);
        }
    }
    event.target.value = '';
    return imported;
}
