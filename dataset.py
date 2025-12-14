import pandas as pd
import requests


class BibleDataset:
    def __init__(self):
        self.bbe_df = pd.read_csv("t_bbe.csv")
        self.books: dict[int, dict[str | int, str]] = {}

        for _, row in self.bbe_df.iterrows():
            b = int(row["b"])
            c = int(row["c"])
            t = str(row["t"])

            if b not in self.books:
                self.books[b] = {"full_text": ""}

            self.books[b]["full_text"] += t + " "

            if c not in self.books[b]:
                self.books[b][c] = ""

            self.books[b][c] += t + " "


if __name__ == "__main__":
    data = BibleDataset()
    # sw = SongWriter()
    # lyrics = sw.create_lyrics(data.books[1][1])

    # Send to webhook
    requests.post(
        "https://schedules-transactions-began-merely.trycloudflare.com",
        json={"status": "complete", "lyrics": "test lyrics"},
        timeout=5,
    )

    # responses = sw.create_music(lyrics)
    # print(responses[0] + "\n\n" + responses[1])
