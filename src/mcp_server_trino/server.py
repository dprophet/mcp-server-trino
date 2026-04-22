import asyncio
import logging
import os
import sys
from mcp.server import Server
from mcp.types import Resource, Tool, TextContent
from pydantic import AnyUrl

# Trino DB-API
from trino.dbapi import connect
from trino.exceptions import TrinoExternalError, TrinoQueryError
from trino.auth import (
    ClientCredentials,
    DeviceCode,
    OidcConfig,
    ManualUrlsConfig
)

# Configure logging to stderr (stdout is reserved for JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("mcp_server_trino")

def get_db_config():
    """Get Trino configuration from environment variables."""
    # Parse TRINO_HOST which may include port (e.g., "sql.example.com:443")
    trino_host = os.getenv("TRINO_HOST")
    if not trino_host:
        raise ValueError("TRINO_HOST is required")

    if ":" in trino_host:
        host, port_str = trino_host.rsplit(":", 1)
        port = int(port_str)
    else:
        host = trino_host
        port = 443

    config = {
        "host": host,
        "port": port,

        # Authentication - client_credentials (default) or device_code
        "auth_mode": os.getenv("AUTH_MODE", "client_credentials"),
        "client_id": os.getenv("CLIENT_ID"),
        "client_secret": os.getenv("CLIENT_SECRET"),

        # OAuth endpoints
        "token_endpoint": os.getenv("TOKEN_ENDPOINT"),
        "oidc_discovery_url": os.getenv("OIDC_DISCOVERY_URL"),
    }

    # Validate OAuth configuration
    auth_mode = config["auth_mode"]

    if auth_mode not in ["client_credentials", "device_code"]:
        raise ValueError(
            f"Invalid AUTH_MODE: '{auth_mode}'. "
            "Must be 'client_credentials' or 'device_code'"
        )

    if not config["client_id"]:
        raise ValueError("CLIENT_ID is required")
    if not config["client_secret"]:
        raise ValueError("CLIENT_SECRET is required")

    if auth_mode == "client_credentials" and not config["token_endpoint"]:
        raise ValueError("TOKEN_ENDPOINT is required for client_credentials mode")

    if auth_mode == "device_code" and not config["oidc_discovery_url"]:
        raise ValueError("OIDC_DISCOVERY_URL is required for device_code mode")

    return config

def create_auth_config(cfg):
    """Create authentication configuration based on auth_mode."""
    auth_mode = cfg["auth_mode"]

    logger.info(f"Using authentication mode: {auth_mode}")

    # Client credentials flow - for service-to-service authentication
    if auth_mode == "client_credentials":
        return ClientCredentials(
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            url_config=ManualUrlsConfig(token_endpoint=cfg["token_endpoint"])
        )

    # Device code flow - for interactive authentication
    elif auth_mode == "device_code":
        return DeviceCode(
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            url_config=OidcConfig(oidc_discovery_url=cfg["oidc_discovery_url"])
        )

    else:
        raise ValueError(
            f"Invalid AUTH_MODE: '{auth_mode}'. "
            "Must be 'client_credentials' or 'device_code'"
        )

def create_trino_connection():
    """Create a Trino connection using OAuth."""
    cfg = get_db_config()
    auth = create_auth_config(cfg)

    # Build connection params - only include catalog/schema if set
    conn_params = {
        "host": cfg["host"],
        "port": cfg["port"],
        "http_scheme": "https",
        "auth": auth
    }

    # Add optional catalog/schema if set in environment
    catalog = os.getenv("CATALOG")
    schema = os.getenv("SCHEMA")
    if catalog:
        conn_params["catalog"] = catalog
    if schema:
        conn_params["schema"] = schema

    return connect(**conn_params)

# Initialize server
app = Server("mcp_server_trino")



@app.list_tools()
async def list_tools() -> list[Tool]:
    """
    List available Trino tools (here just a generic SQL executor).
    """
    logger.info("Listing tools...")
    return [
        Tool(
            name="execute_sql",
            description="Execute an SQL query on the Trino cluster",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute"
                    }
                },
                "required": ["query"]
            }
        )
    ]

# Update these functions to not use cursor as a context manager

@app.list_resources()
async def list_resources() -> list[Resource]:
    """
    List Trino resources dynamically based on configuration:
    - No CATALOG set: show all catalogs
    - CATALOG set but no SCHEMA: show schemas in that catalog
    - Both set: show tables in that catalog.schema
    """
    catalog = os.getenv("CATALOG")
    schema = os.getenv("SCHEMA")

    try:
        conn = create_trino_connection()
        cursor = conn.cursor()
        resources = []

        # Case 1: No catalog set - show all catalogs
        if not catalog:
            logger.info("No CATALOG set - listing all catalogs")
            cursor.execute("SHOW CATALOGS")
            catalogs = cursor.fetchall()
            logger.info(f"Found catalogs: {catalogs}")

            for (catalog_name,) in catalogs:
                resources.append(
                    Resource(
                        uri=f"trino://catalog/{catalog_name}",
                        name=f"Catalog: {catalog_name}",
                        mimeType="text/plain",
                        description=f"Trino catalog: {catalog_name}"
                    )
                )

        # Case 2: Catalog set but no schema - show schemas in catalog
        elif not schema:
            logger.info(f"CATALOG={catalog} set - listing schemas")
            cursor.execute(f"SHOW SCHEMAS FROM {catalog}")
            schemas = cursor.fetchall()
            logger.info(f"Found schemas: {schemas}")

            for (schema_name,) in schemas:
                resources.append(
                    Resource(
                        uri=f"trino://catalog/{catalog}/schema/{schema_name}",
                        name=f"Schema: {catalog}.{schema_name}",
                        mimeType="text/plain",
                        description=f"Trino schema in {catalog}: {schema_name}"
                    )
                )

        # Case 3: Both catalog and schema set - show tables
        else:
            logger.info(f"CATALOG={catalog}, SCHEMA={schema} set - listing tables")
            cursor.execute(f"SHOW TABLES IN {catalog}.{schema}")
            tables = cursor.fetchall()
            logger.info(f"Found tables: {tables}")

            for (table_name,) in tables:
                resources.append(
                    Resource(
                        uri=f"trino://{catalog}/{schema}/{table_name}",
                        name=f"Table: {catalog}.{schema}.{table_name}",
                        mimeType="text/plain",
                        description=f"Table in {catalog}.{schema}: {table_name}"
                    )
                )

        cursor.close()
        conn.close()
        return resources

    except (TrinoQueryError, TrinoExternalError) as e:
        logger.error(f"Failed to list resources: {str(e)}")
        return []

@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    """
    Read up to 100 rows from the given table resource.
    Expects a URI like: trino://table_name/data
    """
    catalog = os.getenv("CATALOG")
    schema = os.getenv("SCHEMA")
    uri_str = str(uri)
    logger.info(f"Reading resource: {uri_str}")

    if not uri_str.startswith("trino://"):
        raise ValueError(f"Invalid URI scheme: {uri_str}")

    # Grab the table name: "trino://table/data" -> "table"
    parts = uri_str[8:].split('/')
    table = parts[0]

    try:
        conn = create_trino_connection()
        cursor = conn.cursor()

        # Build query with optional catalog.schema prefix
        if catalog and schema:
            query = f"SELECT * FROM {catalog}.{schema}.{table} LIMIT 100"
        else:
            query = f"SELECT * FROM {table} LIMIT 100"

        logger.info(f"Executing query: {query}")
        cursor.execute(query)

        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        result_lines = [",".join(map(str, row)) for row in rows]
        header_line = ",".join(columns)

        cursor.close()
        conn.close()
        return "\n".join([header_line] + result_lines)

    except (TrinoQueryError, TrinoExternalError) as e:
        logger.error(f"Database error reading resource {uri_str}: {str(e)}")
        raise RuntimeError(f"Database error: {str(e)}")

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """
    Execute SQL commands on Trino.
    """
    logger.info(f"Calling tool: {name} with arguments: {arguments}")

    if name != "execute_sql":
        raise ValueError(f"Unknown tool: {name}")

    query = arguments.get("query")
    if not query:
        raise ValueError("Query is required")

    try:
        conn = create_trino_connection()
        cursor = conn.cursor()
        logger.info(f"Executing query: {query}")
        cursor.execute(query)

        # For statements like SHOW TABLES, we fetch and return them
        if query.strip().upper().startswith("SHOW "):
            rows = cursor.fetchall()
            result = []
            for row in rows:
                result.append("\t".join(map(str, row)))
            cursor.close()
            conn.close()
            return [TextContent(type="text", text="\n".join(result))]

        # For SELECT queries, fetch data
        elif query.strip().upper().startswith("SELECT"):
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            result_lines = [",".join(map(str, row)) for row in rows]
            header_line = ",".join(columns)
            cursor.close()
            conn.close()
            return [TextContent(type="text", text="\n".join([header_line] + result_lines))]

        # For other queries (CREATE, DROP, INSERT, etc.), just return success info
        else:
            cursor.close()
            conn.close()
            return [TextContent(type="text", text="Query executed successfully.")]

    except (TrinoQueryError, TrinoExternalError) as e:
        logger.error(f"Error executing query '{query}': {e}")
        return [TextContent(type="text", text=f"Error executing query: {str(e)}")]

async def main():
    """Main entry point to run the MCP server via STDIO."""
    from mcp.server.stdio import stdio_server

    logger.info("Starting Trino MCP server...")

    try:
        config = get_db_config()
        logger.info(f"Trino: {config['host']}:{config['port']}")
        logger.info(f"Auth mode: {config['auth_mode']}")
    except Exception as e:
        logger.error(f"Configuration error: {str(e)}")
        raise

    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options()
            )
        except Exception as e:
            logger.error(f"Server error: {str(e)}", exc_info=True)
            raise

if __name__ == "__main__":
    asyncio.run(main())
