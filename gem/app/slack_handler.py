from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_sdk.web.async_client import AsyncWebClient # For type hinting mostly with bolt
from .config import settings, logger
from .mcp_models import (
    InitialRequestContext, SlackContext, EnrichedTicketContext, ParsedTicketDetails,
    SimilarityCheckContext, FinalTicketCreationContext, JiraTicketData,
    CreationConfirmationContext, SimilarTicketInfo
)
from .nlp_service import extract_ticket_details_from_text, generate_keywords_from_text, get_semantic_similarity_score
from .jira_client import create_jira_ticket, search_similar_jira_tickets
from typing import Dict, Any, List, Optional # Added Optional and List

# Initialize Slack App
# It's better to initialize this in main.py and pass it around or use a global app object
# For now, let's assume it will be initialized in main.py
# slack_app = AsyncApp(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)
# app_handler = AsyncSlackRequestHandler(slack_app)


# In-memory store for conversation state (for multi-step interactions)
# For production, consider Redis or a database
# Key: user_id-channel_id, Value: BotStateData (or dict representation of MCP models)
conversation_state_store: Dict[str, Any] = {} # Replace Any with BotStateData from mcp_models if you define it

async def handle_message_im(event: Dict, say, client: AsyncWebClient, body: Dict):
    """
    Handles direct messages to the bot. This is the entry point for most interactions.
    event: The actual event payload for 'message.im'
    say: Utility function to send a message to the same channel.
    client: Slack AsyncWebClient instance.
    body: Full request body from Slack.
    """
    user_id = event.get("user")
    channel_id = event.get("channel") # This is the DM channel ID
    text = event.get("text", "").strip()
    timestamp = event.get("ts")
    team_id = body.get("team_id")

    if not user_id or not text:
        logger.warning("Received message.im event with no user or text.")
        return

    # Avoid responding to own messages or bot messages if any slip through
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    logger.info(f"Received DM from user {user_id} in channel {channel_id}: '{text}'")

    # MCP Stage 1: Initial Request Parsing Context
    slack_ctx = SlackContext(user_id=user_id, channel_id=channel_id, team_id=team_id)
    initial_request = InitialRequestContext(
        event_type="message.im",
        user_id=user_id,
        channel_id=channel_id,
        text=text,
        timestamp=timestamp,
        slack_context=slack_ctx
    )
    logger.debug(f"MCP Stage 1: {initial_request.model_dump_json(indent=2)}")

    # Acknowledge receipt (optional, good for long processing)
    # await say("Got it! Processing your request...")

    # MCP Stage 2: NLP Processing to get EnrichedTicketContext
    parsed_details: Optional[ParsedTicketDetails] = await extract_ticket_details_from_text(text)

    if not parsed_details:
        await client.chat_postMessage(channel=channel_id, text="I had trouble understanding the details for the Jira ticket. Could you please try rephrasing or be more specific about the summary, description, and type (Bug, Task, Story)?")
        return

    enriched_context = EnrichedTicketContext(
        slack_context=slack_ctx,
        raw_request=text,
        parsed_ticket_details=parsed_details,
        status="pending_similarity_check"
    )
    logger.debug(f"MCP Stage 2: {enriched_context.model_dump_json(indent=2)}")

    # MCP Stage 3: Similarity Check
    # Basic keyword search first
    # For more advanced keywords, you can use the generate_keywords_from_text function
    # keywords = await generate_keywords_from_text(parsed_details.summary + " " + parsed_details.description)
    # For now, let's use summary as a search term

    # Use default project key or one derived if logic exists
    project_key_to_search = settings.default_jira_project_key

    similar_jira_issues: List[SimilarTicketInfo] = await search_similar_jira_tickets(
        project_key=project_key_to_search,
        summary=parsed_details.summary, # Or use specific keywords
        issue_types=[parsed_details.issue_type] # Search in same issue type initially
    )

    # Optional: Advanced Semantic Similarity with Claude if basic search yields results
    # This adds latency and cost, enable as per plan Task 4.1
    scored_similar_issues: List[SimilarTicketInfo] = []
    if similar_jira_issues and False: # Set to True to enable semantic scoring
        logger.info("Performing semantic similarity scoring on found tickets...")
        for ticket_info in similar_jira_issues[:3]: # Score top N results
            # Ensure your SimilarTicketInfo from search_similar_jira_tickets includes description if needed by get_semantic_similarity_score
            # For now, assuming get_semantic_similarity_score can work with summary if description isn't readily available or needed
            # If ticket_info contains 'description', use it. Otherwise, adapt or use summary.
            # This example assumes 'ticket_info.summary' is sufficient for a coarse comparison or NLP can handle it.
            # You might need to fetch full descriptions for better semantic scoring if not already present.
            # For this example, we'll compare the new request's description with the found ticket's summary.
            score = await get_semantic_similarity_score(parsed_details.description, ticket_info.summary)
            if score is not None and score > 0.6: # Threshold from plan (e.g., 0.75, adjust as needed)
                ticket_info.score = score # Add score to the model instance
                scored_similar_issues.append(ticket_info)
        scored_similar_issues.sort(key=lambda t: t.score if t.score is not None else 0.0, reverse=True)
        logger.info(f"Scored similar issues: {scored_similar_issues}")
        # Replace similar_jira_issues with scored_similar_issues if you use this semantic scoring
        if scored_similar_issues: # Only use if we got any scores above threshold
             similar_jira_issues_to_present = scored_similar_issues
        else: # Fallback to keyword search results if semantic doesn't yield better ones
             similar_jira_issues_to_present = similar_jira_issues
    else:
        similar_jira_issues_to_present = similar_jira_issues


    similarity_context = SimilarityCheckContext(
        enriched_context=enriched_context,
        similar_tickets_found=similar_jira_issues_to_present,
        status="pending_user_decision_on_similarity" if similar_jira_issues_to_present else "pending_confirmation"
    )
    logger.debug(f"MCP Stage 3: {similarity_context.model_dump_json(indent=2)}")

    state_key = f"{user_id}-{channel_id}" # Simple state key

    if similarity_context.similar_tickets_found:
        conversation_state_store[state_key] = similarity_context.model_dump()
        blocks = build_similarity_check_blocks(similarity_context)
        await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Found some existing tickets that might be similar.")
    else:
        final_ticket_ctx = FinalTicketCreationContext(
            slack_context=slack_ctx,
            jira_ticket_data=JiraTicketData(
                project_key=settings.default_jira_project_key,
                summary=parsed_details.summary,
                description=f"{parsed_details.description}\n\n---\nRequested by Slack User: <@{user_id}>",
                issue_type_name=parsed_details.issue_type
            ),
            status="ready_for_creation"
        )
        conversation_state_store[state_key] = final_ticket_ctx.model_dump()
        blocks = build_pre_creation_confirmation_blocks(final_ticket_ctx)
        await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Please confirm the details for the new Jira ticket.")


def build_similarity_check_blocks(context: SimilarityCheckContext) -> List[Dict]:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"I found some existing Jira tickets that might be similar to your request for: *'{context.enriched_context.parsed_ticket_details.summary}'*."
            }
        },
        {"type": "divider"}
    ]
    for ticket in context.similar_tickets_found[:3]: # Show top 3
        score_text = f" (Similarity: {ticket.score:.2f})" if ticket.score is not None else ""
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{ticket.url}|{ticket.key}>*: {ticket.summary}{score_text}"
            }
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"This is a duplicate of {ticket.key}"},
                    "action_id": "mark_duplicate",
                    "value": ticket.key,
                    # "style": "danger" # Consider style based on your preference
                }
            ]
        })
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Create New Ticket Anyway"},
                "action_id": "create_new_ticket_anyway",
                "value": "create_new_ticket_anyway",
                "style": "primary"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Cancel"},
                "action_id": "cancel_creation_similarity",
                "value": "cancel_creation_similarity",
            }
        ]
    })
    return blocks

def build_pre_creation_confirmation_blocks(context: FinalTicketCreationContext) -> List[Dict]:
    ticket = context.jira_ticket_data
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Please review and confirm the details for the new Jira ticket:"
            }
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Project:*\n{ticket.project_key}"},
                {"type": "mrkdwn", "text": f"*Type:*\n{ticket.issue_type_name}"},
                {"type": "mrkdwn", "text": f"*Summary:*\n{ticket.summary}"}
            ]
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Description:*\n{ticket.description}"
            }
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Confirm & Create Ticket"},
                    "action_id": "confirm_create_ticket_action",
                    "value": "confirm_create",
                    "style": "primary"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": "cancel_creation_confirmation",
                    "value": "cancel_creation_confirmation",
                }
            ]
        }
    ]
    return blocks


async def handle_interactive_action(ack, body: Dict, client: AsyncWebClient, say):
    await ack()

    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"] # For DMs, this is the DM channel ID
    action = body["actions"][0]
    action_id = action["action_id"]
    action_value = action.get("value")

    logger.info(f"Interactive action triggered: user={user_id}, channel={channel_id}, action_id='{action_id}', value='{action_value}'")

    state_key = f"{user_id}-{channel_id}"
    current_state_dict = conversation_state_store.get(state_key)

    if not current_state_dict:
        await client.chat_postMessage(channel=channel_id, text="Sorry, I couldn't find the context for this action. Please try starting over.")
        return

    original_message_ts = body["message"]["ts"]

    if action_id == "create_new_ticket_anyway":
        try:
            similarity_context = SimilarityCheckContext.model_validate(current_state_dict)
            enriched_context = similarity_context.enriched_context
            final_ticket_ctx = FinalTicketCreationContext(
                slack_context=enriched_context.slack_context,
                jira_ticket_data=JiraTicketData(
                    project_key=settings.default_jira_project_key,
                    summary=enriched_context.parsed_ticket_details.summary,
                    description=f"{enriched_context.parsed_ticket_details.description}\n\n---\nRequested by Slack User: <@{user_id}>",
                    issue_type_name=enriched_context.parsed_ticket_details.issue_type
                ),
                status="ready_for_creation"
            )
            conversation_state_store[state_key] = final_ticket_ctx.model_dump()
            blocks = build_pre_creation_confirmation_blocks(final_ticket_ctx)

            # Replace original message with similarity check with the confirmation message
            await client.chat_update(
                channel=channel_id,
                ts=original_message_ts,
                blocks=blocks,
                text="Please confirm ticket details:"
            )
        except Exception as e:
            logger.error(f"Error processing 'create_new_ticket_anyway': {e}")
            await client.chat_postMessage(channel=channel_id, text="An error occurred. Please try again.")

    elif action_id == "mark_duplicate":
        ticket_key_duplicate = action_value
        await client.chat_update(
            channel=channel_id,
            ts=original_message_ts,
            text=f"Okay, I've noted that this is a duplicate of {ticket_key_duplicate}. I won't create a new ticket.",
            blocks=[] # Clear blocks
        )
        conversation_state_store.pop(state_key, None)

    elif action_id == "cancel_creation_similarity" or action_id == "cancel_creation_confirmation":
        await client.chat_update(
            channel=channel_id,
            ts=original_message_ts,
            text="Okay, I've cancelled the ticket creation process.",
            blocks=[] # Clear blocks
        )
        conversation_state_store.pop(state_key, None)

    elif action_id == "confirm_create_ticket_action":
        try:
            final_ticket_context = FinalTicketCreationContext.model_validate(current_state_dict)
            logger.debug(f"MCP Stage 4: {final_ticket_context.model_dump_json(indent=2)}")

            await client.chat_update(channel=channel_id, ts=original_message_ts, text="Creating your Jira ticket, please wait...", blocks=[])

            created_ticket_info: Optional[CreatedTicketInfo] = await create_jira_ticket(final_ticket_context.jira_ticket_data)

            if created_ticket_info:
                confirmation_ctx = CreationConfirmationContext(
                    slack_context=final_ticket_context.slack_context,
                    created_ticket_info=created_ticket_info,
                    status="ticket_created_successfully"
                )
                logger.debug(f"MCP Stage 5: {confirmation_ctx.model_dump_json(indent=2)}")
                # Post as a new message for the final confirmation
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f"✅ Success! I've created Jira ticket <{created_ticket_info.url}|{created_ticket_info.key}> for you."
                )
                # Optionally delete the "Creating your Jira ticket..." message if it was an update
                # If chat_update was used for "Creating...", then the original message is already replaced.
                # If you posted "Creating..." as a new message, you might want to delete that specific message.
                # For simplicity, we updated the original, and now post a new one.
            else:
                # Re-enable buttons or provide a clear error on the original message
                await client.chat_update(
                    channel=channel_id,
                    ts=original_message_ts, # Update the same message where confirmation was asked
                    text="❌ Sorry, I couldn't create the Jira ticket. Please check the logs or try again. You can try confirming again or cancel.",
                    blocks=build_pre_creation_confirmation_blocks(final_ticket_context) # Show confirmation blocks again
                )
                # Note: State is not cleared here so user can retry confirming.
                # You might want a retry limit.

            if created_ticket_info: # Only clear state on success
                conversation_state_store.pop(state_key, None)

        except Exception as e:
            logger.error(f"Error processing 'confirm_create_ticket_action': {e}")
            # Attempt to update the message with the error and allow retry
            try:
                final_ticket_context_for_retry = FinalTicketCreationContext.model_validate(current_state_dict)
                await client.chat_update(
                    channel=channel_id,
                    ts=original_message_ts,
                    text="An error occurred while trying to create the ticket. Please try again or cancel.",
                    blocks=build_pre_creation_confirmation_blocks(final_ticket_context_for_retry)
                )
            except Exception as update_err: # Fallback if update fails
                 logger.error(f"Failed to update Slack message with error: {update_err}")
                 await client.chat_postMessage(channel=channel_id, text="An unexpected error occurred. Please try starting over.")
                 conversation_state_store.pop(state_key, None) # Clear state on total failure

    else:
        logger.warning(f"Unhandled action_id: {action_id}")
        await client.chat_postMessage(channel=channel_id, text="Sorry, I didn't understand that action.")