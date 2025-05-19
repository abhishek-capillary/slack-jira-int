import httpx # Using httpx for async requests, or 'anthropic' SDK
from anthropic import Anthropic, AsyncAnthropic # Ensure anthropic library is installed
from typing import Optional, List # Added Optional and List here
from .config import settings, logger
from .mcp_models import ParsedTicketDetails

# Initialize the Anthropic client
# Ensure CLAUDE_API_KEY is set in your environment
try:
    # client = Anthropic() # For synchronous
    async_client = AsyncAnthropic(api_key=settings.claude_api_key) # For asynchronous
except Exception as e:
    logger.error(f"Failed to initialize Anthropic client: {e}")
    async_client = None

# Store prompts here or in a separate config file
PROMPT_EXTRACT_TICKET_DETAILS = """
You are an assistant helping to parse user requests into Jira tickets.
Extract the summary, description, and suggested issue type from the following user request.
The issue type should be one of: 'Bug', 'Task', 'Story'.
If the description is short or not explicitly provided, use the summary as the description, or elaborate slightly if appropriate.
Format the output as a JSON object with keys 'summary', 'description', and 'issueType'.

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
    Uses Claude API to extract ticket details from user text.
    """
    if not async_client:
        logger.error("Anthropic client not initialized. Cannot extract ticket details.")
        return None

    prompt = PROMPT_EXTRACT_TICKET_DETAILS.format(user_text=user_text)
    try:
        logger.info(f"Sending request to Claude for details extraction for text: {user_text[:50]}...")
        response = await async_client.messages.create(
            model="claude-3-opus-20240229", # Or your preferred model, e.g., claude-3-haiku-20240307 for speed
            max_tokens=500,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        # The response structure for claude-3 Messages API is a list of content blocks
        # Assuming the first content block is of type 'text' and contains the JSON
        if response.content and response.content[0].type == "text":
            json_response_str = response.content[0].text.strip()
            # Claude might sometimes wrap the JSON in ```json ... ```
            if json_response_str.startswith("```json"):
                json_response_str = json_response_str.replace("```json", "").replace("```", "").strip()

            logger.debug(f"Claude raw response for extraction: {json_response_str}")
            parsed_data = ParsedTicketDetails.model_validate_json(json_response_str)
            logger.info(f"Successfully parsed ticket details from Claude: {parsed_data}")
            return parsed_data
        else:
            logger.error(f"Unexpected response structure from Claude: {response}")
            return None

    except Exception as e:
        logger.error(f"Error calling Claude API for extraction: {e}")
        return None

async def get_semantic_similarity_score(desc1: str, desc2: str) -> Optional[float]:
    """
    Uses Claude API to get a semantic similarity score between two descriptions.
    """
    if not async_client:
        logger.error("Anthropic client not initialized. Cannot get similarity score.")
        return None

    prompt = PROMPT_SEMANTIC_SIMILARITY.format(desc1=desc1, desc2=desc2)
    try:
        logger.info("Sending request to Claude for semantic similarity...")
        response = await async_client.messages.create(
            model="claude-3-haiku-20240307", # A faster model might be good here
            max_tokens=50,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        if response.content and response.content[0].type == "text":
            score_str = response.content[0].text.strip()
            logger.debug(f"Claude raw response for similarity: {score_str}")
            return float(score_str)
        else:
            logger.error(f"Unexpected response structure from Claude for similarity: {response}")
            return None
    except Exception as e:
        logger.error(f"Error calling Claude API for similarity: {e}")
        return None

# Placeholder for keyword generation (as described in 2.5.2)
async def generate_keywords_from_text(text: str) -> List[str]:
    """
    (Placeholder) Uses Claude or a simpler NLP technique to extract keywords.
    """
    # Example: Could be another Claude call or local NLP (e.g., spaCy, NLTK if added)
    logger.warning("Keyword generation is a placeholder.")
    # For now, just split and take some words, you'll want a better approach
    return [word for word in text.lower().split() if len(word) > 3][:5]
