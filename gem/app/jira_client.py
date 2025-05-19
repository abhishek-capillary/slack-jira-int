from jira import JIRA, JIRAError
from typing import List, Optional, Dict, Any
from .config import settings, logger
from .mcp_models import ( # Ensure all used models are imported
    JiraTicketData, SimilarTicketInfo, CreatedTicketInfo,
    JiraProject, JiraIssueType # Added JiraIssueType
)

jira_client: Optional[JIRA] = None

def get_jira_client() -> Optional[JIRA]:
    """Initializes and returns the Jira client instance."""
    global jira_client
    if jira_client is None:
        try:
            logger.info(f"Initializing Jira client for server: {settings.jira_server}")
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
    """Fetches all accessible Jira projects."""
    client = get_jira_client()
    if not client:
        logger.error("Jira client not available. Cannot fetch projects.")
        return []
    projects_info: List[JiraProject] = []
    try:
        logger.info("Fetching list of Jira projects...")
        raw_projects = client.projects() # This is a synchronous call
        for proj in raw_projects:
            projects_info.append(
                JiraProject(id=proj.id, key=proj.key, name=proj.name)
            )
        logger.info(f"Successfully fetched {len(projects_info)} Jira projects.")
    except JIRAError as e:
        logger.error(f"Jira API Error fetching projects: {e.status_code} - {e.text}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error fetching Jira projects: {e}", exc_info=True)
    return projects_info

async def get_project_creatable_issue_types(project_key: str) -> List[JiraIssueType]:
    """
    Fetches creatable issue types for a specific project using the createmeta endpoint.
    """
    client = get_jira_client()
    if not client:
        logger.error(f"Jira client not available. Cannot fetch issue types for project {project_key}.")
        return []

    issue_types_info: List[JiraIssueType] = []
    try:
        logger.info(f"Fetching creatable issue types for project: {project_key}...")
        # The createmeta endpoint is the most reliable way to get creatable issue types.
        # It's a GET request. The jira-python library might not have a direct wrapper
        # for the exact structure of createmeta, so we might use _get_json or a direct session call.
        # For simplicity, let's assume client.createmeta exists or adapt.
        # A common way is client.createmeta(projectKeys=project_key, expand="projects.issuetypes")

        # The `jira` library's `createmeta` is a bit complex.
        # A more direct way to get issue types for a project is to fetch the project object
        # and then its issue types, but this might not guarantee "creatable" ones or all metadata.
        # Let's try fetching the project and its associated issue types first.
        # If this doesn't provide enough detail (like icons), we might need a raw REST call.

        project = client.project(project_key) # Synchronous call
        if project and hasattr(project, 'issueTypes') and project.issueTypes:
            for issuetype_ref in project.issueTypes:
                # The issuetype_ref might be a simplified reference. We might need to fetch full details.
                # For now, let's assume it has id and name.
                # To get more details like iconUrl, you might need client.issue_type(issuetype_ref.id)
                # but that's an extra API call per issue type.
                # Let's see what `project.issueTypes` gives us.
                # The objects in project.issueTypes are typically full IssueType objects.

                issue_types_info.append(
                    JiraIssueType(
                        id=issuetype_ref.id,
                        name=issuetype_ref.name,
                        description=getattr(issuetype_ref, 'description', None),
                        icon_url=getattr(issuetype_ref, 'iconUrl', None)
                        # subtask=getattr(issuetype_ref, 'subtask', False)
                    )
                )
            logger.info(f"Successfully fetched {len(issue_types_info)} issue types for project {project_key} via project details.")
        else:
            # Fallback or more robust method: use createmeta if the above is insufficient
            # This is a more complex call to parse correctly with the jira library.
            # For now, we'll rely on project.issueTypes.
            # If you need createmeta, it would look something like:
            # meta = client.createmeta(projectKeys=project_key, expand="projects.issuetypes")
            # if meta.get('projects'):
            #     for proj_meta in meta['projects']:
            #         if proj_meta['key'] == project_key:
            #             for it_meta in proj_meta['issuetypes']:
            #                 issue_types_info.append(JiraIssueType(id=it_meta['id'], name=it_meta['name'], ...))
            logger.warning(f"Could not retrieve issue types via project.issueTypes for {project_key}. Consider implementing createmeta.")

    except JIRAError as e:
        logger.error(f"Jira API Error fetching issue types for project {project_key}: {e.status_code} - {e.text}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error fetching issue types for project {project_key}: {e}", exc_info=True)

    return issue_types_info


async def create_jira_ticket(ticket_data: JiraTicketData) -> Optional[CreatedTicketInfo]:
    """Creates a Jira ticket with the provided data."""
    client = get_jira_client()
    if not client:
        logger.error("Jira client not available. Cannot create ticket.")
        return None

    fields = {
        'project': {'key': ticket_data.project_key}, # project_key should be set by now
        'summary': ticket_data.summary,
        'description': ticket_data.description,
        'issuetype': {'name': ticket_data.issue_type_name}, # Use the selected issue type name
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

    # Temporarily disable components due to permission issues
    # if ticket_data.components:
    #     fields['components'] = ticket_data.components
    #     logger.debug(f"Added Components: {ticket_data.components} to Jira payload")
    # else:
    #     logger.warning("Components value is None or empty, not adding to payload.")

    try:
        logger.info(f"Attempting to create Jira ticket with payload: {fields}")
        new_issue = client.create_issue(fields=fields) # Synchronous call
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
    # description_keywords: Optional[List[str]] = None, # Not currently used
    issue_types: Optional[List[str]] = None, # List of issue type names
    max_results: int = 5
) -> List[SimilarTicketInfo]:
    """Searches for similar Jira tickets based on summary and issue types."""
    client = get_jira_client()
    if not client:
        logger.error("Jira client not available. Cannot search tickets.")
        return []

    jql_conditions = [f'project = "{project_key}"']

    if issue_types:
        types_str = ", ".join([f'"{it}"' for it in issue_types])
        jql_conditions.append(f"issuetype IN ({types_str})")
    else: # Default if no specific issue types provided for search (e.g. after NLP but before user selection)
        jql_conditions.append("issuetype IN (Bug, Story, Task)")


    search_terms_jql_parts = []
    if summary:
        summary_escaped = summary.replace('"', '\\"')
        search_terms_jql_parts.append(f'summary ~ "{summary_escaped}"')

    # if description_keywords: # Example if you re-add keyword search
    #     for keyword in description_keywords:
    #         keyword_escaped = keyword.replace('"', '\\"')
    #         search_terms_jql_parts.append(f'description ~ "{keyword_escaped}"')

    if search_terms_jql_parts:
        jql_conditions.append(f"({' OR '.join(search_terms_jql_parts)})")

    jql_query_conditions = " AND ".join(jql_conditions)
    jql_query = f"{jql_query_conditions} ORDER BY created DESC"

    try:
        logger.info(f"Searching Jira with JQL: {jql_query}")
        # Request relevant fields for display or further processing
        issues = client.search_issues(jql_query, maxResults=max_results, fields="summary,issuetype,project")
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
