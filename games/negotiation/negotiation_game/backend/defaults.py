"""
Default configuration constants for the Negotiation Environment.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


def _find_repo_root() -> Path:
    """Walk up from this file to find the repository root (directory containing .git).
    Falls back to the directory containing this file (e.g. Cloud Run where .git is absent)."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return Path(__file__).resolve().parent


REPO_ROOT = _find_repo_root()

load_dotenv()

# --- LLM Provider Presets ---
# Each preset: (api_base, default_model, api_format, key_placeholder)
# api_format: "openai" for /chat/completions, "anthropic" for /v1/messages
LLM_PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_format": "openai",
        "key_placeholder": "sk-...",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "api_base": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-5-20250929",
        "api_format": "anthropic",
        "key_placeholder": "sk-ant-...",
    },
    "gemini": {
        "label": "Google Gemini",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.0-flash",
        "api_format": "openai",
        "key_placeholder": "AIza...",
    },
    "gemini_vertexai": {
        "label": "Google Gemini (Vertex AI / ADC)",
        "api_base": "",
        "model": "publishers/google/models/gemini-3-flash-preview",
        "api_format": "vertexai",
        "gcp_project": os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        "gcp_location": os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        "key_placeholder": "",
    },
    "claude_vertexai": {
        "label": "Anthropic Claude (Vertex AI / ADC)",
        "api_base": "",
        "model": "claude-sonnet-4-6",
        "api_format": "vertexai_anthropic",
        "gcp_project": os.environ.get("ANTHROPIC_VERTEX_PROJECT", ""),
        "gcp_location": os.environ.get("ANTHROPIC_VERTEX_LOCATION", "global"),
        "vertex_adc_file": os.environ.get(
            "ANTHROPIC_VERTEX_ADC_FILE",
            "",
        ),
        "key_placeholder": "",
    },
    "llama_vertexai": {
        "label": "Meta Llama (Vertex AI / ADC)",
        "api_base": "",
        "model": "meta/llama-4-maverick-17b-128e-instruct-maas",
        "api_format": "vertexai_openai",
        "gcp_project": os.environ.get("LLAMA_VERTEX_PROJECT", ""),
        "gcp_location": os.environ.get("LLAMA_VERTEX_LOCATION", "us-east5"),
        "vertex_adc_file": os.environ.get(
            "LLAMA_VERTEX_ADC_FILE",
            "",
        ),
        "key_placeholder": "",
    },
    "ollama": {
        "label": "Ollama (local)",
        "api_base": "http://localhost:11434/v1",
        "model": "llama3",
        "api_format": "openai",
        "key_placeholder": "ollama",
    },
    "openrouter": {
        "label": "OpenRouter",
        "api_base": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct",
        "api_format": "openai",
        "key_placeholder": "sk-or-v1-...",
    },
    "custom": {
        "label": "Custom Endpoint",
        "api_base": "",
        "model": "",
        "api_format": "openai",
        "key_placeholder": "",
    },
}

# --- Environment Defaults ---
DEFAULT_RESOURCE_TYPES = ["wood", "stone", "gold"]
DEFAULT_RESOURCE_SUPPLY = {"wood": 10, "stone": 10, "gold": 6}
DEFAULT_RESOURCE_COSTS = {"wood": 1.0, "stone": 1.5, "gold": 3.0}

DEFAULT_AGENT_BUDGET = 15.0
DEFAULT_NUM_ROUNDS = 5
DEFAULT_CHEAP_TALK_TURNS = 3
DEFAULT_MAX_RESOURCE_TYPES_PER_TURN = 2

# --- Project-Based Defaults ---
DEFAULT_PROJECTS_A = [
    {"name": "project_a", "requirements": {"wood": 3, "stone": 2}, "reward": 5},
    {"name": "project_b", "requirements": {"wood": 2, "gold": 1}, "reward": 4},
]
DEFAULT_PROJECTS_B = [
    {"name": "project_a", "requirements": {"stone": 2, "gold": 1}, "reward": 5},
    {"name": "project_b", "requirements": {"gold": 2}, "reward": 3},
]

DEFAULT_PORT = 8080

# Minimum seconds between consecutive LLM API requests (per agent)
API_REQUEST_COOLDOWN = 1.0

# Max retries when an agent submits malformed JSON for a purchase decision
MAX_DECISION_RETRIES = 3

# Retry settings for transient API errors (429, 5xx)
API_MAX_RETRIES = 10
API_BACKOFF_BASE = 2.0  # seconds; exponential: 2, 4, 8, 16, ...
API_BACKOFF_MAX = 120.0  # Cap per-retry wait time (prevents 512s+ waits)

# --- Storage ---
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "games")

# --- Cloud Run / Firestore ---
IS_CLOUD_RUN = os.environ.get("K_SERVICE") is not None
FIRESTORE_COLLECTION = "game_traces_v2"
# Demo visitor signups (email + environment outcome), separate from research episodes.
VISITOR_COLLECTION = "demo_visitors"
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "")
