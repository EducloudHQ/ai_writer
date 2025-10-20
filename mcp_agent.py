
import logging
from typing import Any, Dict, List, Optional
import json, os

import uuid
from datetime import datetime, timezone

import boto3

from bedrock_agentcore import BedrockAgentCoreApp
from botocore.exceptions import ClientError
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

CLIENT_ID = ""
CLIENT_SECRET = ""
TOKEN_URL = ""
MCP_GATEWAY_URL = ""
s3 = boto3.client("s3", region_name=AWS_REGION)

DDB_TABLE_NAME = os.getenv("DDB_TABLE_NAME", "ai-writer-table")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
ddb_table = dynamodb.Table(DDB_TABLE_NAME)


@tool
def save_articles_to_dynamodb(
    articles: list[dict],
    author_id: str,
    status: str = "IDEA",
) -> dict:
    """
    Persist a batch of article ideas/outlines to DynamoDB (single-table):
      - PK = USER#{author_id}
      - SK = ARTICLE#{article_id}
    Each article gets a fresh UUID article_id.
    Returns a manifest: {count, sample, ids}
    """


    created_at = now_iso()
    items = []
    ids = []
    sample = []

    for a in articles:
        # Expect at least a title; outline optional but encouraged
        title = (a.get("title") or "").strip()
        outline = a.get("outline") or a.get("sections") or a.get("body") or ""
        if not title:
            # skip bad rows instead of failing the whole batch
            # you can choose to raise instead
            continue

        article_id = str(uuid.uuid4())
        ids.append(article_id)
        if len(sample) < 5:
            sample.append({"title": title, "article_id": article_id})

        item = {
            "PK": f"ARTICLE#{article_id}",
            "SK": f"ARTICLE#{article_id}",
            "GSI1PK": f"USER#{author_id}",
            "GSI1SK": f"ARTICLE#{article_id}",
            "entityType": "ARTICLE",
            "articleId": article_id,
            "title": title,
            "outline": outline,
            "status": status,
            "createdAt": created_at,
            "updatedAt": created_at,
            "keywords": a.get("keywords") or [],
            "notes": a.get("notes") or "",
            "score": a.get("score"),
        }

        items.append(item)

    if not items:
        return {"error": "no valid articles to write (missing titles?)"}

    try:
        with ddb_table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
            for it in items:
                batch.put_item(Item=it)
    except ClientError as e:
        return {"error": f"DynamoDB batch put failed: {e.response['Error']['Message']}"}

    return {"count": len(items), "sample": sample, "ids": ids}

# --- NEW: Tools for saving and retrieving keyword analysis ---
@tool
def save_keyword_report_to_dynamodb(
        keyword_data: dict,
        author_id: str = "anon",
        report_title: str = "Untitled Keyword Report"
) -> dict:
    """
    Persist a keyword analysis report (e.g., from Data4SEO) to DynamoDB.
      - PK = REPORT#{report_id}
      - SK = REPORT#{report_id}
      - GSI1PK = USER#{author_id}
    Returns a manifest: {report_id, title}
    """
    report_id = str(uuid.uuid4())
    created_at = now_iso()

    item = {
        "PK": f"REPORT#{report_id}",
        "SK": f"REPORT#{report_id}",
        "GSI1PK": f"USER#{author_id}",
        "GSI1SK": f"REPORT#{report_id}",
        "entityType": "REPORT",
        "reportId": report_id,
        "title": report_title,
        "authorId": author_id,
        "reportData": keyword_data,
        "createdAt": created_at,
        "updatedAt": created_at,
    }

    try:
        ddb_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK)"
        )
        return {"report_id": report_id, "title": report_title}
    except ClientError as e:
        return {"error": f"DynamoDB put failed: {e.response['Error']['Message']}"}


@tool
def get_keyword_report_from_dynamodb(report_id: str) -> dict:
    """
    Retrieves a specific keyword analysis report from DynamoDB using its report_id.
    """
    try:
        response = ddb_table.get_item(
            Key={"PK": f"REPORT#{report_id}", "SK": f"REPORT#{report_id}"}
        )
        item = response.get("Item")
        if not item:
            return {"error": "Report not found."}

        # Return the key data the next agent needs
        return {
            "report_id": item.get("reportId"),
            "title": item.get("title"),
            "keyword_data": item.get("reportData")
        }
    except ClientError as e:
        return {"error": f"DynamoDB get failed: {e.response['Error']['Message']}"}

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

outline_writer_model = BedrockModel(
    model_id="us.anthropic.claude-opus-4-20250514-v1:0",
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

    # --- 1) Generate ~20 seed keywords from interests ---
    generate_keyword_agent = Agent(
        name="generate_keyword_agent",
        description="Turns user interests into ~20 mid-tail keywords and normalized settings.",
        model=reasoning_model,
        system_prompt=(
            "You convert a user's interests into ~20 high-quality, mid-tail keywords (2–4 words each). "
            "Normalize settings too. Your input contains the original payload.\n\n"
            "INPUT JSON SHAPE (from payload):\n"
            "{\n"
            '  "interests": [string],\n'
            '  "language": "en", ...\n'
            '  "author_details": {"author_id": "..."}\n'
            "}\n\n"
            "OUTPUT JSON SHAPE (for next agent):\n"
            "{\n"
            '  "language_code": "en",\n'
            '  "location_code": 2840,\n'
            '  "date_range": "2024-01-01..2024-12-31",\n'
            '  "keywords": ["... up to ~20 ..."]\n'
            "}\n\n"
            "TASK:\n"
            "1. Generate the output JSON shape based on the input payload.\n"
            "2. **Your FINAL action MUST be to call `use_agent`** to handoff to the next agent.\n"
            "3. Call `use_agent` with:\n"
            "   - agent_name='keywords_volume_agent'\n"
            "   - message='Generated keywords, please fetch volumes.'\n"
            "   - context: {\n"
            "       \"keyword_settings\": <The JSON you just generated>,\n"
            "       \"original_payload\": <The full original payload you received>\n"
            "     }"
        ),
    )

    # --- 2) Fetch volumes via DataForSEO (MCP tools) ---
    # This agent will call your MCP tools. Keep its prompt laser-focused on tool use + shaping outputs.
    all_tools = [*mcp_tools,save_keyword_report_to_dynamodb]

    keywords_volume_agent = Agent(
        name="keywords_volume_agent",
        description="Fetch metrics via Data4SEO MCP, store to file, return only a manifest.",
        model=reasoning_model,
        tools=all_tools,
        system_prompt=(
            "Your INPUT is a JSON context from the previous agent(generate_keyword_agent). It contains 'keyword_settings' and 'original_payload'.\n\n"
            "TASK:\n"
            "1. Extract 'keywords', 'language_code', etc., from 'keyword_settings'.\n"
            "2. Call the DataForSEO MCP keyword volume search tool to get metrics for the keywords.\n"
            "3. Format the tool's raw output into the structured JSON report (e.g., {'task_id': ..., 'results': [...]}).\n"
            "4. Extract 'author_id' from 'original_payload.author_details'. Default to 'anon' if missing.\n"
            "5. Call `save_keyword_report_to_dynamodb` with the JSON report as `keyword_data` and the 'author_id'.\n"
            "6. Generate a human-readable markdown 'summary' of the results.\n"
            "7. **Your FINAL action MUST be to call `use_agent`** to handoff to the outline writer.\n"
            "8. Call `use_agent` with:\n"
            "   - agent_name='outline_writer_agent'\n"
            "   - message='Keyword report is ready, please write outlines.'\n"
            "   - context: {\n"
            "       \"report_id\": <The ID from save_keyword_report_to_dynamodb tool call>,\n"
            "       \"summary\": <Your markdown summary>,\n"
            "       \"original_payload\": <The original_payload you received>\n"
            "     }"
        ),
    )

    # --- 4) Produce titles + outlines for the top picks ---
    outline_writer_agent = Agent(
        name="outline_writer_agent",
        description="Retrieves a keyword report from DynamoDB, generates titles/outlines, and saves them.",
        model=outline_writer_model,
        tools=[save_articles_to_dynamodb,get_keyword_report_from_dynamodb],
        system_prompt=(
            "You are an SEO content strategist. Your INPUT is a JSON context from the previous agent.\n"
            "The input context contains 'report_id' and 'original_payload'.\n\n"
            "TASK:\n"
            "1. Extract the `report_id` from your input context.\n"
            "2. **Your FIRST action MUST be to call `get_keyword_report_from_dynamodb`** using this `report_id`.\n"
            "3. Analyze the 'keyword_data' from the tool's response.\n"
            "4. Generate ~10 SEO-friendly article titles and concise outlines (H2s/H3s) based on the data.\n"
            "5. Extract `author_id` from 'original_payload.author_details'. Default to 'anon' if missing.\n"
            "6. **Your FINAL action MUST be to call `save_articles_to_dynamodb`** with your generated articles and the 'author_id'.\n"
            "7. Return ONLY the manifest (count, sample, ids) from the tool. This is the final output of the swarm."
        ),
    )


    swarm_agent = Swarm(

            nodes=[generate_keyword_agent,keywords_volume_agent,outline_writer_agent],
            entry_point=generate_keyword_agent,
            max_handoffs=10,
            max_iterations=15
        )
    initial_payload: Dict[str, Any] = {
            "interests": ["agentic applications", "edtech", "knowledge bases", "poetry", "deep research", "ai"],
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