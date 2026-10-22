"""Deploy the improved warehouse agent to AgentCore Runtime for Lab 09 Stage 2.

Stage 2 verifies the Stage-1 fix in production, which first requires shipping that
fix to the Lab 7 runtime. That redeploy is pure AgentCore plumbing (write the entrypoint
and requirements, configure the Runtime, patch the generated Dockerfile, launch, wait for
READY) and not the focus of an evaluation lab, so it lives here instead of the notebook.

The deployed entrypoint imports ``build_improved_warehouse_agent`` from ``util.warehouse_agent``
(the Dockerfile copies ``util/`` into the container), so the agent graded in production is the
exact same eval-driven architecture Stage 1 grades locally — the ``get_warehouse_stock`` deep tool
plus the selector/``odata_caller`` fallback — true parity, no duplicated code.

The agent is deployed as an A2A-protocol runtime (``protocol="A2A"``) secured with a Cognito
JWT authorizer reused from Lab 7. Invocations use JSON-RPC 2.0 over HTTPS rather than
``invoke_agent_runtime``.
"""

from __future__ import annotations

import json
import os

# Entrypoint written into the build context and run inside the AgentCore container.
# Uses LiteLLMModel with the sap/ prefix to route through SAP GenAI Hub.
# SAP AI Core credentials are injected as AICORE_* env vars at launch() time
# (config.json is gitignored, so env vars are the reliable delivery mechanism).
# StrandsA2AExecutor in strands-agents>=1.52.0 accepts a single `agent` argument.
ENTRYPOINT_SOURCE = '''\
# Lab 09 Stage 2 entrypoint: the improved warehouse agent deployed as an A2A runtime.
from strands.multiagent.a2a.executor import StrandsA2AExecutor
from bedrock_agentcore.runtime import serve_a2a

from strands.models.litellm import LiteLLMModel
from util.warehouse_agent import build_improved_warehouse_agent

# sap/ prefix routes through SAP GenAI Hub via LiteLLM.
# Credentials come from AICORE_* env vars injected at launch() time.
# stream=False: SAP GenAI Hub returns 400 for streaming on Claude models.
model = LiteLLMModel(
    model_id="sap/{model_id}",
    params={{"max_tokens": 4096, "stream": False}},
)

# Build the agent once at startup (strands-agents>=1.52 StrandsA2AExecutor takes
# a single agent instance, not agent_factory).
agent = build_improved_warehouse_agent(model)

if __name__ == "__main__":
    serve_a2a(StrandsA2AExecutor(agent=agent))
'''

# Container dependencies — A2A extras added for StrandsA2AExecutor and serve_a2a.
# Versions are pinned to mirror pyproject.toml, NOT loosened, so the deployed runtime
# resolves the same stack the notebook was validated against.
REQUIREMENTS = """\
# AgentCore requirements (pins mirror pyproject.toml) — A2A protocol build
strands-agents[a2a]==1.52.0
strands-agents[litellm]==1.52.0
strands-agents-tools==0.2.0
uv
boto3>=1.37.0
bedrock-agentcore[a2a]>=1.11.0
bedrock-agentcore-starter-toolkit==0.1.14
uvicorn
# SAP GenAI Hub and warehouse agent dependencies
sap-ai-sdk-gen[all]>=6.10.0
litellm>=1.0.0
pyyaml
requests
python-dotenv
"""

# Lines inserted into the generated Dockerfile (before CMD) so the container has the util
# package and the OpenAPI knowledgebase.
# SAP AI Core credentials are injected as AICORE_* env vars at launch() time — config.json
# is gitignored and excluded from the CodeBuild zip, so COPY config.json would fail.
_DOCKERFILE_ADDITIONS = [
    "",
    "# Copy util directory (required for SAP GenAI Hub model and OData tool)",
    "COPY util/ ./util/",
    "",
    "# Copy assets directory (required for OpenAPI knowledgebase)",
    "COPY assets/ ./assets/",
    "",
]

END_STATUSES = ["READY", "CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"]


def _write_build_context(entrypoint_file, model_id):
    """Write the entrypoint, requirements.txt, and config.json into the build context."""
    with open(entrypoint_file, "w") as f:
        f.write(ENTRYPOINT_SOURCE.format(model_id=model_id))

    with open("requirements.txt", "w") as f:
        f.write(REQUIREMENTS)

    # Note: config.json is NOT written to the build context here because it is listed
    # in .gitignore and the toolkit's zip filter excludes gitignored files from the
    # CodeBuild source upload. SAP AI Core credentials are injected as AICORE_* env
    # vars at launch() time instead — LiteLLM checks them before the config file.


def _patch_dockerfile(dockerfile="Dockerfile"):
    """Insert the util/ and assets/ lines before CMD (idempotent)."""
    with open(dockerfile, "r") as f:
        content = f.read()

    if "COPY util/ ./util/" in content:
        return "Dockerfile already contains util/ configuration, skipping."

    lines = content.split("\n")
    cmd_index = next((i for i, line in enumerate(lines) if line.strip().startswith("CMD")), -1)
    if cmd_index == -1:
        raise SystemExit("Could not find CMD instruction in the generated Dockerfile.")

    modified = lines[:cmd_index] + _DOCKERFILE_ADDITIONS + lines[cmd_index:]
    with open(dockerfile, "w") as f:
        f.write("\n".join(modified))
    return "Dockerfile modified with util/ and assets/."


def _load_aicore_env_vars() -> dict:
    """Read SAP AI Core credentials from ~/.aicore/config.json and return as AICORE_* env vars.

    LiteLLM's sap/ provider checks individual AICORE_* env vars (source 3) before
    the config file (source 4), so injecting them at launch() time works even though
    config.json is gitignored and excluded from the CodeBuild zip.
    """
    config_path = os.path.expanduser("~/.aicore/config.json")
    if not os.path.exists(config_path):
        raise SystemExit(
            f"{config_path} not found. Run notebook 00 to configure SAP AI Core credentials first."
        )
    with open(config_path, "r") as f:
        cfg = json.load(f)

    mapping = {
        "AICORE_CLIENT_ID":      cfg.get("AICORE_CLIENT_ID"),
        "AICORE_CLIENT_SECRET":  cfg.get("AICORE_CLIENT_SECRET"),
        "AICORE_AUTH_URL":       cfg.get("AICORE_AUTH_URL"),
        "AICORE_BASE_URL":       cfg.get("AICORE_BASE_URL"),
        "AICORE_RESOURCE_GROUP": cfg.get("AICORE_RESOURCE_GROUP", "default"),
    }
    missing = [k for k, v in mapping.items() if not v]
    if missing:
        raise SystemExit(f"SAP AI Core config is missing keys: {missing}. Check ~/.aicore/config.json.")
    print(f"  Loaded {len(mapping)} SAP AI Core credential keys from {config_path}")
    return mapping


def deploy_improved_agent(
    agent_name,
    region,
    sap_api_key,
    discovery_url,
    client_id,
    entrypoint_file="warehouse_agent_agentcore.py",
    model_id="anthropic--claude-4.5-sonnet",
    poll_seconds=10,
):
    """Deploy the improved warehouse agent as a new A2A-protocol AgentCore Runtime.

    Creates a fresh runtime named ``agent_name`` secured with a Cognito JWT authorizer
    (``discovery_url`` / ``client_id`` reused from Lab 7). The runtime serves the
    ``StrandsA2AExecutor``-based entrypoint and is invocable via JSON-RPC 2.0 over HTTPS.
    Blocks until the runtime reaches READY, raising ``SystemExit`` on any failure status.

    Args:
        agent_name: Name for the new AgentCore Runtime.
        region: AWS region to deploy into.
        sap_api_key: SAP S/4HANA key injected into the container for OData authentication.
        discovery_url: Cognito OIDC discovery URL (from Lab 7 Cognito setup).
        client_id: Cognito app client ID (from Lab 7 Cognito setup).
        entrypoint_file: Path for the generated entrypoint file.
        model_id: Model identifier (without sap/ prefix) passed to LiteLLMModel.
            The entrypoint prepends "sap/" so LiteLLM routes via SAP GenAI Hub.
        poll_seconds: Seconds between runtime status polls.

    Returns:
        The launch_result object (agent_id, agent_arn).
    """
    import time
    from pathlib import Path
    from bedrock_agentcore_starter_toolkit import Runtime

    _write_build_context(entrypoint_file, model_id)
    print("Wrote entrypoint and requirements.txt into the build context.")
    aicore_env = _load_aicore_env_vars()

    # Clear any stale cached agent state so configure() creates a fresh runtime.
    stale_config = Path.home() / ".bedrock_agentcore.yaml"
    if stale_config.exists():
        stale_config.unlink()
        print("Cleared stale agent config: ~/.bedrock_agentcore.yaml")

    runtime = Runtime()
    print(f"Deploying new A2A AgentCore Runtime: {agent_name}")
    runtime.configure(
        entrypoint=entrypoint_file,
        auto_create_execution_role=True,
        auto_create_ecr=True,
        requirements_file="requirements.txt",
        region=region,
        agent_name=agent_name,
        protocol="A2A",
        authorizer_configuration={
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedClients": [client_id],
                # allowedAudience is intentionally omitted —
                # Cognito client_credentials tokens have no 'aud' claim
            }
        },
        non_interactive=True,
    )
    print(_patch_dockerfile())

    launch_result = runtime.launch(
        env_vars={
            "SAP_S4HANA_PUBLIC_CLOUD_KEY": sap_api_key,
            **aicore_env,  # AICORE_CLIENT_ID, AICORE_CLIENT_SECRET, AICORE_AUTH_URL, AICORE_BASE_URL, AICORE_RESOURCE_GROUP
        },
    )
    print(f"A2A deployment initiated.")
    print(f"  Agent ID : {launch_result.agent_id}")
    print(f"  Agent ARN: {launch_result.agent_arn}")

    # Explicitly enable observability for this runtime. The toolkit calls
    # enable_traces_delivery_for_runtime() inside launch() automatically, but it
    # catches all exceptions and continues silently — so if it failed (e.g. a
    # transient permission issue) the agent shows "Tracing - Not enabled" in the
    # console and the AgentCore evaluators find no spans.
    try:
        import boto3 as _boto3
        from bedrock_agentcore_starter_toolkit.services.xray import (
            enable_traces_delivery_for_runtime,
            enable_transaction_search_if_needed,
        )
        _account_id = _boto3.client("sts", region_name=region).get_caller_identity()["Account"]
        print("Configuring observability (Transaction Search + traces delivery)...")
        enable_transaction_search_if_needed(region, _account_id)
        enable_traces_delivery_for_runtime(
            agent_id=launch_result.agent_id,
            agent_arn=launch_result.agent_arn,
            region=region,
        )
        print("  Observability configured.")
    except Exception as _obs_err:
        print(f"  WARNING: observability setup failed: {_obs_err}")
        print("  Stage 2 grading may return empty scores — verify tracing in the AgentCore console.")

    status = runtime.status().endpoint["status"]
    print(f"Initial status: {status}")
    while status not in END_STATUSES:
        time.sleep(poll_seconds)
        status = runtime.status().endpoint["status"]
        print(status)

    print(f"\nFinal status: {status}")
    if status != "READY":
        raise SystemExit(
            f"Deploy did not reach READY (final status: {status}). Stage 2 aborted."
        )
    return launch_result
