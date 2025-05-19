from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
import uvicorn
import asyncio
from typing import List # For type hinting

from .config import settings, logger
from . import slack_handler
from .jira_client import get_jira_client, get_available_jira_projects
from .mcp_models import JiraProject # Import JiraProject for caching

# Initialize Slack App
slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)
app_handler = AsyncSlackRequestHandler(slack_app)

# Create FastAPI app
app = FastAPI()

# --- Global Cache for Jira Projects ---
# This list will be populated on startup and can be accessed by handlers.
jira_projects_cache: List[JiraProject] = []

# --- Slack Event Handlers ---

@slack_app.event("message")
async def handle_message_events(event, say, client, body):
    """Handles incoming messages to the bot."""
    # Filter for direct messages (im) and ensure it's not from a bot
    if event.get("channel_type") == "im" and not event.get("bot_id"):
        # Pass the cached projects to the handler
        await slack_handler.handle_message_im(event, say, client, body, jira_projects_cache)

# --- Slack Action Handlers ---

@slack_app.action("confirm_create_ticket_action")
async def handle_confirm_create_action_specifically(ack, body, client, say):
    """Handles the specific action for confirming ticket creation."""
    logger.info("SLACK_BOLT SPECIFIC ACTION HANDLER: 'confirm_create_ticket_action' was hit!")
    await ack() # Acknowledge the action immediately
    logger.info("SLACK_BOLT SPECIFIC ACTION HANDLER: Delegating to slack_handler.handle_interactive_action")
    # Pass cached projects if the interactive handler might need them (though usually state is preferred for multi-step)
    await slack_handler.handle_interactive_action(ack, body, client, say, jira_projects_cache)


@slack_app.action("select_jira_project_action") # New specific action for project selection
async def handle_project_selection_action(ack, body, client, say):
    """Handles the specific action for when a user selects a Jira project from a dropdown."""
    logger.info("SLACK_BOLT SPECIFIC ACTION HANDLER: 'select_jira_project_action' was hit!")
    await ack() # Acknowledge the action immediately
    logger.info("SLACK_BOLT SPECIFIC ACTION HANDLER: Delegating project selection to slack_handler.handle_interactive_action")
    await slack_handler.handle_interactive_action(ack, body, client, say, jira_projects_cache)


@slack_app.action(".*") # Generic handler for any other actions not caught by specific handlers
async def handle_all_other_actions(ack, body, client, say):
    """A generic handler for Slack actions that are not specifically handled above."""
    action_id = "N/A"
    if body.get("actions") and len(body["actions"]) > 0:
        action_id = body["actions"][0].get("action_id", "N/A")

    logger.info(f"SLACK_BOLT GENERIC ACTION HANDLER (.*): Received action_id: '{action_id}'")

    # This check helps in understanding if a specific action accidentally fell through
    # or if this is genuinely an action not covered by a specific decorator.
    if action_id in ["confirm_create_ticket_action", "select_jira_project_action"]:
        logger.warning(f"SLACK_BOLT GENERIC ACTION HANDLER: Action '{action_id}' was caught by generic handler, but a specific handler exists. This might indicate an issue in handler ordering or registration if the specific handler was expected to exclusively handle it.")
        # Depending on design, you might choose to NOT process it here if specific handlers are meant to be exclusive.
        # For now, we let it delegate to allow flexibility during development.

    await slack_handler.handle_interactive_action(ack, body, client, say, jira_projects_cache)


# --- FastAPI Endpoints ---

@app.post("/slack/events")
async def slack_events_endpoint(req: Request):
    """Endpoint for Slack Events API (e.g., new messages)."""
    logger.info("FASTAPI ENDPOINT: Received request on /slack/events")
    return await app_handler.handle(req)

@app.post("/slack/interactive")
async def slack_interactive_endpoint(req: Request):
    """Endpoint for Slack Interactivity & Shortcuts (e.g., button clicks, dropdown selections)."""
    logger.info("FASTAPI ENDPOINT: Received request on /slack/interactive")
    try:
        # Log raw body for debugging purposes
        raw_body = await req.body()
        logger.debug(f"FASTAPI ENDPOINT: /slack/interactive RAW BODY: {raw_body.decode()}")
        logger.debug(f"FASTAPI ENDPOINT: /slack/interactive HEADERS: {dict(req.headers)}")
    except Exception as e:
        logger.error(f"FASTAPI ENDPOINT: Error reading body/headers from /slack/interactive: {e}", exc_info=True)

    # The app_handler.handle(req) dispatches to the @slack_app.action listeners
    response = await app_handler.handle(req)
    logger.info(f"FASTAPI ENDPOINT: /slack/interactive response status: {response.status_code}")
    return response

# --- Application Lifecycle ---

@app.on_event("startup")
async def startup_event():
    """Actions to perform when the application starts up."""
    logger.info("Application startup...")
    logger.info("Initializing Jira client connection...")
    if get_jira_client(): # Initializes and tests connection
        logger.info("Jira client connected successfully on startup.")

        # Fetch and cache Jira projects
        logger.info("Fetching available Jira projects on startup...")
        projects = await get_available_jira_projects() # This is an async function
        if projects:
            logger.info(f"Found {len(projects)} Jira projects.")
            global jira_projects_cache # Declare intent to modify the global variable
            jira_projects_cache = projects # Store fetched projects in the cache
            for proj in jira_projects_cache:
                logger.info(f"  - Cached Project Key: {proj.key}, Name: {proj.name}, ID: {proj.id}")
        else:
            logger.warning("No Jira projects found or failed to fetch projects on startup.")
    else:
        logger.error("Failed to connect to Jira on startup. Project list will not be fetched.")

    logger.info(f"Default Jira Project Key (will be overridden by user selection if feature is used): {settings.default_jira_project_key}")
    logger.info(f"Slack Bot Token: {settings.slack_bot_token[:5]}... (masked for security)")
    logger.info("Application ready.")

@app.get("/")
async def root():
    """Basic health check endpoint."""
    return {"message": "Jira Slackbot is running!"}

# --- Main execution block for running with Uvicorn directly ---
if __name__ == "__main__":
    # These host/port settings are for direct execution (e.g., python app/main.py)
    # Uvicorn CLI command usually overrides these.
    run_host = "0.0.0.0"
    run_port = 3000
    # Example: uncomment and use if host/port are defined in your Settings model
    # if hasattr(settings, 'host') and settings.host: run_host = settings.host
    # if hasattr(settings, 'port') and settings.port: run_port = settings.port

    uvicorn.run(
        "app.main:app", # Path to the FastAPI app instance
        host=run_host,
        port=run_port,
        reload=True, # Enable auto-reload for development
        log_level=settings.app_log_level.lower() # Set log level from settings
    )
