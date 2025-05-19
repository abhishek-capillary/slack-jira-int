from jira import JIRA, JIRAError
from typing import List, Optional, Dict, Any
from .config import settings, logger
from .mcp_models import JiraTicketData, SimilarTicketInfo, CreatedTicketInfo

jira_client: Optional[JIRA] = None

def get_jira_client() -> Optional[JIRA]:
    global jira_client
    if jira_client is None:
        try:
            logger.info(f"Initializing Jira client for server: {settings.jira_server}")
            options = {'server': settings.jira_server}
            # Ensure SSL verification is enabled by default.
            # For self-signed certs in dev, options might include 'verify': False, but not for prod.
            jira_client = JIRA(options, basic_auth=(settings.jira_username, settings.jira_api_token))
            jira_client.server_info() # Test connection
            logger.info("Jira client initialized successfully.")
        except JIRAError as e:
            # Log the full text of the JIRAError, which often contains JSON from Jira
            logger.error(f"Failed to connect to Jira. Status: {e.status_code}, Text: {e.text}", exc_info=True)
            jira_client = None
        except Exception as e:
            logger.error(f"An unexpected error occurred during Jira client initialization: {e}", exc_info=True)
            jira_client = None
    return jira_client

async def create_jira_ticket(ticket_data: JiraTicketData) -> Optional[CreatedTicketInfo]:
    client = get_jira_client()
    if not client:
        logger.error("Jira client not available. Cannot create ticket.")
        return None

    fields = {
        'project': {'key': ticket_data.project_key or settings.default_jira_project_key},
        'summary': ticket_data.summary,
        'description': ticket_data.description,
        'issuetype': {'name': ticket_data.issue_type_name},
        # 'reporter': {'name': ticket_data.reporter_email}, # Ensure this user exists in Jira and bot has permissions
        # Add other fields like priority, assignee, custom fields as needed
        # 'priority': {'name': ticket_data.priority_name} if ticket_data.priority_name else None,
    }
    fields = {k: v for k, v in fields.items() if v is not None} # Filter out None values

    try:
        logger.info(f"Attempting to create Jira ticket with payload: {fields}")
        new_issue = client.create_issue(fields=fields)
        ticket_url = f"{settings.jira_server}/browse/{new_issue.key}"
        logger.info(f"Successfully created Jira ticket: {new_issue.key} - URL: {ticket_url}")
        return CreatedTicketInfo(key=new_issue.key, id=new_issue.id, url=ticket_url)
    except JIRAError as e:
        # Log the status code and the full text of the JIRAError, which often contains detailed JSON from Jira
        logger.error(
            f"Jira API Error creating ticket. Status: {e.status_code}. Text: {e.text}. URL: {e.url}. Request_Body: {fields}",
            exc_info=True # This will include the stack trace for the JIRAError
        )
        # You might want to parse e.text for more specific error messages to return to the user in Slack
        return None
    except Exception as e:
        logger.error(f"Unexpected error creating Jira ticket: {e}. Payload was: {fields}", exc_info=True)
        return None

async def search_similar_jira_tickets(
    project_key: str,
    summary: Optional[str] = None,
    description_keywords: Optional[List[str]] = None,
    issue_types: Optional[List[str]] = None,
    max_results: int = 5
) -> List[SimilarTicketInfo]:
    client = get_jira_client()
    if not client:
        logger.error("Jira client not available. Cannot search tickets.")
        return []

    jql_conditions = []
    if project_key:
        jql_conditions.append(f'project = "{project_key}"')
    else:
        jql_conditions.append(f'project = "{settings.default_jira_project_key}"')

    if issue_types:
        types_str = ", ".join([f'"{it}"' for it in issue_types])
        jql_conditions.append(f"issuetype IN ({types_str})")
    else:
        jql_conditions.append("issuetype IN (Bug, Story, Task)")

    search_terms_jql_parts = []
    if summary:
        summary_escaped = summary.replace('"', '\\"')
        search_terms_jql_parts.append(f'summary ~ "{summary_escaped}"')

    if description_keywords:
        for keyword in description_keywords:
            keyword_escaped = keyword.replace('"', '\\"')
            search_terms_jql_parts.append(f'description ~ "{keyword_escaped}"')

    if search_terms_jql_parts:
        jql_conditions.append(f"({' OR '.join(search_terms_jql_parts)})")

    jql_query_conditions = " AND ".join(jql_conditions)
    jql_query = f"{jql_query_conditions} ORDER BY created DESC"

    try:
        logger.info(f"Searching Jira with JQL: {jql_query}")
        issues = client.search_issues(jql_query, maxResults=max_results, fields="summary,description,issuetype")
        similar_tickets = []
        for issue in issues:
            ticket_url = f"{settings.jira_server}/browse/{issue.key}"
            similar_tickets.append(SimilarTicketInfo(
                key=issue.key,
                summary=issue.fields.summary,
                url=ticket_url
            ))
        logger.info(f"Found {len(similar_tickets)} potentially similar tickets.")
        return similar_tickets
    except JIRAError as e:
        logger.error(f"Jira API Error searching tickets. Status: {e.status_code}, Text: {e.text}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Unexpected error searching Jira tickets: {e}", exc_info=True)
        return []
