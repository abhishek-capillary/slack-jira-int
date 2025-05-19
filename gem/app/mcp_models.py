from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

# --- Slack Context and Initial Request ---
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

# --- NLP and Project/Issue Type Selection ---
class ParsedTicketDetails(BaseModel):
    """Details parsed from the user's initial text by NLP."""
    summary: str
    description: str
    issue_type: str = Field(..., alias='issueType') # NLP's suggestion

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

class ProjectSelectionContext(BaseModel):
    """Context when the bot is waiting for the user to select a Jira project."""
    enriched_ticket_context: EnrichedTicketContext
    available_projects: List[JiraProject]
    status: str = "pending_project_selection"

class IssueTypeSelectionContext(BaseModel):
    """Context when the bot is waiting for the user to select an issue type for the chosen project."""
    enriched_ticket_context: EnrichedTicketContext
    selected_project: JiraProject
    available_issue_types: List[JiraIssueType]
    status: str = "pending_issue_type_selection"

# --- Required Field Details from Createmeta ---
class AllowedValue(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    value: Optional[str] = None

class RequiredFieldDetail(BaseModel):
    field_id: str
    name: str
    is_custom: bool = Field(False, description="Indicates if the field is a custom field")
    allowed_values: Optional[List[AllowedValue]] = Field(None, alias="allowedValues")
    # schema_type: Optional[str] = None # To store field_info['schema']['type'] if needed

class SequentialFieldsInputContext(BaseModel): # RENAMED and RESTRUCTURED
    """Context for sequentially collecting dynamically required Jira fields."""
    enriched_ticket_context: EnrichedTicketContext
    selected_project: JiraProject
    selected_issue_type: JiraIssueType

    # List of fields that still need to be collected from the user.
    # This list will be filtered to exclude summary, description, project, issuetype.
    fields_to_collect_sequentially: List[RequiredFieldDetail]

    # Index of the current field in 'fields_to_collect_sequentially' being prompted for.
    current_field_prompt_index: int = 0

    # Stores values as {field_id: value} as they are collected.
    collected_dynamic_field_values: Dict[str, Any] = Field(default_factory=dict)

    status: str = "pending_sequential_field_input"


# --- Similarity and Final Ticket Data ---
class SimilarTicketInfo(BaseModel):
    key: str
    summary: str
    url: str
    score: Optional[float] = None

class SimilarityCheckContext(BaseModel):
    slack_context: SlackContext
    raw_request: str
    parsed_ticket_details: ParsedTicketDetails
    selected_project: JiraProject
    selected_issue_type: JiraIssueType
    dynamic_fields_data: Optional[Dict[str, Any]] = None # User-provided values for dynamic fields
    similar_tickets_found: List[SimilarTicketInfo] = []
    status: str = "pending_user_decision_on_similarity"

class JiraTicketData(BaseModel):
    project_key: str
    summary: str
    description: str
    issue_type_name: str
    reporter_email: Optional[str] = None
    components: Optional[List[Dict[str, str]]] = None
    dynamic_fields: Optional[Dict[str, Any]] = None # Stores {field_id: value} for user-provided dynamic fields

# --- Final Contexts and Bot State ---
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
