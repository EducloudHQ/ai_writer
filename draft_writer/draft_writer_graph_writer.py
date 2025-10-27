import os
import json
import uuid
import pathlib
import datetime
import logging
from typing import Any
from datetime import datetime, timezone

# --- Strands & AWS Imports ---
import boto3
from botocore.exceptions import ClientError
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

# NEW: Import Graph for workflow
from strands.multiagent import GraphBuilder

# --- Tool Imports ---
from strands_tools import memory, use_agent
from strands_tools.tavily import tavily_search, tavily_extract

# REMOVED: from strands_tools.use_agent import use_agent (no longer needed)
# REMOVED: from strands.multiagent import Swarm (no longer needed)

# ---------- ENV ----------
DDB_TABLE_NAME = os.getenv("DDB_TABLE_NAME", "ai-writer-table")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
ddb_table = dynamodb.Table(DDB_TABLE_NAME)
os.environ['TAVILY_API_KEY'] = ""
STRANDS_KB_ID = os.getenv("STRANDS_KB_ID", "GTD2UBSGBU")

KB_ID = "GTD2UBSGBU"
REGION = "us-east-1"

logging.getLogger("email_agent").setLevel(logging.DEBUG)
logging.basicConfig(format="%(levelname)s | %(name)s | %(message)s",
                    handlers=[logging.StreamHandler()])

app = BedrockAgentCoreApp()

retriever_model = BedrockModel(model_id="us.amazon.nova-pro-v1:0",
                               region_name='us-east-1',

                               top_p=1,
                               temperature=0.2)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- Tools (Unchanged) ----------
@tool
def save_draft_to_dynamodb(
        title: str,
        markdown: str,
        author_id: str,
        article_id: str | None = None,
        outline: Any = None,
        brief: dict | None = None,
        status: str = "DRAFT"
) -> dict:
    try:
        # ALWAYS strings
        author_id = (author_id or "anon").strip()
        article_id = (article_id or "").strip() or str(uuid.uuid4())
        draft_id = str(uuid.uuid4())
        created_at = now_iso()

        item = {
            "PK": f"DRAFT#{draft_id}",
            "SK": f"DRAFT#{draft_id}",
            "GSI2PK": f"USER#{author_id}",
            "GSI2SK": f"DRAFT#{draft_id}",
            "entityType": "DRAFT",
            "draftId": draft_id,
            "articleId": article_id,
            "title": (title or "").strip(),
            "status": status,
            "outline": outline if outline is not None else "",
            "markdown": markdown,
            "brief": brief or {},
            "createdAt": created_at,
            "updatedAt": created_at,
        }

        ddb_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            ReturnConsumedCapacity="TOTAL"
        )
        return {"pk": item["PK"], "sk": item["SK"], "draftId": draft_id, "articleId": article_id}
    except ClientError as e:
        return {"error": f"DynamoDB put failed: {e.response['Error']['Message']}"}


# REMOVED: handoff_for_writing_draft tool is no longer needed

# ---------- Agents (with Simplified Prompts) ----------

research_agent = Agent(
    name="research_agent",
    description="Search the web and return a concise brief (bullets + citations).",
    model=retriever_model,
    tools=[tavily_search, tavily_extract],
    system_prompt=(
        "You are a helpful research assistant.\n"
        "Your input is a JSON object containing at least 'title' and 'outline'.\n\n"
        "DO:\n"
        f"1) Use the 'title' and 'outline' to perform a search using tavily_search(topic='general', max_results=8, include_raw_content='markdown').\n"
        "2) Optionally call tavily_extract on top 3–5 URLs.\n"
        "3) Return STRICT JSON: { 'brief': { 'key_points':[], 'stats':[], 'sources':[] } }.\n"
        "RULES:\n"
        "- Keep key_points <= 10; use authoritative sources.\n"
        # REMOVED: All "AFTER YOU RETURN..." handoff instructions
    ),
)

kb_enricher_agent = Agent(
    name="kb_enricher_agent",
    description="Retrieves relevant notes, facts, and brand voice from the KB.",
    model=retriever_model,
    tools=[memory,use_agent],
    system_prompt=(
        f"You retrieve content from AWS Knowledge base with ID={KB_ID} in {REGION}.\n"
        "Your input is a JSON object containing 'title', 'outline', and 'research_brief'.\n"
        "Use the 'title' and 'outline' as search queries for the KB.\n\n"
        "DO:\n"
        "1. Always call the 'memory' tool with a retrieve action to find relevant information from the knowledge base."
        "Set topK to 10\n"
        "   Ensure you pass STRANDS_KNOWLEDGE_BASE_ID and region_name.\n"
        "2. Summarize the findings.\n"
        "3. Return STRICT JSON: { 'kb_brief': { 'key_points':[], 'snippets':[], "
        "'brand_voice':{tone,audience,style_rules[]}, 'sources':[] } }.\n"
        "RULES:\n"
        "- If nothing relevant is found, return empty arrays for points, snippets, and sources.\n"

    ),
)

draft_writer_agent = Agent(
    name="draft_writer_agent",
    description="Write a first draft using the outline and research+KB brief, then save it.",
    model=retriever_model,
    tools=[save_draft_to_dynamodb],
    system_prompt=(
        """
        You are an excellent long-form writer.

        Your input is a JSON object containing:
        { 'title': str, 'outline': any, 'brief': {...},
          'article_id': str?, 'author_details': { 'author_id': str }? }

        TASK:
        - Write the full article (~1200–1600 words) using H2/H3 from 'outline' with intro, FAQs, and conclusion.
        - Use only supported facts from the 'brief'; omit unknowns.
        - After writing, set:
          - author_id = author_details.author_id if present else 'anon'
          - article_id = provided article_id if present else generate a new one
        - CALL `save_draft_to_dynamodb` with exactly:
          { title, markdown, outline, brief, author_id, article_id }.

        PRIVACY & OUTPUT RULES:
        - Do NOT include the full markdown in your response to the user.
        - Only pass {title, markdown, outline, brief, author_id, article_id} to the tool.
        - Your final response to the user must be a VERY THIN JSON summary (no excerpted paragraphs), strictly:
          {
            "summary": "<one short sentence (<= 25 words) describing the draft’s focus>",
            "word_count": <int>,
            "sections": ["<H2 1>", "<H2 2>", "..."],  
            "ddb": { ... }  // This is the output from the save_draft_to_dynamodb tool
          }
        - Do not include quotes from the draft, sample paragraphs, or large text. Keep 'summary' minimal and generic.
        """
    ),
)


# ---------- NEW: Graph Node Functions ----------
# These functions wrap agent calls and manage the state dictionary.
# The 'payload' argument is the current state of the workflow.
# They return a dictionary of keys/values to update the state with.



# ---------- NEW: Define the Workflow Graph ----------
workflow =  GraphBuilder()

# 1. Add nodes
workflow.add_node(research_agent,"research")
workflow.add_node(kb_enricher_agent,"kb_enrich")

workflow.add_node(draft_writer_agent,"write_draft")

# 2. Define the flow (edges)
workflow.add_edge("research", "kb_enrich")
workflow.add_edge("kb_enrich", "write_draft")

# Set entry points (optional - will be auto-detected if not specified)
workflow.set_entry_point("research")

# Optional: Configure execution limits for safety
workflow.set_execution_timeout(600)   # 10 minute timeout

# Build the graph
graph = workflow.build()



@app.entrypoint
def invoke(payload):
    """
    Invokes the compiled workflow.
    The 'payload' is the initial state for the graph.
    Example payload:
    {
        "title": "The Future of AI in Writing",
        "outline": ["Introduction", "AI Tools", "Impact on SEO", "Conclusion"],
        "author_details": { "author_id": "user-123" }
    }
    """
    logging.info(f"Starting workflow with payload: {payload}")

    # REMOVED: Swarm definition and invocation

    try:

        final_result = graph("research and write a good draft on" + json.dumps(payload))

        logging.info(f"Workflow finished. Result: {final_result}")
        return final_result

    except Exception as e:
        logging.error(f"An exception occurred during workflow execution: {e}", exc_info=True)
        return {"error": str(e)}


if __name__ == "__main__":
    app.run()