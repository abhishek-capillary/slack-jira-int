from jira import JIRA, JIRAError
from typing import List, Optional, Dict, Any
from .config import settings, logger
from .mcp_models import JiraTicketData, SimilarTicketInfo, CreatedTicketInfo, JiraProject # Added JiraProject

jira_client: Optional[JIRA] = None

def get_jira_client() -> Optional[JIRA]:
    global jira_client
    if jira_client is None:
        try:
            logger.info(f"Initializing Jira client for server: {settings.jira_server}")
            # Ensure your settings object correctly provides jira_server, jira_username, and jira_api_token
            options = {'server': settings.jira_server}
            jira_client = JIRA(options, basic_auth=(settings.jira_username, settings.jira_api_token))
            jira_client.server_info() # Test connection
            logger.info("Jira client initialized successfully.")
        except JIRAError as e:
            logger.error(f"Failed to connect to Jira. Status: {e.status_code}, Text: {e.text}", exc_info=True)
            jira_client = None
        except Exception as e:
            logger.error(f"An unexpected error occurred during Jira client initialization: {e}", exc_info=True)
            jira_client = None
    return jira_client

async def get_available_jira_projects() -> List[JiraProject]:
    """Fetches all accessible Jira projects and returns them as a list of JiraProject models."""
    client = get_jira_client()
    if not client:
        logger.error("Jira client not available. Cannot fetch projects.")
        return []

    projects_info: List[JiraProject] = []
    try:
        logger.info("Fetching list of Jira projects...")
        # The jira.projects() call is synchronous, so we run it in a thread pool
        # if we were in a highly async context where blocking is an issue.
        # For startup or less frequent calls, direct call might be acceptable.
        # However, FastAPI runs handlers in an event loop, so blocking calls should be avoided.
        # For simplicity in this example, we'll make the call directly.
        # In a production FastAPI app, you might use `loop.run_in_executor`.
        # For now, let's assume this function is called in a context where a short block is okay (like startup)
        # or refactor if it needs to be truly non-blocking in an async path.

        # This is a synchronous call. If get_available_jira_projects itself needs to be
        # called from an async path frequently, consider using run_in_executor.
        raw_projects = client.projects()

        for proj in raw_projects:
            projects_info.append(
                JiraProject(
                    id=proj.id,
                    key=proj.key,
                    name=proj.name
                    # project_type_key=getattr(proj, 'projectTypeKey', None), # Example of accessing more attributes
                    # lead_name=getattr(getattr(proj, 'lead', {}), 'displayName', None)
                )
            )
        logger.info(f"Successfully fetched {len(projects_info)} Jira projects.")
    except JIRAError as e:
        logger.error(f"Jira API Error fetching projects: {e.status_code} - {e.text}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error fetching Jira projects: {e}", exc_info=True)

    return projects_info


async def create_jira_ticket(ticket_data: JiraTicketData) -> Optional[CreatedTicketInfo]:
    client = get_jira_client()
    if not client:
        logger.error("Jira client not available. Cannot create ticket.")
        return None

    # Base fields
    fields = {
        'project': {'key': ticket_data.project_key or settings.default_jira_project_key},
        'summary': ticket_data.summary,
        'description': ticket_data.description,
        'issuetype': {'name': ticket_data.issue_type_name},
    }

    if ticket_data.brand is not None:
        fields['customfield_11997'] = ticket_data.brand
        logger.debug(f"Added Brand (customfield_11997): {ticket_data.brand} to Jira payload")
    else:
        logger.warning("Brand value is None, not adding customfield_11997 to payload.")

    if ticket_data.environment is not None:
        fields['customfield_11800'] = ticket_data.environment
        logger.debug(f"Added Environment (customfield_11800): {ticket_data.environment} to Jira payload")
    else:
        logger.warning("Environment value is None, not adding customfield_11800 to payload.")

    if ticket_data.components:
        fields['components'] = ticket_data.components
        logger.debug(f"Added Components: {ticket_data.components} to Jira payload")
    else:
        logger.warning("Components value is None or empty, not adding to payload.")

    try:
        logger.info(f"Attempting to create Jira ticket with payload: {fields}")
        new_issue = client.create_issue(fields=fields)
        ticket_url = f"{settings.jira_server}/browse/{new_issue.key}"
        logger.info(f"Successfully created Jira ticket: {new_issue.key} - URL: {ticket_url}")
        return CreatedTicketInfo(key=new_issue.key, id=new_issue.id, url=ticket_url)
    except JIRAError as e:
        logger.error(
            f"Jira API Error creating ticket. Status: {e.status_code}. Text: {e.text}. URL: {e.url}. Request_Body: {fields}",
            exc_info=True
        )
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
        issues = client.search_issues(jql_query, maxResults=max_results, fields="summary,description,issuetype,project") # Added project to fields
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
