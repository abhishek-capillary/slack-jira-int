from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
import uvicorn
import asyncio
from typing import List

from .config import settings, logger
from . import slack_handler
from .jira_client import get_jira_client, get_available_jira_projects
from .mcp_models import JiraProject

# Initialize Slack App
slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)
app_handler = AsyncSlackRequestHandler(slack_app)

# Create FastAPI app
app = FastAPI()

jira_projects_cache: List[JiraProject] = []

# --- Slack Event Handlers ---
@slack_app.event("message")
async def handle_message_events(event, say, client, body):
    if event.get("channel_type") == "im" and not event.get("bot_id"):
        await slack_handler.handle_message_im(event, say, client, body, jira_projects_cache)

# --- Slack Action Handlers (for buttons, select menus in messages) ---
@slack_app.action("confirm_create_ticket_action")
async def handle_confirm_create_action_specifically(ack, body, client, say):
    logger.info("SLACK_BOLT ACTION HANDLER: 'confirm_create_ticket_action' was hit!")
    await ack()
    await slack_handler.handle_interactive_action(ack, body, client, say, jira_projects_cache)

@slack_app.action("select_jira_project_action")
async def handle_project_selection_action(ack, body, client, say):
    logger.info("SLACK_BOLT ACTION HANDLER: 'select_jira_project_action' was hit!")
    await ack()
    await slack_handler.handle_interactive_action(ack, body, client, say, jira_projects_cache)

@slack_app.action("select_jira_issue_type_action")
async def handle_issue_type_selection_action(ack, body, client, say):
    logger.info("SLACK_BOLT ACTION HANDLER: 'select_jira_issue_type_action' was hit!")
    await ack()
    await slack_handler.handle_interactive_action(ack, body, client, say, jira_projects_cache)

# Generic action handler for other interactive components in messages
@slack_app.action(".*")
async def handle_all_other_actions(ack, body, client, say):
    await ack()
    action_id = "N/A"
    if body.get("actions") and len(body["actions"]) > 0:
        action_id = body["actions"][0].get("action_id", "N/A")
    logger.info(f"SLACK_BOLT GENERIC ACTION HANDLER (.*): Received action_id: '{action_id}'")
    if action_id in ["confirm_create_ticket_action", "select_jira_project_action", "select_jira_issue_type_action"]:
        logger.warning(f"SLACK_BOLT GENERIC ACTION HANDLER: Action '{action_id}' was caught by generic handler, but a specific handler exists.")
    await slack_handler.handle_interactive_action(ack, body, client, say, jira_projects_cache)

# --- Slack View Submission Handler (for modals) ---
# The callback_id for the modal will be "dynamic_fields_modal"
@slack_app.view("dynamic_fields_modal_submission")
async def handle_dynamic_fields_modal_submission(ack, body, client, view, say, logger_from_bolt): # logger is passed by bolt
    """Handles the submission of the modal for dynamic required fields."""
    logger_from_bolt.info(f"SLACK_BOLT VIEW HANDLER: 'dynamic_fields_modal_submission' received.")
    # Acknowledge the view submission first.
    # If there are validation errors you want to show in the modal, you can pass them in ack().
    # For now, just a basic ack. If processing fails, send a message.
    await ack()

    # Delegate the actual processing to a function in slack_handler
    # Pass necessary arguments: body (contains user, view state), client, say (to send messages)
    await slack_handler.handle_dynamic_fields_submission(body, client, say, logger_from_bolt)


# --- FastAPI Endpoints ---
@app.post("/slack/events")
async def slack_events_endpoint(req: Request):
    logger.info("FASTAPI ENDPOINT: Received request on /slack/events")
    return await app_handler.handle(req)

@app.post("/slack/interactive") # Handles both block_actions and view_submission if not separated by Bolt
async def slack_interactive_endpoint(req: Request):
    logger.info("FASTAPI ENDPOINT: Received request on /slack/interactive")
    # The app_handler will route to @slack_app.action or @slack_app.view based on payload type
    try:
        raw_body = await req.body() # For debugging
        logger.debug(f"FASTAPI ENDPOINT: /slack/interactive RAW BODY: {raw_body.decode()}")
    except Exception as e:
        logger.error(f"Error reading body from /slack/interactive: {e}")

    response = await app_handler.handle(req)
    logger.info(f"FASTAPI ENDPOINT: /slack/interactive response status: {response.status_code}")
    return response

# --- Application Lifecycle ---
@app.on_event("startup")
async def startup_event():
    logger.info("Application startup...")
    if get_jira_client():
        logger.info("Jira client connected successfully on startup.")
        logger.info("Fetching available Jira projects on startup...")
        projects = await get_available_jira_projects()
        if projects:
            logger.info(f"Found {len(projects)} Jira projects.")
            global jira_projects_cache
            jira_projects_cache = projects
            for proj in jira_projects_cache: logger.info(f"  - Cached Project Key: {proj.key}, Name: {proj.name}, ID: {proj.id}")
        else: logger.warning("No Jira projects found or failed to fetch projects on startup.")
    else: logger.error("Failed to connect to Jira on startup. Project list will not be fetched.")
    logger.info(f"Default Jira Project Key: {settings.default_jira_project_key}")
    logger.info(f"Slack Bot Token: {settings.slack_bot_token[:5]}... (masked)")
    logger.info("Application ready.")

@app.get("/")
async def root(): return {"message": "Jira Slackbot is running!"}

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=3000, reload=True, log_level=settings.app_log_level.lower())
