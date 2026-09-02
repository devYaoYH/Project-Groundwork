// Client-side SA scenario generator — extracted from optimizer.html
// All functions are parameterized (no global DOM state).

function cpr(p, costs) {
    return Object.keys(p.req).reduce((s, r) => s + (costs[r] || 0) * p.req[r], 0);
}

function maxRuns(p, cash, costs, supply) {
    let byRes = Infinity;
    for (const r of Object.keys(p.req)) {
        byRes = Math.min(byRes, Math.floor((supply[r] || 0) / p.req[r]));
    }
    const cp = cpr(p, costs);
    return Math.min(isFinite(byRes) ? byRes : 0, cp > 0 ? Math.floor(cash / cp) : 9999);
}

function checkSynergy(syn, myTotal, otherTotal) {
    if (!syn) return false;
    return myTotal >= 1 && otherTotal >= 1 && myTotal + otherTotal >= syn.threshold;
}

function countResTypes(projs, runs) {
    const types = new Set();
    projs.forEach((p, i) => { if (runs[i] > 0) for (const r of Object.keys(p.req)) if (p.req[r] > 0) types.add(r); });
    return types.size;
}

function fitsSupply(projs, runs, supply) {
    const used = {};
    for (let i = 0; i < projs.length; i++) {
        if (runs[i] <= 0) continue;
        for (const r of Object.keys(projs[i].req)) {
            used[r] = (used[r] || 0) + runs[i] * projs[i].req[r];
            if (used[r] > (supply[r] || 0)) return false;
        }
    }
    return true;
}

function hasDuplicateProjects(projs) {
    // Check all projects against each other (catches within-player and cross-player duplicates)
    for (let i = 0; i < projs.length; i++) {
        for (let j = i + 1; j < projs.length; j++) {
            const a = projs[i], b = projs[j];
            if (a.reward !== b.reward) continue;
            const ak = Object.keys(a.req).sort(), bk = Object.keys(b.req).sort();
            if (ak.length !== bk.length) continue;
            if (ak.every((k, idx) => k === bk[idx] && a.req[k] === b.req[k])) return true;
        }
    }
    return false;
}

function playerCoversAllTypes(projs, assign, zone, resourceTypes) {
    const used = new Set();
    for (const p of projs) {
        if (assign[p.id] === zone) {
            for (const r of Object.keys(p.req)) if (p.req[r] > 0) used.add(r);
        }
    }
    return resourceTypes.every(r => used.has(r));
}

function actionSpaceSize(projs, cash, costs, supply, maxTypes = 99) {
    if (projs.length === 0) return 0;
    // General recursive enumeration for N projects
    const caps = projs.map(p => Math.min(maxRuns(p, cash, costs, supply), 30));
    const cprs = projs.map(p => cpr(p, costs));
    let count = 0;
    const runs = new Array(projs.length).fill(0);

    function enumerate(idx, cashLeft) {
        if (idx === projs.length) {
            if (countResTypes(projs, runs) > maxTypes) return;
            if (!fitsSupply(projs, runs, supply)) return;
            count++;
            return;
        }
        const maxN = Math.min(caps[idx], cprs[idx] > 0 ? Math.floor(cashLeft / cprs[idx]) : caps[idx]);
        for (let n = 0; n <= maxN; n++) {
            runs[idx] = n;
            enumerate(idx + 1, cashLeft - n * cprs[idx]);
        }
        runs[idx] = 0;
    }
    enumerate(0, cash);
    return count;
}

function maxForPlayer(projs, cash, costs, supply, maxTypes = 99) {
    if (!projs.length) return { reward: 0, baseReward: 0, synergyBonus: 0, detail: '---', spent: 0, resUsed: {} };
    let best = { reward: 0, baseReward: 0, synergyBonus: 0, detail: '---', spent: 0, resUsed: {} };
    // General recursive enumeration for N projects
    const caps = projs.map(p => Math.min(maxRuns(p, cash, costs, supply), 30));
    const cprs = projs.map(p => cpr(p, costs));
    const runs = new Array(projs.length).fill(0);

    function enumerate(idx, cashLeft) {
        if (idx === projs.length) {
            if (countResTypes(projs, runs) > maxTypes) return;
            if (!fitsSupply(projs, runs, supply)) return;
            let tot = 0, sp = 0;
            for (let i = 0; i < projs.length; i++) {
                tot += runs[i] * projs[i].reward;
                sp += runs[i] * cprs[i];
            }
            if (tot > best.reward) {
                const detail = projs.map((p, i) => runs[i] > 0 ? p.name + ' x' + runs[i] : null).filter(Boolean).join(', ') || '---';
                const ru = {};
                for (let i = 0; i < projs.length; i++) {
                    if (runs[i] > 0) for (const r of Object.keys(projs[i].req)) if (projs[i].req[r] > 0) ru[r] = (ru[r] || 0) + runs[i] * projs[i].req[r];
                }
                best = { reward: tot, baseReward: tot, synergyBonus: 0, detail, spent: sp, resUsed: ru };
            }
            return;
        }
        const maxN = Math.min(caps[idx], cprs[idx] > 0 ? Math.floor(cashLeft / cprs[idx]) : caps[idx]);
        for (let n = 0; n <= maxN; n++) {
            runs[idx] = n;
            enumerate(idx + 1, cashLeft - n * cprs[idx]);
        }
        runs[idx] = 0;
    }
    enumerate(0, cash);
    return best;
}

function collabMax(pp1, pp2, cash1, cash2, costs, supply, synergy, maxTypes = 99) {
    const rkeys = Object.keys(supply);
    const empty = { total: 0, detail: '---', resUsed: {}, baseScore: 0, baseScore1: 0, baseScore2: 0, synergyBonus: 0, collabDetail: '---' };
    if (!pp1.length && !pp2.length) return { ...empty, bestP1Favor: 0, bestP2Favor: 0, optimalCount: 0 };
    const best = { ...empty, resUsed: {} };
    let bestP1Favor = 0;
    let bestP2Favor = 0;
    let optimalCount = 0;

    const synRes = synergy ? synergy.res : null;
    const synThresh = synergy ? synergy.threshold : 0;
    const synBonus = synergy ? synergy.bonus : 0;
    const caps1 = pp1.map(p => Math.min(maxRuns(p, cash1, costs, supply), 10));
    const caps2 = pp2.map(p => Math.min(maxRuns(p, cash2, costs, supply), 10));
    const cprs1 = pp1.map(p => cpr(p, costs));
    const cprs2 = pp2.map(p => cpr(p, costs));
    const discCap = synRes ? Math.min(synThresh, supply[synRes] || 0) : 0;
    const synCost = synRes ? (costs[synRes] || 0) : 0;
    const r1arr = new Array(pp1.length).fill(0);
    const r2arr = new Array(pp2.length).fill(0);

    function evalAlloc(d1, d2) {
        const used = {};
        rkeys.forEach(r => { used[r] = 0; });
        let c1 = 0, c2 = 0;
        r1arr.forEach((n, i) => { const p = pp1[i]; for (const r of Object.keys(p.req)) { used[r] += n * p.req[r]; } c1 += n * cprs1[i]; });
        r2arr.forEach((n, i) => { const p = pp2[i]; for (const r of Object.keys(p.req)) { used[r] += n * p.req[r]; } c2 += n * cprs2[i]; });
        if (synRes) { used[synRes] = (used[synRes] || 0) + d1 + d2; c1 += d1 * synCost; c2 += d2 * synCost; }
        if (!rkeys.every(r => (used[r] || 0) <= (supply[r] || 0)) || c1 > cash1 || c2 > cash2) return;

        const pu1 = {}, pu2 = {};
        r1arr.forEach((n, i) => { const p = pp1[i]; for (const r of Object.keys(p.req)) { pu1[r] = (pu1[r] || 0) + n * p.req[r]; } });
        r2arr.forEach((n, i) => { const p = pp2[i]; for (const r of Object.keys(p.req)) { pu2[r] = (pu2[r] || 0) + n * p.req[r]; } });
        if (synRes) { pu1[synRes] = (pu1[synRes] || 0) + d1; pu2[synRes] = (pu2[synRes] || 0) + d2; }

        const types1 = new Set(Object.keys(pu1).filter(r => (pu1[r] || 0) > 0));
        const types2 = new Set(Object.keys(pu2).filter(r => (pu2[r] || 0) > 0));
        if (types1.size > maxTypes || types2.size > maxTypes) return;

        let base1 = 0, base2 = 0, synBon = 0;
        r1arr.forEach((n, i) => { base1 += n * pp1[i].reward; });
        r2arr.forEach((n, i) => { base2 += n * pp2[i].reward; });
        if (synRes) {
            const myT1 = pu1[synRes] || 0, myT2 = pu2[synRes] || 0;
            if (checkSynergy(synergy, myT1, myT2)) {
                synBon += synBonus * 2;
            }
        }
        const tot = base1 + base2 + synBon;
        if (base1 >= base2 && tot > bestP1Favor) bestP1Favor = tot;
        if (base2 >= base1 && tot > bestP2Favor) bestP2Favor = tot;
        if (tot > best.total) {
            optimalCount = 1;
            const parts = r1arr.map((n, i) => pp1[i].name + 'x' + n)
                .concat(r2arr.map((n, i) => pp2[i].name + 'x' + n))
                .concat(d1 > 0 && synRes ? ['disc:' + synRes + 'x' + d1 + '(P1)'] : [])
                .concat(d2 > 0 && synRes ? ['disc:' + synRes + 'x' + d2 + '(P2)'] : [])
                .filter(s => s.indexOf('x0') === -1);
            best.total = tot;
            best.detail = parts.join(', ') || '---';
            best.resUsed = { ...used };
            best.resUsed1 = { ...pu1 };
            best.resUsed2 = { ...pu2 };
            best.baseScore = base1 + base2;
            best.baseScore1 = base1;
            best.baseScore2 = base2;
            best.synergyBonus = synBon;
            best.collabDetail = parts.join(', ') || '---';
        } else if (tot === best.total && tot > 0) {
            optimalCount++;
        }
    }

    function loop2(i2, cashLeft2) {
        if (i2 === pp2.length) {
            for (let d1 = 0; d1 <= discCap; d1++) for (let d2 = 0; d2 <= discCap; d2++) evalAlloc(d1, d2);
            return;
        }
        const maxN = Math.min(caps2[i2], cashLeft2 > 0 ? Math.floor(cashLeft2 / cprs2[i2]) : 0);
        for (let n = 0; n <= maxN; n++) { r2arr[i2] = n; loop2(i2 + 1, cashLeft2 - n * cprs2[i2]); }
        r2arr[i2] = 0;
    }
    function loop1(i1, cashLeft1) {
        if (i1 === pp1.length) { loop2(0, cash2); return; }
        const maxN = Math.min(caps1[i1], cashLeft1 > 0 ? Math.floor(cashLeft1 / cprs1[i1]) : 0);
        for (let n = 0; n <= maxN; n++) { r1arr[i1] = n; loop1(i1 + 1, cashLeft1 - n * cprs1[i1]); }
        r1arr[i1] = 0;
    }
    loop1(0, cash1);
    best.bestP1Favor = bestP1Favor;
    best.bestP2Favor = bestP2Favor;
    best.optimalCount = optimalCount;
    return best;
}

function resourceOverlap(ru1, ru2) {
    // Jaccard overlap of resource types used (by quantity > 0)
    const s1 = new Set(Object.keys(ru1).filter(r => (ru1[r] || 0) > 0));
    const s2 = new Set(Object.keys(ru2).filter(r => (ru2[r] || 0) > 0));
    if (s1.size === 0 && s2.size === 0) return 0;
    let inter = 0;
    for (const r of s1) if (s2.has(r)) inter++;
    return inter / (s1.size + s2.size - inter);
}

function resourceDivergence(soloRes, collabRes) {
    // How much did the player's resource profile change from solo to collab?
    // 1 - Jaccard(solo_resources, collab_resources). 0 = same profile, 1 = completely different.
    const s1 = new Set(Object.keys(soloRes).filter(r => (soloRes[r] || 0) > 0));
    const s2 = new Set(Object.keys(collabRes).filter(r => (collabRes[r] || 0) > 0));
    if (s1.size === 0 && s2.size === 0) return 0;
    let inter = 0;
    for (const r of s1) if (s2.has(r)) inter++;
    const jaccard = inter / (s1.size + s2.size - inter);
    return 1 - jaccard;
}

function evaluate(projs, assign, cash, costs, supply, synergy, maxTypes = 99) {
    const pp1 = projs.filter(p => assign[p.id] === 'p1');
    const pp2 = projs.filter(p => assign[p.id] === 'p2');
    const r1 = maxForPlayer(pp1, cash, costs, supply, maxTypes);
    const r2 = maxForPlayer(pp2, cash, costs, supply, maxTypes);
    const combined = r1.reward + r2.reward;
    const cm = collabMax(pp1, pp2, cash, cash, costs, supply, synergy, maxTypes);
    const mcRatio = combined > 0 ? cm.total / combined : 1;
    const asc1 = actionSpaceSize(pp1, cash, costs, supply, maxTypes);
    const asc2 = actionSpaceSize(pp2, cash, costs, supply, maxTypes);
    const swapFairness = Math.min(cm.bestP1Favor, cm.bestP2Favor) / Math.max(cm.bestP1Favor, cm.bestP2Favor, 1);

    // Negotiation pressure: how much solo strategies conflict and how much the
    // joint-optimal requires players to change their resource profiles.
    // soloOverlap: Jaccard overlap of resources used by each player's solo-best strategy.
    //   High overlap = both players want the same resources = potential conflict.
    // collabDivergence: average of per-player resource profile divergence (solo vs collab).
    //   High divergence = the joint-optimal forces at least one player to switch strategies.
    // negotiationPressure = soloOverlap * collabDivergence
    //   High pressure = resources conflict AND the solution requires strategy change = interesting!
    const soloOverlap = resourceOverlap(r1.resUsed, r2.resUsed);
    const div1 = resourceDivergence(r1.resUsed, cm.resUsed1 || {});
    const div2 = resourceDivergence(r2.resUsed, cm.resUsed2 || {});
    const collabDivergence = (div1 + div2) / 2;
    const negotiationPressure = soloOverlap * collabDivergence;

    return { combined, collab: cm.total, mcRatio, gap: Math.abs(r1.reward - r2.reward), r1: r1.reward, r2: r2.reward, base1: cm.baseScore1, base2: cm.baseScore2, collabDetail: cm.collabDetail, resUsed: cm.resUsed, synergyBonus: cm.synergyBonus, asc1, asc2, bestP1Favor: cm.bestP1Favor, bestP2Favor: cm.bestP2Favor, swapFairness, optimalCount: cm.optimalCount, soloOverlap, collabDivergence, negotiationPressure };
}

function score(ev, targetMC) {
    const ratioDiff = Math.abs(ev.mcRatio - targetMC);
    // Hard constraint: M/C must be within ±0.05 of target, soft penalty within that band
    const ratioErr = ratioDiff > 0.05 ? 500 + ratioDiff * 100 : ratioDiff * 100;
    const leakPenalty = (targetMC <= 1.0 && ev.mcRatio > 1.0) ? 300 : 0;
    // Hard fairness constraints: V1 must equal V2, swap fairness must be 100%
    const gapPenalty = ev.gap > 0 ? 500 : 0;
    const trivialPenalty = ev.combined < 2 ? 200 : 0;
    const scorePenalty = Math.abs(ev.combined - 40) * 1.5;
    let superPenalty = 0;
    if (targetMC > 1.0) {
        if (ev.base1 >= ev.r1) superPenalty += 100;
        if (ev.base2 >= ev.r2) superPenalty += 100;
    }
    const cardPenalty = Math.abs(ev.asc1 - ev.asc2) / Math.max(ev.asc1, ev.asc2, 1) * 30;
    const swapPenalty = ev.swapFairness < 1.0 ? 500 : 0;
    const ambiguityPenalty = (ev.optimalCount < 2) ? 150 : 0;
    // Reward high negotiation pressure: solo strategies conflict and joint-optimal
    // requires at least one player to change resource profile. Subtract to reward.
    const negotiationBonus = (ev.negotiationPressure || 0) * 200;
    return ratioErr + leakPenalty + 0.5 * gapPenalty + trivialPenalty + scorePenalty + superPenalty + cardPenalty + swapPenalty + ambiguityPenalty - negotiationBonus;
}

// --- SA helpers ---
function deepCloneProjects(projs) {
    return projs.map(p => ({ id: p.id, name: p.name, req: { ...p.req }, reward: p.reward }));
}
function cloneAssign(a) { return { ...a }; }
function randInt(lo, hi) { return lo + Math.floor(Math.random() * (hi - lo + 1)); }
function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

function neighbour(projs, assign, cash, resourceTypes, allowSynergy, synergy, costs) {
    const p2 = deepCloneProjects(projs);
    const a2 = cloneAssign(assign);
    let newSynergy = synergy ? { ...synergy } : null;
    const move = randInt(0, allowSynergy ? 4 : 3);

    if (move === 0) {
        const zones = ['p1', 'p2'];
        const p = pick(p2);
        const from = a2[p.id];
        const fromCount = p2.filter(x => a2[x.id] === from).length;
        if (fromCount > 3) {
            const prevZone = a2[p.id];
            a2[p.id] = zones[(zones.indexOf(from) + 1) % 2];
            if (!playerCoversAllTypes(p2, a2, 'p1', resourceTypes) ||
                !playerCoversAllTypes(p2, a2, 'p2', resourceTypes) ||
                hasDuplicateProjects(p2)) {
                a2[p.id] = prevZone;
            }
        }
    } else if (move === 1) {
        const p = pick(p2);
        const prevReward = p.reward;
        p.reward = Math.min(10, Math.max(1, p.reward + pick([-2, -1, 1, 2])));
        if (hasDuplicateProjects(p2)) {
            p.reward = prevReward;
        }
    } else if (move === 2) {
        const p = pick(p2);
        const rkeys = Object.keys(p.req);
        if (rkeys.length) {
            const r = pick(rkeys);
            const prev = p.req[r];
            p.req[r] = Math.min(6, Math.max(1, p.req[r] + pick([-1, 1])));
            if (cpr(p, costs) > cash ||
                !playerCoversAllTypes(p2, a2, 'p1', resourceTypes) ||
                !playerCoversAllTypes(p2, a2, 'p2', resourceTypes) ||
                hasDuplicateProjects(p2)) {
                p.req[r] = prev;
            }
        }
    } else if (move === 3) {
        const pp1z = p2.filter(x => a2[x.id] === 'p1');
        const pp2z = p2.filter(x => a2[x.id] === 'p2');
        if (pp1z.length >= 3 && pp2z.length >= 3) {
            const pa = pick(pp1z), pb = pick(pp2z);
            a2[pa.id] = 'p2'; a2[pb.id] = 'p1';
            if (!playerCoversAllTypes(p2, a2, 'p1', resourceTypes) ||
                !playerCoversAllTypes(p2, a2, 'p2', resourceTypes) ||
                hasDuplicateProjects(p2)) {
                a2[pa.id] = 'p1'; a2[pb.id] = 'p2';
            }
        }
    } else {
        // Synergy perturbation
        if (!newSynergy) {
            newSynergy = { res: pick(resourceTypes), threshold: randInt(4, 10), bonus: randInt(1, 4) };
        } else {
            const sub = randInt(0, 2);
            if (sub === 0) newSynergy = null;
            else if (sub === 1) newSynergy = { ...newSynergy, threshold: Math.max(2, newSynergy.threshold + pick([-2, -1, 1, 2])) };
            else newSynergy = { ...newSynergy, bonus: Math.min(8, Math.max(1, newSynergy.bonus + pick([-1, 1]))) };
        }
    }
    return { projs: p2, assign: a2, cash, synergy: newSynergy };
}

/**
 * Run SA solver client-side. Returns a promise resolving to the same shape as
 * the server endpoint: {agent_projects, scenario_synergy, oracle_stats, solver_stats}
 *
 * @param {number} targetMC - target M/C ratio (e.g. 0.7 or 1.2)
 * @param {string[]} resourceTypes - e.g. ["wood","stone","gold"]
 * @param {Object} costs - e.g. {wood:2, stone:1, gold:4}
 * @param {Object} supply - e.g. {wood:10, stone:10, gold:6}
 * @param {number} cashPerPlayer
 * @param {function} [onProgress] - callback(pct, bestState)
 */
export { maxForPlayer, collabMax, evaluate, score, actionSpaceSize, cpr, maxRuns, playerCoversAllTypes, hasDuplicateProjects };

export async function generateScenario(targetMC, resourceTypes, costs, supply, cashPerPlayer, onProgress, maxTypes = 2) {
    const numProjects = 6;
    // Initialize projects with resource coverage retry
    let projs, assign;
    for (let attempt = 0; attempt < 200; attempt++) {
        projs = [];
        for (let i = 0; i < numProjects; i++) {
            const nRes = randInt(1, Math.min(2, resourceTypes.length));
            const chosen = [];
            const pool = [...resourceTypes];
            for (let k = 0; k < nRes; k++) {
                const idx = randInt(0, pool.length - 1);
                chosen.push(pool.splice(idx, 1)[0]);
            }
            let req = {};
            do {
                req = {};
                for (const r of chosen) req[r] = randInt(1, 4);
            } while (cpr({ req }, costs) > cashPerPlayer);
            projs.push({ id: 'p' + i, name: 'p' + i, req, reward: randInt(2, 6) });
        }
        assign = {};
        for (let i = 0; i < projs.length; i++) {
            assign[projs[i].id] = i < 3 ? 'p1' : 'p2';
        }
        if (playerCoversAllTypes(projs, assign, 'p1', resourceTypes) &&
            playerCoversAllTypes(projs, assign, 'p2', resourceTypes) &&
            !hasDuplicateProjects(projs)) break;
    }

    const allowSynergy = targetMC > 1.0;
    let synergy = allowSynergy ? { res: pick(resourceTypes), threshold: randInt(4, 8), bonus: randInt(1, 4) } : null;
    const cash = cashPerPlayer;

    let curEv = evaluate(projs, assign, cash, costs, supply, synergy, maxTypes);
    let curScore = score(curEv, targetMC);
    let bestState = { projs: deepCloneProjects(projs), assign: cloneAssign(assign), cash, ev: curEv, score: curScore, synergy: synergy ? { ...synergy } : null };

    const T0 = 80, Tmin = 0.5, maxIter = 5000;
    const alpha = Math.pow(Tmin / T0, 1 / maxIter);
    let T = T0, iter = 0, improvements = 0;

    return new Promise(resolve => {
        function step() {
            if (iter >= maxIter || bestState.score < 0.5) {
                if (onProgress) onProgress(100, bestState);
                resolve(formatResult(bestState, iter * 12, improvements));
                return;
            }
            for (let k = 0; k < 12; k++) {
                const nb = neighbour(projs, assign, cash, resourceTypes, allowSynergy, synergy, costs);
                const nbEv = evaluate(nb.projs, nb.assign, nb.cash, costs, supply, nb.synergy, maxTypes);
                const nbScore = score(nbEv, targetMC);
                const diff = nbScore - curScore;
                if (diff < 0 || Math.random() < Math.exp(-diff / T)) {
                    projs = nb.projs; assign = nb.assign; synergy = nb.synergy;
                    curEv = nbEv; curScore = nbScore;
                    if (curScore < bestState.score) {
                        bestState = { projs: deepCloneProjects(projs), assign: cloneAssign(assign), cash, ev: curEv, score: curScore, synergy: synergy ? { ...synergy } : null };
                        improvements++;
                    }
                }
                T *= alpha; iter++;
            }
            if (onProgress) onProgress(Math.round(100 * iter / maxIter), bestState);
            setTimeout(step, 0);
        }
        step();
    });
}

function formatResult(bestState, candidates, improvements) {
    const ev = bestState.ev;
    const pp1 = bestState.projs.filter(p => bestState.assign[p.id] === 'p1').slice(0, 3);
    const pp2 = bestState.projs.filter(p => bestState.assign[p.id] === 'p2').slice(0, 3);

    const toServerFormat = projs => projs.map((p, i) => ({
        name: `project_${'abc'[i] || i}`,
        requirements: { ...p.req },
        reward: p.reward,
    }));

    const scenarioSynergy = bestState.synergy ? {
        resource: bestState.synergy.res,
        threshold: bestState.synergy.threshold,
        bonus: bestState.synergy.bonus,
    } : null;

    return {
        agent_projects: [toServerFormat(pp1), toServerFormat(pp2)],
        scenario_synergy: scenarioSynergy,
        oracle_stats: {
            v1: ev.r1,
            v2: ev.r2,
            combined: ev.combined,
            collab_max: ev.collab,
            mc_ratio: ev.mcRatio,
            base_score_a: ev.base1,
            base_score_b: ev.base2,
            synergy_bonus: ev.synergyBonus,
            collab_detail: ev.collabDetail || '---',
            res_used: ev.resUsed || {},
            asc1: ev.asc1,
            asc2: ev.asc2,
            best_p1_favor: ev.bestP1Favor,
            best_p2_favor: ev.bestP2Favor,
            swap_fairness: ev.swapFairness,
            optimal_paths: ev.optimalCount,
            solo_overlap: ev.soloOverlap,
            collab_divergence: ev.collabDivergence,
            negotiation_pressure: ev.negotiationPressure,
        },
        solver_stats: {
            candidates,
            improvements,
        },
    };
}
