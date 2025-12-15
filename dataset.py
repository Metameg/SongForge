import pandas as pd
from pprint import pprint


class BibleDataset:
    def __init__(self):
        self.bbe_df = pd.read_csv("t_bbe.csv")
        self.books: dict[int, dict[str | int, str]] = {}

        for _, row in self.bbe_df.iterrows():
            b = int(row["b"])
            c = int(row["c"])
            t = str(row["t"])

            if b not in self.books:
                self.books[b] = {}

            if c not in self.books[b]:
                self.books[b][c] = ""

            self.books[b][c] += t + " "


if __name__ == "__main__":
    data = BibleDataset()
    pprint(data.books[1].keys())
