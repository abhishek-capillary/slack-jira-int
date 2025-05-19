from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse # For direct responses if needed

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
import uvicorn

from .config import settings, logger
from . import slack_handler
from .jira_client import get_jira_client

# Initialize Slack App
slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)
app_handler = AsyncSlackRequestHandler(slack_app)

# Create FastAPI app
app = FastAPI()

# --- Slack Event Handlers ---

@slack_app.event("message")
async def handle_message_events(event, say, client, body):
    if event.get("channel_type") == "im" and not event.get("bot_id"):
        await slack_handler.handle_message_im(event, say, client, body)

# --- Slack Action Handlers ---

# Specific handler for diagnostics (as suggested by Bolt warning)
@slack_app.action("confirm_create_ticket_action")
async def handle_confirm_create_action_specifically(ack, body, client, say):
    logger.info("SLACK_BOLT SPECIFIC ACTION HANDLER: 'confirm_create_ticket_action' was hit!")
    await ack()
    # Delegate to the main handler logic, or handle directly for testing
    # For now, let's just log and ack to see if this specific handler is reached.
    # In a real scenario, you'd call your existing logic:
    # await slack_handler.handle_interactive_action(ack, body, client, say)
    # For this diagnostic, we'll let the generic one handle the full logic IF this one isn't hit.
    # If this IS hit, then the problem might be how the generic one is registered or prioritized.
    # For now, just ack and send a simple message to see if it works.
    user_id = body.get("user", {}).get("id")
    channel_id = body.get("channel", {}).get("id")
    if user_id and channel_id:
        # This will be a new message, not an update to the original button message.
        # await client.chat_postMessage(channel=channel_id, text="Specific handler for confirm_create_ticket_action was called!")
        # Actually, let's delegate to the proper handler to test the full flow if this specific listener works.
        # The ack() has already been called.
        # The `slack_handler.handle_interactive_action` expects `ack` to be passed to it,
        # but it calls `await ack()` again. This is okay, subsequent acks are no-ops.
        logger.info("SLACK_BOLT SPECIFIC ACTION HANDLER: Delegating to slack_handler.handle_interactive_action")
        await slack_handler.handle_interactive_action(ack, body, client, say) # ack will be called again inside, which is fine
    else:
        logger.error("SLACK_BOLT SPECIFIC ACTION HANDLER: Could not get user_id or channel_id from body.")


# Generic handler for all other actions
@slack_app.action(".*")
async def handle_all_other_actions(ack, body, client, say):
    action_id = body.get("actions")[0].get("action_id") if body.get("actions") else "N/A"
    logger.info(f"SLACK_BOLT GENERIC ACTION HANDLER (.*): Received action_id: '{action_id}'")

    # Avoid double processing if the specific handler for 'confirm_create_ticket_action' was already invoked.
    # This check is a bit of a hack for diagnostics.
    # A cleaner way would be for the specific handler to fully handle it or not exist.
    if action_id == "confirm_create_ticket_action":
        logger.info("SLACK_BOLT GENERIC ACTION HANDLER: 'confirm_create_ticket_action' should have been caught by specific handler. This is unexpected if specific handler is working.")
        # If the specific handler is meant to be the sole handler, this generic one might not even need to call the main logic for it.
        # However, if the specific one is just for logging, then the generic one should proceed.
        # Given the goal is to make `confirm_create_ticket_action` work, let's assume the specific one will delegate.
        # For safety, if it reaches here, let it try to process.

    await slack_handler.handle_interactive_action(ack, body, client, say)


# --- FastAPI Endpoints ---

@app.post("/slack/events")
async def slack_events_endpoint(req: Request):
    logger.info("FASTAPI ENDPOINT: Received request on /slack/events")
    return await app_handler.handle(req)

@app.post("/slack/interactive")
async def slack_interactive_endpoint(req: Request):
    logger.info("FASTAPI ENDPOINT: Received request on /slack/interactive")
    try:
        raw_body = await req.body()
        logger.debug(f"FASTAPI ENDPOINT: /slack/interactive RAW BODY: {raw_body.decode()}")
        logger.debug(f"FASTAPI ENDPOINT: /slack/interactive HEADERS: {req.headers}")
    except Exception as e:
        logger.error(f"FASTAPI ENDPOINT: Error reading body/headers from /slack/interactive: {e}")

    # The app_handler.handle(req) is what dispatches to the @slack_app.action listeners
    response = await app_handler.handle(req)
    logger.info(f"FASTAPI ENDPOINT: /slack/interactive response status: {response.status_code}")
    return response

# --- Application Lifecycle ---

@app.on_event("startup")
async def startup_event():
    logger.info("Application startup...")
    logger.info("Initializing Jira client connection...")
    if get_jira_client():
        logger.info("Jira client connected successfully on startup.")
    else:
        logger.error("Failed to connect to Jira on startup.")
    logger.info(f"Default Jira Project Key: {settings.default_jira_project_key}")
    logger.info(f"Slack Bot Token: {settings.slack_bot_token[:5]}... (masked)")
    logger.info("Application ready.")

@app.get("/")
async def root():
    return {"message": "Jira Slackbot is running!"}

if __name__ == "__main__":
    run_host = "0.0.0.0"
    run_port = 3000
    # if hasattr(settings, 'host'): run_host = settings.host
    # if hasattr(settings, 'port'): run_port = settings.port
    uvicorn.run("app.main:app", host=run_host, port=run_port, reload=True, log_level=settings.app_log_level.lower())
