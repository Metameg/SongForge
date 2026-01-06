import os


class Config:
    OPEN_AI_KEY = os.getenv("OPEN_AI_KEY")
    MUSICGPT_KEY = os.getenv("MUSICGPT_KEY")
    REDIS_HOST = "localhost"
    REDIS_PORT = 6379
    REDIS_DB = 0


class DevelopmentConfig(Config):
    PUBLIC_BASE_URL = "https://auckland-pleasant-kathy-made.trycloudflare.com"
    WEBHOOK_URL = PUBLIC_BASE_URL + "/webhook"


class ProductionConfig(Config):
    PUBLIC_BASE_URL = "https://yourapp.com"
