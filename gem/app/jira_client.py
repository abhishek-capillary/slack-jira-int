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
            # Ensure SSL verification is enabled by default, or configurable if needed.
            # For self-signed certs in dev, options might include 'verify': False, but not for prod.
            jira_client = JIRA(options, basic_auth=(settings.jira_username, settings.jira_api_token))
            # Test connection by fetching server info or projects
            # This implicitly tests authentication and server reachability.
            jira_client.server_info()
            logger.info("Jira client initialized successfully.")
        except JIRAError as e:
            logger.error(f"Failed to connect to Jira: {e.status_code} - {e.text}")
            jira_client = None # Ensure it stays None if init fails
        except Exception as e:
            logger.error(f"An unexpected error occurred during Jira client initialization: {e}")
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
        # 'reporter': {'name': ticket_data.reporter_email}, # Requires Jira user mapping or ensuring the user exists
        # Add other fields like priority, assignee, custom fields as needed
        # 'priority': {'name': ticket_data.priority_name} if ticket_data.priority_name else None,
    }
    # Filter out None values from fields that might be optional
    fields = {k: v for k, v in fields.items() if v is not None}

    try:
        logger.info(f"Creating Jira ticket with data: {fields}")
        new_issue = client.create_issue(fields=fields)
        ticket_url = f"{settings.jira_server}/browse/{new_issue.key}"
        logger.info(f"Successfully created Jira ticket: {new_issue.key} - {ticket_url}")
        return CreatedTicketInfo(key=new_issue.key, id=new_issue.id, url=ticket_url)
    except JIRAError as e:
        logger.error(f"Jira API Error creating ticket: {e.status_code} - {e.text}")
        # You might want to parse e.text for more specific error messages to return to the user
        return None
    except Exception as e:
        logger.error(f"Unexpected error creating Jira ticket: {e}")
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

    jql_conditions = [] # Store individual conditions here

    if project_key:
        jql_conditions.append(f'project = "{project_key}"')
    else: # Fallback to default if not specified for search
        jql_conditions.append(f'project = "{settings.default_jira_project_key}"')


    if issue_types:
        types_str = ", ".join([f'"{it}"' for it in issue_types])
        jql_conditions.append(f"issuetype IN ({types_str})")
    else: # Default search issue types
        jql_conditions.append("issuetype IN (Bug, Story, Task)") # As per plan

    search_terms_jql_parts = []
    if summary:
        # Escape special characters for JQL summary search.
        # JQL uses backslash to escape: ", \, etc.
        # A more robust solution might involve a regex or a dedicated escaping function.
        summary_escaped = summary.replace('"', '\\"')
        search_terms_jql_parts.append(f'summary ~ "{summary_escaped}"')

    if description_keywords:
        for keyword in description_keywords:
            keyword_escaped = keyword.replace('"', '\\"')
            search_terms_jql_parts.append(f'description ~ "{keyword_escaped}"')

    if search_terms_jql_parts:
        # Join multiple search terms with OR inside parentheses
        jql_conditions.append(f"({' OR '.join(search_terms_jql_parts)})")

    # Join all conditions with "AND"
    jql_query_conditions = " AND ".join(jql_conditions)

    # Append the ORDER BY clause correctly
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
                # Description can be added if needed for semantic similarity scoring later
                # description=issue.fields.description
            ))
        logger.info(f"Found {len(similar_tickets)} potentially similar tickets.")
        return similar_tickets
    except JIRAError as e:
        logger.error(f"Jira API Error searching tickets: {e.status_code} - {e.text}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error searching Jira tickets: {e}")
        return []

# You might want to add functions to get project details or issue types dynamically if needed
# async def get_jira_project_issue_types(project_key: str): ...
