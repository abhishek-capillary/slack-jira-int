from fastapi import FastAPI, Request, HTTPException
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
import uvicorn # For running the app

from .config import settings, logger # Your centralized config and logger
from . import slack_handler # Import your Slack event handlers
from .jira_client import get_jira_client # To initialize Jira client on startup if needed
# from .mcp_models import BotStateData # If you plan to use it more globally

# Initialize Slack App with signing secret and token
slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
    # You can enable request verification explicitly if needed, but Bolt does it
)
app_handler = AsyncSlackRequestHandler(slack_app)

# Create FastAPI app
app = FastAPI()

# --- Register Slack Event Handlers ---
# These are wired up to functions in slack_handler.py

# Handle 'message.im' events (Direct Messages to the bot)
@slack_app.event("message") # More generic, bolt will filter for DMs if bot is only in DMs
async def handle_all_messages(event, say, client, body):
    # Bolt's 'message' listener can get all messages.
    # We are primarily interested in DMs. 'message.im' is specific to DMs.
    # Check if it's a DM and not from a bot
    if event.get("channel_type") == "im" and not event.get("bot_id"):
        # Delegate to the specific DM handler logic
        await slack_handler.handle_message_im(event, say, client, body)
    # You can add other channel_type handlers or app_mention here if needed

# Handle interactive components (buttons, menus, modals)
@slack_app.action(".*") # Regex to catch all action_ids
async def handle_all_actions(ack, body, client, say):
    await slack_handler.handle_interactive_action(ack, body, client, say)

# Handle modal view submissions (if you add modals)
# @slack_app.view(/.*/) # Regex to catch all callback_ids for views
# async def handle_all_view_submissions(ack, body, client, view, say):
#     # Delegate to a view handler in slack_handler.py
#     await slack_handler.handle_view_submission(ack, body, client, view, say, logger)


# --- FastAPI Endpoints for Slack ---

@app.post("/slack/events")
async def slack_events_endpoint(req: Request):
    """
    Endpoint for Slack Events API.
    All events (like messages, app mentions) will be sent here.
    """
    return await app_handler.handle(req)

@app.post("/slack/interactive")
async def slack_interactive_endpoint(req: Request):
    """
    Endpoint for Slack Interactivity & Shortcuts.
    Button clicks, modal submissions, etc., will be sent here.
    """
    return await app_handler.handle(req)

# --- Application Startup Event ---
@app.on_event("startup")
async def startup_event():
    logger.info("Application startup...")
    logger.info("Initializing Jira client connection...")
    if get_jira_client(): # Attempt to initialize and test connection
        logger.info("Jira client connected successfully on startup.")
    else:
        logger.error("Failed to connect to Jira on startup. Check credentials and Jira server reachability.")
    logger.info(f"Default Jira Project Key: {settings.default_jira_project_key}")
    logger.info(f"Slack Bot Token: {settings.slack_bot_token[:5]}... (masked)") # Be careful logging tokens
    logger.info("Application ready.")


# --- Basic Health Check Endpoint ---
@app.get("/")
async def root():
    return {"message": "Jira Slackbot is running!"}

# --- To run the app (for local development without Docker) ---
# You would typically run this using: uvicorn app.main:app --reload --port 3000
# The Dockerfile/docker-compose.yml will handle this in a containerized environment.
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0", # Listen on all available IPs
        port=int(settings.port) if hasattr(settings, 'port') else 3000, # From .env or default
        reload=True, # Enable auto-reload for development
        log_level=settings.app_log_level.lower()
    )