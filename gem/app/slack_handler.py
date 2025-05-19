from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from .config import settings, logger
from .mcp_models import (
    InitialRequestContext, SlackContext, EnrichedTicketContext, ParsedTicketDetails,
    SimilarityCheckContext, FinalTicketCreationContext, JiraTicketData,
    CreationConfirmationContext, SimilarTicketInfo, BotStateData, JiraProject,
    ProjectSelectionContext, JiraIssueType, IssueTypeSelectionContext,
    RequiredFieldDetail, AllowedValue, SequentialFieldsInputContext # Changed from RequiredFieldsInputContext
)
from .nlp_service import extract_ticket_details_from_text
from .jira_client import (
    create_jira_ticket, search_similar_jira_tickets,
    get_available_jira_projects, get_project_creatable_issue_types,
    get_required_fields_for_issue_type
)
from typing import Dict, Any, List, Optional
import time
import json

# In-memory store for conversation state
conversation_state_store: Dict[str, Any] = {}

# --- Constants for Action IDs ---
ACTION_ID_SUBMIT_DYNAMIC_FIELD_SELECT = "submit_dynamic_field_select"


# --- Helper to ask for the next required field ---
async def _ask_for_next_required_field(client: AsyncWebClient, channel_id: str, state_key: str, bot_state: BotStateData):
    """
    Checks the current state for sequential field input and prompts the user for the next field,
    or proceeds if all fields are collected.
    """
    try:
        sequential_ctx = SequentialFieldsInputContext.model_validate(bot_state.context_data)
    except Exception as e:
        logger.error(f"Error validating SequentialFieldsInputContext from bot_state: {e}", exc_info=True)
        await client.chat_postMessage(channel=channel_id, text="There was an issue with my memory. Please try starting over.")
        conversation_state_store.pop(state_key, None)
        return

    current_index = sequential_ctx.current_field_prompt_index
    fields_to_collect = sequential_ctx.fields_to_collect_sequentially

    if current_index >= len(fields_to_collect):
        # All required dynamic fields have been collected
        logger.info(f"All {len(fields_to_collect)} dynamic fields collected for user {bot_state.user_id}. Proceeding to next step.")

        # --- Proceed to similarity check or final confirmation ---
        enriched_ctx = sequential_ctx.enriched_ticket_context
        selected_project = sequential_ctx.selected_project
        selected_issue_type = sequential_ctx.selected_issue_type
        collected_dynamic_values = sequential_ctx.collected_dynamic_field_values

        similar_jira_issues: List[SimilarTicketInfo] = await search_similar_jira_tickets(
            project_key=selected_project.key,
            summary=enriched_ctx.parsed_ticket_details.summary,
            issue_types=[selected_issue_type.name]
        )

        similarity_context = SimilarityCheckContext(
            slack_context=enriched_ctx.slack_context, raw_request=enriched_ctx.raw_request,
            parsed_ticket_details=enriched_ctx.parsed_ticket_details,
            selected_project=selected_project, selected_issue_type=selected_issue_type,
            dynamic_fields_data=collected_dynamic_values, # Pass the collected dynamic fields
            similar_tickets_found=similar_jira_issues,
            status="pending_user_decision_on_similarity" if similar_jira_issues else "pending_confirmation"
        )
        bot_state.current_mcp_stage = SimilarityCheckContext.__name__
        bot_state.context_data = similarity_context.model_dump(by_alias=True)
        conversation_state_store[state_key] = bot_state.model_dump()

        if similarity_context.similar_tickets_found:
            blocks = build_similarity_check_blocks(similarity_context)
            await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Found similar tickets after collecting details.")
        else:
            jira_data = JiraTicketData(
                project_key=selected_project.key,
                summary=enriched_ctx.parsed_ticket_details.summary,
                description=f"{enriched_ctx.parsed_ticket_details.description}\n\n---\nRequested by <@{bot_state.user_id}>",
                issue_type_name=selected_issue_type.name,
                components=None, # Still disabled
                dynamic_fields=collected_dynamic_values
            )
            final_ticket_ctx = FinalTicketCreationContext(slack_context=enriched_ctx.slack_context, jira_ticket_data=jira_data, status="ready_for_creation")
            bot_state.current_mcp_stage = FinalTicketCreationContext.__name__
            bot_state.context_data = final_ticket_ctx.model_dump(by_alias=True)
            conversation_state_store[state_key] = bot_state.model_dump()
            blocks = build_pre_creation_confirmation_blocks(final_ticket_ctx)
            await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Please confirm ticket details (including additional fields).")
        return

    # Ask for the current field
    field_to_ask = fields_to_collect[current_index]
    prompt_text = f"Please provide the value for *{field_to_ask.name}*:"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": prompt_text}}]

    if field_to_ask.allowed_values:
        options = []
        for val in field_to_ask.allowed_values:
            display_name = val.name or val.value or val.id or "Unknown Option"
            if len(display_name) > 75: display_name = display_name[:72] + "..."
            option_value = val.id or val.value or val.name
            if option_value and len(option_value) > 75: option_value = option_value[:75]
            if option_value:
                options.append({"text": {"type": "plain_text", "text": display_name, "emoji": True}, "value": option_value})

        if options:
            # Include field_id in the action_id or value if using a generic handler,
            # or use specific action_ids per field if preferred.
            # For simplicity, we'll use a generic action_id and pass field_id in the value.
            # However, Slack action_id for select must be unique if not using block_id to distinguish.
            # Let's make action_id unique for this select to avoid conflicts.
            select_action_id = f"{ACTION_ID_SUBMIT_DYNAMIC_FIELD_SELECT}_{field_to_ask.field_id}"
            blocks.append({
                "type": "actions",
                "block_id": f"dynamic_field_select_block_{field_to_ask.field_id}",
                "elements": [{
                    "type": "static_select",
                    "action_id": select_action_id,
                    "placeholder": {"type": "plain_text", "text": f"Select {field_to_ask.name[:30]}...", "emoji": True},
                    "options": options
                }]
            })
            logger.info(f"Prompting user {bot_state.user_id} with dropdown for field: {field_to_ask.name} (ID: {field_to_ask.field_id})")
        else: # Fallback if no valid options for select
            logger.warning(f"Field '{field_to_ask.name}' has allowedValues but no valid Slack options. Prompting for text.")
            blocks[0]["text"]["text"] += "\n(Please type your answer)" # Modify prompt
            # State remains waiting for text input
    else:
        logger.info(f"Prompting user {bot_state.user_id} for text input for field: {field_to_ask.name} (ID: {field_to_ask.field_id})")
        # State already indicates we are waiting for a text DM for this field.
        # The `current_field_prompt_index` points to this text field.

    await client.chat_postMessage(channel=channel_id, blocks=blocks, text=prompt_text)
    # The state (SequentialFieldsInputContext with current_field_prompt_index) is already set
    # to expect input for this field (either a DM or an action from the select).


# --- Block Kit Builder Functions (Project, Issue Type, Similarity, Confirmation) ---
# These remain largely the same as in slack_handler_py_dynamic_fields_modal,
# ensure they are present and correct.
def build_project_selection_blocks(context: ProjectSelectionContext) -> List[Dict]:
    # ... (implementation from previous version)
    if not context.available_projects: return [{"type": "section", "text": {"type": "mrkdwn", "text": "No Jira projects available."}}]
    project_options = []
    for proj in context.available_projects:
        option_text = f"{proj.name} ({proj.key})"
        if len(option_text) > 75: option_text = option_text[:72] + "..."
        project_options.append({"text": {"type": "plain_text", "text": option_text, "emoji": True}, "value": proj.key})
        if len(project_options) >= 100: break
    if not project_options: return [{"type": "section", "text": {"type": "mrkdwn", "text": "No projects formatted."}}]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Parsed request for: *'{context.enriched_ticket_context.parsed_ticket_details.summary}'*.\nSelect Jira project:"}},
        {"type": "actions", "block_id": "project_selection_block", "elements": [{"type": "static_select", "placeholder": {"type": "plain_text", "text": "Select a project", "emoji": True}, "options": project_options, "action_id": "select_jira_project_action"}]},
        {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Cancel", "emoji": True}, "action_id": "cancel_creation_project_select", "value": "cancel_creation_project_select", "style": "danger"}]}
    ]

def build_issue_type_selection_blocks(context: IssueTypeSelectionContext) -> List[Dict]:
    # ... (implementation from previous version)
    if not context.available_issue_types: return [{"type": "section", "text": {"type": "mrkdwn", "text": f"No creatable issue types for project *{context.selected_project.key}*."}}]
    issue_type_options = []
    nlp_suggested = context.enriched_ticket_context.parsed_ticket_details.issue_type.lower()
    initial_opt = None
    for itype in context.available_issue_types:
        opt_text = itype.name
        if itype.description and len(opt_text + itype.description) < 65: opt_text += f" - {itype.description[:(65 - len(opt_text))]}"
        if len(opt_text) > 75: opt_text = opt_text[:72] + "..."
        option = {"text": {"type": "plain_text", "text": opt_text, "emoji": True}, "value": itype.id}
        issue_type_options.append(option)
        if itype.name.lower() == nlp_suggested and not initial_opt: initial_opt = option
        if len(issue_type_options) >= 100: break
    if not issue_type_options: return [{"type": "section", "text": {"type": "mrkdwn", "text": "No issue types formatted."}}]
    select_el = {"type": "static_select", "placeholder": {"type": "plain_text", "text": "Select an issue type", "emoji": True}, "options": issue_type_options, "action_id": "select_jira_issue_type_action"}
    if initial_opt: select_el["initial_option"] = initial_opt
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Project *{context.selected_project.name} ({context.selected_project.key})* chosen.\nNLP suggested: *'{context.enriched_ticket_context.parsed_ticket_details.issue_type}'*.\nConfirm or select issue type:"}},
        {"type": "actions", "block_id": "issue_type_selection_block", "elements": [select_el]},
        {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Cancel", "emoji": True}, "action_id": "cancel_creation_issue_type_select", "value": "cancel_creation_issue_type_select", "style": "danger"}]}
    ]

def build_similarity_check_blocks(context: SimilarityCheckContext) -> List[Dict]:
    # ... (implementation from previous version, ensure it uses context.selected_project.key and context.selected_issue_type.name)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"For project *{context.selected_project.key}* / type *{context.selected_issue_type.name}*, I found similar tickets for: *'{context.parsed_ticket_details.summary}'*."}},
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
    # ... (implementation from previous version, ensure it iterates context.jira_ticket_data.dynamic_fields)
    ticket = context.jira_ticket_data
    fields_to_display = [
        {"type": "mrkdwn", "text": f"*Project:*\n{ticket.project_key}"},
        {"type": "mrkdwn", "text": f"*Type:*\n{ticket.issue_type_name}"},
        {"type": "mrkdwn", "text": f"*Summary:*\n{ticket.summary}"},
    ]
    if ticket.dynamic_fields:
        for field_id, field_value in ticket.dynamic_fields.items():
            field_name_display = field_id # Ideally map to actual field name
            fields_to_display.append({"type": "mrkdwn", "text": f"*{field_name_display.replace('_', ' ').title()}:*\n{field_value}"})
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Please review and confirm the details for the new Jira ticket:"}},
        {"type": "section", "fields": fields_to_display},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Description:*\n{ticket.description}"}},
        {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Confirm & Create Ticket", "emoji": True}, "action_id": "confirm_create_ticket_action", "value": "confirm_create", "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "Cancel", "emoji": True}, "action_id": "cancel_creation_confirmation", "value": "cancel_creation_confirmation"}]}]
    return blocks

# --- Main Handler Logic ---
async def handle_message_im(event: Dict, say, client: AsyncWebClient, body: Dict, jira_projects_cache: List[JiraProject]):
    user_id = event.get("user")
    channel_id = event.get("channel")
    text = event.get("text", "").strip()
    timestamp = event.get("ts")
    team_id = body.get("team_id") # from body

    if not user_id or not text or event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    state_key = f"{user_id}-{channel_id}"
    raw_state_data = conversation_state_store.get(state_key)

    if raw_state_data:
        try:
            bot_state = BotStateData.model_validate(raw_state_data)
            if bot_state.current_mcp_stage == SequentialFieldsInputContext.__name__:
                sequential_ctx = SequentialFieldsInputContext.model_validate(bot_state.context_data)
                current_field_index = sequential_ctx.current_field_prompt_index

                if current_field_index < len(sequential_ctx.fields_to_collect_sequentially):
                    field_being_prompted = sequential_ctx.fields_to_collect_sequentially[current_field_index]
                    if not field_being_prompted.allowed_values: # Expecting text input
                        logger.info(f"User {user_id} provided text '{text}' for dynamic field '{field_being_prompted.name}'")
                        sequential_ctx.collected_dynamic_field_values[field_being_prompted.field_id] = text
                        sequential_ctx.current_field_prompt_index += 1

                        bot_state.context_data = sequential_ctx.model_dump(by_alias=True)
                        conversation_state_store[state_key] = bot_state.model_dump()

                        await _ask_for_next_required_field(client, channel_id, state_key, bot_state)
                        return # Handled as input for dynamic field
        except Exception as e:
            logger.error(f"Error processing state in handle_message_im for dynamic field input: {e}", exc_info=True)
            # Potentially clear state and let it proceed as a new message
            conversation_state_store.pop(state_key, None)


    # If not handling dynamic field input, proceed as a new request
    logger.info(f"Received DM from user {user_id} in channel {channel_id}: '{text}' (New Request)")
    slack_ctx = SlackContext(user_id=user_id, channel_id=channel_id, team_id=team_id)
    parsed_details: Optional[ParsedTicketDetails] = await extract_ticket_details_from_text(text)
    if not parsed_details:
        await client.chat_postMessage(channel=channel_id, text="I had trouble understanding the details. Please try rephrasing.")
        return
    enriched_context = EnrichedTicketContext(slack_context=slack_ctx, raw_request=text, parsed_ticket_details=parsed_details)
    available_projects = jira_projects_cache or await get_available_jira_projects()
    if not available_projects:
        await client.chat_postMessage(channel=channel_id, text="Couldn't fetch Jira projects. Please contact an admin.")
        return
    project_selection_ctx = ProjectSelectionContext(enriched_ticket_context=enriched_context, available_projects=available_projects)

    # New state setup
    bot_state_payload = BotStateData(user_id=user_id, channel_id=channel_id, current_mcp_stage=ProjectSelectionContext.__name__,
                                     context_data=project_selection_ctx.model_dump(by_alias=True), timestamp=time.time())
    conversation_state_store[state_key] = bot_state_payload.model_dump()

    blocks = build_project_selection_blocks(project_selection_ctx)
    await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Select a Jira project.")


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
        logger.error(f"Interactive payload without 'actions': {body}")
        await client.chat_postMessage(channel=channel_id, text="Error processing action.")
        return

    logger.info(f"Interactive action: user={user_id}, channel={channel_id}, action_id='{action_id}', value='{action_value}'")
    state_key = f"{user_id}-{channel_id}"
    raw_state_data = conversation_state_store.get(state_key)

    if not raw_state_data:
        await client.chat_postMessage(channel=channel_id, text="Context lost. Please start over.")
        return

    try:
        bot_state = BotStateData.model_validate(raw_state_data)
        current_mcp_context_data = bot_state.context_data
    except Exception as e:
        logger.error(f"Error validating state: {e}. Raw: {raw_state_data}", exc_info=True)
        await client.chat_postMessage(channel=channel_id, text="Memory issue. Please try again.")
        return

    original_message_ts = body.get("message", {}).get("ts")

    if action_id == "select_jira_project_action":
        try:
            project_selection_ctx = ProjectSelectionContext.model_validate(current_mcp_context_data)
            selected_project_key = action_value
            selected_project_obj = next((p for p in project_selection_ctx.available_projects if p.key == selected_project_key), None)
            if not selected_project_obj:
                await client.chat_postMessage(channel=channel_id, text="Invalid project. Try again.")
                return

            if original_message_ts:
                await client.chat_update(channel=channel_id, ts=original_message_ts, text=f"Project *{selected_project_obj.name}* selected. Fetching issue types...", blocks=[])
            else: # Should have original_message_ts for this action
                 await client.chat_postMessage(channel=channel_id, text=f"Project *{selected_project_obj.name}* selected. Fetching issue types...")


            available_issue_types = await get_project_creatable_issue_types(selected_project_key)
            if not available_issue_types:
                await client.chat_postMessage(channel=channel_id, text=f"No creatable issue types for *{selected_project_key}*.")
                conversation_state_store.pop(state_key, None)
                return

            issue_type_selection_ctx = IssueTypeSelectionContext(
                enriched_ticket_context=project_selection_ctx.enriched_ticket_context,
                selected_project=selected_project_obj,
                available_issue_types=available_issue_types
            )
            bot_state.current_mcp_stage = IssueTypeSelectionContext.__name__
            bot_state.context_data = issue_type_selection_ctx.model_dump(by_alias=True)
            conversation_state_store[state_key] = bot_state.model_dump()
            blocks = build_issue_type_selection_blocks(issue_type_selection_ctx)
            await client.chat_postMessage(channel=channel_id, blocks=blocks, text=f"Select issue type for {selected_project_key}:")
        except Exception as e:
            logger.error(f"Error in 'select_jira_project_action': {e}", exc_info=True)
            await client.chat_postMessage(channel=channel_id, text="Error selecting project.")

    elif action_id == "select_jira_issue_type_action":
        try:
            issue_type_selection_ctx = IssueTypeSelectionContext.model_validate(current_mcp_context_data)
            selected_issue_type_id = action_value
            selected_issue_type_obj = next((it for it in issue_type_selection_ctx.available_issue_types if it.id == selected_issue_type_id), None)
            if not selected_issue_type_obj:
                await client.chat_postMessage(channel=channel_id, text="Invalid issue type. Try again.")
                return

            if original_message_ts:
                await client.chat_update(channel=channel_id, ts=original_message_ts,
                                        text=f"Project *{issue_type_selection_ctx.selected_project.key}*, Type *{selected_issue_type_obj.name}* selected. Fetching required fields...",
                                        blocks=[])
            else:
                await client.chat_postMessage(channel=channel_id, text=f"Project *{issue_type_selection_ctx.selected_project.key}*, Type *{selected_issue_type_obj.name}* selected. Fetching required fields...")

            required_fields_details = await get_required_fields_for_issue_type(
                issue_type_selection_ctx.selected_project.key,
                selected_issue_type_obj.name
            )
            logger.info(f"Fetched {len(required_fields_details)} required fields for PK='{issue_type_selection_ctx.selected_project.key}', IT='{selected_issue_type_obj.name}'.")

            pre_collected_field_ids = {"summary", "description", "project", "issuetype", "reporter"}

            fields_to_ask_sequentially = [
                rf for rf in required_fields_details
                if rf.field_id not in pre_collected_field_ids
            ]
            logger.info(f"Fields to ask sequentially: {[f.name for f in fields_to_ask_sequentially]}")

            if fields_to_ask_sequentially:
                sequential_input_ctx = SequentialFieldsInputContext(
                    enriched_ticket_context=issue_type_selection_ctx.enriched_ticket_context,
                    selected_project=issue_type_selection_ctx.selected_project,
                    selected_issue_type=selected_issue_type_obj,
                    fields_to_collect_sequentially=fields_to_ask_sequentially,
                    current_field_prompt_index=0,
                    collected_dynamic_field_values={}
                )
                bot_state.current_mcp_stage = SequentialFieldsInputContext.__name__
                bot_state.context_data = sequential_input_ctx.model_dump(by_alias=True)
                conversation_state_store[state_key] = bot_state.model_dump()

                # Start prompting for the first field
                await _ask_for_next_required_field(client, channel_id, state_key, bot_state)
                return # Wait for user input for the dynamic fields
            else:
                # No additional dynamic fields to ask, proceed to similarity/confirmation
                logger.info("No additional dynamic fields to ask. Proceeding to similarity/confirmation.")
                enriched_ctx = issue_type_selection_ctx.enriched_ticket_context
                similar_jira_issues: List[SimilarTicketInfo] = await search_similar_jira_tickets(
                    project_key=issue_type_selection_ctx.selected_project.key,
                    summary=enriched_ctx.parsed_ticket_details.summary,
                    issue_types=[selected_issue_type_obj.name]
                )
                similarity_context = SimilarityCheckContext(
                    slack_context=enriched_ctx.slack_context, raw_request=enriched_ctx.raw_request,
                    parsed_ticket_details=enriched_ctx.parsed_ticket_details,
                    selected_project=issue_type_selection_ctx.selected_project,
                    selected_issue_type=selected_issue_type_obj,
                    dynamic_fields_data={}, # No dynamic fields collected yet
                    similar_tickets_found=similar_jira_issues,
                    status="pending_user_decision_on_similarity" if similar_jira_issues else "pending_confirmation"
                )
                bot_state.current_mcp_stage = SimilarityCheckContext.__name__
                bot_state.context_data = similarity_context.model_dump(by_alias=True)
                conversation_state_store[state_key] = bot_state.model_dump()
                if similarity_context.similar_tickets_found:
                    blocks = build_similarity_check_blocks(similarity_context)
                    await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Found similar tickets.")
                else:
                    jira_data = JiraTicketData(
                        project_key=similarity_context.selected_project.key,
                        summary=enriched_ctx.parsed_ticket_details.summary,
                        description=f"{enriched_ctx.parsed_ticket_details.description}\n\n---\nRequested by <@{user_id}>",
                        issue_type_name=similarity_context.selected_issue_type.name,
                        components=None,
                        dynamic_fields={}
                    )
                    final_ticket_ctx = FinalTicketCreationContext(slack_context=enriched_ctx.slack_context, jira_ticket_data=jira_data, status="ready_for_creation")
                    bot_state.current_mcp_stage = FinalTicketCreationContext.__name__
                    bot_state.context_data = final_ticket_ctx.model_dump(by_alias=True)
                    conversation_state_store[state_key] = bot_state.model_dump()
                    blocks = build_pre_creation_confirmation_blocks(final_ticket_ctx)
                    await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Confirm ticket details.")
        except Exception as e:
            logger.error(f"Error in 'select_jira_issue_type_action': {e}", exc_info=True)
            await client.chat_postMessage(channel=channel_id, text="Error selecting issue type.")

    # Handle dynamic field select submission
    elif action_id.startswith(f"{ACTION_ID_SUBMIT_DYNAMIC_FIELD_SELECT}_"):
        try:
            field_id_from_action = action_id.replace(f"{ACTION_ID_SUBMIT_DYNAMIC_FIELD_SELECT}_", "")
            sequential_ctx = SequentialFieldsInputContext.model_validate(current_mcp_context_data)

            current_field_index = sequential_ctx.current_field_prompt_index
            if current_field_index < len(sequential_ctx.fields_to_collect_sequentially):
                field_being_prompted = sequential_ctx.fields_to_collect_sequentially[current_field_index]
                if field_being_prompted.field_id == field_id_from_action:
                    logger.info(f"User {user_id} selected value '{action_value}' for dynamic field '{field_being_prompted.name}' (ID: {field_id_from_action})")
                    sequential_ctx.collected_dynamic_field_values[field_id_from_action] = action_value
                    sequential_ctx.current_field_prompt_index += 1

                    bot_state.context_data = sequential_ctx.model_dump(by_alias=True)
                    conversation_state_store[state_key] = bot_state.model_dump()

                    # Update the message that contained the dropdown
                    if original_message_ts:
                        await client.chat_update(
                            channel=channel_id, ts=original_message_ts,
                            text=f"You selected: {action_value} for {field_being_prompted.name}.",
                            blocks=[] # Clear the dropdown message
                        )

                    await _ask_for_next_required_field(client, channel_id, state_key, bot_state)
                    return
                else:
                    logger.warning(f"Received dynamic field select for '{field_id_from_action}' but expected '{field_being_prompted.field_id}'. Ignoring.")
            else:
                logger.warning("Received dynamic field select but no more fields were expected. Ignoring.")
        except Exception as e:
            logger.error(f"Error processing dynamic field select action '{action_id}': {e}", exc_info=True)
            await client.chat_postMessage(channel=channel_id, text="Error processing your selection for the dynamic field.")


    elif action_id == "create_new_ticket_anyway":
        # ... (ensure dynamic_fields_data is used from similarity_context)
        try:
            similarity_context = SimilarityCheckContext.model_validate(current_mcp_context_data)
            jira_data = JiraTicketData(
                project_key=similarity_context.selected_project.key,
                summary=similarity_context.parsed_ticket_details.summary,
                description=f"{similarity_context.parsed_ticket_details.description}\n\n---\nRequested by <@{user_id}>",
                issue_type_name=similarity_context.selected_issue_type.name,
                components=None,
                dynamic_fields=similarity_context.dynamic_fields_data or {}
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
            if original_message_ts:
                await client.chat_update(channel=channel_id, ts=original_message_ts, blocks=blocks, text="Please confirm ticket details:")
            else: # Should not happen often for this action
                await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Please confirm ticket details:")
        except Exception as e:
            logger.error(f"Error processing 'create_new_ticket_anyway': {e}", exc_info=True)
            await client.chat_postMessage(channel=channel_id, text="An error occurred. Please try again.")


    elif action_id == "confirm_create_ticket_action":
        # ... (ensure dynamic_fields is used from final_ticket_context.jira_ticket_data)
        try:
            final_ticket_context = FinalTicketCreationContext.model_validate(current_mcp_context_data)
            logger.debug(f"SLACK_HANDLER: JiraTicketData for creation: {final_ticket_context.jira_ticket_data.model_dump_json(indent=2, by_alias=True)}")
            if original_message_ts:
                await client.chat_update(channel=channel_id, ts=original_message_ts, text="Creating your Jira ticket, please wait...", blocks=[])
            else:
                await client.chat_postMessage(channel=channel_id, text="Creating your Jira ticket, please wait...")

            created_ticket_info: Optional[CreatedTicketInfo] = await create_jira_ticket(final_ticket_context.jira_ticket_data)
            if created_ticket_info:
                await client.chat_postMessage(channel=channel_id,
                    text=f"✅ Success! Jira ticket <{created_ticket_info.url}|{created_ticket_info.key}> created in project *{final_ticket_context.jira_ticket_data.project_key}*.")
                conversation_state_store.pop(state_key, None)
            else:
                if original_message_ts:
                    await client.chat_update(channel=channel_id, ts=original_message_ts,
                        text="❌ Sorry, I couldn't create the Jira ticket. Please check the logs or try again.",
                        blocks=build_pre_creation_confirmation_blocks(final_ticket_context))
                else:
                     await client.chat_postMessage(channel=channel_id,
                        text="❌ Sorry, I couldn't create the Jira ticket. Please check the logs or try again.",
                        blocks=build_pre_creation_confirmation_blocks(final_ticket_context))
        except Exception as e:
            logger.error(f"Error processing 'confirm_create_ticket_action': {e}", exc_info=True)
            try:
                final_ticket_context_for_retry = FinalTicketCreationContext.model_validate(current_mcp_context_data)
                if original_message_ts:
                    await client.chat_update(channel=channel_id, ts=original_message_ts,
                        text="An unexpected error occurred. Please try again or cancel.",
                        blocks=build_pre_creation_confirmation_blocks(final_ticket_context_for_retry))
                else:
                     await client.chat_postMessage(channel=channel_id,
                        text="An unexpected error occurred. Please try again or cancel.",
                        blocks=build_pre_creation_confirmation_blocks(final_ticket_context_for_retry))
            except Exception as update_err:
                 logger.error(f"Failed to update Slack message with error: {update_err}", exc_info=True)
                 await client.chat_postMessage(channel=channel_id, text="An unexpected error. Please start over.")
                 conversation_state_store.pop(state_key, None)

    elif action_id in ["mark_duplicate", "cancel_creation_similarity", "cancel_creation_confirmation",
                       "cancel_creation_project_select", "cancel_creation_issue_type_select"]:
        cancel_text = "Okay, I've cancelled the ticket creation process."
        if action_id == "mark_duplicate": cancel_text = f"Okay, noted as duplicate of {action_value}. No new ticket created."
        try:
            if original_message_ts:
                await client.chat_update(channel=channel_id, ts=original_message_ts, text=cancel_text, blocks=[])
            else:
                await client.chat_postMessage(channel=channel_id, text=cancel_text)
        except SlackApiError as e:
            logger.warning(f"Failed to update original message for cancel action {action_id}: {e.response['error'] if e.response else str(e)}. Sending new message.")
            await client.chat_postMessage(channel=channel_id, text=cancel_text)
        conversation_state_store.pop(state_key, None)
    else:
        logger.warning(f"Unhandled interactive action_id: {action_id}")

# Note: handle_dynamic_fields_submission (for modals) is removed as we are switching to sequential messages.
