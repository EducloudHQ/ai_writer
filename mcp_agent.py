import logging
from typing import Any, Dict
import json, os
from datetime import datetime, timezone
import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent import Swarm
import requests

from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client

# =========================
# ENV & LOGGING
# =========================


AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

CLIENT_ID = "CLIENT ID"
CLIENT_SECRET = "CLIENT SECRET"
TOKEN_URL = "Token URL"
MCP_GATEWAY_URL="MCP_GATEWAY_URL"
s3 = boto3.client("s3", region_name=AWS_REGION)


def fetch_access_token(client_id, client_secret, token_url):
    response = requests.post(
        token_url,
        data="grant_type=client_credentials&client_id={client_id}&client_secret={client_secret}".format(
            client_id=client_id, client_secret=client_secret),
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    return response.json()['access_token']



logging.getLogger("strands").setLevel(logging.INFO)
logging.basicConfig(
    format="%(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()]
)



def now_iso():
    return datetime.now(timezone.utc).isoformat()



'''@tool
async def dfseo_fetch_volumes(
        language_code: str,
        location_code: int,
        date_range: str,
        keywords: List[str],
        author_id: str = "anon",
        batch_size: int = 80,
        concurrency: int = 2,
        per_call_timeout: float = 30.0,
) -> Dict[str, Any]:
    """
    Fetches keyword metrics (via DataForSEO MCP or your internal logic),
    stores the FULL payload to S3, writes a tiny manifest to DynamoDB,
    and returns that manifest (safe for the model context).

    DynamoDB item (single-table):
      PK = USER#{author_id}
      SK = METRICS#{job_id}

    S3 object:
      s3://{S3_BUCKET}/metrics/{author_id}/{job_id}.json
    """
    # 1) (Placeholder) fetch metrics in batches; fill all_metrics with rows like:
    #    {"keyword": "...", "avg_monthly_searches": 5400, "cpc": 1.25, "competition": 0.37, "serp_features": [...]}
    #    Integrate your MCP tool calls / DataForSEO jobs here.
    all_metrics: List[Dict[str, Any]] = []  # <-- populate me

    # 2) Write to S3 (entire blob)
    job_id = str(uuid.uuid4())
    s3_key = f"metrics/{author_id}/{job_id}.json"
    body = {
        "language_code": language_code,
        "location_code": location_code,
        "date_range": date_range,
        "metrics": all_metrics,
        "createdAt": now_iso()
    }
    s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=json.dumps(body).encode("utf-8"), ContentType="application/json")
    s3_uri = f"s3://{S3_BUCKET}/{s3_key}"

    # 3) Write tiny manifest to DynamoDB
    sample = all_metrics[:5] if all_metrics else []
    item = {
        "PK": f"USER#{author_id}",
        "SK": f"METRICS#{job_id}",
        "entityType": "METRICS_MANIFEST",
        "authorId": author_id,
        "jobId": job_id,
        "s3Uri": s3_uri,
        "languageCode": language_code,
        "locationCode": location_code,
        "dateRange": date_range,
        "totalCount": len(all_metrics),
        "sample": sample,
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    ddb_table.put_item(Item=item)

    # 4) Return tiny manifest for downstream agents
    return {
        "manifest": {
            "table": DDB_TABLE_NAME,
            "pk": item["PK"],
            "sk": item["SK"],
            "s3_uri": s3_uri,
            "total_count": len(all_metrics),
            "sample": sample,
        }
    }

'''
reasoning_model = BedrockModel(
    model_id="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    temperature=0.5,
)


# =========================
# MCP transport
# =========================
def create_streamable_http_transport():
    MCP_BEARER_TOKEN = fetch_access_token(CLIENT_ID, CLIENT_SECRET, TOKEN_URL)
    print(f"mcp bearer token is {MCP_BEARER_TOKEN}")
    if not MCP_BEARER_TOKEN or MCP_BEARER_TOKEN.startswith("<PUT_"):
        raise RuntimeError("Set MCP_BEARER_TOKEN to a valid token.")
    return streamablehttp_client(
        MCP_GATEWAY_URL,
        headers={"Authorization": f"Bearer {MCP_BEARER_TOKEN}"}
    )

mcp_client = MCPClient(create_streamable_http_transport)

# =========================
# Build agents
# =========================
with  mcp_client:
    # Discover MCP tools (e.g., your DataForSEO SERP/Keywords endpoints)
    mcp_tools = mcp_client.list_tools_sync()

    # --- 1) Generate ~120 seed keywords from interests ---
    generate_keyword_agent = Agent(
        name="generate_keyword_agent",
        description="Turns user interests into ~120 mid-tail keywords and normalized settings.",
        model=reasoning_model,
        system_prompt=(
            "You convert a user's interests into ~120 high-quality, mid-tail keywords (2–4 words each). "
            "Normalize settings too.\n\n"
            "INPUT JSON SHAPE:\n"
            "{\n"
            '  "interests": [string],\n'
            '  "language": "en",\n'
            '  "location": "United States",\n'
            '  "date_range": "1y"\n'
            "}\n\n"
            "OUTPUT JSON SHAPE (STRICT):\n"
            "{\n"
            '  "language_code": "en",\n'
            '  "location_code": 2840,\n'
            '  "date_range": "2024-01-01..2024-12-31",\n'
            '  "keywords": ["... up to ~120 ..."]\n'
            "}\n\n"
            "- Use ISO date range or a relative 1y range resolved to YYYY-MM-DD..YYYY-MM-DD.\n"
            "- language_code must be IETF (e.g., 'en').\n"
            "- location_code should be a DataForSEO location code (e.g., 2840 for US). "
            "If unknown, pick 2840 for United States.\n"
            "- Return ONLY the JSON."
        ),
    )

    # --- 2) Fetch volumes via DataForSEO (MCP tools) ---
    # This agent will call your MCP tools. Keep its prompt laser-focused on tool use + shaping outputs.
    all_tools = [*mcp_tools]

    keywords_volume_agent = Agent(
        name="keywords_volume_agent",
        description="Fetch metrics via Data4SEO MCP, store to file, return only a manifest.",
        model=reasoning_model,
        tools=all_tools,
        system_prompt=(
            "Primary: use the DataForSEO MCP keyword metrics tool if available.\n"

            "Rules:\n"
            "- NEVER paste full raw metrics into the chat.\n"
            "- When fetching metrics, write the full result to a file and return only "
            "a small manifest: {uri, total_count, sample}.\n"

        ),
    )

    swarm_agent = Swarm(
            #nodes=[research_agent, kb_enricher_agent, draft_writer_agent],
            nodes=[generate_keyword_agent,keywords_volume_agent],
            entry_point=generate_keyword_agent,
            max_handoffs=10,
            max_iterations=15
        )
    initial_payload: Dict[str, Any] = {
            "interests": ["stoic thinking", "minimalism", "writing", "poetry", "medicine", "ai"],
            "language": "English",
            "location": "United States",
            "date_range": "1y",
            "author_details": {
                "author_id": "6d2a9e9f-e1fb-43e0-b8b2-492d54e074e1"
            }

        }

    response = swarm_agent(
            "Generate keyword strategy and outlines using this payload. "
            "Return STRICT JSON at every step.\n" + json.dumps(initial_payload)
        )

    print(response)