import requests
from pprint import pprint
from openai import OpenAI


class MusicAPIClient:
    def __init__(self, open_ai_key: str, musicgpt_key: str, webhook_url: str):
        self.open_ai_key = open_ai_key
        self.musicgpt_key = musicgpt_key
        self.music_url = "https://api.musicgpt.com/api/public/v1/MusicAI"
        self.webhook_url = webhook_url

    def create_lyrics(self, text):
        client = OpenAI(api_key=self.open_ai_key)

        response = client.responses.create(
            model="gpt-3.5-turbo",
            input=[
                {
                    "role": "system",
                    "content": "You are a music producer / songwriter that receives text and turns it into lyrics.",
                },
                {
                    "role": "user",
                    "content": f"""You are given text that a user wants turned into song lyrics.

Your rules:
1. If the text is already structured song lyrics (has verses, choruses, repeated lines, or poetic structure), return it AS-IS — do not rewrite or paraphrase it.
2. If the text is prose, a sentence, or a short phrase, expand it into a full song. Keep the user's exact words and phrases IN ORDER as the core lines. Only add creative filler (repeated choruses, bridges, additional verses) to make it a complete, full-length song.
3. Always use <verse>, <chorus>, and <bridge> tags to organize the output.
4. Never omit or reorder any of the user's original words.

Here is the user's text:
{text}""",
                },
            ],
        )

        if not response.output_text:
            raise RuntimeError("OpenAI returned empty lyrics")

        return response.output_text

    def create_music(self, prompt, lyrics):
        payload = {
            "prompt": prompt,
            "lyrics": lyrics if lyrics else "",
            "make_instrumental": False,
            "vocal_only": False,
            "webhook_url": self.webhook_url,
        }
        headers = {
            "Authorization": self.musicgpt_key,
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(self.music_url, json=payload, headers=headers)
        except requests.RequestException as e:
            return "", 0.0, f"Music API request failed: {e}"

        # Non-200 response
        if response.status_code != 200:
            return "", 0.0, f"Music API HTTP {response.status_code}: {response.text}"

        data = response.json()

        # extract the conversion IDs that will be in the webhook later
        conversion_ids = [data["conversion_id_1"], data["conversion_id_2"]]
        return conversion_ids, data["credit_estimate"], None
