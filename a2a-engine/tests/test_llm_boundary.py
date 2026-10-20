"""Keep provider transport implementations inside a2a_engine.llm.

This source-level guard complements mocked provider tests. It covers the known
SDK/HTTP bypass patterns, including the indirect transport in DSPy's default LM.
"""

import ast
from pathlib import Path
import re

import pytest


REPO = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    "a2a-engine", "a2a-agent", "a2a-judge", "expt-runner", "games",
    "cloud_platform", "local_stack", "scripts", "deploy",
)
SDK_MODULES = {"openai", "anthropic", "litellm", "vertexai", "ollama", "google.genai", "google.generativeai"}
ENDPOINT = re.compile(r"/chat/completions|:generateContent|:rawPredict|:streamRawPredict|:streamGenerateContent")


def violations(source):
    findings = []
    aliases = {}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
                modules.append(name.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            modules = [module] + [f"{module}.{name.name}" for name in node.names]
            for name in node.names:
                aliases[name.asname or name.name] = f"{module}.{name.name}"
        for module in modules:
            if any(module == sdk or module.startswith(sdk + ".") for sdk in SDK_MODULES):
                findings.append((node.lineno, "provider SDK import"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and ENDPOINT.search(node.value):
            findings.append((node.lineno, "model HTTP endpoint"))
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            root, *rest = name.split(".")
            name = ".".join([aliases.get(root, root), *rest])
            if name == "dspy.LM" or name.endswith((
                ".generate_content", ".generate_content_stream", ".messages.create",
                ".completions.create", ".responses.create", ".invoke_model",
            )):
                findings.append((node.lineno, "direct model transport"))
    return findings


def test_model_transports_are_confined_to_engine_llm():
    findings = []
    for directory in SOURCE_ROOTS:
        for path in (REPO / directory).rglob("*.py"):
            relative = path.relative_to(REPO)
            if "tests" in relative.parts or path.name.startswith("test_"):
                continue
            if relative.parts[:3] == ("a2a-engine", "a2a_engine", "llm"):
                continue
            for line, reason in violations(path.read_text()):
                findings.append(f"{relative}:{line}: {reason}")
    assert not findings, "Route model calls through a2a_engine.llm:\n" + "\n".join(findings)


@pytest.mark.parametrize("source", [
    "from openai import OpenAI", "import anthropic as sdk", "from google import genai",
    "import litellm", 'client.models.generate_content(model="fixture")',
    'client.messages.create(model="fixture")', 'client.chat.completions.create(model="fixture")',
    'url = f"{base}/chat/completions"', 'url = "model:generateContent"',
    'import dspy as dsp\nlm = dsp.LM("vertex_ai/gemini")',
    'from dspy import LM as ProviderLM\nlm = ProviderLM("vertex_ai/gemini")',
])
def test_guard_catches_known_bypass_patterns(source):
    assert violations(source)


def test_guard_allows_engine_calls_and_non_model_networking():
    assert not violations('''
from a2a_engine.llm import api
from google.auth import default
import httpx
api.call_llm_oneshot("openai", "fixture", "", "fixture", [])
httpx.get("http://fixture/api/health")
''')
