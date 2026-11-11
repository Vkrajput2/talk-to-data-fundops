"""Static checks of the container, CI, Azure and repo-hygiene files (public repo: no secrets, pinned deps)."""
import re

from tests.conftest import ROOT


def read(p):
    return (ROOT / p).read_text(encoding="utf-8")


def test_dockerfile_is_multistage_non_root_with_healthcheck():
    d = read("Dockerfile")
    assert len(re.findall(r"^FROM ", d, flags=re.M)) == 2 and "python:3.12-slim" in d
    users = re.findall(r"^USER (\S+)", d, flags=re.M)
    assert users and users[-1] not in ("root", "0")
    assert "HEALTHCHECK" in d and "/health" in d and "PORT" in d
    assert "${PORT}" in d and "ENV" in d and "PORT=8000" in d
    assert not re.search(r"COPY .*\.env", d) and "requirements.txt" in d and "requirements-dev" not in d
    assert "COPY app" in d and "COPY Data" in d and "gsk_" not in d


def test_dockerignore_excludes_secrets_and_scratch():
    lines = set(read(".dockerignore").split())
    for must in (".env", ".agents", "*.db", ".venv", ".git", "tests"):
        assert must in lines, must


def test_compose_has_env_file_port_volume_restart():
    c = read("docker-compose.yml")
    assert "env_file: .env" in c and '"8000:8000"' in c and "msim-data:/data" in c and "restart: unless-stopped" in c


def test_ci_runs_non_live_tests_and_builds_the_image():
    ci = read(".github/workflows/ci.yml")
    assert 'pytest -m "not live"' in ci and "docker build" in ci and "permissions:\n  contents: read" in ci
    assert "push:" in ci and "pull_request:" in ci and "requirements-dev.txt" in ci and "${{ secrets" not in ci


def test_deploy_workflow_is_manual_oidc_and_leaks_nothing():
    w = read(".github/workflows/deploy-azure.yml")
    on_block = w.split("jobs:")[0]
    assert "workflow_dispatch" in on_block
    for trigger in ("push:", "pull_request:", "schedule:"):
        assert not re.search(rf"^\s*{trigger}", on_block, flags=re.M), trigger
    assert "azure/login@" in w and "id-token: write" in w and "contents: read" in w
    for s in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "LLM_API_KEY"):
        assert "${{ secrets." + s + " }}" in w
    assert "gsk_" not in w and not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", w)
    assert "client-secret" not in w


def test_bicep_keeps_the_key_secure():
    b = read("infra/azure/main.bicep")
    assert re.search(r"@secure\(\)\s*\nparam llmApiKey string", b)
    assert "name: 'llm-api-key', value: llmApiKey" in b and "secretRef: 'llm-api-key'" in b
    assert not re.search(r"name: 'LLM_API_KEY', value", b)
    assert "targetPort: 8000" in b and "maxReplicas: 1" in b


def test_azure_doc_mentions_the_key_points():
    d = read("docs/azure-deploy.md")
    for word in ("ephemeral", "AZURE_CLIENT_ID", "workflow_dispatch", "Azure Files", "OIDC"):
        assert word in d, word


def test_gitignore_and_env_example_keep_secrets_out():
    g = set(read(".gitignore").split())
    for must in (".env", "*.db", ".venv/", "__pycache__/", ".agents/"):
        assert must in g, must
    for line in read(".env.example").splitlines():
        m = re.match(r"^(LLM_API_KEY|GROQ_API_KEY|GROK_API_DEV_KEY)=(.*)$", line)
        if m:
            assert m.group(2).strip() == "", line
    assert "LLM_API_KEY" in read(".env.example") and "GROK_API_DEV_KEY" in read(".env.example")


def test_requirements_are_fully_pinned_and_runtime_has_no_test_tools():
    req = [l for l in read("requirements.txt").splitlines() if l.strip()]
    assert len(req) >= 20 and all(re.match(r"^[A-Za-z0-9_.\-]+==\d[\w.]*$", l) for l in req), req
    names = {l.split("==")[0].lower().replace("_", "-") for l in req}
    assert {"fastapi", "uvicorn", "groq", "pydantic", "python-dotenv", "sqlglot", "httpx"} <= names
    assert "pytest" not in names
    dev = read("requirements-dev.txt")
    assert "-r requirements.txt" in dev and re.search(r"^pytest==\d", dev, flags=re.M)


def test_readme_has_required_sections_and_public_repo_statements():
    r = read("README.md")
    for h in ("## Changing the model or API key", "## Synthetic data", "## Running with Docker", "## Tests", "## Live evaluation",
              "## Security notes"):
        assert h in r, h
    low = r.lower()
    assert "fictional" in low and "environment variable" in low and "eval_models.py" in r
    assert "LLM_PROVIDER" in r and "LLM_MODEL" in r and "LLM_API_KEY" in r and "LLM_BASE_URL" in r
    for provider in ("Groq", "xAI", "OpenAI", "Azure OpenAI", "OpenRouter", "Ollama"):
        assert provider in r, provider
    assert "gsk_" not in r


def test_no_secret_like_tokens_in_tracked_text_files():
    pat = re.compile(r"(gsk_[A-Za-z0-9]{20,}|xai-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,})")
    skip = {".venv", ".git", ".agents", "__pycache__", ".pytest_cache"}
    hits = []
    for p in ROOT.rglob("*"):
        if p.is_dir() or skip & set(p.relative_to(ROOT).parts) or p.suffix in {".db", ".docx", ".png"} or p.name == ".env":
            continue
        try:
            if pat.search(p.read_text(encoding="utf-8")):
                hits.append(str(p.relative_to(ROOT)))
        except (UnicodeDecodeError, OSError):
            continue
    assert hits == []
