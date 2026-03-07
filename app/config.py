import os


class Config:
    OPEN_AI_KEY = os.getenv("OPEN_AI_KEY")
    MUSICGPT_KEY = os.getenv("MUSICGPT_KEY")
    REDIS_HOST = "localhost"
    REDIS_PORT = 6379
    REDIS_DB = 0
    PLAYLIST_STATIC_KEY = "playlist:static"
    PLAYLIST_DYNAMIC_KEY = "playlist:dynamic"
    PLAYLIST_PROCESSING_KEY = "playlist:processing"
    HISTORY_KEY = "playlist:history"
    MAX_HISTORY_JOBS = 10
    JOB_KEY_TEMPLATE = "job:{}"


class DevelopmentConfig(Config):
    PUBLIC_BASE_URL = "https://trees-shark-presenting-benjamin.trycloudflare.com"
    WEBHOOK_URL = PUBLIC_BASE_URL + "/webhook"
    DEV_SIMULATE_WEBHOOK = False  # Skip MusicGPT API; simulate webhook locally after a delay


class ProductionConfig(Config):
    PUBLIC_BASE_URL = "https://yourapp.com"
