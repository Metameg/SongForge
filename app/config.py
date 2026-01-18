import os


class Config:
    OPEN_AI_KEY = os.getenv("OPEN_AI_KEY")
    MUSICGPT_KEY = os.getenv("MUSICGPT_KEY")
    REDIS_HOST = "localhost"
    REDIS_PORT = 6379
    REDIS_DB = 0
    PLAYLIST_STATIC_KEY = "playlist:static"
    PLAYLIST_DYNAMIC_KEY = "playlist:dynamic"
    JOB_KEY_TEMPLATE = "job:{}"


class DevelopmentConfig(Config):
    PUBLIC_BASE_URL = (
        "https://according-installations-polymer-painted.trycloudflare.com"
    )
    WEBHOOK_URL = PUBLIC_BASE_URL + "/webhook"


class ProductionConfig(Config):
    PUBLIC_BASE_URL = "https://yourapp.com"
