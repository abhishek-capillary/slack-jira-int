from slack_sdk.web.async_client import AsyncWebClient
from .config import settings, logger
from .mcp_models import (
    InitialRequestContext, SlackContext, EnrichedTicketContext, ParsedTicketDetails,
    SimilarityCheckContext, FinalTicketCreationContext, JiraTicketData,
    CreationConfirmationContext, SimilarTicketInfo, BotStateData, JiraProject,
    ProjectSelectionContext, JiraIssueType, IssueTypeSelectionContext # Added JiraIssueType & Context
)
from .nlp_service import extract_ticket_details_from_text
from .jira_client import (
    create_jira_ticket, search_similar_jira_tickets,
    get_available_jira_projects, get_project_creatable_issue_types # Added get_project_creatable_issue_types
)
from typing import Dict, Any, List, Optional
import time

# In-memory store for conversation state
conversation_state_store: Dict[str, Any] = {}

# --- Block Kit Builder Functions ---
def build_project_selection_blocks(context: ProjectSelectionContext) -> List[Dict]:
    if not context.available_projects: # Should be caught before calling this
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "No Jira projects available."}}]

    project_options = []
    for proj in context.available_projects:
        option_text = f"{proj.name} ({proj.key})"
        if len(option_text) > 75: option_text = option_text[:72] + "..."
        project_options.append({
            "text": {"type": "plain_text", "text": option_text, "emoji": True},
            "value": proj.key
        })
        if len(project_options) >= 100: break

    if not project_options:
         return [{"type": "section", "text": {"type": "mrkdwn", "text": "No projects formatted for selection."}}]

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Okay, I've parsed your request for: *'{context.enriched_ticket_context.parsed_ticket_details.summary}'*.\n\nPlease select the Jira project for this ticket:"}},
        {"type": "actions", "block_id": "project_selection_block",
         "elements": [{"type": "static_select", "placeholder": {"type": "plain_text", "text": "Select a project", "emoji": True},
                       "options": project_options, "action_id": "select_jira_project_action"}]},
        {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Cancel Ticket Creation", "emoji": True},
                                          "action_id": "cancel_creation_project_select", "value": "cancel_creation_project_select", "style": "danger"}]}
    ]
    return blocks

def build_issue_type_selection_blocks(context: IssueTypeSelectionContext) -> List[Dict]:
    """Builds Slack blocks for issue type selection using a dropdown."""
    if not context.available_issue_types:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f"Sorry, I couldn't find any creatable issue types for project *{context.selected_project.key}*."}}]

    issue_type_options = []
    nlp_suggested_issue_type_name = context.enriched_ticket_context.parsed_ticket_details.issue_type.lower()
    initial_option_to_set = None

    for itype in context.available_issue_types:
        option_text = itype.name
        if itype.description and len(option_text + itype.description) < 65: # Keep it concise
             option_text += f" - {itype.description[:(65 - len(option_text))]}"
        if len(option_text) > 75: option_text = option_text[:72] + "..."

        option = {
            "text": {"type": "plain_text", "text": option_text, "emoji": True},
            "value": itype.id # Use issue type ID as value for precision
        }
        issue_type_options.append(option)

        # Check if this issue type matches NLP suggestion for initial_option
        if itype.name.lower() == nlp_suggested_issue_type_name and not initial_option_to_set:
            initial_option_to_set = option

        if len(issue_type_options) >= 100: break

    if not issue_type_options:
         return [{"type": "section", "text": {"type": "mrkdwn", "text": "No issue types formatted for selection."}}]

    select_element = {
        "type": "static_select",
        "placeholder": {"type": "plain_text", "text": "Select an issue type", "emoji": True},
        "options": issue_type_options,
        "action_id": "select_jira_issue_type_action"
    }
    if initial_option_to_set:
        select_element["initial_option"] = initial_option_to_set
        logger.info(f"Pre-selecting issue type: {initial_option_to_set['text']['text']} based on NLP suggestion.")


    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Project *{context.selected_project.name} ({context.selected_project.key})* selected.\nYour original request mentioned issue type: *'{context.enriched_ticket_context.parsed_ticket_details.issue_type}'*.\n\nPlease confirm or select the specific issue type for this project:"}},
        {"type": "actions", "block_id": "issue_type_selection_block", "elements": [select_element]},
        {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Cancel Ticket Creation", "emoji": True},
                                          "action_id": "cancel_creation_issue_type_select", "value": "cancel_creation_issue_type_select", "style": "danger"}]}
    ]
    return blocks

# --- Main Handler Logic ---
async def handle_message_im(event: Dict, say, client: AsyncWebClient, body: Dict, jira_projects_cache: List[JiraProject]):
    user_id = event.get("user")
    channel_id = event.get("channel")
    text = event.get("text", "").strip()
    timestamp = event.get("ts")
    team_id = body.get("team_id")

    if not user_id or not text or event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    logger.info(f"Received DM from user {user_id} in channel {channel_id}: '{text}'")
    slack_ctx = SlackContext(user_id=user_id, channel_id=channel_id, team_id=team_id)

    parsed_details: Optional[ParsedTicketDetails] = await extract_ticket_details_from_text(text)
    if not parsed_details:
        await client.chat_postMessage(channel=channel_id, text="I had trouble understanding the details for the Jira ticket. Could you please try rephrasing?")
        return

    enriched_context = EnrichedTicketContext(slack_context=slack_ctx, raw_request=text, parsed_ticket_details=parsed_details)
    logger.debug(f"MCP Stage: EnrichedTicketContext created: {enriched_context.model_dump_json(indent=2, by_alias=True)}")

    available_projects = jira_projects_cache
    if not available_projects:
        logger.warning("Jira projects cache was empty, attempting to fetch projects now.")
        available_projects = await get_available_jira_projects()
    if not available_projects:
        await client.chat_postMessage(channel=channel_id, text="I couldn't fetch the list of Jira projects. Please try again later or contact an admin.")
        return

    project_selection_ctx = ProjectSelectionContext(enriched_ticket_context=enriched_context, available_projects=available_projects)
    logger.debug(f"MCP Stage: ProjectSelectionContext created: {project_selection_ctx.model_dump_json(indent=2, by_alias=True)}")

    state_key = f"{user_id}-{channel_id}"
    bot_state_payload = BotStateData(user_id=user_id, channel_id=channel_id, current_mcp_stage=ProjectSelectionContext.__name__,
                                     context_data=project_selection_ctx.model_dump(by_alias=True), timestamp=time.time())
    conversation_state_store[state_key] = bot_state_payload.model_dump()

    blocks = build_project_selection_blocks(project_selection_ctx)
    await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Please select a Jira project for your ticket.")


async def handle_interactive_action(ack, body: Dict, client: AsyncWebClient, say, jira_projects_cache: List[JiraProject]):
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    action_id, action_value = None, None

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
            project_selection_ctx = ProjectSelectionContext.model_validate(current_mcp_context_data)
            selected_project_key = action_value
            if not selected_project_key:
                await client.chat_postMessage(channel=channel_id, text="No project was selected. Please try again.")
                return

            selected_project_obj = next((proj for proj in project_selection_ctx.available_projects if proj.key == selected_project_key), None)
            if not selected_project_obj:
                logger.error(f"Selected project key {selected_project_key} not found in available projects list.")
                await client.chat_postMessage(channel=channel_id, text="Invalid project selected. Please try again.")
                return

            logger.info(f"User {user_id} selected project: {selected_project_key} ({selected_project_obj.name})")
            await client.chat_update(channel=channel_id, ts=original_message_ts,
                                     text=f"Project *{selected_project_obj.name} ({selected_project_key})* selected. Now fetching issue types...", blocks=[])

            available_issue_types = await get_project_creatable_issue_types(selected_project_key)
            if not available_issue_types:
                await client.chat_postMessage(channel=channel_id, text=f"Sorry, I couldn't fetch any creatable issue types for project *{selected_project_key}*.")
                conversation_state_store.pop(state_key, None) # Clear state as we can't proceed
                return

            issue_type_selection_ctx = IssueTypeSelectionContext(
                enriched_ticket_context=project_selection_ctx.enriched_ticket_context,
                selected_project=selected_project_obj,
                available_issue_types=available_issue_types
            )
            logger.debug(f"MCP Stage: IssueTypeSelectionContext created: {issue_type_selection_ctx.model_dump_json(indent=2, by_alias=True)}")

            bot_state.current_mcp_stage = IssueTypeSelectionContext.__name__
            bot_state.context_data = issue_type_selection_ctx.model_dump(by_alias=True)
            conversation_state_store[state_key] = bot_state.model_dump()

            blocks = build_issue_type_selection_blocks(issue_type_selection_ctx)
            await client.chat_postMessage(channel=channel_id, blocks=blocks, text=f"Please select an issue type for project {selected_project_key}:")

        except Exception as e:
            logger.error(f"Error processing 'select_jira_project_action': {e}", exc_info=True)
            await client.chat_postMessage(channel=channel_id, text="An error occurred while selecting the project. Please try again.")

    # --- Handle Issue Type Selection ---
    elif action_id == "select_jira_issue_type_action":
        try:
            issue_type_selection_ctx = IssueTypeSelectionContext.model_validate(current_mcp_context_data)
            selected_issue_type_id = action_value # This is the ID of the issue type

            selected_issue_type_obj = next((it for it in issue_type_selection_ctx.available_issue_types if it.id == selected_issue_type_id), None)
            if not selected_issue_type_obj:
                logger.error(f"Selected issue type ID {selected_issue_type_id} not found in available list.")
                await client.chat_postMessage(channel=channel_id, text="Invalid issue type selected. Please try again.")
                return

            logger.info(f"User {user_id} selected issue type: {selected_issue_type_obj.name} (ID: {selected_issue_type_id}) for project {issue_type_selection_ctx.selected_project.key}")

            await client.chat_update(channel=channel_id, ts=original_message_ts,
                                     text=f"Project *{issue_type_selection_ctx.selected_project.key}* and issue type *{selected_issue_type_obj.name}* selected. Now checking for similar tickets...",
                                     blocks=[])

            enriched_ctx = issue_type_selection_ctx.enriched_ticket_context
            similar_jira_issues: List[SimilarTicketInfo] = await search_similar_jira_tickets(
                project_key=issue_type_selection_ctx.selected_project.key,
                summary=enriched_ctx.parsed_ticket_details.summary,
                issue_types=[selected_issue_type_obj.name] # Search using the selected issue type name
            )

            similarity_context = SimilarityCheckContext(
                slack_context=enriched_ctx.slack_context,
                raw_request=enriched_ctx.raw_request,
                parsed_ticket_details=enriched_ctx.parsed_ticket_details, # Original NLP details
                selected_project=issue_type_selection_ctx.selected_project,
                selected_issue_type=selected_issue_type_obj, # Store the selected JiraIssueType object
                similar_tickets_found=similar_jira_issues,
                status="pending_user_decision_on_similarity" if similar_jira_issues else "pending_confirmation"
            )
            logger.debug(f"MCP Stage: SimilarityCheckContext created: {similarity_context.model_dump_json(indent=2, by_alias=True)}")

            bot_state.current_mcp_stage = SimilarityCheckContext.__name__
            bot_state.context_data = similarity_context.model_dump(by_alias=True)
            conversation_state_store[state_key] = bot_state.model_dump()

            if similarity_context.similar_tickets_found:
                blocks = build_similarity_check_blocks(similarity_context)
                await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Found some existing tickets that might be similar.")
            else:
                jira_data = JiraTicketData(
                    project_key=similarity_context.selected_project.key,
                    summary=enriched_ctx.parsed_ticket_details.summary, # Use original NLP summary
                    description=f"{enriched_ctx.parsed_ticket_details.description}\n\n---\nRequested by Slack User: <@{user_id}>",
                    issue_type_name=similarity_context.selected_issue_type.name, # Use selected issue type name
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
                bot_state.context_data = final_ticket_ctx.model_dump(by_alias=True)
                conversation_state_store[state_key] = bot_state.model_dump()

                blocks = build_pre_creation_confirmation_blocks(final_ticket_ctx)
                await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Please confirm the details for the new Jira ticket.")

        except Exception as e:
            logger.error(f"Error processing 'select_jira_issue_type_action': {e}", exc_info=True)
            await client.chat_postMessage(channel=channel_id, text="An error occurred while selecting the issue type. Please try again.")


    elif action_id == "create_new_ticket_anyway":
        try:
            similarity_context = SimilarityCheckContext.model_validate(current_mcp_context_data)
            jira_data = JiraTicketData(
                project_key=similarity_context.selected_project.key,
                summary=similarity_context.parsed_ticket_details.summary,
                description=f"{similarity_context.parsed_ticket_details.description}\n\n---\nRequested by Slack User: <@{user_id}>",
                issue_type_name=similarity_context.selected_issue_type.name, # Use name from selected issue type
                brand="DefaultBrand",
                environment="DefaultEnvironment",
                components=None
            )
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
        # ... (same as before)
        ticket_key_duplicate = action_value
        await client.chat_update(channel=channel_id, ts=original_message_ts,
            text=f"Okay, I've noted that this is a duplicate of {ticket_key_duplicate}. I won't create a new ticket.",
            blocks=[])
        conversation_state_store.pop(state_key, None)


    elif action_id in ["cancel_creation_similarity", "cancel_creation_confirmation",
                       "cancel_creation_project_select", "cancel_creation_issue_type_select"]: # Added new cancel action
        await client.chat_update(channel=channel_id, ts=original_message_ts,
            text="Okay, I've cancelled the ticket creation process.", blocks=[])
        conversation_state_store.pop(state_key, None)

    elif action_id == "confirm_create_ticket_action":
        try:
            final_ticket_context = FinalTicketCreationContext.model_validate(current_mcp_context_data)
            await client.chat_update(channel=channel_id, ts=original_message_ts, text="Creating your Jira ticket, please wait...", blocks=[])
            created_ticket_info: Optional[CreatedTicketInfo] = await create_jira_ticket(final_ticket_context.jira_ticket_data)

            if created_ticket_info:
                # ... (success message same as before, ensuring project_key is in the message)
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f"✅ Success! I've created Jira ticket <{created_ticket_info.url}|{created_ticket_info.key}> for you in project *{final_ticket_context.jira_ticket_data.project_key}*."
                )
                conversation_state_store.pop(state_key, None)
            else:
                await client.chat_update(channel=channel_id, ts=original_message_ts,
                    text="❌ Sorry, I couldn't create the Jira ticket. Please check the logs or try again.",
                    blocks=build_pre_creation_confirmation_blocks(final_ticket_context)
                )
        except Exception as e:
            logger.error(f"Error processing 'confirm_create_ticket_action': {e}", exc_info=True)
            # ... (error handling same as before)
            try:
                final_ticket_context_for_retry = FinalTicketCreationContext.model_validate(current_mcp_context_data)
                await client.chat_update(
                    channel=channel_id, ts=original_message_ts,
                    text="An unexpected error occurred while trying to create the ticket. Please try again or cancel.",
                    blocks=build_pre_creation_confirmation_blocks(final_ticket_context_for_retry)
                )
            except Exception as update_err:
                 logger.error(f"Failed to update Slack message with error: {update_err}", exc_info=True)
                 await client.chat_postMessage(channel=channel_id, text="An unexpected error occurred. Please try starting over.")
                 conversation_state_store.pop(state_key, None)
    else:
        logger.warning(f"Unhandled action_id: {action_id}")

# Ensure these block builder functions are defined or imported
def build_similarity_check_blocks(context: SimilarityCheckContext) -> List[Dict]:
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"For project *{context.selected_project.key}* and issue type *{context.selected_issue_type.name}*, I found some existing tickets that might be similar to your request for: *'{context.parsed_ticket_details.summary}'*."}},
        {"type": "divider"}
    ]
    for ticket in context.similar_tickets_found[:3]:
        score_text = f" (Similarity: {ticket.score:.2f})" if ticket.score is not None else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*<{ticket.url}|{ticket.key}>*: {ticket.summary}{score_text}"}})
        blocks.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": f"This is a duplicate of {ticket.key}", "emoji": True}, "action_id": "mark_duplicate", "value": ticket.key}]})
    blocks.append({"type": "divider"})
    blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Create New Ticket Anyway", "emoji": True}, "action_id": "create_new_ticket_anyway", "value": "create_new_ticket_anyway", "style": "primary"},
            {"type": "button", "text": {"type": "plain_text", "text": "Cancel", "emoji": True}, "action_id": "cancel_creation_similarity", "value": "cancel_creation_similarity"}]})
    return blocks

def build_pre_creation_confirmation_blocks(context: FinalTicketCreationContext) -> List[Dict]:
    ticket = context.jira_ticket_data
    fields_to_display = [
        {"type": "mrkdwn", "text": f"*Project:*\n{ticket.project_key}"},
        {"type": "mrkdwn", "text": f"*Type:*\n{ticket.issue_type_name}"}, # This is now the user-confirmed type
        {"type": "mrkdwn", "text": f"*Summary:*\n{ticket.summary}"},
    ]
    if ticket.brand is not None: fields_to_display.append({"type": "mrkdwn", "text": f"*Brand:*\n{ticket.brand}"})
    if ticket.environment is not None: fields_to_display.append({"type": "mrkdwn", "text": f"*Environment:*\n{ticket.environment}"})
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Please review and confirm the details for the new Jira ticket:"}},
        {"type": "section", "fields": fields_to_display},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Description:*\n{ticket.description}"}},
        {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Confirm & Create Ticket", "emoji": True}, "action_id": "confirm_create_ticket_action", "value": "confirm_create", "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "Cancel", "emoji": True}, "action_id": "cancel_creation_confirmation", "value": "cancel_creation_confirmation"}]}]
    return blocks
