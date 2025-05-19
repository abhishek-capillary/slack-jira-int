from jira import JIRA, JIRAError
from typing import List, Optional, Dict, Any
import asyncio # For running sync code in async context

from .config import settings, logger
from .mcp_models import (
    JiraTicketData, SimilarTicketInfo, CreatedTicketInfo,
    JiraProject, JiraIssueType,
    RequiredFieldDetail, AllowedValue # Added new models
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
            jira_client.server_info()
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
    if not client: return []
    projects_info: List[JiraProject] = []
    try:
        logger.info("Fetching list of Jira projects...")
        # Run synchronous client.projects() in a thread to avoid blocking asyncio event loop
        raw_projects = await asyncio.to_thread(client.projects)
        for proj in raw_projects:
            projects_info.append(JiraProject(id=proj.id, key=proj.key, name=proj.name))
        logger.info(f"Successfully fetched {len(projects_info)} Jira projects.")
    except Exception as e: # Catch generic exception as well for robustness
        logger.error(f"Error fetching Jira projects: {e}", exc_info=True)
    return projects_info

async def get_project_creatable_issue_types(project_key: str) -> List[JiraIssueType]:
    """Fetches creatable issue types for a specific project."""
    client = get_jira_client()
    if not client: return []
    issue_types_info: List[JiraIssueType] = []
    try:
        logger.info(f"Fetching issue types for project: {project_key}...")
        project = await asyncio.to_thread(client.project, project_key)
        if project and hasattr(project, 'issueTypes') and project.issueTypes:
            for issuetype_object in project.issueTypes:
                issue_types_info.append(
                    JiraIssueType(
                        id=issuetype_object.id,
                        name=issuetype_object.name,
                        description=getattr(issuetype_object, 'description', None),
                        icon_url=getattr(issuetype_object, 'iconUrl', None)
                    )
                )
            logger.info(f"Successfully fetched {len(issue_types_info)} issue types for project {project_key} via project details.")
        else:
            logger.warning(f"Could not retrieve issue types via project.issueTypes for {project_key}.")
    except Exception as e:
        logger.error(f"Error fetching issue types for project {project_key}: {e}", exc_info=True)
    return issue_types_info

async def get_required_fields_for_issue_type(project_key: str, issue_type_name: str) -> List[RequiredFieldDetail]:
    """
    Fetches required fields for a given project and issue type using createmeta.
    Returns a list of RequiredFieldDetail objects.
    """
    client = get_jira_client()
    if not client:
        logger.error("Jira client not available. Cannot fetch required fields.")
        return []

    required_fields_list: List[RequiredFieldDetail] = []
    try:
        logger.info(f"Fetching createmeta for project '{project_key}', issue type '{issue_type_name}'...")

        # The createmeta call is synchronous
        meta = await asyncio.to_thread(
            client.createmeta,
            projectKeys=project_key,
            issuetypeNames=issue_type_name,
            expand="projects.issuetypes.fields"
        )

        if not meta.get('projects'):
            logger.error(f"No 'projects' array found in createmeta response for PK='{project_key}', IT='{issue_type_name}'. Response: {meta}")
            return []

        # Assuming the first project and first issue type if multiple are returned (should be specific enough)
        project_meta = meta['projects'][0]
        if not project_meta.get('issuetypes'):
            logger.error(f"No 'issuetypes' array found for project '{project_key}' in createmeta. Response: {project_meta}")
            return []

        issue_type_meta = project_meta['issuetypes'][0]
        fields_meta = issue_type_meta.get('fields', {})

        for field_id, field_info in fields_meta.items():
            if field_info.get("required"):
                allowed_values_data = field_info.get("allowedValues")
                parsed_allowed_values: Optional[List[AllowedValue]] = None
                if allowed_values_data:
                    parsed_allowed_values = []
                    for val_data in allowed_values_data:
                        # Adapt based on actual structure of allowedValues items
                        # Common fields are 'id', 'name', 'value'
                        parsed_allowed_values.append(
                            AllowedValue(
                                id=val_data.get('id'),
                                name=val_data.get('name'),
                                value=val_data.get('value')
                                # Add other fields as necessary from val_data
                            )
                        )

                required_fields_list.append(
                    RequiredFieldDetail(
                        field_id=field_id, # field_id is the key from fields_meta
                        name=field_info.get("name", "Unknown Field Name"),
                        is_custom=field_id.startswith("customfield_"),
                        allowed_values=parsed_allowed_values
                        # schema=field_info.get("schema") # If you need the full schema
                    )
                )
                print(f"Required field: {required_fields_list}")
        logger.info(f"Found {len(required_fields_list)} required fields for PK='{project_key}', IT='{issue_type_name}'.")

    except JIRAError as e:
        logger.error(f"Jira API Error fetching createmeta for PK='{project_key}', IT='{issue_type_name}': {e.status_code} - {e.text}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error fetching createmeta for PK='{project_key}', IT='{issue_type_name}': {e}", exc_info=True)

    return required_fields_list


async def create_jira_ticket(ticket_data: JiraTicketData) -> Optional[CreatedTicketInfo]:
    """Creates a Jira ticket with the provided data, including dynamic fields."""
    client = get_jira_client()
    if not client: return None

    fields = {
        'project': {'key': ticket_data.project_key},
        'summary': ticket_data.summary,
        'description': ticket_data.description,
        'issuetype': {'name': ticket_data.issue_type_name},
    }

    # Add standard custom fields if they exist and have values
    if ticket_data.brand is not None: fields['customfield_11997'] = ticket_data.brand
    if ticket_data.environment is not None: fields['customfield_11800'] = ticket_data.environment
    # if ticket_data.components: fields['components'] = ticket_data.components # Still commented out

    # Add dynamically fetched required fields
    if ticket_data.dynamic_fields:
        for field_id, field_value in ticket_data.dynamic_fields.items():
            if field_value is not None: # Only add if a value was provided
                fields[field_id] = field_value
                logger.debug(f"Added dynamic field {field_id}: {field_value} to Jira payload")
            else:
                logger.warning(f"Dynamic field {field_id} has None value, not adding to payload.")

    try:
        logger.info(f"Attempting to create Jira ticket with payload: {fields}")
        new_issue = await asyncio.to_thread(client.create_issue, fields=fields)
        ticket_url = f"{settings.jira_server}/browse/{new_issue.key}"
        logger.info(f"Successfully created Jira ticket: {new_issue.key} - URL: {ticket_url}")
        return CreatedTicketInfo(key=new_issue.key, id=new_issue.id, url=ticket_url)
    except JIRAError as e:
        logger.error(f"Jira API Error creating ticket. Status: {e.status_code}. Text: {e.text}. URL: {e.url}. Request_Body: {fields}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Unexpected error creating Jira ticket: {e}. Payload was: {fields}", exc_info=True)
        return None

async def search_similar_jira_tickets(
    project_key: str, summary: Optional[str] = None,
    issue_types: Optional[List[str]] = None, max_results: int = 5
) -> List[SimilarTicketInfo]:
    client = get_jira_client()
    if not client: return []
    jql_conditions = [f'project = "{project_key}"']
    if issue_types:
        types_str = ", ".join([f'"{it}"' for it in issue_types])
        jql_conditions.append(f"issuetype IN ({types_str})")
    else:
        jql_conditions.append("issuetype IN (Bug, Story, Task)")
    search_terms_jql_parts = []
    if summary:
        summary_escaped = summary.replace('"', '\\"')
        search_terms_jql_parts.append(f'summary ~ "{summary_escaped}"')
    if search_terms_jql_parts:
        jql_conditions.append(f"({' OR '.join(search_terms_jql_parts)})")
    jql_query = " AND ".join(jql_conditions) + " ORDER BY created DESC"
    try:
        logger.info(f"Searching Jira with JQL: {jql_query}")
        issues = await asyncio.to_thread(client.search_issues, jql_query, maxResults=max_results, fields="summary,issuetype,project")
        similar_tickets = [SimilarTicketInfo(key=issue.key, summary=issue.fields.summary, url=f"{settings.jira_server}/browse/{issue.key}") for issue in issues]
        logger.info(f"Found {len(similar_tickets)} potentially similar tickets.")
        return similar_tickets
    except Exception as e:
        logger.error(f"Error searching Jira tickets: {e}", exc_info=True)
        return []
