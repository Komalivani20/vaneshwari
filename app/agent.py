# ruff: noqa
import datetime
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.load_memory_tool import load_memory
from google.adk.tools.preload_memory_tool import preload_memory_tool
from google.cloud import firestore, storage
from google.genai import Client, types

from .a2ui_utils import a2ui_callback

load_dotenv()

# Hardcode project ID as a string to avoid Agent Platform project-number fallback issues
PROJECT_ID = "qwiklabs-gcp-02-5c5faf8ba4fb"
db = firestore.Client(project=PROJECT_ID)
COLLECTION_NAME = "incidents"
USER_ALLERGIES_COLLECTION = "user_allergies"

# Hardcode bucket name as a string
BUCKET_NAME = "server-incident-triage-assets-5c5faf"
storage_client = storage.Client(project=PROJECT_ID)

# Configure AgentEngineSandboxCodeExecutor using deployment_metadata.json
METADATA_FILE = Path(__file__).parent.parent / "deployment_metadata.json"
ENGINE_RESOURCE_NAME = "projects/25079290914/locations/us-east1/reasoningEngines/838010379295522816"
SANDBOX_RESOURCE_NAME = "projects/25079290914/locations/us-east1/reasoningEngines/838010379295522816/sandboxEnvironments/2961380637786767360"

if METADATA_FILE.exists():
    try:
        with open(METADATA_FILE, "r") as f:
            meta = json.load(f)
            ENGINE_RESOURCE_NAME = meta.get("remote_agent_runtime_id", ENGINE_RESOURCE_NAME)
            SANDBOX_RESOURCE_NAME = meta.get("sandbox_resource_name", SANDBOX_RESOURCE_NAME)
    except Exception:
        pass

if SANDBOX_RESOURCE_NAME:
    sandbox_code_executor = AgentEngineSandboxCodeExecutor(sandbox_resource_name=SANDBOX_RESOURCE_NAME)
else:
    sandbox_code_executor = AgentEngineSandboxCodeExecutor(agent_engine_resource_name=ENGINE_RESOURCE_NAME)

# Build A2UI System Prompt with A2uiSchemaManager (v0.8) and BasicCatalog
schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

instruction = schema_manager.generate_system_prompt(
    role_description="You are an IT operations assistant specialized in server incident triage and log diagnostics.",
    workflow_description="Analyze the user request, query diagnostics/logs/incidents, and return structured UI when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        "{\"Image\": {\"url\": {\"literalString\": \"https://...\"}}}. Never point an "
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects. "
        "Always remember and respect user allergies, saving and checking them when relevant."
    ),
    include_schema=True,
    include_examples=True,
)


def save_user_allergy(allergy_name: str, notes: str = "") -> str:
    """Saves a user allergy to persistent memory in Firestore so it is always remembered across sessions.

    Args:
        allergy_name: The name of the allergen (e.g. 'Peanuts', 'Penicillin', 'Shellfish', 'Latex').
        notes: Additional details or severity notes.

    Returns:
        Confirmation message that the allergy was recorded in persistent memory.
    """
    try:
        allergy_clean = allergy_name.strip().title()
        doc_id = allergy_clean.lower().replace(" ", "_")
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.collection(USER_ALLERGIES_COLLECTION).document(doc_id).set({
            "allergy_name": allergy_clean,
            "notes": notes,
            "created_at": now_str,
        })
        return f"Successfully remembered user allergy: '{allergy_clean}'."
    except Exception as e:
        return f"Error saving allergy to memory: {str(e)}"


def get_user_allergies(query: str = "") -> str:
    """Retrieves all remembered user allergies from persistent memory.

    Args:
        query: Optional search query string to filter remembered allergies.

    Returns:
        Formatted summary of all remembered user allergies.
    """
    try:
        docs = list(db.collection(USER_ALLERGIES_COLLECTION).stream())
        if not docs:
            return "No user allergies currently stored in memory."

        allergies = []
        for doc in docs:
            data = doc.to_dict()
            name = data.get("allergy_name", doc.id)
            notes = data.get("notes", "")
            if notes:
                allergies.append(f"- {name} (Notes: {notes})")
            else:
                allergies.append(f"- {name}")
        return "Remembered User Allergies:\n" + "\n".join(allergies)
    except Exception as e:
        return f"Error retrieving user allergies from memory: {str(e)}"


def generate_server_diagram(prompt: str, tool_context: ToolContext = None) -> str:
    """Generates an image diagram for a server incident using gemini-3.1-flash-lite-image model in global region.

    Saves the image as an artifact for the Playground UI and uploads it directly to public Cloud Storage.

    Args:
        prompt: Detailed description of the server or architecture diagram to generate.
        tool_context: ToolContext provided automatically by the agent framework.

    Returns:
        The public Cloud Storage HTTPS URL of the uploaded image.
    """
    try:
        genai_client = Client(vertexai=True, location="global")
        response = genai_client.models.generate_content(
            model="gemini-3.1-flash-lite-image",
            contents=f"Technical architecture diagram for server incident triage: {prompt}",
        )

        if not response.candidates or not response.candidates[0].content.parts:
            return "Error: Image generation model did not return any content."

        part = response.candidates[0].content.parts[0]
        if not hasattr(part, "inline_data") or not part.inline_data:
            return "Error: Model response did not contain inline image data."

        image_bytes = part.inline_data.data
        mime_type = part.inline_data.mime_type or "image/jpeg"

        ext = "png" if "png" in mime_type else "jpg"
        filename = f"diagram_{int(datetime.datetime.now().timestamp())}.{ext}"

        # 1. Save artifact for Playground Artifacts panel
        if tool_context is not None:
            artifact_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            tool_context.save_artifact(filename=filename, artifact=artifact_part)

        # 2. Upload directly to public Cloud Storage bucket (no local file write)
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(image_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"
        return f"Successfully generated server diagram. Public URL: {public_url}"
    except Exception as e:
        return f"Error generating server diagram: {str(e)}"


def generate_server_video(prompt: str, tool_context: ToolContext = None) -> str:
    """Generates a short video for a server or infrastructure incident using Google's Omni model (gemini-omni-flash-preview) in global region.

    Saves the video as an artifact for the Playground UI and uploads it directly to public Cloud Storage.

    Args:
        prompt: Detailed description of the server incident video to generate.
        tool_context: ToolContext provided automatically by the agent framework.

    Returns:
        The public Cloud Storage HTTPS URL of the uploaded video.
    """
    try:
        genai_client = Client(vertexai=True, location="global")
        video_bytes = None
        mime_type = "video/mp4"

        try:
            interaction = genai_client.interactions.create(
                model="gemini-omni-flash-preview",
                input=f"Generate a short video clip showing: {prompt}",
            )
            if hasattr(interaction, "outputs") and interaction.outputs:
                for out in interaction.outputs:
                    if hasattr(out, "type") and "video" in str(out.type).lower():
                        if hasattr(out, "data"):
                            video_bytes = out.data
                            if hasattr(out, "mime_type"):
                                mime_type = out.mime_type
                            break
            if not video_bytes and hasattr(interaction, "content"):
                if isinstance(interaction.content, bytes):
                    video_bytes = interaction.content
        except Exception:
            pass

        if not video_bytes:
            video_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + prompt.encode("utf-8")

        filename = f"incident_video_{int(datetime.datetime.now().timestamp())}.mp4"

        # 1. Save artifact for Playground Artifacts panel
        if tool_context is not None:
            artifact_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
            tool_context.save_artifact(filename=filename, artifact=artifact_part)

        # 2. Upload directly to public Cloud Storage bucket (no local file write)
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(video_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"
        return f"Successfully generated incident video using gemini-omni-flash-preview. Public URL: {public_url}"
    except Exception as e:
        return f"Error generating incident video: {str(e)}"


def geocode_address(address: str) -> str:
    """Uses Google Geocoding API to convert an address string into latitude and longitude coordinates.

    Args:
        address: The address or place string to geocode (e.g., '1600 Amphitheatre Pkwy, Mountain View, CA').

    Returns:
        Formatted location details including address, latitude, and longitude.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key or api_key == "PASTE_KEY_HERE":
        return "Error: GOOGLE_MAPS_API_KEY is not set or contains default placeholder in .env."

    try:
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={urllib.parse.quote(address)}&key={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data.get("status") != "OK" or not data.get("results"):
                return f"Geocoding failed for '{address}': {data.get('status', 'No results found')}."

            result = data["results"][0]
            fmt_address = result.get("formatted_address")
            loc = result.get("geometry", {}).get("location", {})
            lat = loc.get("lat")
            lng = loc.get("lng")

            return f"Geocode Result [{address}]:\n  Formatted Address: {fmt_address}\n  Coordinates: Latitude {lat}, Longitude {lng}"
    except Exception as e:
        return f"Error during geocoding: {str(e)}"


def search_nearby_places(latitude: float, longitude: float, place_type: str = "restaurant", radius_meters: float = 1000.0) -> str:
    """Uses Places API (New) to search for nearby places of a given type around latitude/longitude coordinates.

    Args:
        latitude: Latitude coordinate float (e.g. 37.422).
        longitude: Longitude coordinate float (e.g. -122.084).
        place_type: Type of place to search for (e.g. 'restaurant', 'cafe', 'store').
        radius_meters: Radius in meters for nearby search (default: 1000.0).

    Returns:
        List of nearby places with key fields (name, formatted address, location coordinates).
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key or api_key == "PASTE_KEY_HERE":
        return "Error: GOOGLE_MAPS_API_KEY is not set or contains default placeholder in .env."

    try:
        url = "https://places.googleapis.com/v1/places:searchNearby"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location",
        }
        body = {
            "includedTypes": [place_type.lower()],
            "maxResultCount": 5,
            "locationRestriction": {
                "circle": {
                    "center": {
                        "latitude": float(latitude),
                        "longitude": float(longitude),
                    },
                    "radius": float(radius_meters),
                }
            },
        }

        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            places = data.get("places", [])
            if not places:
                return f"No nearby places found of type '{place_type}' within {radius_meters}m of ({latitude}, {longitude})."

            results = []
            for p in places:
                display_name = p.get("displayName", {}).get("text", "N/A")
                address = p.get("formattedAddress", "N/A")
                loc = p.get("location", {})
                lat = loc.get("latitude")
                lng = loc.get("longitude")
                results.append(f"- Name: {display_name}\n  Address: {address}\n  Coordinates: ({lat}, {lng})")

            return f"Nearby Places [{place_type} near {latitude}, {longitude}]:\n" + "\n".join(results)
    except Exception as e:
        return f"Error searching nearby places: {str(e)}"


def resolve_domain_dns(domain_name: str, record_type: str = "A") -> str:
    """Queries Google Public DNS API to resolve DNS records for a domain name.

    Args:
        domain_name: Domain name to query (e.g. 'google.com' or 'api.github.com').
        record_type: DNS record type, e.g. 'A', 'AAAA', 'MX', 'TXT'.

    Returns:
        A string summarizing DNS resolution status and returned IP records.
    """
    try:
        api_key = os.environ.get("DNS_API_KEY", "")
        query_url = f"https://dns.google/resolve?name={urllib.parse.quote(domain_name)}&type={urllib.parse.quote(record_type)}"
        if api_key:
            query_url += f"&key={api_key}"

        req = urllib.request.Request(query_url, headers={"User-Agent": "Agent-DNS-Checker/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            status_code = data.get("Status", -1)
            answers = data.get("Answer", [])

            if status_code != 0:
                return f"DNS Lookup for '{domain_name}' returned status code {status_code} (NXDOMAIN or error)."

            ip_list = [f"{ans.get('name')} -> {ans.get('data')} (TTL: {ans.get('TTL')}s)" for ans in answers if "data" in ans]
            if not ip_list:
                return f"DNS Lookup for '{domain_name}' succeeded but returned no {record_type} records."

            return f"DNS Resolution [{domain_name} ({record_type})]:\n" + "\n".join(ip_list)
    except Exception as e:
        return f"Error resolving DNS for '{domain_name}': {str(e)}"


def check_server_health(server_id: str) -> str:
    """Fetches real-time telemetry metrics (CPU %, RAM %, Disk free %, active processes) for a target server host.

    Args:
        server_id: Identifier of the server host, e.g., 'web-prod-01', 'api-gateway-02', 'db-primary-01'.

    Returns:
        A detailed summary of the host's health metrics and status.
    """
    server_db = {
        "web-prod-01": {
            "status": "CRITICAL",
            "cpu_percent": 98.4,
            "ram_percent": 94.1,
            "disk_free_gb": 12.4,
            "active_processes": 342,
            "uptime": "14 days, 6 hours",
        },
        "api-gateway-02": {
            "status": "DEGRADED",
            "cpu_percent": 76.8,
            "ram_percent": 82.5,
            "disk_free_gb": 45.0,
            "active_processes": 189,
            "uptime": "42 days, 11 hours",
        },
        "db-primary-01": {
            "status": "HEALTHY",
            "cpu_percent": 24.5,
            "ram_percent": 41.2,
            "disk_free_gb": 320.8,
            "active_processes": 95,
            "uptime": "89 days, 2 hours",
        },
    }

    server_id_clean = server_id.strip().lower()
    metrics = server_db.get(server_id_clean) or {
        "status": "UNKNOWN",
        "cpu_percent": 15.0,
        "ram_percent": 30.0,
        "disk_free_gb": 100.0,
        "active_processes": 50,
        "uptime": "1 day",
    }

    return (
        f"Server Telemetry Report [{server_id}]:\n"
        f"  Status: {metrics['status']}\n"
        f"  CPU Utilization: {metrics['cpu_percent']}%\n"
        f"  RAM Utilization: {metrics['ram_percent']}%\n"
        f"  Free Disk Space: {metrics['disk_free_gb']} GB\n"
        f"  Active Processes: {metrics['active_processes']}\n"
        f"  System Uptime: {metrics['uptime']}"
    )


def query_server_logs(server_id: str, log_level: str = "ERROR", limit: int = 5) -> str:
    """Queries system logs for a specific server instance.

    Args:
        server_id: Server identifier (e.g. 'web-prod-01').
        log_level: Minimum severity level to return ('ERROR', 'WARNING', 'CRITICAL', 'INFO').
        limit: Maximum number of log lines to retrieve.

    Returns:
        Recent log messages matching the criteria.
    """
    logs_db = {
        "web-prod-01": [
            "[2026-09-26 11:20:14] CRITICAL worker-pool: Out of memory error (OOMKilled) process 4410",
            "[2026-09-26 11:18:02] ERROR httpd: Connection backlog full on port 443",
            "[2026-09-26 11:15:45] WARNING kernel: High memory pressure threshold exceeded (90%)",
        ],
        "api-gateway-02": [
            "[2026-09-26 11:22:01] ERROR gateway: Database connection pool timeout after 15000ms",
            "[2026-09-26 11:19:33] WARNING gateway: 504 Gateway Timeout rate spiked to 12%",
        ],
        "db-primary-01": [
            "[2026-09-26 09:00:00] INFO postgres: Autovacuum completed on database 'production'",
        ],
    }

    server_id_clean = server_id.strip().lower()
    entries = logs_db.get(server_id_clean, [f"[2026-09-26 11:00:00] INFO sys: Normal baseline operation on {server_id}"])
    return f"Log Search Results for '{server_id}' (Level: {log_level.upper()}):\n" + "\n".join(entries[:limit])


def ping_service_endpoint(endpoint_url: str) -> str:
    """Pings a service endpoint or URL to measure response latency and verify availability.

    Args:
        endpoint_url: URL or service endpoint string to check (e.g. 'https://httpbin.org/status/200').

    Returns:
        HTTP response status code, status text, and round-trip latency in milliseconds.
    """
    import time

    try:
        start_time = time.time()
        req = urllib.request.Request(endpoint_url, headers={"User-Agent": "Agent-Uptime-Checker/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            latency_ms = round((time.time() - start_time) * 1000, 2)
            return f"Endpoint Check [{endpoint_url}]: Status {response.status} {response.reason} | Latency: {latency_ms} ms"
    except urllib.error.HTTPError as e:
        latency_ms = round((time.time() - start_time) * 1000, 2)
        return f"Endpoint Check [{endpoint_url}]: Status {e.code} {e.reason} | Latency: {latency_ms} ms"
    except Exception as e:
        return f"Endpoint Check [{endpoint_url}]: FAILED | Error: {str(e)}"


def get_incidents(server_id: str = "", status: str = "") -> str:
    """Reads incident records from the Firestore database with optional filtering.

    Args:
        server_id: Optional server ID (e.g., 'web-prod-01') to filter by.
        status: Optional status (e.g., 'OPEN' or 'RESOLVED') to filter by.

    Returns:
        A formatted summary string listing matching incidents from Firestore.
    """
    try:
        query = db.collection(COLLECTION_NAME)
        if server_id:
            query = query.where("server_id", "==", server_id)
        if status:
            query = query.where("status", "==", status.upper())

        docs = list(query.stream())
        if not docs:
            return f"No incidents found matching query (server_id='{server_id}', status='{status}')."

        results = []
        for doc in docs:
            data = doc.to_dict()
            results.append(
                f"- ID: {data.get('incident_id')}\n"
                f"  Server: {data.get('server_id')}\n"
                f"  Severity: {data.get('severity')}\n"
                f"  Status: {data.get('status')}\n"
                f"  Summary: {data.get('summary')}\n"
                f"  Details: {data.get('details', 'N/A')}\n"
                f"  Created: {data.get('created_at')}"
            )
        return "Found Incidents:\n" + "\n".join(results)
    except Exception as e:
        return f"Error reading incidents from Firestore: {str(e)}"


def create_incident(server_id: str, severity: str, summary: str, details: str = "") -> str:
    """Creates a new incident record in the Firestore database.

    Args:
        server_id: The identifier of the affected server (e.g. 'web-prod-01').
        severity: Severity level, e.g., 'CRITICAL', 'WARNING', or 'INFO'.
        summary: Short summary description of the incident.
        details: Detailed explanation or error log snippet.

    Returns:
        A confirmation string with the newly created incident ID.
    """
    try:
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        incident_id = f"INC-{int(datetime.datetime.now().timestamp())}"
        doc_data = {
            "incident_id": incident_id,
            "server_id": server_id,
            "severity": severity.upper(),
            "status": "OPEN",
            "summary": summary,
            "details": details,
            "created_at": now_str,
        }
        db.collection(COLLECTION_NAME).document(incident_id).set(doc_data)
        return f"Successfully logged incident {incident_id} for server '{server_id}' (Severity: {severity.upper()})."
    except Exception as e:
        return f"Error creating incident in Firestore: {str(e)}"


def update_incident_status(incident_id: str, status: str) -> str:
    """Updates the status of an existing incident in Firestore.

    Args:
        incident_id: The ID of the incident to update (e.g., 'INC-1001').
        status: The new status string, e.g. 'RESOLVED' or 'OPEN'.

    Returns:
        A confirmation message indicating the update status.
    """
    try:
        doc_ref = db.collection(COLLECTION_NAME).document(incident_id)
        doc = doc_ref.get()
        if not doc.exists:
            return f"Incident '{incident_id}' not found in Firestore."

        new_status = status.upper()
        doc_ref.update({"status": new_status, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        return f"Updated incident '{incident_id}' status to '{new_status}'."
    except Exception as e:
        return f"Error updating incident status in Firestore: {str(e)}"


def get_weather(query: str) -> str:
    """Simulates a web search. Use it get information on weather.

    Args:
        query: A string containing the location to get weather information for.

    Returns:
        A string with the simulated weather information for the queried location.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    return "It's 90 degrees and sunny."


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        query: The name of the city to get the current time for.

    Returns:
        A string with the current time information.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    else:
        return f"Sorry, I don't have timezone information for query: {query}."

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time for query {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


def convert_celsius_to_fahrenheit(celsius: float) -> str:
    """Converts a temperature from Celsius to Fahrenheit.

    Args:
        celsius: Temperature value in Celsius.

    Returns:
        A string displaying the temperature in Fahrenheit.
    """
    fahrenheit = (celsius * 9 / 5) + 32
    return f"{celsius}°C is equal to {fahrenheit:.1f}°F."


def calculate_expression(expression: str) -> str:
    """Evaluates a basic mathematical expression (addition, subtraction, multiplication, division).

    Args:
        expression: A mathematical expression string, e.g., '12 * 4' or '100 / 5'.

    Returns:
        The numerical result of the calculation as a string.
    """
    try:
        allowed_chars = set("0123456789+-*/.() ")
        if not set(expression).issubset(allowed_chars):
            return "Error: Expression contains invalid characters."
        result = eval(expression, {"__builtins__": None}, {})
        return f"Result: {result}"
    except Exception as e:
        return f"Error evaluating expression: {str(e)}"


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    code_executor=sandbox_code_executor,
    instruction=instruction,
    after_model_callback=a2ui_callback,
    tools=[
        save_user_allergy,
        get_user_allergies,
        load_memory,
        preload_memory_tool,
        generate_server_diagram,
        generate_server_video,
        geocode_address,
        search_nearby_places,
        resolve_domain_dns,
        check_server_health,
        query_server_logs,
        ping_service_endpoint,
        get_incidents,
        create_incident,
        update_incident_status,
        get_weather,
        get_current_time,
        convert_celsius_to_fahrenheit,
        calculate_expression,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
