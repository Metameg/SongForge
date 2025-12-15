import requests
from pprint import pprint
from dotenv import load_dotenv
import os
from openai import OpenAI


class SongWriter:
    def __init__(self):
        load_dotenv()
        self.open_ai_key = os.getenv("OPEN_AI_KEY")
        self.musicgpt_key = os.getenv("MUSICGPT_KEY")
        self.music_url = "https://api.musicgpt.com/api/public/v1/MusicAI"

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

    def create_music(self, lyrics, webhook_url):
        payload = {
            "prompt": "Create a soulful song that is very catchy and empahsises an incredible voice that compliments a bluesy riff",
            "music_style": "blues rap 1960s",
            "lyrics": lyrics,
            "make_instrumental": False,
            "vocal_only": False,
            "webhook_url": webhook_url,
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


if __name__ == "__main__":
    lyrics = """<verse>
                Of Confederation and perpetual Union
                Between the States, we stand as one
                New-Hampshire, Massachusetts, and Rhode Island too
                Connecticut, New-York, we share this view
                New-Jersey, Pennsylvania, Delaware in the mix
                Maryland, Virginia, our bond unbreakable
                North-Carolina, and South too
                Carolina and Georgia, together we'll push through

                <chorus>
                The United States of America we'll be
                A confederacy for all to see
                Our stile and spirit strong and free
                In unity, we will always believe

                <bridge>
                A union forever, we pledge our allegiance
                States united in this grand sequence
                Bound by history, forged by fate
                In the United States, we celebrate

                <verse>
                Of Confederation and perpetual Union
                This land, we call our own dominion
                Together we stand, divided we fall
                In the United States, we heed the call

                <chorus>
                The United States of America we'll be
                A confederacy for all to see
                Our stile and spirit strong and free
                In unity, we will always believe

                <bridge>
                A union forever, we pledge our allegiance
                States united in this grand sequence
                Bound by history, forged by fate
                In the United States, we celebrate

                <chorus>
                The United States of America we'll be
                A confederacy for all to see
                Our stile and spirit strong and free
                In unity, we will always believe"""

    sw = SongWriter()

    text = """Of Confederation and perpetual Union between the States of New-Hampshire,
            Massachusetts-Bay, Rhode Island and Providence Plantations, Connecticut, New-York,
            New-Jersey, Pennsylvania, Delaware, Maryland, Virginia, North-Carolina, South-Carolina and Georgia.
            The Stile of this confederacy shall be "The United States of America"."""

    # lyrics = sw.create_lyrics(text)
    # sw.create_music(lyrics)
