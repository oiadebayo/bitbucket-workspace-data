import json
import time
from datetime import datetime
import asyncio
from typing import Any, Optional
import httpx
from decouple import config
from loguru import logger
from httpx import BasicAuth

# These are the credentials passed by the variables of your pipeline to your tasks and in to your env
PORT_CLIENT_ID = config("PORT_CLIENT_ID")
PORT_CLIENT_SECRET = config("PORT_CLIENT_SECRET")
BITBUCKET_USERNAME = config("BITBUCKET_USERNAME")
BITBUCKET_PASSWORD = config("BITBUCKET_PASSWORD")
BITBUCKET_API_URL = config("BITBUCKET_HOST")
BITBUCKET_PROJECTS_FILTER = config(
    "BITBUCKET_PROJECTS_FILTER", cast=lambda v: v.split(",") if v else None, default=[]
)
PORT_API_URL = "https://api.getport.io/v1"
WEBHOOK_SECRET = config("WEBHOOK_SECRET", default="bitbucket_webhook_secret")

# According to https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/
RATE_LIMIT = 1000  # Maximum number of requests allowed per hour
RATE_PERIOD = 3600  # Rate limit reset period in seconds (1 hour)
WEBHOOK_IDENTIFIER = "bitbucket_mapper"
WEBHOOK_EVENTS = [
    "repo:modified",
    "project:modified",
    "pr:modified",
    "pr:opened",
    "pr:merged",
    "pr:reviewer:updated",
    "pr:declined",
    "pr:deleted",
    "pr:comment:deleted",
    "pr:from_ref_updated",
    "pr:comment:edited",
    "pr:reviewer:unapproved",
    "pr:reviewer:needs_work",
    "pr:reviewer:approved",
    "pr:comment:added",
]

# Initialize rate limiting variables
request_count = 0
rate_limit_start = time.time()

bitbucket_auth = BasicAuth(username=BITBUCKET_USERNAME, password=BITBUCKET_PASSWORD)

# Obtain the access token synchronously
credentials = {"clientId": PORT_CLIENT_ID, "clientSecret": PORT_CLIENT_SECRET}
token_response = httpx.post(f"{PORT_API_URL}/auth/access_token", json=credentials)
token_response.raise_for_status()
access_token = token_response.json()["accessToken"]
port_headers = {"Authorization": f"Bearer {access_token}"}

# Initialize the global AsyncClient with a timeout
client = httpx.AsyncClient(timeout=httpx.Timeout(60))

async def get_or_create_port_webhook():
    logger.info("Checking if a Bitbucket webhook is configured on Port...")
    try:
        response = await client.get(
            f"{PORT_API_URL}/webhooks/{WEBHOOK_IDENTIFIER}",
            headers=port_headers,
        )
        response.raise_for_status()
        webhook_url = response.json().get("integration", {}).get("url")
        logger.info(f"Webhook configuration exists in Port. URL: {webhook_url}")
        return webhook_url
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.info("Port webhook not found, creating a new one.")
            return await create_port_webhook()
        else:
            logger.error(f"Error checking Port webhook: {e.response.status_code}")
            return None

async def create_port_webhook():
    logger.info("Creating a webhook for Bitbucket on Port...")
    with open("./resources/webhook_configuration.json", "r") as file:
        mappings = json.load(file)
    webhook_data = {
        "identifier": WEBHOOK_IDENTIFIER,
        "title": "Bitbucket Webhook",
        "description": "Webhook for receiving Bitbucket events",
        "icon": "BitBucket",
        "mappings": mappings,
        "enabled": True,
        "security": {
            "secret": WEBHOOK_SECRET,
            "signatureHeaderName": "X-Hub-Signature",
            "signatureAlgorithm": "sha256",
            "signaturePrefix": "sha256=",
            "requestIdentifierPath": ".headers['X-Request-ID']",
        },
        "integrationType": "custom",
    }

    try:
        response = await client.post(
            f"{PORT_API_URL}/webhooks",
            json=webhook_data,
            headers=port_headers,
        )
        response.raise_for_status()
        webhook_url = response.json().get("integration", {}).get("url")
        logger.info(f"Webhook configuration successfully created in Port: {webhook_url}")
        return webhook_url
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 442:
            logger.error("Incorrect mapping, kindly fix!")
            return None
        logger.error(f"Error creating Port webhook: {e.response.status_code}")
        return None

async def get_or_create_project_webhook(project_key: str, webhook_url: str, events: list[str]):
    logger.info(f"Checking webhooks for project: {project_key}")
    if webhook_url is not None:
        try:
            matching_webhooks = [
                webhook
                async for project_webhooks_batch in get_paginated_resource(
                    path=f"projects/{project_key}/webhooks"
                )
                for webhook in project_webhooks_batch
                if webhook["url"] == webhook_url
            ]
            if matching_webhooks:
                logger.info(f"Webhook already exists for project {project_key}")
                return matching_webhooks[0]
            logger.info(f"Webhook not found for project {project_key}. Creating a new one.")
            return await create_project_webhook(
                project_key=project_key, webhook_url=webhook_url, events=events
            )
        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP error when checking webhooks for project {project_key}: {e.response.status_code}"
            )
            return None
    else:
        logger.error("Port webhook URL is not available. Skipping webhook check...")
        return None

async def create_project_webhook(project_key: str, webhook_url: str, events: list[str]):
    logger.info(f"Creating webhook for project: {project_key}")
    webhook_data = {
        "name": "Port Webhook",
        "url": webhook_url,
        "events": events,
        "active": True,
        "sslVerificationRequired": True,
        "configuration": {
            "secret": WEBHOOK_SECRET,
            "createdBy": "Port",
        },
    }
    try:
        response = await client.post(
            f"{BITBUCKET_API_URL}/rest/api/1.0/projects/{project_key}/webhooks",
            json=webhook_data,
            auth=bitbucket_auth,
        )
        response.raise_for_status()
        logger.info(f"Successfully created webhook for project {project_key}")
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error when creating webhook for project {project_key}: {e.response.status_code}"
            )
        return None

async def add_entity_to_port(blueprint_id, entity_object):
    response = await client.post(
        f"{PORT_API_URL}/blueprints/{blueprint_id}/entities?upsert=true&merge=true",
        json=entity_object,
        headers=port_headers,
    )
    logger.info(response.json())

async def get_paginated_resource(
        path: str,
        params: dict[str, Any] = None,
        page_size: int = 25,
        full_response: bool = False,
):
    logger.info(f"Requesting data for {path}")

    global request_count, rate_limit_start

    # Check if we've exceeded the rate limit, and if so, wait until the reset period is over
    if request_count >= RATE_LIMIT:
        elapsed_time = time.time() - rate_limit_start
        if elapsed_time < RATE_PERIOD:
            sleep_time = RATE_PERIOD - elapsed_time
            await asyncio.sleep(sleep_time)

        # Reset the rate limiting variables
        request_count = 0
        rate_limit_start = time.time()

    url = f"{BITBUCKET_API_URL}/rest/api/1.0/{path}"
    params = params or {}
    params["limit"] = page_size
    next_page_start = None

    while True:
        try:
            if next_page_start:
                params["start"] = next_page_start

            response = await client.get(url=url, auth=bitbucket_auth, params=params)
            response.raise_for_status()
            page_json = response.json()
            request_count += 1

            if full_response:
                yield page_json
            else:
                batch_data = page_json["values"]
                yield batch_data

            next_page_start = page_json.get("nextPageStart")
            if not next_page_start:
                break
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.info(
                    f"Could not find the requested resources {path}. Terminating gracefully..."
                )
                return
            logger.error(
                f"HTTP error with code {e.response.status_code}, content: {e.response.text}"
            )
            raise
        logger.info(f"Successfully fetched paginated data for {path}")

async def get_single_project(project_key: str):
    response = await client.get(
        f"{BITBUCKET_API_URL}/rest/api/1.0/projects/{project_key}", auth=bitbucket_auth
    )
    response.raise_for_status()
    return response.json()

def convert_to_datetime(timestamp: int):
    converted_datetime = datetime.utcfromtimestamp(timestamp / 1000.0)
    return converted_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_repository_file_response(file_response: dict[str, Any]) -> str:
    lines = file_response.get("lines", [])
    logger.info(f"Received readme file with {len(lines)} entries")
    readme_content = ""

    for line in lines:
        readme_content += line.get("text", "") + "\n"

    return readme_content

async def process_user_entities(users_data: list[dict[str, Any]]):
    blueprint_id = "bitbucketUser"

    for user in users_data:
        entity = {
            "identifier": user.get("emailAddress"),
            "title": user.get("displayName"),
            "properties": {
                "username": user.get("name"),
                "url": user.get("links", {}).get("self", [{}])[0].get("href"),
            },
            "relations": {},
        }
        await add_entity_to_port(blueprint_id=blueprint_id, entity_object=entity)

async def process_project_entities(projects_data: list[dict[str, Any]]):
    blueprint_id = "bitbucketProject"

    for project in projects_data:
        entity = {
            "identifier": project.get("key"),
            "title": project.get("name"),
            "properties": {
                "description": project.get("description"),
                "public": project.get("public"),
                "type": project.get("type"),
                "link": project.get("links", {}).get("self", [{}])[0].get("href"),
            },
            "relations": {},
        }
        await add_entity_to_port(blueprint_id=blueprint_id, entity_object=entity)

async def process_repository_entities(repository_data: list[dict[str, Any]]):
    blueprint_id = "bitbucketRepository"

    for repo in repository_data:
        readme_content = await get_repository_readme(
            project_key=repo["project"]["key"], repo_slug=repo["slug"]
        )
        entity = {
            "identifier": repo.get("slug"),
            "title": repo.get("name"),
            "properties": {
                "description": repo.get("description"),
                "state": repo.get("state"),
                "forkable": repo.get("forkable"),
                "public": repo.get("public"),
                "link": repo.get("links", {}).get("self", [{}])[0].get("href"),
                "documentation": readme_content,
                "swagger_url": f"https://api.{repo.get('slug')}.com",
            },
            "relations": dict(
                project=repo.get("project", {}).get("key"),
                latestCommitAuthor=repo.get("__latestCommit", {})
                .get("committer", {})
                .get("emailAddress"),
            ),
        }
        await add_entity_to_port(blueprint_id=blueprint_id, entity_object=entity)

async def process_pullrequest_entities(pullrequest_data: list[dict[str, Any]]):
    blueprint_id = "bitbucketPullrequest"

    for pr in pullrequest_data:
        entity = {
            "identifier": str(pr.get("id")),
            "title": pr.get("title"),
            "properties": {
                "created_on": convert_to_datetime(pr.get("createdDate")),
                "updated_on": convert_to_datetime(pr.get("updatedDate")),
                "merge_commit": pr.get("fromRef", {}).get("latestCommit"),
                "description": pr.get("description"),
                "state": pr.get("state"),
                "owner": pr.get("author", {}).get("user", {}).get("displayName"),
                "link": pr.get("links", {}).get("self", [{}])[0].get("href"),
                "destination": pr.get("toRef", {}).get("displayId"),
                "reviewers": [
                    user.get("user", {}).get("displayName") for user in pr.get("reviewers", [])
                ],
                "source": pr.get("fromRef", {}).get("displayId"),
            },
            "relations": {
                "repository": pr["toRef"]["repository"]["slug"],
                "participants": [pr.get("author", {}).get("user", {}).get("emailAddress")]
                                + [user.get("user", {}).get("emailAddress") for user in pr.get("participants", [])],
            },
        }
        await add_entity_to_port(blueprint_id=blueprint_id, entity_object=entity)

async def get_repository_readme(project_key: str, repo_slug: str) -> str:
    file_path = f"projects/{project_key}/repos/{repo_slug}/browse/README.md"
    readme_content = ""
    async for readme_file_batch in get_paginated_resource(
            path=file_path, page_size=500, full_response=True
    ):
        file_content = parse_repository_file_response(readme_file_batch)
        readme_content += file_content
    return readme_content

async def get_latest_commit(project_key: str, repo_slug: str) -> dict[str, Any]:
    try:
        commit_path = f"projects/{project_key}/repos/{repo_slug}/commits"
        async for commit_batch in get_paginated_resource(path=commit_path, page_size=1):
            if commit_batch:
                latest_commit = commit_batch[0]
                return latest_commit
    except Exception as e:
        logger.error(f"Error fetching latest commit for repo {repo_slug}: {e}")
    return {}

async def get_repositories(project: dict[str, Any]):
    repositories_path = f"projects/{project['key']}/repos"
    async for repositories_batch in get_paginated_resource(path=repositories_path):
        logger.info(
            f"received repositories batch with size {len(repositories_batch)} from project: {project['key']}"
        )
        await process_repository_entities(
            repository_data=[
                {
                    **repo,
                    "__latestCommit": await get_latest_commit(
                        project_key=project["key"], repo_slug=repo["slug"]
                    ),
                }
                for repo in repositories_batch
            ]
        )

        await get_repository_pull_requests(repository_batch=repositories_batch)

async def get_repository_pull_requests(repository_batch: list[dict[str, Any]]):
    pr_params = {"state": "ALL"}  ## Fetch all pull requests
    for repository in repository_batch:
        pull_requests_path = f"projects/{repository['project']['key']}/repos/{repository['slug']}/pull-requests"
        async for pull_requests_batch in get_paginated_resource(
                path=pull_requests_path, params=pr_params
        ):
            logger.info(
                f"received pull requests batch with size {len(pull_requests_batch)} from repo: {repository['slug']}"
            )
            await process_pullrequest_entities(pullrequest_data=pull_requests_batch)

async def main():
    logger.info("Starting Bitbucket data extraction")
    async for users_batch in get_paginated_resource(path="admin/users"):
        logger.info(f"received users batch with size {len(users_batch)}")
        await process_user_entities(users_data=users_batch)

    project_path = "projects"
    if BITBUCKET_PROJECTS_FILTER:
        projects = [await get_single_project(key) for key in BITBUCKET_PROJECTS_FILTER]
    else:
        projects = get_paginated_resource(path=project_path)

    port_webhook_url = await get_or_create_port_webhook()
    if not port_webhook_url:
        logger.error("Failed to get or create Port webhook. Skipping webhook setup...")

    async for projects_batch in projects:
        logger.info(f"received projects batch with size {len(projects_batch)}")
        await process_project_entities(projects_data=projects_batch)

        for project in projects_batch:
            await get_repositories(project=project)
            await get_or_create_project_webhook(
                project_key=project["key"],
                webhook_url=port_webhook_url,
                events=WEBHOOK_EVENTS,
            )

    logger.info("Bitbucket data extraction completed")
    await client.aclose()

if __name__ == "__main__":
    asyncio.run(main())
