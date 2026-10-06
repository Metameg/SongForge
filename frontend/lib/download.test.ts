/**
 * Download history + labels (issue #39): the client-observed size-2 history
 * (now-playing + just-played), the same-origin download href and the dropdown labels.
 * Pure logic, no DOM.
 */

import { describe, expect, it, vi } from "vitest";
import {
  DOWNLOAD_UNAVAILABLE,
  downloadEntries,
  downloadHref,
  emptyHistory,
  justPlayedLabel,
  nowPlayingLabel,
  probeDownload,
  reduceHistory,
} from "./download";

const a = { song_id: "a", title: "Alpha" };
const b = { song_id: "b", title: "Beta" };
const c = { song_id: "c", title: "Gamma" };

describe("reduceHistory", () => {
  it("starts empty", () => {
    expect(emptyHistory).toEqual({ nowPlaying: null, justPlayed: null });
  });
  it("a fresh visitor has now-playing only, no just-played", () => {
    expect(reduceHistory(emptyHistory, a)).toEqual({ nowPlaying: a, justPlayed: null });
  });
  it("after one witnessed song change both entries are present", () => {
    const s = reduceHistory(reduceHistory(emptyHistory, a), b);
    expect(s).toEqual({ nowPlaying: b, justPlayed: a });
  });
  it("keeps only the last two songs", () => {
    const s = [a, b, c].reduce(reduceHistory, emptyHistory);
    expect(s).toEqual({ nowPlaying: c, justPlayed: b });
  });
  it("a repeated identical song_id does not shift a distinct track into just-played", () => {
    const once = reduceHistory(emptyHistory, a);
    expect(reduceHistory(once, a)).toEqual({ nowPlaying: a, justPlayed: null });
    const twice = reduceHistory(once, b);
    expect(reduceHistory(twice, b)).toEqual({ nowPlaying: b, justPlayed: a });
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
});

describe("reduceHistory edge cases", () => {
  it("many distinct changes always keep newest now-playing and the prior as just-played", () => {
    const d = { song_id: "d", title: "Delta" };
    const s = [a, b, c, d].reduce(reduceHistory, emptyHistory);
    expect(s).toEqual({ nowPlaying: d, justPlayed: c });
  });
  it("consecutive identical song_id returns the same state object", () => {
    const s = reduceHistory(emptyHistory, a);
    expect(reduceHistory(s, { ...a })).toBe(s);
  });
  it("a returning older song replaces now-playing and demotes the current one", () => {
    const s = [a, b, a].reduce(reduceHistory, emptyHistory);
    expect(s).toEqual({ nowPlaying: a, justPlayed: b });
  });
  it("does not mutate the previous state", () => {
    const before = reduceHistory(emptyHistory, a);
    const snapshot = { ...before };
    reduceHistory(before, b);
    expect(before).toEqual(snapshot);
  });
  it("an empty song_id (idle/blank transition) is recorded without throwing", () => {
    const idle = { song_id: "", title: "" };
    const s = reduceHistory(reduceHistory(emptyHistory, a), idle);
    expect(s).toEqual({ nowPlaying: idle, justPlayed: a });
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
  it("has two entries after a change, now-playing first", () => {
    const s = [a, b].reduce(reduceHistory, emptyHistory);
    expect(downloadEntries(s)).toEqual([
      { key: "now", label: "Now playing — Beta", href: "/download/b" },
      { key: "prev", label: "Just played — Alpha", href: "/download/a" },
    ]);
  });
  it("never exceeds two entries", () => {
    expect(downloadEntries([a, b, c].reduce(reduceHistory, emptyHistory))).toHaveLength(2);
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
    const f = vi.fn().mockRejectedValue(new TypeError("network")) as unknown as typeof fetch;
    expect(await probeDownload("/download/a", f)).toBe(false);
  });
  it("probes the href without following redirects", async () => {
    const f = vi.fn().mockResolvedValue({ type: "opaqueredirect", ok: false });
    await probeDownload("/download/a", f as unknown as typeof fetch);
    expect(f).toHaveBeenCalledWith("/download/a", expect.objectContaining({ redirect: "manual" }));
  });
});
