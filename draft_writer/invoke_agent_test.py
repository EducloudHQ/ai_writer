import json
import boto3
from pathlib import Path

# ---------- 1. Configure ---------- #
RUNTIME_ARN = (
    "RUNTIME_ARN"
)
JSON_PATH = Path("payload.json")  # the big JSON you pasted earlier
REGION = "us-east-1"

agent_core = boto3.client("bedrock-agentcore", region_name=REGION)

with JSON_PATH.open("r", encoding="utf-8") as f:
    sample = json.load(f)


payload_bytes = json.dumps(sample).encode("utf-8")

rsp = agent_core.invoke_agent_runtime(
    agentRuntimeArn=RUNTIME_ARN,
    payload=payload_bytes,
    runtimeSessionId="1-6893d561-0d487089048fdfdkdfj733f26f20c0890812989"
)

# Check HTTP status (optional)
assert rsp["statusCode"] == 200  # or rsp["ResponseMetadata"]["HTTPStatusCode"]

# Drain the StreamingBody <— this gives you bytes
body_bytes = rsp["response"].read()

result = json.loads(body_bytes.decode("utf-8"))
print(result) 

# Grab any diagnostics you need
session_id = rsp["runtimeSessionId"]
trace_id = rsp["traceId"]
print("session:", session_id, "trace:", trace_id)
