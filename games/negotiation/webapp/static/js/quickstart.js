// --- Quickstart: simplified poster-demo launcher (human vs LLM, paper preset) ---

import { hasConsent } from './config.js';
import { launchGame } from './live.js';

// Main-cohort models from the paper; provider + credentials resolved server-side.
export const QUICK_MODELS = [
    { id: 'gpt-5-mini', label: 'GPT-5 Mini (OpenAI)' },
    { id: 'claude-sonnet-4-5', label: 'Claude Sonnet 4.5 (Anthropic)' },
    { id: 'qwen/qwen3.5-flash-02-23', label: 'Qwen 3.5 Flash (Alibaba)' },
];

const SCENARIO_POOL_PATH = 'data/scenario_pools/run_009/filtered.json';

export function buildQuickGameConfig() {
    const model = document.getElementById('quickModel').value;
    const condition = document.querySelector('input[name="quickCondition"]:checked').value;
    const lite = document.querySelector('input[name="quickLength"]:checked').value === 'lite';
    const email = document.getElementById('quickEmail').value.trim();

    return {
        mode: 'stable',
        num_rounds: lite ? 2 : 4,
        cheap_talk_turns: 5,
        // LLM speaks first so the visitor sees an example message before typing.
        agents: [{ type: 'llm', model }, { type: 'human' }],
        first_speaker: 0,
        scenario_pool_path: SCENARIO_POOL_PATH,
        // The scenario's M/C ratio is sampled server-side and kept hidden from the
        // player — inferring the condition through negotiation is part of the study.
        target_mc_ratio: 'random',
        rotate_projects: false,
        named_projects: true,
        thinking: true,
        enable_cheap_talk: condition !== 'no_talk',
        full_transparency: condition === 'transparency',
        maximize_joint: condition === 'joint',
        share_projects: false,
        think_about_opponent: false,
        visible_utilities: true,
        visible_opponent_reward: false,
        consent: hasConsent(),
        experiment_label: 'poster_demo',
        visitor_email: email,
    };
}

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export async function launchQuickGame() {
    const emailInput = document.getElementById('quickEmail');
    if (!EMAIL_RE.test(emailInput.value.trim())) {
        emailInput.focus();
        emailInput.setCustomValidity('Please enter your email to try the game.');
        emailInput.reportValidity();
        return;
    }
    emailInput.setCustomValidity('');

    const btn = document.getElementById('quickLaunchBtn');
    btn.disabled = true;
    btn.textContent = 'Starting…';
    try {
        await launchGame(buildQuickGameConfig());
    } finally {
        btn.disabled = false;
        btn.textContent = 'Play against the AI';
    }
}

export function initQuickstart() {
    const select = document.getElementById('quickModel');
    if (!select) return;
    select.innerHTML = QUICK_MODELS
        .map(m => `<option value="${m.id}">${m.label}</option>`)
        .join('');
}
