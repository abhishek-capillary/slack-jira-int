from slack_sdk.web.async_client import AsyncWebClient
from .config import settings, logger
from .mcp_models import (
    InitialRequestContext, SlackContext, EnrichedTicketContext, ParsedTicketDetails,
    SimilarityCheckContext, FinalTicketCreationContext, JiraTicketData,
    CreationConfirmationContext, SimilarTicketInfo, BotStateData, JiraProject,
    ProjectSelectionContext
)
from .nlp_service import extract_ticket_details_from_text
from .jira_client import create_jira_ticket, search_similar_jira_tickets, get_available_jira_projects
from typing import Dict, Any, List, Optional
import time

# In-memory store for conversation state
conversation_state_store: Dict[str, Any] = {}

# Helper to build project selection blocks
def build_project_selection_blocks(context: ProjectSelectionContext) -> List[Dict]:
    """Builds Slack blocks for project selection using a dropdown."""
    if not context.available_projects:
        return [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Sorry, I couldn't find any Jira projects to choose from at the moment. Please try again later or contact an admin."
            }
        }]

    project_options = []
    for proj in context.available_projects:
        # Slack select options have a max text length of 75 characters for text and value
        option_text = f"{proj.name} ({proj.key})"
        if len(option_text) > 75: # Truncate if too long
            option_text = option_text[:72] + "..."

        project_options.append({
            "text": {"type": "plain_text", "text": option_text, "emoji": True}, # Added emoji: True for good measure
            "value": proj.key # Use project key as the value
        })
        if len(project_options) >= 100: # Slack limits options in static_select to 100
            logger.warning("More than 100 Jira projects found, truncating list for Slack dropdown.")
            break

    if not project_options:
         return [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "No projects available for selection after filtering/truncation."
            }
        }]

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Okay, I've parsed your request for: *'{context.enriched_ticket_context.parsed_ticket_details.summary}'*.\n\nPlease select the Jira project for this ticket:"
            }
        },
        {
            "type": "actions",
            "block_id": "project_selection_block",
            "elements": [
                {
                    "type": "static_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select a project",
                        "emoji": True
                    },
                    "options": project_options,
                    "action_id": "select_jira_project_action"
                }
            ]
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel Ticket Creation", "emoji": True},
                    "action_id": "cancel_creation_project_select",
                    "value": "cancel_creation_project_select",
                    "style": "danger"
                }
            ]
        }
    ]
    return blocks

async def handle_message_im(event: Dict, say, client: AsyncWebClient, body: Dict, jira_projects_cache: List[JiraProject]):
    """Handles direct messages to the bot to initiate ticket creation."""
    user_id = event.get("user")
    channel_id = event.get("channel")
    text = event.get("text", "").strip()
    timestamp = event.get("ts")
    team_id = body.get("team_id")

    if not user_id or not text:
        logger.warning("Received message.im event with no user or text.")
        return
    if event.get("bot_id") or event.get("subtype") == "bot_message": # Ignore bot's own messages
        return

    logger.info(f"Received DM from user {user_id} in channel {channel_id}: '{text}'")

    slack_ctx = SlackContext(user_id=user_id, channel_id=channel_id, team_id=team_id)

    # Step 1: Parse ticket details from user text using NLP
    parsed_details: Optional[ParsedTicketDetails] = await extract_ticket_details_from_text(text)
    if not parsed_details:
        await client.chat_postMessage(channel=channel_id, text="I had trouble understanding the details for the Jira ticket. Could you please try rephrasing or be more specific about the summary, description, and type (Bug, Task, Story)?")
        return

    # Step 2: Create enriched context with parsed details
    enriched_context = EnrichedTicketContext(
        slack_context=slack_ctx,
        raw_request=text,
        parsed_ticket_details=parsed_details # This now contains issue_type (Python name)
    )
    logger.debug(f"MCP Stage: EnrichedTicketContext created: {enriched_context.model_dump_json(indent=2, by_alias=True)}") # Log with alias for clarity

    # Step 3: Prepare for project selection
    available_projects = jira_projects_cache
    if not available_projects:
        logger.warning("Jira projects cache was empty, attempting to fetch projects now.")
        available_projects = await get_available_jira_projects()

    if not available_projects:
        await client.chat_postMessage(channel=channel_id, text="I couldn't fetch the list of Jira projects. Please try again later or contact an admin.")
        return

    project_selection_ctx = ProjectSelectionContext(
        enriched_ticket_context=enriched_context,
        available_projects=available_projects
    )
    logger.debug(f"MCP Stage: ProjectSelectionContext created: {project_selection_ctx.model_dump_json(indent=2, by_alias=True)}")

    # Step 4: Store state and ask user to select a project
    state_key = f"{user_id}-{channel_id}"
    bot_state_payload = BotStateData(
        user_id=user_id,
        channel_id=channel_id,
        current_mcp_stage=ProjectSelectionContext.__name__,
        # IMPORTANT: Dump with by_alias=True so that when this is loaded back,
        # Pydantic can correctly map 'issueType' to 'issue_type' in ParsedTicketDetails.
        context_data=project_selection_ctx.model_dump(by_alias=True),
        timestamp=time.time()
    )
    conversation_state_store[state_key] = bot_state_payload.model_dump() # Outer dump is fine

    blocks = build_project_selection_blocks(project_selection_ctx)
    await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Please select a Jira project for your ticket.")


async def handle_interactive_action(ack, body: Dict, client: AsyncWebClient, say, jira_projects_cache: List[JiraProject]):
    """Handles all interactive component actions (buttons, dropdowns)."""
    # ack() is called by specific handlers in main.py

    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]

    action_id = None
    action_value = None

    if body.get("actions"):
        action = body["actions"][0]
        action_id = action["action_id"]
        action_value = action.get("value")
        if action.get("type") == "static_select":
            action_value = action.get("selected_option", {}).get("value")
    else:
        logger.error(f"Received interactive payload without 'actions' array: {body}")
        await client.chat_postMessage(channel=channel_id, text="Sorry, there was an issue processing your action.")
        return

    logger.info(f"Interactive action triggered: user={user_id}, channel={channel_id}, action_id='{action_id}', value='{action_value}'")

    state_key = f"{user_id}-{channel_id}"
    raw_state_data = conversation_state_store.get(state_key)

    if not raw_state_data:
        await client.chat_postMessage(channel=channel_id, text="Sorry, I couldn't find the context for this action. Please try starting over.")
        return

    try:
        # When loading BotStateData, its context_data is already a dict.
        # The crucial part is that this dict (which was dumped with by_alias=True)
        # will be used to validate the specific context model (e.g., ProjectSelectionContext).
        bot_state = BotStateData.model_validate(raw_state_data)
        current_mcp_context_data = bot_state.context_data
    except Exception as e:
        logger.error(f"Error validating or accessing bot state: {e}. Raw state: {raw_state_data}", exc_info=True)
        await client.chat_postMessage(channel=channel_id, text="There was an issue with my memory. Please try again.")
        return

    original_message_ts = body["message"]["ts"]

    # --- Handle Project Selection ---
    if action_id == "select_jira_project_action":
        try:
            # current_mcp_context_data should contain keys with aliases (e.g., 'issueType')
            # because ProjectSelectionContext was stored with model_dump(by_alias=True).
            project_selection_ctx = ProjectSelectionContext.model_validate(current_mcp_context_data)
            selected_project_key = action_value

            if not selected_project_key:
                # This case should ideally not happen if the select menu requires a selection.
                # However, good to handle.
                await client.chat_postMessage(channel=channel_id, text="No project was selected. Please try again.")
                # Optionally, resend the project selection message or update the existing one.
                return

            logger.info(f"User {user_id} selected project: {selected_project_key}")

            await client.chat_update(
                channel=channel_id,
                ts=original_message_ts,
                text=f"Project *{selected_project_key}* selected. Now checking for similar tickets...",
                blocks=[]
            )

            enriched_ctx = project_selection_ctx.enriched_ticket_context

            similar_jira_issues: List[SimilarTicketInfo] = await search_similar_jira_tickets(
                project_key=selected_project_key,
                summary=enriched_ctx.parsed_ticket_details.summary,
                issue_types=[enriched_ctx.parsed_ticket_details.issue_type] # Uses Python name 'issue_type'
            )

            similarity_context = SimilarityCheckContext(
                slack_context=enriched_ctx.slack_context,
                raw_request=enriched_ctx.raw_request,
                parsed_ticket_details=enriched_ctx.parsed_ticket_details, # Contains 'issue_type'
                selected_project_key=selected_project_key,
                similar_tickets_found=similar_jira_issues,
                status="pending_user_decision_on_similarity" if similar_jira_issues else "pending_confirmation"
            )
            logger.debug(f"MCP Stage: SimilarityCheckContext created: {similarity_context.model_dump_json(indent=2, by_alias=True)}")

            bot_state.current_mcp_stage = SimilarityCheckContext.__name__
            # Store the new context, again using by_alias=True for consistency if it contains aliased fields
            bot_state.context_data = similarity_context.model_dump(by_alias=True)
            conversation_state_store[state_key] = bot_state.model_dump()

            if similarity_context.similar_tickets_found:
                blocks = build_similarity_check_blocks(similarity_context)
                await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Found some existing tickets that might be similar.")
            else:
                jira_data = JiraTicketData(
                    project_key=selected_project_key,
                    summary=enriched_ctx.parsed_ticket_details.summary,
                    description=f"{enriched_ctx.parsed_ticket_details.description}\n\n---\nRequested by Slack User: <@{user_id}>",
                    issue_type_name=enriched_ctx.parsed_ticket_details.issue_type, # Use Python name
                    brand="DefaultBrand",
                    environment="DefaultEnvironment",
                    components=None
                )
                final_ticket_ctx = FinalTicketCreationContext(
                    slack_context=enriched_ctx.slack_context,
                    jira_ticket_data=jira_data,
                    status="ready_for_creation"
                )
                bot_state.current_mcp_stage = FinalTicketCreationContext.__name__
                bot_state.context_data = final_ticket_ctx.model_dump(by_alias=True) # Use by_alias if JiraTicketData has aliases
                conversation_state_store[state_key] = bot_state.model_dump()

                blocks = build_pre_creation_confirmation_blocks(final_ticket_ctx)
                await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Please confirm the details for the new Jira ticket.")

        except Exception as e:
            logger.error(f"Error processing 'select_jira_project_action': {e}", exc_info=True)
            await client.chat_postMessage(channel=channel_id, text="An error occurred while selecting the project. Please try again.")

    elif action_id == "create_new_ticket_anyway":
        try:
            similarity_context = SimilarityCheckContext.model_validate(current_mcp_context_data)

            jira_data = JiraTicketData(
                project_key=similarity_context.selected_project_key,
                summary=similarity_context.parsed_ticket_details.summary,
                description=f"{similarity_context.parsed_ticket_details.description}\n\n---\nRequested by Slack User: <@{user_id}>",
                issue_type_name=similarity_context.parsed_ticket_details.issue_type,
                brand="DefaultBrand",
                environment="DefaultEnvironment",
                components=None
            )
            logger.debug(f"SLACK_HANDLER: JiraTicketData created (create_new_ticket_anyway): {jira_data.model_dump_json(indent=2, by_alias=True)}")

            final_ticket_ctx = FinalTicketCreationContext(
                slack_context=similarity_context.slack_context,
                jira_ticket_data=jira_data,
                status="ready_for_creation"
            )
            bot_state.current_mcp_stage = FinalTicketCreationContext.__name__
            bot_state.context_data = final_ticket_ctx.model_dump(by_alias=True)
            conversation_state_store[state_key] = bot_state.model_dump()

            blocks = build_pre_creation_confirmation_blocks(final_ticket_ctx)
            await client.chat_update(channel=channel_id, ts=original_message_ts, blocks=blocks, text="Please confirm ticket details:")
        except Exception as e:
            logger.error(f"Error processing 'create_new_ticket_anyway': {e}", exc_info=True)
            await client.chat_postMessage(channel=channel_id, text="An error occurred. Please try again.")

    elif action_id == "mark_duplicate":
        ticket_key_duplicate = action_value
        await client.chat_update(
            channel=channel_id, ts=original_message_ts,
            text=f"Okay, I've noted that this is a duplicate of {ticket_key_duplicate}. I won't create a new ticket.",
            blocks=[]
        )
        conversation_state_store.pop(state_key, None)

    elif action_id in ["cancel_creation_similarity", "cancel_creation_confirmation", "cancel_creation_project_select"]:
        await client.chat_update(
            channel=channel_id, ts=original_message_ts,
            text="Okay, I've cancelled the ticket creation process.",
            blocks=[]
        )
        conversation_state_store.pop(state_key, None)

    elif action_id == "confirm_create_ticket_action":
        try:
            final_ticket_context = FinalTicketCreationContext.model_validate(current_mcp_context_data)
            logger.debug(f"MCP Stage 4 (confirm_create_ticket_action): {final_ticket_context.model_dump_json(indent=2, by_alias=True)}")
            logger.debug(f"SLACK_HANDLER: JiraTicketData to be sent (confirm_create_ticket_action): {final_ticket_context.jira_ticket_data.model_dump_json(indent=2, by_alias=True)}")

            await client.chat_update(channel=channel_id, ts=original_message_ts, text="Creating your Jira ticket, please wait...", blocks=[])
            created_ticket_info: Optional[CreatedTicketInfo] = await create_jira_ticket(final_ticket_context.jira_ticket_data)

            if created_ticket_info:
                confirmation_ctx = CreationConfirmationContext(
                    slack_context=final_ticket_context.slack_context,
                    created_ticket_info=created_ticket_info,
                    status="ticket_created_successfully"
                )
                logger.debug(f"MCP Stage 5: {confirmation_ctx.model_dump_json(indent=2, by_alias=True)}")
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f"✅ Success! I've created Jira ticket <{created_ticket_info.url}|{created_ticket_info.key}> for you in project *{final_ticket_context.jira_ticket_data.project_key}*."
                )
                conversation_state_store.pop(state_key, None)
            else:
                await client.chat_update(
                    channel=channel_id, ts=original_message_ts,
                    text="❌ Sorry, I couldn't create the Jira ticket. Please check the logs or try again. You can try confirming again or cancel.",
                    blocks=build_pre_creation_confirmation_blocks(final_ticket_context)
                )
        except Exception as e:
            logger.error(f"Error processing 'confirm_create_ticket_action': {e}", exc_info=True)
            try:
                # Ensure current_mcp_context_data is still valid for FinalTicketCreationContext if retrying
                final_ticket_context_for_retry = FinalTicketCreationContext.model_validate(current_mcp_context_data)
                await client.chat_update(
                    channel=channel_id, ts=original_message_ts,
                    text="An unexpected error occurred while trying to create the ticket. Please try again or cancel.",
                    blocks=build_pre_creation_confirmation_blocks(final_ticket_context_for_retry)
                )
            except Exception as update_err: # Fallback if update fails
                 logger.error(f"Failed to update Slack message with error: {update_err}", exc_info=True)
                 await client.chat_postMessage(channel=channel_id, text="An unexpected error occurred. Please try starting over.")
                 conversation_state_store.pop(state_key, None) # Clear state on total failure
    else:
        logger.warning(f"Unhandled action_id: {action_id}")
        # Consider not sending a message for every unhandled action during development
        # await client.chat_postMessage(channel=channel_id, text="Sorry, I didn't understand that action.")

# Ensure these block builder functions are defined or imported
def build_similarity_check_blocks(context: SimilarityCheckContext) -> List[Dict]:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"For project *{context.selected_project_key}*, I found some existing tickets that might be similar to your request for: *'{context.parsed_ticket_details.summary}'*."
            }
        },
        {"type": "divider"}
    ]
    for ticket in context.similar_tickets_found[:3]:
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
                    "text": {"type": "plain_text", "text": f"This is a duplicate of {ticket.key}", "emoji": True},
                    "action_id": "mark_duplicate",
                    "value": ticket.key,
                }
            ]
        })
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Create New Ticket Anyway", "emoji": True},
                "action_id": "create_new_ticket_anyway",
                "value": "create_new_ticket_anyway",
                "style": "primary"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "action_id": "cancel_creation_similarity",
                "value": "cancel_creation_similarity",
            }
        ]
    })
    return blocks


def build_pre_creation_confirmation_blocks(context: FinalTicketCreationContext) -> List[Dict]:
    ticket = context.jira_ticket_data
    fields_to_display = [
        {"type": "mrkdwn", "text": f"*Project:*\n{ticket.project_key}"},
        {"type": "mrkdwn", "text": f"*Type:*\n{ticket.issue_type_name}"},
        {"type": "mrkdwn", "text": f"*Summary:*\n{ticket.summary}"},
    ]
    # Only display these if they have actual values being sent to Jira
    if ticket.brand is not None:
        fields_to_display.append({"type": "mrkdwn", "text": f"*Brand:*\n{ticket.brand}"})
    if ticket.environment is not None:
        fields_to_display.append({"type": "mrkdwn", "text": f"*Environment:*\n{ticket.environment}"})
    # Components are temporarily None, so no need to display them
    # if ticket.components:
    #     component_names = ", ".join([comp.get("name", "N/A") for comp in ticket.components])
    #     fields_to_display.append({"type": "mrkdwn", "text": f"*Components:*\n{component_names}"})

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
            "fields": fields_to_display
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
                    "text": {"type": "plain_text", "text": "Confirm & Create Ticket", "emoji": True},
                    "action_id": "confirm_create_ticket_action",
                    "value": "confirm_create",
                    "style": "primary"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                    "action_id": "cancel_creation_confirmation",
                    "value": "cancel_creation_confirmation",
                }
            ]
        }
    ]
    return blocks
