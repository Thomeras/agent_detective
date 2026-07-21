"""FastAPI dependency providers.

Production wiring is set on app.state by the lifespan in main.py; tests
override these providers via app.dependency_overrides with in-memory fakes.
"""

from fastapi import Request

from .config import Settings
from .payloads import MinioPayloadStore
from .repository import Repository
from .streams import RedisStreamPublisher


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_repository(request: Request) -> Repository:
    return request.app.state.repository


def get_payload_store(request: Request) -> MinioPayloadStore:
    return request.app.state.payload_store


def get_publisher(request: Request) -> RedisStreamPublisher:
    return request.app.state.publisher
