import httpx # Using httpx for async requests, or 'anthropic' SDK
from anthropic import Anthropic, AsyncAnthropic # Ensure anthropic library is installed
from typing import Optional, List
import json # For loading the extracted JSON string
import re # For regular expression-based JSON extraction

from .config import settings, logger
from .mcp_models import ParsedTicketDetails

# Initialize the Anthropic client
try:
    async_client = AsyncAnthropic(api_key=settings.claude_api_key)
except Exception as e:
    logger.error(f"Failed to initialize Anthropic client: {e}")
    async_client = None

PROMPT_EXTRACT_TICKET_DETAILS = """
You are an assistant helping to parse user requests into Jira tickets.
Extract the summary, description, and suggested issue type from the following user request.
The issue type should be one of: 'Bug', 'Task', 'Story'.
If the description is short or not explicitly provided, use the summary as the description, or elaborate slightly if appropriate.
Format the output strictly as a JSON object with keys 'summary', 'description', and 'issueType'. Do not include any other text, comments, or markdown formatting around the JSON.

User Request: "{user_text}"

JSON Output:
"""

PROMPT_SEMANTIC_SIMILARITY = """
On a scale of 0.0 to 1.0, how semantically similar are the following two issue descriptions?
Only provide the score as a float (e.g., 0.75). Do not add any other text.

Description 1: "{desc1}"
Description 2: "{desc2}"

Score:
"""

async def extract_ticket_details_from_text(user_text: str) -> Optional[ParsedTicketDetails]:
    """
    Uses Claude API to extract ticket details from user text with robust JSON parsing.
    """
    if not async_client:
        logger.error("Anthropic client not initialized. Cannot extract ticket details.")
        return None

    prompt = PROMPT_EXTRACT_TICKET_DETAILS.format(user_text=user_text)
    try:
        logger.info(f"Sending request to Claude for details extraction for text: {user_text[:50]}...")
        response = await async_client.messages.create(
            model="claude-3-opus-20240229",
            max_tokens=500,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        if response.content and response.content[0].type == "text":
            raw_response_text = response.content[0].text.strip()
            logger.debug(f"Claude raw response for extraction: {raw_response_text}")

            # --- More Robust JSON Extraction Logic using regex ---
            # This regex looks for a string that starts with { and ends with }
            # It handles nested braces/brackets.
            match = re.search(r'(\{.*\})', raw_response_text, re.DOTALL)

            json_candidate_str: Optional[str] = None
            if match:
                json_candidate_str = match.group(1) # Get the content of the first capturing group
                logger.debug(f"Regex extracted JSON candidate: {json_candidate_str}")
            else:
                # Fallback: if regex fails, try the simpler find method (less reliable for complex cases)
                logger.warning("Regex JSON extraction failed, trying find method.")
                json_start_index = raw_response_text.find('{')
                json_end_index = raw_response_text.rfind('}')
                if json_start_index != -1 and json_end_index != -1 and json_end_index > json_start_index:
                    json_candidate_str = raw_response_text[json_start_index : json_end_index + 1]
                    logger.debug(f"Find method extracted JSON candidate: {json_candidate_str}")

            if json_candidate_str:
                try:
                    # Validate and parse the extracted string as JSON first
                    parsed_json_data = json.loads(json_candidate_str)
                    # Then validate with Pydantic
                    parsed_data = ParsedTicketDetails.model_validate(parsed_json_data)
                    logger.info(f"Successfully parsed ticket details from Claude: {parsed_data}")
                    return parsed_data
                except json.JSONDecodeError as json_err:
                    logger.error(f"Failed to decode extracted JSON string: {json_err}. String was: '{json_candidate_str}'")
                    return None
                except Exception as pydantic_err: # Catch Pydantic validation errors specifically
                    # Ensure parsed_json_data is defined before trying to log it
                    loggable_parsed_json = parsed_json_data if 'parsed_json_data' in locals() else 'N/A'
                    logger.error(f"Pydantic validation error after JSON parsing: {pydantic_err}. JSON data was: '{loggable_parsed_json}'")
                    return None
            else:
                logger.error(f"Could not find a JSON-like structure in Claude's response: '{raw_response_text}'")
                return None
            # --- End of More Robust JSON Extraction Logic ---
        else:
            logger.error(f"Unexpected response structure from Claude: {response}")
            return None

    except Exception as e:
        logger.error(f"Error calling Claude API for extraction: {e}")
        return None

async def get_semantic_similarity_score(desc1: str, desc2: str) -> Optional[float]:
    if not async_client:
        logger.error("Anthropic client not initialized. Cannot get similarity score.")
        return None
    prompt = PROMPT_SEMANTIC_SIMILARITY.format(desc1=desc1, desc2=desc2)
    try:
        logger.info("Sending request to Claude for semantic similarity...")
        response = await async_client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}]
        )
        if response.content and response.content[0].type == "text":
            score_str = response.content[0].text.strip()
            logger.debug(f"Claude raw response for similarity: {score_str}")
            try:
                return float(score_str)
            except ValueError:
                logger.error(f"Claude response for similarity is not a valid float: {score_str}")
                return None
        else:
            logger.error(f"Unexpected response structure from Claude for similarity: {response}")
            return None
    except Exception as e:
        logger.error(f"Error calling Claude API for similarity: {e}")
        return None

async def generate_keywords_from_text(text: str) -> List[str]:
    logger.warning("Keyword generation is a placeholder.")
    return [word for word in text.lower().split() if len(word) > 3][:5]
