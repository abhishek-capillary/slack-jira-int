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
    """Details parsed from the user's initial text by NLP."""
    summary: str
    description: str
    issue_type: str = Field(..., alias='issueType') # NLP's suggestion for issue type

class EnrichedTicketContext(BaseModel):
    """Context after initial NLP parsing of the user's request."""
    slack_context: SlackContext
    raw_request: str
    parsed_ticket_details: ParsedTicketDetails

class JiraProject(BaseModel):
    """Represents essential information about a Jira project."""
    id: str
    key: str
    name: str

class JiraIssueType(BaseModel):
    """Represents an issue type available in Jira for a project."""
    id: str
    name: str
    description: Optional[str] = None
    icon_url: Optional[str] = Field(None, alias="iconUrl")
    # subtask: bool = False # createmeta might not directly give this easily, but good to have if needed

class ProjectSelectionContext(BaseModel):
    """Context when the bot is waiting for the user to select a Jira project."""
    enriched_ticket_context: EnrichedTicketContext
    available_projects: List[JiraProject]
    status: str = "pending_project_selection"

class IssueTypeSelectionContext(BaseModel):
    """Context when the bot is waiting for the user to select an issue type for the chosen project."""
    enriched_ticket_context: EnrichedTicketContext # Contains original parsed summary, desc, NLP issue type
    selected_project: JiraProject # The project chosen by the user
    available_issue_types: List[JiraIssueType] # Fetched for the selected_project
    status: str = "pending_issue_type_selection"

class SimilarTicketInfo(BaseModel):
    key: str
    summary: str
    url: str
    score: Optional[float] = None

class SimilarityCheckContext(BaseModel):
    """Context after project and issue type are selected, and bot is checking for similar tickets."""
    slack_context: SlackContext
    raw_request: str
    parsed_ticket_details: ParsedTicketDetails # Original NLP parsed details
    selected_project: JiraProject
    selected_issue_type: JiraIssueType # The issue type chosen by the user
    similar_tickets_found: List[SimilarTicketInfo] = []
    status: str = "pending_user_decision_on_similarity"

class JiraTicketData(BaseModel):
    """Data structure for creating the final Jira ticket."""
    project_key: str
    summary: str
    description: str
    issue_type_name: str # The name of the user-selected (or validated NLP) issue type
    reporter_email: Optional[str] = None

    brand: Optional[str] = None
    environment: Optional[str] = None
    components: Optional[List[Dict[str, str]]] = None

class FinalTicketCreationContext(BaseModel):
    slack_context: SlackContext
    jira_ticket_data: JiraTicketData
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
    current_mcp_stage: Optional[str] = None
    context_data: Dict[str, Any]
    timestamp: float
