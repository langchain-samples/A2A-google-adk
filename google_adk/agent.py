"""Minimal Google ADK agent exposed via A2A protocol.

This agent demonstrates a simple calculator agent that can perform
basic mathematical operations and is exposed via the A2A protocol.
Includes OpenTelemetry tracing to LangSmith for distributed tracing.
"""

from google.adk import Agent
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.genai import types
from google.adk.models.lite_llm import LiteLlm
from dotenv import load_dotenv
import os
from fastapi import Request
from opentelemetry import trace
from opentelemetry.context import set_value, attach

# Configure OpenTelemetry tracing to LangSmith
from langsmith.integrations.otel import configure

load_dotenv()

# Configure LangSmith tracing with the same project name as other agents
# Project name can be overridden via LANGSMITH_PROJECT environment variable
project_name = os.getenv("LANGSMITH_PROJECT", "a2a-distributed-tracing")
configure(project_name=project_name)

# Optional: Instrument Google ADK for OpenTelemetry tracing
# Note: This requires openinference package which may not be available yet
# The langsmith.integrations.otel.configure() above provides basic tracing
try:
    from openinference.instrumentation.google_adk import GoogleADKInstrumentor
    GoogleADKInstrumentor().instrument()
except ImportError:
    # If openinference is not available, basic tracing via langsmith will still work
    print("OpenInference not available, using basic tracing via LangSmith")
    pass

def calculate(expression: str) -> str:
    """Evaluate a mathematical expression safely.

    Args:
        expression: A string containing a mathematical expression (e.g., "2 + 2", "10 * 5").

    Returns:
        A string with the result of the calculation or an error message.
    """
    try:
        # Use eval with a restricted namespace for basic math operations
        allowed_names = {
            "__builtins__": {},
            "abs": abs,
            "round": round,
            "min": min,
            "max": max,
            "sum": sum,
            "pow": pow,
        }
        result = eval(expression, allowed_names)
        return f"The result is: {result}"
    except Exception as e:
        return f"Error calculating expression: {str(e)}"


llm = LiteLlm(
    model="openai/gpt-4o",
    api_base="https://api.openai.com/v1",
    api_key=os.getenv("OPENAI_API_KEY"),
)

# Create the agent
root_agent = Agent(
    model=llm,
    name="calculator_agent",
    description="A simple calculator agent that can perform basic mathematical operations.",
    instruction="""
        You are a helpful calculator assistant. When users ask you to perform calculations,
        use the calculate tool with a mathematical expression as a string.
        
        Examples:
        - "What is 5 + 3?" -> call calculate("5 + 3")
        - "Calculate 10 * 7" -> call calculate("10 * 7")
        - "What's 100 / 4?" -> call calculate("100 / 4")
        
        Always use the calculate tool for any mathematical operations. Be friendly and clear
        in your responses.
    """,
    tools=[calculate],
    generate_content_config=types.GenerateContentConfig(
        temperature=0.7,
    ),
)

# Expose the agent via A2A protocol
# This creates an A2A-compatible FastAPI app that can be served with uvicorn
a2a_app = to_a2a(root_agent, port=8002)

# Add middleware to extract session_id from metadata and set as thread_id in OpenTelemetry
@a2a_app.middleware("http")
async def set_thread_id_middleware(request: Request, call_next):
    """Extract session_id from metadata and set as thread_id in OpenTelemetry spans."""
    tracer = trace.get_tracer(__name__)
    
    thread_id = None
    if request.method == "POST":
        try:
            body_bytes = await request.body()
            if body_bytes:
                import json
                body = json.loads(body_bytes)
                if "metadata" in body:
                    thread_id = body["metadata"].get("thread_id")
                async def receive():
                    return {"type": "http.request", "body": body_bytes}
                request._receive = receive
        except:
            pass
    
    if thread_id:
        ctx = set_value("thread_id", thread_id)
        token = attach(ctx)
    else:
        token = None
    
    try:
        with tracer.start_as_current_span("google_adk_agent") as span:
            if thread_id:
                span.set_attribute("langsmith.metadata.thread_id", thread_id)
            response = await call_next(request)
            return response
    finally:
        if token:
            from opentelemetry.context import detach
            detach(token)
