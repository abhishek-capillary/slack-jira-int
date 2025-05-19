from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class SlackContext(BaseModel):
    user_id: str
    channel_id: str
    team_id: Optional[str] = None
    thread_ts: Optional[str] = None # If handling threaded replies

class InitialRequestContext(BaseModel):
    source: str = "slack"
    event_type: str
    user_id: str
    channel_id: str
    text: str
    timestamp: str
    slack_context: SlackContext # For easier passing

class ParsedTicketDetails(BaseModel):
    summary: str
    description: str
    issue_type: str # e.g., "Bug", "Task", "Story" - map to Jira's actual types

class EnrichedTicketContext(BaseModel):
    slack_context: SlackContext
    raw_request: str
    parsed_ticket_details: ParsedTicketDetails
    status: str = "pending_similarity_check"
    # You might store user object from slack here too if needed

class SimilarTicketInfo(BaseModel):
    key: str
    summary: str
    url: str
    score: Optional[float] = None # Optional similarity score

class SimilarityCheckContext(BaseModel):
    enriched_context: EnrichedTicketContext
    similar_tickets_found: List[SimilarTicketInfo] = []
    status: str = "pending_user_decision_on_similarity"

class JiraTicketData(BaseModel):
    project_key: str
    summary: str
    description: str
    issue_type_name: str # Name like "Bug", "Task". Map to ID if Jira needs it.
    reporter_email: Optional[str] = None # Or Jira account ID
    # priority_name: Optional[str] = None
    # assignee_name: Optional[str] = None
    # custom_fields: Optional[Dict[str, Any]] = None

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
    """
    A model to hold transient state for a user interaction flow,
    e.g., when waiting for user confirmation after similarity check.
    Key could be user_id or channel_id or a combination.
    """
    user_id: str
    channel_id: str
    current_mcp_stage: Optional[str] = None # e.g. "SimilarityCheckContext"
    context_data: Dict[str, Any] # Store the actual MCP model as dict
    timestamp: float # To expire old states