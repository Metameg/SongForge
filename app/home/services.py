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
                    "content": f"""Given the following body of text, take the highlights and turn it into a catchy
                    song. Use tages like <verse>, <bridge>, and <chorus> to organize the song.

                    Here is the text to turn into a song:
                    {text}

                    Keep as much of the original text IN ORDER as possible in the lyrics. A good rule of thumb is 
                    to use shorter, more meaningful sentences as highlights of the song. It is vital to capture the meaning
                    of the text in these song lyrics.""",
                },
            ],
        )

        if not response.output_text:
            raise RuntimeError("OpenAI returned empty lyrics")

        return response.output_text

    def create_music(self, prompt, lyrics):
        payload = {
            "prompt": "Create a soulful song that is very catchy and empahsises an incredible voice that compliments a bluesy riff",
            "music_style": prompt,
            "lyrics": lyrics,
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
