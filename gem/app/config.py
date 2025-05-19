import os
import pathlib
# from dotenv import load_dotenv # We will let pydantic-settings handle .env loading

# Import BaseSettings and SettingsConfigDict from pydantic_settings
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field # Field is still imported from pydantic

# This print statement might show None if Pydantic hasn't loaded settings yet,
# or if the variable isn't set directly in the shell environment.
# print("JIRA_SERVER from os.getenv before Pydantic:", os.getenv("JIRA_SERVER"))

class Settings(BaseSettings):
    """
    Application settings.
    Pydantic-settings will automatically try to load values from environment variables
    (matching field names, case-insensitive) or from a .env file.
    """
    # Configure Pydantic settings
    # For Pydantic V2, model_config is a dictionary, not a class attribute.
    model_config = SettingsConfigDict(
        env_file=".env",                # Specifies the .env file to load
        env_file_encoding='utf-8',      # Encoding of the .env file
        extra="ignore",                 # Ignore extra fields from .env or environment
        case_sensitive=False            # Environment variable names are typically case-insensitive
    )

    # Define your settings fields.
    # Pydantic-settings will automatically look for environment variables
    # matching these field names (case-insensitively by default with case_sensitive=False).
    # If you need to map to a different env var name, use validation_alias.
    # The '...' indicates a required field.
    slack_bot_token: str = Field(..., validation_alias="SLACK_BOT_TOKEN")
    slack_signing_secret: str = Field(..., validation_alias="SLACK_SIGNING_SECRET")
    jira_server: str = Field(..., validation_alias="JIRA_SERVER")
    jira_username: str = Field(..., validation_alias="JIRA_USERNAME")
    jira_api_token: str = Field(..., validation_alias="JIRA_API_TOKEN")
    claude_api_key: str = Field(..., validation_alias="CLAUDE_API_KEY")
    default_jira_project_key: str = Field(..., validation_alias="DEFAULT_JIRA_PROJECT_KEY")
    app_log_level: str = Field("INFO", validation_alias="APP_LOG_LEVEL") # Default value if not in env

    # host: str = Field("0.0.0.0", validation_alias="HOST") # Uncomment if needed
    # port: int = Field(3000, validation_alias="PORT")     # Uncomment if needed

try:
    print("Attempting to load settings using Pydantic-Settings...")
    # Instantiate the settings. This will trigger loading from .env and the environment.
    settings = Settings()
    # print(f"JIRA_SERVER loaded by Pydantic: {settings.jira_server}") # For debugging
    # print(f"All settings loaded: {settings.model_dump()}") # For comprehensive debugging

except Exception as e:
    # Construct the expected path to the .env file for better error reporting
    # Assumes you run uvicorn from the directory containing the .env file
    # (e.g., /Users/abhisheksingh/dirD/prac/ai-challenge/gem)
    current_working_directory = pathlib.Path().resolve()
    expected_env_path = current_working_directory / ".env"

    print(f"Error loading settings with Pydantic-Settings: {e!r}")
    print(f"Pydantic-Settings was expecting to find a '.env' file relative to the current working directory.")
    print(f"Current working directory: {current_working_directory}")
    print(f"Expected .env file location: {expected_env_path}")

    if not expected_env_path.exists():
        print(f"WARNING: The .env file was NOT FOUND at {expected_env_path}.")
    else:
        print(f"INFO: The .env file WAS FOUND at {expected_env_path}. Please check its contents and ensure all required variables are defined and correctly named (e.g., JIRA_SERVER, SLACK_BOT_TOKEN).")
    raise # Re-raise the exception to halt execution if settings are critical

# Configure logging after settings are potentially loaded
import logging

# Ensure app_log_level is valid before using it.
# Fallback to INFO if settings.app_log_level is somehow not set (though Field has a default)
log_level_to_set = settings.app_log_level.upper() if hasattr(settings, 'app_log_level') and settings.app_log_level else "INFO"
numeric_level = getattr(logging, log_level_to_set, logging.INFO) # Get numeric level

logging.basicConfig(level=numeric_level,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if hasattr(settings, 'jira_server'): # Check if settings were loaded
    logger.info("Configuration loaded successfully using Pydantic-Settings.")
else:
    logger.error("Configuration potentially NOT loaded successfully. 'settings' object might be incomplete.")
