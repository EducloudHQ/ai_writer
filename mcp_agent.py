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
    all_tools = [*mcp_tools,save_keyword_report_to_dynamodb]

    keywords_volume_agent = Agent(
        name="keywords_volume_agent",
        description="Fetch metrics via Data4SEO MCP, store to file, return only a manifest.",
        model=reasoning_model,
        tools=all_tools,
        system_prompt=(
        "1.Use the DataForSEO MCP keyword volume search tool to retrieve keyword search volume based on "
        "keywords, language, location and time frame.\n"
        "2. Create a structured JSON similar to this "
        '''
        "task_id": "task-123",
        "results_count": 10,
        "results": [
            {"keyword": "Minimalist Living Tips", "volume": 320, "cpc": 0.31, "competition": "LOW"},
            {"keyword": "Minimalist Home Organization", "volume": 90, "cpc": 0.13, "competition": "LOW"},
            {"keyword": "Minimalist lifestyle benefits", "volume": 5400, "cpc": 0.18, "competition": "LOW"},
            {"keyword": "Creative Writing Exercises", "volume": 880, "cpc": 7.27, "competition": "LOW"},
            {"keyword": "Poetry Writing Prompts", "volume": 1300, "cpc": 10.17, "competition": "LOW"},
            {"keyword": "AI Productivity Tools", "volume": 1000, "cpc": 15.41, "competition": "MEDIUM"},
            # ... other keywords
            {"keyword": "Stoic philosophy practices", "volume": 0, "cpc": 0, "competition": "NONE"},
        '''
        "3. Call `save_keyword_report_to_dynamodb`, passing the full `keyword_data` from step 2 and the `author_id`.\n"
        "4. **Generate a human-readable markdown summary** of the keyword data (like the user's example: Key Observations, lists, etc.).\n"
        "5. Your FINAL response MUST be a JSON object containing two keys:\n"
        "   - 'summary': The human-readable markdown you generated.\n"
        "   - 'report_id': The ID returned by the `save_keyword_report_to_dynamodb` tool.\n"
        "\n"
        "Example Final Response (DO NOT include markdown in the JSON, only in the 'summary' string):\n"
        "{\n"
        '  "summary": "### Keyword Analysis\\nI found that \\"Minimalist lifestyle benefits\\" has the highest volume...\\n\\n### Keywords with Volume:\\n1. **Minimalist Living Tips**: 320\\n...",\n'
        '  "report_id": "abc-123-xyz-789"\n'
        "}"

        ),
    )

    # --- 4) Produce titles + outlines for the top picks ---
    outline_writer_agent = Agent(
        name="outline_writer_agent",
        description="Retrieves a keyword report from DynamoDB, generates titles/outlines, and saves them.",
        model=outline_writer_model,
        tools=[save_articles_to_dynamodb,get_keyword_report_from_dynamodb],
        system_prompt=(
            "You are an SEO content strategist. You write great titles and outlines.\n"
            "INPUT: The user's request will contain a `report_id`.\n\n"
            "TASK:\n"
            "1. **Your FIRST action MUST be to call `get_keyword_report_from_dynamodb`** using the `report_id` from the input.\n"
            "2. Analyze the `keyword_data` retrieved from the tool (e.g., top-performing keywords).\n"
            "3. Generate ~10 SEO-friendly article titles and concise outlines (H2s/H3s) based on that data.\n"
            "4. Extract `author_id` from the original user payload if present, otherwise fallback to 'anon'.\n"
            "5. **Your FINAL action MUST be to call `save_articles_to_dynamodb`** with the list of articles you generated. Include the relevant keywords in the 'keywords' field for each article.\n"
            "6. Return ONLY the manifest (count, sample, ids) from the `save_articles_to_dynamodb` tool.\n"
            "- NEVER paste the full outlines back into chat."
        ),
    )


    swarm_agent = Swarm(

            nodes=[generate_keyword_agent,keywords_volume_agent,outline_writer_agent],
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