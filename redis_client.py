from typing import Optional
import os
import hashlib
import json
import socket
from typing import Any, Optional
from redis.asyncio import Redis
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError
import logger
import traceback

redis_client: Optional[Redis] = None
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "true").lower() == "true"
ELASTICACHE_ENDPOINT = os.getenv("ELASTICACHE_ENDPOINT", "clustercfg.quicksightelastic.3syauu.use1.cache.amazonaws.com")
ELASTICACHE_AUTH_TOKEN = os.getenv("ELASTICACHE_AUTH_TOKEN")
REDIS_CONNECT_TIMEOUT_SECONDS = 5
REDIS_SOCKET_TIMEOUT_SECONDS = 5
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "quicksight-api")

def get_redis_client() -> Optional[Redis]:
    global redis_client

    if not REDIS_ENABLED or not ELASTICACHE_ENDPOINT:
        print("Redis disabled or ElastiCache endpoint not configured")
        return None

    redis_client = Redis(
            host=ELASTICACHE_ENDPOINT,
            port=6379,
            password=ELASTICACHE_AUTH_TOKEN or None,
            ssl=True,
            decode_responses=True,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_keepalive=True,
            retry_on_timeout=True,
        )
    print("ElastiCache client initialized. endpoint=%s port=%s", ELASTICACHE_ENDPOINT,6379)

    return redis_client



async def check_redis_connection() -> bool:
    try:
        client = get_redis_client()
        if client is None:
            logger.error("Redis client is None")
            return False
        result = await client.ping()

        logger.info("Redis ping successful: %s", result)
        return True
    except Exception as e:
        logger.error("Redis connection failed: %s", repr(e))
        logger.error(traceback.format_exc())
        return False

def build_cache_key(namespace: str, payload: Any) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()
    # print(f"{REDIS_KEY_PREFIX}:{namespace}:{digest}")
    return f"{REDIS_KEY_PREFIX}:{namespace}:{digest}"


async def cache_get_json(key: str) -> Optional[Any]:
    client = get_redis_client()
    # print("cache_get_json", client)
    if client is None:     
        return None
    try:
        cached_value = await client.get(key)
        print("cached_value", cached_value)
        if cached_value is None:
            return None
        return json.loads(cached_value)

    except (
        RedisError,
        RedisConnectionError,
        json.JSONDecodeError,
        TimeoutError,
        socket.timeout,
    ) as exc:
        logger.info(
            "ElastiCache GET failed. key=%s error=%s",
            key,
            repr(exc),
        )
        return None