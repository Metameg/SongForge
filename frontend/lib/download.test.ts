/**
 * Download history + labels (issue #39): the client-observed history window
 * (now-playing + the previous 5 = 6 downloadable tracks), the same-origin download
 * href and the dropdown labels. Pure logic, no DOM.
 */

import { describe, expect, it, vi } from "vitest";
import {
  DOWNLOAD_UNAVAILABLE,
  HISTORY_LIMIT,
  downloadEntries,
  downloadHref,
  emptyHistory,
  justPlayedLabel,
  nowPlayingLabel,
  olderLabel,
  probeDownload,
  reduceHistory,
} from "./download";

const a = { song_id: "a", title: "Alpha" };
const b = { song_id: "b", title: "Beta" };
const c = { song_id: "c", title: "Gamma" };
const d = { song_id: "d", title: "Delta" };
const e = { song_id: "e", title: "Epsilon" };
const f = { song_id: "f", title: "Zeta" };
const g = { song_id: "g", title: "Eta" };

describe("HISTORY_LIMIT", () => {
  it("is now-playing + 5 prior", () => {
    expect(HISTORY_LIMIT).toBe(6);
  });
});

describe("reduceHistory", () => {
  it("starts empty", () => {
    expect(emptyHistory).toEqual({ entries: [] });
  });
  it("a fresh visitor has now-playing only", () => {
    expect(reduceHistory(emptyHistory, a)).toEqual({ entries: [a] });
  });
  it("after one witnessed song change both entries are present, newest-first", () => {
    const s = reduceHistory(reduceHistory(emptyHistory, a), b);
    expect(s).toEqual({ entries: [b, a] });
  });
  it("keeps the window newest-first up to the cap", () => {
    const s = [a, b, c, d, e, f].reduce(reduceHistory, emptyHistory);
    expect(s).toEqual({ entries: [f, e, d, c, b, a] });
  });
  it("never exceeds HISTORY_LIMIT, dropping the oldest", () => {
    const s = [a, b, c, d, e, f, g].reduce(reduceHistory, emptyHistory);
    expect(s.entries).toHaveLength(HISTORY_LIMIT);
    expect(s).toEqual({ entries: [g, f, e, d, c, b] });
    expect(s.entries.map((x) => x.song_id)).not.toContain("a");
  });
  it("a repeated identical song_id does not shift a distinct track", () => {
    const once = reduceHistory(emptyHistory, a);
    expect(reduceHistory(once, a)).toEqual({ entries: [a] });
    const twice = reduceHistory(once, b);
    expect(reduceHistory(twice, b)).toEqual({ entries: [b, a] });
  });
  it("same song_id with a changed title updates the title in place without shifting", () => {
    const s = reduceHistory(reduceHistory(emptyHistory, { song_id: "a", title: "x" }), {
      song_id: "a",
      title: "y",
    });
    expect(s).toEqual({ entries: [{ song_id: "a", title: "y" }] });
    const withPrev = reduceHistory(reduceHistory(emptyHistory, b), { song_id: "a", title: "x" });
    const renamed = reduceHistory(withPrev, { song_id: "a", title: "y" });
    expect(renamed.entries).toEqual([{ song_id: "a", title: "y" }, b]);
  });
});

describe("downloadHref", () => {
  it("is the same-origin download path", () => {
    expect(downloadHref("song-1")).toBe("/download/song-1");
  });
  it("url-encodes the id", () => {
    expect(downloadHref("a/b c")).toBe("/download/a%2Fb%20c");
  });
});

describe("labels", () => {
  it("builds the now-playing label", () => {
    expect(nowPlayingLabel("Alpha")).toBe("Now playing — Alpha");
  });
  it("builds the just-played label", () => {
    expect(justPlayedLabel("Alpha")).toBe("Just played — Alpha");
  });
  it("builds the older label with the age", () => {
    expect(olderLabel(2, "Alpha")).toBe("2 songs ago — Alpha");
    expect(olderLabel(5, "Zeta")).toBe("5 songs ago — Zeta");
  });
});

describe("reduceHistory edge cases", () => {
  it("consecutive identical song_id returns the same state object", () => {
    const s = reduceHistory(emptyHistory, a);
    expect(reduceHistory(s, { ...a })).toBe(s);
  });
  it("a returning older song replaces now-playing and demotes the current one", () => {
    const s = [a, b, a].reduce(reduceHistory, emptyHistory);
    expect(s).toEqual({ entries: [a, b, a] });
  });
  it("does not mutate the previous state", () => {
    const before = reduceHistory(emptyHistory, a);
    const snapshot = { entries: [...before.entries] };
    reduceHistory(before, b);
    expect(before).toEqual(snapshot);
  });
  it("an empty song_id (idle/blank transition) is recorded without throwing", () => {
    const idle = { song_id: "", title: "" };
    const s = reduceHistory(reduceHistory(emptyHistory, a), idle);
    expect(s).toEqual({ entries: [idle, a] });
  });
});

describe("downloadEntries", () => {
  it("is empty before anything is witnessed", () => {
    expect(downloadEntries(emptyHistory)).toEqual([]);
  });
  it("has one now-playing entry on a fresh state", () => {
    expect(downloadEntries(reduceHistory(emptyHistory, a))).toEqual([
      { key: "now", label: "Now playing — Alpha", href: "/download/a" },
    ]);
  });
  it("labels now-playing, just-played, then older tracks by age", () => {
    const s = [a, b, c, d].reduce(reduceHistory, emptyHistory);
    expect(downloadEntries(s)).toEqual([
      { key: "now", label: "Now playing — Delta", href: "/download/d" },
      { key: "prev", label: "Just played — Gamma", href: "/download/c" },
      { key: "older-2", label: "2 songs ago — Beta", href: "/download/b" },
      { key: "older-3", label: "3 songs ago — Alpha", href: "/download/a" },
    ]);
  });
  it("never exceeds HISTORY_LIMIT entries with unique keys", () => {
    const entries = downloadEntries([a, b, c, d, e, f, g].reduce(reduceHistory, emptyHistory));
    expect(entries).toHaveLength(HISTORY_LIMIT);
    expect(new Set(entries.map((x) => x.key)).size).toBe(HISTORY_LIMIT);
  });
});

describe("downloadHref encoding", () => {
  it.each([
    ["a b", "/download/a%20b"],
    ["a/b", "/download/a%2Fb"],
    ["a?b#c", "/download/a%3Fb%23c"],
    ["50%", "/download/50%25"],
    ["é夜", "/download/%C3%A9%E5%A4%9C"],
  ])("encodes %s", (id, expected) => {
    expect(downloadHref(id)).toBe(expected);
  });
});

describe("DOWNLOAD_UNAVAILABLE", () => {
  it("is the inline failure message", () => {
    expect(DOWNLOAD_UNAVAILABLE).toBe("Download unavailable");
  });
});

describe("probeDownload", () => {
  const fakeFetch = (res: Partial<Response>) =>
    vi.fn().mockResolvedValue(res) as unknown as typeof fetch;

  it("treats an opaqueredirect as available", async () => {
    expect(await probeDownload("/download/a", fakeFetch({ type: "opaqueredirect", ok: false }))).toBe(true);
  });
  it("treats a 2xx as available", async () => {
    expect(await probeDownload("/download/a", fakeFetch({ type: "basic", ok: true }))).toBe(true);
  });
  it.each([404, 500, 503])("treats a %i as unavailable", async (status) => {
    expect(
      await probeDownload("/download/a", fakeFetch({ type: "basic", ok: false, status })),
    ).toBe(false);
  });
  it("treats a thrown network error as unavailable", async () => {
    const fn = vi.fn().mockRejectedValue(new TypeError("network")) as unknown as typeof fetch;
    expect(await probeDownload("/download/a", fn)).toBe(false);
  });
  it("probes the href without following redirects", async () => {
    const fn = vi.fn().mockResolvedValue({ type: "opaqueredirect", ok: false });
    await probeDownload("/download/a", fn as unknown as typeof fetch);
    expect(fn).toHaveBeenCalledWith("/download/a", expect.objectContaining({ redirect: "manual" }));
  });
});
