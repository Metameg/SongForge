/**
 * Download history + labels (issue #39): the client-observed size-2 history
 * (now-playing + just-played), the same-origin download href and the dropdown labels.
 * Pure logic, no DOM.
 */

import { describe, expect, it } from "vitest";
import {
  downloadHref,
  emptyHistory,
  justPlayedLabel,
  nowPlayingLabel,
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
