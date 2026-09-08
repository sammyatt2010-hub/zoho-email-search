"""Shared constants for the zoho-email-search app."""

# --- Zoho endpoints (EU data center) ---
ZOHO_ACCOUNTS_BASE = "https://accounts.zoho.eu"
ZOHO_API_BASE = "https://www.zohoapis.eu"
ZOHO_TOKEN_URL = f"{ZOHO_ACCOUNTS_BASE}/oauth/v2/token"
ZOHO_COQL_URL = f"{ZOHO_API_BASE}/crm/v8/coql"

# --- Modules searched at Stage 1 (record matching) ---
# Each entry: module API name -> fields to select in COQL (id + Email + display fields)
MODULES = {
    "Contacts": {
        "select_fields": ["id", "Email", "Full_Name", "Account_Name"],
        "email_field": "Email",
        "name_field": "Full_Name",
    },
    "Leads": {
        "select_fields": ["id", "Email", "Full_Name", "Company"],
        "email_field": "Email",
        "name_field": "Full_Name",
    },
    "Accounts": {
        "select_fields": ["id", "Email", "Account_Name"],
        "email_field": "Email",
        "name_field": "Account_Name",
    },
}

# --- Safeguards ---
# Stop paging Stage 1 (record matching) once this many matching records have
# been found across all three modules combined. Prevents a very broad domain
# search from silently trying to pull thousands of records' email histories.
MAX_MATCHED_RECORDS = 500

# COQL page size used for the record-matching stage. Kept at the cheapest
# credit tier (1-200 = 1 credit) for the pre-flight check; bumped to 1000 for
# the full pull once the user confirms a broad search.
COQL_PREFLIGHT_LIMIT = 200
COQL_FULL_LIMIT = 1000

# Get-Emails-of-Record always returns 10 per page (fixed by Zoho, not
# configurable), used by zoho_client.get_record_emails().
EMAILS_PAGE_SIZE = 10

# Default "type" filter for the Get Emails endpoint. Zoho supports:
#   sent_from_crm, scheduled_in_crm, drafts, user_emails
DEFAULT_EMAIL_TYPES = ["sent_from_crm"]
ALL_EMAIL_TYPES = ["sent_from_crm", "scheduled_in_crm", "drafts", "user_emails"]

# Cap on rows shown in the interactive results table (full set still goes to
# the exported XLSX/PDF regardless of this cap).
MAX_DISPLAY_ROWS = 500

# Network timeout (seconds) for every Zoho API call.
REQUEST_TIMEOUT = 30

# Retry policy for transient errors (429 / 5xx).
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5  # seconds, exponential: base * 2**attempt

# Concurrency for Stage 2 (per-record email log fetches).
MAX_WORKERS = 4
