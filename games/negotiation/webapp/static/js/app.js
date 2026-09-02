// --- Entry point: imports all modules, theme, consent, tabs ---

import { applyProvider, initConfigListeners, handleModelSelect, generateScenario, swapAgentConfigs } from './config.js';
import { launchGame, openLiveView, stopGame, sendHumanInput, exportGameLog } from './live.js';
import { loadHistory, loadLocalGameDetail, loadGameDetail, exportLocalGame } from './history.js';
import { loadDataset, exportCurrentTrace, exportDatasetJSONL, initDatasetListeners } from './dataset.js';
import { launchBatch } from './batch.js';
import { handleJSONLImport } from './export.js';
import { launchQuickGame, initQuickstart } from './quickstart.js';

// --- App-wide config ---
window._appConfig = {
    firestore_collection: 'game_traces' // Default fallback
};

async function fetchAppConfig() {
    try {
        const resp = await fetch('/api/config');
        const data = await resp.json();
        window._appConfig = data;
    } catch (e) {
        console.error('Failed to fetch app config:', e);
    }
}

// --- Theme ---
function getTheme() {
    return localStorage.getItem('theme') || 'light';
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    document.getElementById('themeIcon').textContent = theme === 'dark' ? '\u2600' : '\u263E';
}

function toggleTheme() {
    const next = getTheme() === 'dark' ? 'light' : 'dark';
    localStorage.setItem('theme', next);
    applyTheme(next);
}

applyTheme(getTheme());

// --- Consent ---
window._firestoreAvailable = false;

async function checkConsent() {
    try {
        const resp = await fetch('/api/consent/status');
        const data = await resp.json();
        window._firestoreAvailable = data.firestore_available;
    } catch (e) {
        window._firestoreAvailable = false;
    }
    if (window._firestoreAvailable && !localStorage.getItem('data_consent')) {
        document.getElementById('consentBanner').style.display = 'block';
    }
}

function acceptConsent() {
    localStorage.setItem('data_consent', 'accepted');
    document.getElementById('consentBanner').style.display = 'none';
}

function declineConsent() {
    localStorage.setItem('data_consent', 'declined');
    document.getElementById('consentBanner').style.display = 'none';
}

checkConsent();
fetchAppConfig();

// --- Lightbox ---
function openLightbox(imgEl) {
    const lb = document.getElementById('lightbox');
    const lbImg = document.getElementById('lightboxImg');
    lbImg.src = imgEl.src;
    lbImg.alt = imgEl.alt;
    lb.classList.add('active');
}

function closeLightbox() {
    document.getElementById('lightbox').classList.remove('active');
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeLightbox();
});

// --- Tab switching ---
function switchToTab(tabName) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');
    document.getElementById('tab-' + tabName).classList.add('active');
    if (tabName === 'history') loadHistory();
    if (tabName === 'dataset') loadDataset();
}

document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => switchToTab(tab.dataset.tab));
});

// --- Human input Enter key ---
document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && document.getElementById('humanInputBar').style.display !== 'none'
        && document.getElementById('humanChatSection').style.display !== 'none') {
        e.preventDefault();
        sendHumanInput();
    }
});

// --- Initialize modules ---
initConfigListeners();
initDatasetListeners();
initQuickstart();

// --- Expose to inline onclick handlers in HTML ---
window.toggleTheme = toggleTheme;
window.acceptConsent = acceptConsent;
window.declineConsent = declineConsent;
window.openLightbox = openLightbox;
window.closeLightbox = closeLightbox;
window.switchToTab = switchToTab;
window.applyProvider = applyProvider;
window.handleModelSelect = handleModelSelect;
window.swapAgentConfigs = swapAgentConfigs;
window.launchGame = launchGame;
window.launchQuickGame = launchQuickGame;
window.stopGame = stopGame;
window.exportGameLog = exportGameLog;
window.sendHumanInput = sendHumanInput;
window.loadHistory = loadHistory;
window.loadLocalGameDetail = loadLocalGameDetail;
window.loadGameDetail = loadGameDetail;
window.exportLocalGame = exportLocalGame;
window.handleJSONLImport = async (event) => {
    const imported = await handleJSONLImport(event);
    if (imported > 0) loadHistory();
};
window.launchBatch = launchBatch;
window.generateScenario = generateScenario;
window.exportCurrentTrace = exportCurrentTrace;
window.exportDatasetJSONL = exportDatasetJSONL;
window.loadDataset = loadDataset;
