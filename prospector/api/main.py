from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import main as prospector


# from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from typing import Optional

origins = [
    "http://localhost:3000",
    "localhost:3000"
]


api_metadata = [
    {
        "name": "data",
        "description": "Operations with data used to train ML models.",
    },
    {
        "name": "jobs",
        "description": "Manage jobs.",
        "externalDocs": {
            "description": "Items external docs",
            "url": "https://fastapi.tiangolo.com/",
        },
    },
]

app = FastAPI(openapi_tags=api_metadata)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# -----------------------------------------------------------------------------
#
@app.post("/jobs/", tags=['jobs'])
async def create_job(vulnerability_id='', description='', url=''):
    return {
        "id": 0,
        "status": "WAITING",
        "query": "CVE-1234-5678",
        "results": []
    }


@app.get("/jobs/{job_id}", tags=['jobs'])
async def get_job(job_id):
    return {
        "id": job_id,
        "status": "RUNNING",
        "query": "CVE-1234-5678",
        "results": []
    }

# -----------------------------------------------------------------------------
# Data here refers to training data, used to train ML models
# TODO find a less generic term
@app.get("/data", tags=['data'])
async def get_data():
    return [
        {
        'repository_url': "https://github.com/apache/struts",
        'commit_id': 'a4612fe8232678cab3297',
        'label': 1,
        'vulnerability_id': 'CVE-XXXX-YYYY'
        },
        {
        'repository_url': "https://github.com/apache/struts",
        'commit_id': 'a4612fe8232678cab3297',
        'label': 1,
        'vulnerability_id': 'CVE-XXXX-YYYY'
        },
        {
        'repository_url': "https://github.com/apache/struts",
        'commit_id': 'a4612fe8232678cab3297',
        'label': 1,
        'vulnerability_id': 'CVE-XXXX-YYYY'
        },
    ]


@app.post("/data", tags=['data'])
async def create_data(repository_url, commit_id, label, vulnerability_id):
    return {
        'repository_url': repository_url,
        'commit_id': commit_id,
        'label': label,
        'vulnerability_id': vulnerability_id
    }

# -----------------------------------------------------------------------------
@app.post("/legacy", response_class=HTMLResponse)
async def make_legacy_query(vulnerability_id,repository_url=""):
    prospector.main(vulnerability_id=vulnerability_id, repo_url = repository_url)
    return """
    <html>
        <head>
            <title>Prospector</title>
        </head>
        <body>
            <h1>Results</h1>
            .........
        </body>
    </html>
    """


# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def read_items():
    return """
    <html>
        <head>
            <title>Prospector</title>
        </head>
        <body>
            <h1>Prospector API</h1>
            Click <a href="/docs">here</a> for docs and here for <a href="/openapi.json">OpenAPI specs</a>.
        </body>
    </html>
    """
