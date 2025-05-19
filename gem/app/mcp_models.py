from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class SlackContext(BaseModel):
    user_id: str
    channel_id: str
    team_id: Optional[str] = None
    thread_ts: Optional[str] = None

class InitialRequestContext(BaseModel):
    source: str = "slack"
    event_type: str
    user_id: str
    channel_id: str
    text: str
    timestamp: str
    slack_context: SlackContext

class ParsedTicketDetails(BaseModel):
    summary: str
    description: str
    issue_type: str = Field(..., alias='issueType')

class EnrichedTicketContext(BaseModel):
    """Context after initial NLP parsing of the user's request."""
    slack_context: SlackContext
    raw_request: str
    parsed_ticket_details: ParsedTicketDetails
    # No status here, this is just the parsed data before project selection

class JiraProject(BaseModel):
    """Represents essential information about a Jira project."""
    id: str
    key: str
    name: str

class ProjectSelectionContext(BaseModel):
    """Context when the bot is waiting for the user to select a Jira project."""
    enriched_ticket_context: EnrichedTicketContext # Contains parsed summary, desc, type
    available_projects: List[JiraProject]
    status: str = "pending_project_selection"

class SimilarTicketInfo(BaseModel):
    key: str
    summary: str
    url: str
    score: Optional[float] = None

class SimilarityCheckContext(BaseModel):
    """Context after project is selected and bot is checking for similar tickets (or waiting for user decision)."""
    slack_context: SlackContext # From EnrichedTicketContext
    raw_request: str            # From EnrichedTicketContext
    parsed_ticket_details: ParsedTicketDetails # From EnrichedTicketContext
    selected_project_key: str   # The project chosen by the user
    similar_tickets_found: List[SimilarTicketInfo] = []
    status: str = "pending_user_decision_on_similarity" # Or pending_confirmation if no similar found

class JiraTicketData(BaseModel):
    project_key: str # This will now be the user-selected project key
    summary: str
    description: str
    issue_type_name: str
    reporter_email: Optional[str] = None

    brand: Optional[str] = None
    environment: Optional[str] = None
    components: Optional[List[Dict[str, str]]] = None

class FinalTicketCreationContext(BaseModel):
    slack_context: SlackContext
    jira_ticket_data: JiraTicketData # Will include the user-selected project_key
    status: str = "ready_for_creation"

class CreatedTicketInfo(BaseModel):
    key: str
    id: str
    url: str

class CreationConfirmationContext(BaseModel):
    slack_context: SlackContext
    created_ticket_info: CreatedTicketInfo
    status: str = "ticket_created_successfully"

class BotStateData(BaseModel):
    user_id: str
    channel_id: str
    current_mcp_stage: Optional[str] = None # e.g., "ProjectSelectionContext", "SimilarityCheckContext"
    context_data: Dict[str, Any] # Store the actual MCP model as dict
    timestamp: float
